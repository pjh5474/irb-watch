"""Proof of access to members-only boards.

A user who wants to read a login-only source gives their OWN hospital login once. The
server logs in with it, opens the board, then forgets the password (it is never stored or
logged) and records only "this user proved access until <date>". The database read
policies then let the user see that source's data (see private.can_read_source).

Abuse guard: each attempt is logged and a user may try only a few times per hour, so this
endpoint cannot be used to guess passwords or to lock hospital accounts.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import CrawlError, LoginExpiredError, LoginRejectedError
from irb_watch.services.errors import (
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink, UserStore


class AccessStore(Protocol):
    def get_source(self, source_id: int) -> Source | None: ...
    def grant(
        self, user_id: str, source_id: int, verified_at: datetime, expires_at: datetime
    ) -> None: ...
    def count_attempts_since(self, user_id: str, since: datetime) -> int: ...


# check(source, credentials) raises on failure; see services.source_login.verify_user_access
AccessCheck = Callable[[Source, Credentials], None]


class AccessService:
    def __init__(
        self,
        users: UserStore,
        store: AccessStore,
        audit: AuditSink,
        check: AccessCheck,
        ttl_days: int = 30,
        attempts_per_hour: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._store = store
        self._audit = audit
        self._check = check
        self._ttl = timedelta(days=ttl_days)
        self._attempts_per_hour = attempts_per_hour
        self._clock = clock

    def prove_access(
        self, user_id: str, source_id: int, login_id: str, password: str
    ) -> datetime:
        """Return the new expiry. Raises with a reason the user can act on."""
        profile = self._users.get_profile(user_id)
        if profile is None or profile.deleted_at is not None:
            raise ForbiddenError("this account cannot prove access")
        source = self._store.get_source(source_id)
        if source is None:
            raise NotFoundError("source not found")
        if not source.requires_login:
            raise ValidationError("this source is public; no login is needed")
        login_id = login_id.strip()
        if not login_id or not password:
            raise ValidationError("login ID and password are both required")

        now = self._clock()
        if self._store.count_attempts_since(user_id, now - timedelta(hours=1)) >= (
            self._attempts_per_hour
        ):
            raise TooManyRequestsError(
                f"too many login checks; try again in an hour (limit {self._attempts_per_hour}/hour)"
            )
        # Logged before the check, so a crash or a timeout still counts as an attempt. The
        # entry is also the hourly counter, so if it cannot be written no hospital login
        # is tried at all (fail closed).
        self._audit.record_required(
            "user", "access.attempted", user_id, source_id=source.id, title=source.name
        )

        try:
            self._check(source, Credentials(login_id, password))
        except LoginRejectedError as exc:
            self._failed(user_id, source, "login_rejected")
            raise ForbiddenError(str(exc)) from None
        except LoginExpiredError:
            self._failed(user_id, source, "board_not_readable")
            raise ForbiddenError(
                "your login worked, but it cannot open this board; no access was granted"
            ) from None
        except CrawlError as exc:
            self._failed(user_id, source, "check_failed")
            raise UpstreamError(f"could not check your access right now: {exc}") from None

        expires_at = now + self._ttl
        self._store.grant(user_id, source.id, now, expires_at)
        self._audit.record(
            "user",
            "access.granted",
            user_id,
            source_id=source.id,
            title=source.name,
            payload={"expires_at": expires_at.isoformat()},
        )
        return expires_at

    def _failed(self, user_id: str, source: Source, reason: str) -> None:
        self._audit.record(
            "user",
            "access.denied",
            user_id,
            source_id=source.id,
            title=source.name,
            payload={"reason": reason},
        )
