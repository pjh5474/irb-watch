"""Verify Supabase access tokens against the project's public signing keys.

The project signs tokens with asymmetric keys (ES256), so no secret is needed:
public keys are fetched from <SUPABASE_URL>/auth/v1/.well-known/jwks.json and cached.
Only asymmetric algorithms are accepted, which blocks the "HS256 signed with the public
key" downgrade trick.
"""

from dataclasses import dataclass

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError

ALLOWED_ALGORITHMS = ("ES256", "RS256")
AUDIENCE = "authenticated"


class InvalidTokenError(Exception):
    """The token is missing, malformed, expired or not issued for this project."""


class AuthUnavailableError(Exception):
    """The public keys could not be fetched; the token could not be checked."""


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str | None


class TokenVerifier:
    def __init__(self, supabase_url: str, jwks_client: PyJWKClient | None = None) -> None:
        self._issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        self._jwks = jwks_client or PyJWKClient(
            f"{self._issuer}/.well-known/jwks.json",
            cache_keys=True,
            lifespan=600,
            timeout=10,
        )

    def verify(self, token: str) -> AuthUser:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("malformed token") from exc
        # Reject before any network call so garbage tokens cannot trigger key fetches.
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise InvalidTokenError("unsupported signing algorithm")

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=AUDIENCE,
                issuer=self._issuer,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except PyJWKClientConnectionError as exc:
            raise AuthUnavailableError("could not fetch signing keys") from exc
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(str(exc)) from exc

        return AuthUser(id=str(claims["sub"]), email=claims.get("email"))
