"""A user's own R-BAY login (owner, 2026-11-20; docs/decisions.md "User's own R-BAY login").

What it is: the person's R-BAY account (one login for every hospital), saved by the person
themselves, encrypted. It is used only for actions that person starts: a login test now, later IRB
jobs such as a new-plan draft. Never by the scheduler and never by a scan: those use the
platform's own logins (crawlers/credentials, the admin API), which this module never reads.

Rules (strict, owner's choice):
  * saving, testing and deleting are logged first and fail closed (a failed log write means the
    action is not done); the log never holds the login ID or the password;
  * at most one test per 10 minutes and 10 per 24 hours per user, counted in the row itself (it
    survives deleting the login, so saving and deleting again does not reset it);
  * a test is a job (a login takes 5-20 seconds): saving or testing answers at once with status
    'testing'; the page polls. A password R-BAY rejects sets 'rejected': no job may use it until
    a new password is saved. A site that did not answer sets 'failed', which blames nobody;
  * a rejected password stays stored but unusable (the status needs a row to live in); the user
    can delete it. When R-BAY cannot be reached the login is stored as 'failed' and may be tried
    again.
"""

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import (
    CredentialStoreError,
    LayoutChangedError,
    LoginRejectedError,
    MissingLoginError,
    SiteUnavailableError,
)
from irb_watch.services.draft_jobs import JobRunner
from irb_watch.services.errors import (
    ConflictError,
    NotFoundError,
    ServiceError,
    TooManyRequestsError,
    UnavailableError,
    ValidationError,
)
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

SITE = "rbay-account"  # the person's R-BAY account; never a hospital site name
TEST_GAP = timedelta(minutes=10)
TEST_WINDOW = timedelta(hours=24)
TEST_PER_WINDOW = 10
# R-BAY locks an account after 5 failed logins (its own notice). Stop at 3 in a row and wait a day.
REJECT_LIMIT = 3
REJECT_PAUSE = timedelta(hours=24)
MAX_FIELD = 200  # the same cap the admin credential API uses


@dataclass(frozen=True)
class UserLogin:
    """What the API may say about a saved login: never the password, never the ciphertext."""

    user_id: str
    site: str = SITE
    has_login: bool = False
    login_hint: str | None = None  # first two characters of the ID, e.g. "ab***"
    status: str = "untested"  # untested, testing, ok, rejected, failed
    last_error: str | None = None
    last_tested_at: datetime | None = None
    last_ok_at: datetime | None = None
    test_window_start: datetime | None = None
    test_count: int = 0
    reject_count: int = 0  # passwords R-BAY refused in a row


class UserLoginStore(Protocol):
    def get(self, user_id: str, site: str) -> UserLogin | None: ...

    def get_credentials(self, user_id: str, site: str) -> Credentials | None: ...

    def put_secret(
        self, user_id: str, site: str, credentials: Credentials, hint: str, fields: dict
    ) -> bool:
        """Save the login and its status fields. False, and nothing written, when a test is
        running (decided in one database statement, so two requests at once cannot both pass)."""
        ...

    def update_if_saved(
        self, user_id: str, site: str, fields: dict, *,
        not_testing: bool = False, only_if_tested_at: datetime | None = None,
    ) -> bool:
        """Change status fields only while a login is still saved (a delete during a test wins).
        not_testing: and only when no test is running (starting one). only_if_tested_at: and only
        for the test that started at that time (a stale test's answer changes nothing)."""
        ...

    def add_rejection(
        self, user_id: str, site: str, reason: str, tested_at: datetime | None
    ) -> bool:
        """Count one refused password in the database (status rejected, reject_count + 1). With
        tested_at, only for the test that started then; without it (a job), always."""
        ...

    def clear(self, user_id: str, site: str) -> bool: ...

    def fail_testing(self, reason: str) -> int: ...

    def ensure_ready(self) -> None:
        """Raise CredentialStoreError when logins cannot be stored on this server (no key): checked
        before the audit entry of a save, so a log line never records a save that could not happen."""
        ...


def _hint(login_id: str) -> str:
    return login_id[:2] + "***"


@contextmanager
def _store_errors() -> Iterator[None]:
    """A store that cannot be read or written (no key, database down) is a 503 with its reason,
    never a bare 500. CredentialStoreError text never holds a secret."""
    try:
        yield
    except MissingLoginError:
        raise  # a kind of CredentialStoreError, but a real answer ("no login saved"), not a store fault
    except CredentialStoreError as exc:
        raise UnavailableError(f"your R-BAY login cannot be stored or read on this server: {exc}") from None


