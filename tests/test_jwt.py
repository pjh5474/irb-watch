import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.exceptions import PyJWKClientConnectionError

from irb_watch.auth.jwt import AuthUnavailableError, InvalidTokenError, TokenVerifier

URL = "https://proj.supabase.co"
ISS = f"{URL}/auth/v1"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


class FakeJWKS:
    def __init__(self, public_key=None, error=None):
        self._key = public_key or KEY.public_key()
        self._error = error

    def get_signing_key_from_jwt(self, token):
        if self._error:
            raise self._error
        return SimpleNamespace(key=self._key)


def make_token(key=KEY, alg="ES256", **overrides):
    claims = {
        "iss": ISS,
        "aud": "authenticated",
        "sub": "user-123",
        "email": "a@example.com",
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "k1"})


@pytest.fixture
def verifier():
    return TokenVerifier(URL, jwks_client=FakeJWKS())


def test_valid_token(verifier):
    user = verifier.verify(make_token())
    assert user.id == "user-123" and user.email == "a@example.com"


def test_expired_token(verifier):
    with pytest.raises(InvalidTokenError):
        verifier.verify(make_token(exp=int(time.time()) - 10))


def test_wrong_audience(verifier):
    with pytest.raises(InvalidTokenError):
        verifier.verify(make_token(aud="something-else"))


def test_wrong_issuer(verifier):
    with pytest.raises(InvalidTokenError):
        verifier.verify(make_token(iss="https://evil.example/auth/v1"))


def test_missing_subject(verifier):
    with pytest.raises(InvalidTokenError):
        verifier.verify(make_token(sub=None))


def test_missing_expiry(verifier):
    with pytest.raises(InvalidTokenError):
        verifier.verify(make_token(exp=None))


def test_signature_from_another_key(verifier):
    with pytest.raises(InvalidTokenError):
        verifier.verify(make_token(key=OTHER_KEY))


def test_hs256_token_is_rejected_before_any_key_lookup():
    # Downgrade attack: HS256 "signed" with the public key text must never be accepted.
    called = []
    jwks = FakeJWKS()
    jwks.get_signing_key_from_jwt = lambda t: called.append(t)
    token = jwt.encode(
        {"iss": ISS, "aud": "authenticated", "sub": "x", "exp": int(time.time()) + 60},
        "a-long-enough-shared-secret-for-hs256-ok",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        TokenVerifier(URL, jwks_client=jwks).verify(token)
    assert called == []


def test_alg_none_rejected(verifier):
    token = jwt.encode(
        {"iss": ISS, "aud": "authenticated", "sub": "x", "exp": int(time.time()) + 60},
        None,
        algorithm="none",
    )
    with pytest.raises(InvalidTokenError):
        verifier.verify(token)


def test_garbage_token(verifier):
    with pytest.raises(InvalidTokenError):
        verifier.verify("not-a-token")


def test_key_fetch_failure_is_unavailable_not_invalid():
    v = TokenVerifier(URL, jwks_client=FakeJWKS(error=PyJWKClientConnectionError("down")))
    with pytest.raises(AuthUnavailableError):
        v.verify(make_token())
