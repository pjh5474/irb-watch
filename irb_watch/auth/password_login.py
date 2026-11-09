"""Sign in with email and password against the project's login service (Supabase Auth).

Used only by POST /auth/token, so a person can get a bearer token for the API docs page.
Plain HTTP call with the publishable key; the server's shared Supabase client is not used, so no
login state can leak into it. The password is passed through once and never logged or stored.
"""

from dataclasses import dataclass

import httpx


class BadCredentialsError(Exception):
    """The email or password is wrong (or the account cannot sign in)."""


class LoginBusyError(Exception):
    """The login service is rate limiting; try again later."""


class LoginUnavailableError(Exception):
    """The login service could not be reached or answered something unexpected."""


@dataclass(frozen=True)
class IssuedToken:
    access_token: str
    expires_in: int
    user_id: str


class SupabasePasswordLogin:
    def __init__(
        self,
        supabase_url: str,
        publishable_key: str,
        timeout: float = 10.0,
        http: httpx.Client | None = None,
    ) -> None:
        self._url = supabase_url.rstrip("/") + "/auth/v1/token"
        self._key = publishable_key
        self._timeout = timeout
        self._http = http or httpx.Client()

    def sign_in(self, email: str, password: str) -> IssuedToken:
        try:
            response = self._http.post(
                self._url,
                params={"grant_type": "password"},
                headers={"apikey": self._key},
                json={"email": email, "password": password},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise LoginUnavailableError(type(exc).__name__) from exc
        if response.status_code == 200:
            try:
                data = response.json()
                return IssuedToken(
                    access_token=data["access_token"],
                    expires_in=int(data.get("expires_in") or 3600),
                    user_id=data["user"]["id"],
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise LoginUnavailableError("unexpected login answer") from exc
        if response.status_code in (400, 401, 422):
            raise BadCredentialsError
        if response.status_code == 429:
            raise LoginBusyError
        raise LoginUnavailableError(f"HTTP {response.status_code}")
