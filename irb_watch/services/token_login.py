"""A bearer token for the API docs page: sign in with email and password.

Rules (owner decisions 2026-11-09):
  * Any active user may get a token for their own account; nobody else's. This is the same login
    the web app uses, only reachable from the Authorize button of /docs.
  * Wrong guesses are limited: 5 failures per 15 minutes for one email (from anywhere) and 5
    per 15 minutes for one client address (for any email). The limit lives in this process and
    resets when the server restarts. A correct login clears that email's count (not the address's).
  * A success is written to the audit log (user id only). Failures are not logged.
  * The password and the token are never logged. The answer says nothing about whether the email
    exists: wrong email and wrong password look the same.
"""

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.auth.password_login import (
    BadCredentialsError,
    IssuedToken,
    LoginBusyError,
    LoginUnavailableError,
)
from irb_watch.services.errors import (
    RateLimitedError,
    UnauthorizedError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.users import AuditSink, UserStore, active_profile

log = logging.getLogger(__name__)

MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)
MAX_TRACKED_KEYS = 10_000
MAX_EMAIL_CHARS = 320
MAX_PASSWORD_CHARS = 200


class PasswordLogin(Protocol):
    def sign_in(self, email: str, password: str) -> IssuedToken: ...


class FailureLimiter:
    """Counts recent failures per key; blocks a key once it has MAX_FAILURES in the window."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._failures: dict[str, deque[datetime]] = {}

    def _recent(self, key: str, now: datetime) -> deque[datetime]:
        times = self._failures.get(key)
        if times is None:
            return deque()
        while times and now - times[0] >= WINDOW:
            times.popleft()
        if not times:
            del self._failures[key]
        return times

    def wait_seconds(self, key: str) -> int:
        """0 if the key may try; otherwise the seconds until its oldest failure expires."""
        now = self._clock()
        times = self._recent(key, now)
        if len(times) < MAX_FAILURES:
            return 0
        return max(1, int((times[0] + WINDOW - now).total_seconds()) + 1)

    def fail(self, key: str) -> None:
        now = self._clock()
        if key not in self._failures and len(self._failures) >= MAX_TRACKED_KEYS:
            # Never grow without bound: drop expired keys, then the oldest ones.
            for stale in [k for k in self._failures if not self._recent(k, now)]:
                self._failures.pop(stale, None)
            while len(self._failures) >= MAX_TRACKED_KEYS:
                self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(key, deque()).append(now)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


@dataclass(frozen=True)
class Token:
    access_token: str
    expires_in: int


class TokenLoginService:
    def __init__(
        self,
        login: PasswordLogin | None,
        users: UserStore,
        audit: AuditSink,
        limiter: FailureLimiter,
    ) -> None:
        self._login = login
        self._users = users
        self._audit = audit
        self._limiter = limiter

    def issue(self, email: str, password: str, client_ip: str) -> Token:
        if self._login is None:
            raise UnavailableError(
                "login by token is not set up on this server (SUPABASE_PUBLISHABLE_KEY)"
            )
        email = (email or "").strip().lower()
        if not email or not password:
            raise ValidationError("email and password are required")
        if len(email) > MAX_EMAIL_CHARS or len(password) > MAX_PASSWORD_CHARS:
            raise ValidationError("email or password is too long")

        email_key, ip_key = f"email:{email}", f"ip:{client_ip}"
        wait = max(self._limiter.wait_seconds(email_key), self._limiter.wait_seconds(ip_key))
        if wait:
            raise RateLimitedError(
                f"too many wrong attempts; try again in about {max(1, wait // 60)} minute(s)", wait
            )

        try:
            issued = self._login.sign_in(email, password)
        except BadCredentialsError:
            self._limiter.fail(email_key)
            self._limiter.fail(ip_key)
            raise UnauthorizedError("email or password is incorrect") from None
        except LoginBusyError:
            raise RateLimitedError("the login service is busy; try again in a minute", 60) from None
        except LoginUnavailableError as exc:
            log.warning("token login failed: %s", exc)
            raise UpstreamError("the login service did not answer; try again later") from None

        # A correct password for an account that was deleted still gets no token.
        active_profile(self._users, issued.user_id, "this account is not active")
        # Only the email's count is cleared: an attacker's own valid login must not reset the
        # address's count while they guess other people's passwords.
        self._limiter.clear(email_key)
        self._audit.record(
            "user", "auth.token_issued", issued.user_id, payload={"via": "api_docs"}
        )
        return Token(issued.access_token, issued.expires_in)
