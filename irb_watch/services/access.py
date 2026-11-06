"""Proof of access to members-only boards.

A user who wants to read a login-only source gives their OWN hospital login once. The
server logs in with it, opens the board, then forgets the password (it is never stored or
logged) and records only "this user proved access until <date>". The database read
policies then let the user see that source's data (see private.can_read_source).

Abuse guards, so this endpoint cannot be used to guess passwords or to lock hospital accounts:
  * each app user may start only a few checks per hour (counted from the audit log);
  * each hospital ID may have only a few failed logins per hour through this server, whoever
    types it. The ID is recorded only as a keyed hash (HMAC-SHA256 with ACCESS_ID_HASH_KEY),
    so the append-only log never holds hospital IDs and cannot be used to test guesses.
"""

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import CrawlError, LoginExpiredError, LoginRejectedError
from irb_watch.services.errors import (
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink, UserStore, active_profile

# A denied check with one of these reasons did not judge the typed credentials as wrong
# (the login worked but the board is closed to it, or the site was down), so it is not
# counted as a failed login against the hospital ID.
NOT_A_FAILED_LOGIN = frozenset({"board_not_readable", "check_failed"})


class AccessStore(Protocol):
    def get_source(self, source_id: int) -> Source | None: ...
    def grant(
        self, user_id: str, source_id: int, verified_at: datetime, expires_at: datetime
    ) -> None: ...
    def count_attempts_since(self, user_id: str, since: datetime) -> int: ...
    def id_outcomes_since(self, fingerprint: str, since: datetime) -> list[tuple[str, str | None]]:
        """(event_type, reason) of access.attempted / granted / denied rows for this ID."""
        ...


# check(source, credentials) raises on failure; see services.source_login.verify_user_access
AccessCheck = Callable[[Source, Credentials], None]


def id_fingerprint(key: str, login_id: str) -> str:
    """Keyed hash of a hospital ID (trimmed, case-folded so 'Abc' and 'abc ' count as one)."""
    return hmac.new(
        key.encode("utf-8"), login_id.strip().casefold().encode("utf-8"), hashlib.sha256
    ).hexdigest()


def unsettled_attempts(outcomes: list[tuple[str, str | None]]) -> int:
    """Attempts that count against a hospital ID: failed, or still running.

    Attempts minus those that ended well (granted) or without judging the credentials. An
    attempt whose result was never logged stays counted, which is the safe direction.
    """
    attempts = sum(1 for kind, _ in outcomes if kind == "access.attempted")
    settled = sum(
        1
        for kind, reason in outcomes
        if kind == "access.granted"
        or (kind == "access.denied" and reason in NOT_A_FAILED_LOGIN)
    )
    return max(attempts - settled, 0)


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
        id_hash_key: str = "",
        id_failures_per_hour: int = 3,
    ) -> None:
        self._users = users
        self._store = store
        self._audit = audit
        self._check = check
        self._ttl = timedelta(days=ttl_days)
        self._attempts_per_hour = attempts_per_hour
        self._clock = clock
        self._id_hash_key = id_hash_key
        self._id_failures_per_hour = id_failures_per_hour

    def prove_access(
        self, user_id: str, source_id: int, login_id: str, password: str
    ) -> datetime:
        """Return the new expiry. Raises with a reason the user can act on."""
        active_profile(self._users, user_id, "this account cannot prove access")
        source = self._store.get_source(source_id)
        if source is None:
            raise NotFoundError("source not found")
        if not source.requires_login:
            raise ValidationError("this source is public; no login is needed")
        login_id = login_id.strip()
        if not login_id or not password:
            raise ValidationError("login ID and password are both required")
        if not self._id_hash_key:
            # Fail closed: without the lockout protection no hospital login is tried.
            raise UnavailableError(
                "access checks are not configured on this server (ACCESS_ID_HASH_KEY is missing)"
            )

        now = self._clock()
        hour_ago = now - timedelta(hours=1)
        if self._store.count_attempts_since(user_id, hour_ago) >= self._attempts_per_hour:
            raise TooManyRequestsError(
                f"too many login checks; try again in an hour (limit {self._attempts_per_hour}/hour)"
            )
        fingerprint = id_fingerprint(self._id_hash_key, login_id)
        outcomes = self._store.id_outcomes_since(fingerprint, hour_ago)
        if unsettled_attempts(outcomes) >= self._id_failures_per_hour:
            raise TooManyRequestsError(
                "this hospital ID had too many failed logins through this service in the last "
                f"hour (limit {self._id_failures_per_hour}/hour); no login was tried. "
                "Check the ID and password, and try again later"
            )
        # Logged before the check, so a crash or a timeout still counts as an attempt. The
        # entry is also the counter, so if it cannot be written no hospital login is tried
        # at all (fail closed).
        self._audit.record_required(
            "user",
            "access.attempted",
            user_id,
            source_id=source.id,
            title=source.name,
            payload={"id_fp": fingerprint},
        )

        try:
            self._check(source, Credentials(login_id, password))
        except LoginRejectedError as exc:
            self._failed(user_id, source, "login_rejected", fingerprint)
            raise ForbiddenError(str(exc)) from None
        except LoginExpiredError:
            self._failed(user_id, source, "board_not_readable", fingerprint)
            raise ForbiddenError(
                "your login worked, but it cannot open this board; no access was granted"
            ) from None
        except CrawlError as exc:
            self._failed(user_id, source, "check_failed", fingerprint)
            raise UpstreamError(f"could not check your access right now: {exc}") from None

        expires_at = now + self._ttl
        self._store.grant(user_id, source.id, now, expires_at)
        self._audit.record(
            "user",
            "access.granted",
            user_id,
            source_id=source.id,
            title=source.name,
            payload={"expires_at": expires_at.isoformat(), "id_fp": fingerprint},
        )
        return expires_at

    def _failed(self, user_id: str, source: Source, reason: str, fingerprint: str) -> None:
        self._audit.record(
            "user",
            "access.denied",
            user_id,
            source_id=source.id,
            title=source.name,
            payload={"reason": reason, "id_fp": fingerprint},
        )