def _guarded(method):
    """Run a public method with store failures turned into a 503 that says why."""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        with _store_errors():
            return method(*args, **kwargs)

    return wrapper


class UserLoginService:
    def __init__(
        self,
        store: UserLoginStore,
        audit: AuditSink,
        check_login: Callable[[Credentials], None],
        runner: JobRunner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._check = check_login
        self._runner = runner
        self._now = clock

    # -- what the page shows ---------------------------------------------------------------------

    @_guarded
    def status(self, user_id: str) -> UserLogin:
        return self._store.get(user_id, SITE) or UserLogin(user_id)

    # -- the person's own actions ----------------------------------------------------------------

    @_guarded
    def save(self, user_id: str, login_id: str, password: str) -> UserLogin:
        login_id = login_id.strip()
        if not login_id or not password:
            raise ValidationError("enter both the R-BAY ID and the password")
        if len(login_id) > MAX_FIELD or len(password) > MAX_FIELD:
            raise ValidationError(f"the ID and the password may be at most {MAX_FIELD} characters")
        row = self.status(user_id)
        self._refuse_if_testing(row)
        window_start, count = self._take_test_slot(row)
        # The key is checked before the log line, so a save that cannot happen is not logged as one.
        # The log line itself comes before the write on purpose (fail closed: no log, no save); if
        # the database write then fails, the user gets a 503 and the log keeps one orphan "saved".
        self._store.ensure_ready()
        self._audit.record_required(
            "user", "rbay_login.saved", user_id, title="R-BAY login saved",
            payload={"site": SITE, "tested": True},
        )
        now = self._now()
        stored = self._store.put_secret(
            user_id, SITE, Credentials(login_id, password), _hint(login_id),
            {
                "status": "testing", "last_error": None, "last_tested_at": now.isoformat(),
                "test_window_start": window_start.isoformat(), "test_count": count,
                "updated_at": now.isoformat(),
            },
        )
        if not stored:  # another request started a test between the check above and the write
            raise ConflictError("a login test is already running; wait for its result")
        self._start_test(user_id, now)
        return self.status(user_id)

    @_guarded
    def test(self, user_id: str) -> UserLogin:
        row = self.status(user_id)
        if not row.has_login:
            raise NotFoundError("no R-BAY login is saved; save it first")
        if row.status == "rejected":  # the same password again is one more failed login
            raise ConflictError(
                "R-BAY refused the saved password. Testing it again would only add a failed login; "
                "save a new password instead"
            )
        self._refuse_if_testing(row)
        window_start, count = self._take_test_slot(row)
        self._audit.record_required(
            "user", "rbay_login.test_started", user_id, title="R-BAY login test started",
            payload={"site": SITE},
        )
        now = self._now()
        started = self._store.update_if_saved(
            user_id, SITE,
            {
                "status": "testing", "last_error": None, "last_tested_at": now.isoformat(),
                "test_window_start": window_start.isoformat(), "test_count": count,
                "updated_at": now.isoformat(),
            },
            not_testing=True,  # one statement: two requests at once cannot both start a test
        )
        if not started:
            if self.status(user_id).has_login:
                raise ConflictError("a login test is already running; wait for its result")
            raise NotFoundError("no R-BAY login is saved; save it first")
        self._start_test(user_id, now)
        return self.status(user_id)

    @_guarded
    def delete(self, user_id: str) -> None:
        if not self.status(user_id).has_login:
            raise NotFoundError("no R-BAY login is saved")
        self._audit.record_required(
            "user", "rbay_login.deleted", user_id, title="R-BAY login deleted", payload={"site": SITE},
        )
        self._store.clear(user_id, SITE)

    def fail_unfinished(self) -> int:
        """At the server's start: a test that was running cannot continue (one API process)."""
        return self._store.fail_testing("the test was interrupted by a server restart; run it again")

    # -- for jobs the person starts (services/user_rbay.py) -------------------------------------

    @_guarded
    def credentials_for_job(self, user_id: str) -> Credentials:
        """The person's login for a job they started. Never anyone else's, never the platform's."""
        row = self.status(user_id)
        if not row.has_login:
            raise MissingLoginError(
                "you have not saved your R-BAY login yet; save it under Settings, My R-BAY login"
            )
        if row.status == "rejected":
            raise LoginRejectedError(
                "R-BAY refused your saved password; save a new one under Settings, My R-BAY login"
            )
        found = self._store.get_credentials(user_id, SITE)
        if found is None:
            raise MissingLoginError("your R-BAY login could not be read; save it again")
        return found

    def mark_rejected(self, user_id: str, reason: str) -> None:
        """R-BAY refused the password in a job: no more attempts until a new one is saved."""
        try:
            self._store.add_rejection(user_id, SITE, reason, None)  # counted in the database
        except Exception:  # the job's own error is the one to show
            log.exception("could not mark a user's R-BAY login as rejected")

    # -- the test ------------------------------------------------------------------------------

    def _refuse_if_testing(self, row: UserLogin) -> None:
        if row.status == "testing":
            raise ConflictError("a login test is already running; wait for its result")

    def _take_test_slot(self, row: UserLogin) -> tuple[datetime, int]:
        """Check both limits and return the window and count to store. Counted before anything is
        sent to R-BAY, so an attempt that fails still counts."""
        now = self._now()
        if (
            row.reject_count >= REJECT_LIMIT
            and row.last_tested_at is not None
            and now - row.last_tested_at < REJECT_PAUSE
        ):
            raise TooManyRequestsError(
                f"R-BAY refused this account's password {row.reject_count} times in a row, and R-BAY "
                "locks an account after 5 failed logins. Nothing more is tried for 24 hours. Log in on "
                "r-bay.co.kr yourself to check the password (or reset it there), then save it again tomorrow"
            )
        if row.last_tested_at is not None and now - row.last_tested_at < TEST_GAP:
            wait = int((TEST_GAP - (now - row.last_tested_at)).total_seconds() // 60) + 1
            raise TooManyRequestsError(
                f"a login test was done a moment ago; wait about {wait} minute(s) before the next one "
                "(this protects your R-BAY account from being locked)"
            )
        start, count = row.test_window_start, row.test_count
        if start is None or now - start >= TEST_WINDOW:
            start, count = now, 0
        if count >= TEST_PER_WINDOW:
            raise TooManyRequestsError(
                f"at most {TEST_PER_WINDOW} login tests a day; try again tomorrow"
            )
        return start, count + 1

    def _start_test(self, user_id: str, started: datetime) -> None:
        """`started` is this test's token: its answer is written only while the row still carries
        it, so a test of a deleted or replaced login can never change the newer state."""
        try:
            self._runner.submit(lambda: self._run_test(user_id, started))
        except Exception:
            log.exception("a login test could not be queued")
            self._store.update_if_saved(
                user_id, SITE,
                {"status": "failed", "last_error": "the test could not be started; please try again",
                 "updated_at": self._now().isoformat()},
                only_if_tested_at=started,
            )
            raise ServiceError("the test could not be started; please try again") from None

    def _run_test(self, user_id: str, started: datetime) -> None:
        """Never raises (a worker thread). Always ends in ok, rejected or failed, with a reason."""
        try:
            fields = self._test_outcome(user_id)
        except Exception:
            log.exception("a login test crashed")
            fields = {"status": "failed", "last_error": "the test failed unexpectedly"}
        fields["updated_at"] = self._now().isoformat()
        try:
            if fields["status"] == "rejected":
                self._store.add_rejection(user_id, SITE, fields["last_error"], started)  # counted in the database
            else:
                self._store.update_if_saved(user_id, SITE, fields, only_if_tested_at=started)
        except Exception:
            log.exception("a login test result could not be saved")
        try:  # best effort: the outcome kind only, never the ID or the password
            self._audit.record(
                "user", "rbay_login.tested", user_id, title="R-BAY login tested",
                payload={"site": SITE, "outcome": fields["status"]},
            )
        except Exception:
            log.exception("a login test result could not be logged")

    def _test_outcome(self, user_id: str) -> dict:
        try:
            creds = self._store.get_credentials(user_id, SITE)
        except CredentialStoreError as exc:
            return {"status": "failed", "last_error": str(exc)[:300]}
        if creds is None:  # deleted while queued
            return {"status": "untested", "last_error": None}
        try:
            self._check(creds)
        except LoginRejectedError as exc:
            return {"status": "rejected", "last_error": str(exc)[:300]}  # counted by add_rejection
        except (SiteUnavailableError, LayoutChangedError) as exc:
            return {"status": "failed", "last_error": str(exc)[:300]}
        return {"status": "ok", "last_error": None, "last_ok_at": self._now().isoformat(), "reject_count": 0}
