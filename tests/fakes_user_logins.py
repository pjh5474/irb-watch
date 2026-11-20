"""In-memory stand-ins for the user R-BAY login feature (no database, no browser)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import CredentialStoreError
from irb_watch.services.user_logins import UserLogin


class FakeUserLoginStore:
    """Rows like user_site_credentials: a cleared login keeps its row (and its test counters)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], UserLogin] = {}
        self.secrets: dict[tuple[str, str], Credentials] = {}
        self.broken: str | None = None  # a reason: every call then fails like a store without a key

    def ensure_ready(self):
        if self.broken:
            raise CredentialStoreError(self.broken)

    def get(self, user_id, site):
        self.ensure_ready()
        return self.rows.get((user_id, site))

    def get_credentials(self, user_id, site):
        return self.secrets.get((user_id, site))

    def put_secret(self, user_id, site, credentials, hint, fields):
        row = self.rows.get((user_id, site)) or UserLogin(user_id, site)
        if row.status == "testing":  # the real store decides this in one statement
            return False
        self.rows[(user_id, site)] = self._apply(replace(row, has_login=True, login_hint=hint), fields)
        self.secrets[(user_id, site)] = credentials
        return True

    def update_if_saved(self, user_id, site, fields, *, not_testing=False, only_if_tested_at=None):
        row = self.rows.get((user_id, site))
        if row is None or not row.has_login:
            return False
        if not_testing and row.status == "testing":
            return False
        if only_if_tested_at is not None and row.last_tested_at != only_if_tested_at:
            return False
        self.rows[(user_id, site)] = self._apply(row, fields)
        return True

    def add_rejection(self, user_id, site, reason, tested_at):
        row = self.rows.get((user_id, site))
        if row is None or not row.has_login:
            return False
        if tested_at is not None and row.last_tested_at != tested_at:
            return False
        self.rows[(user_id, site)] = replace(
            row, status="rejected", last_error=reason[:300], reject_count=row.reject_count + 1,
            last_tested_at=row.last_tested_at if tested_at is not None else datetime.now(UTC),
        )
        return True

    def clear(self, user_id, site):
        row = self.rows.get((user_id, site))
        if row is None or not row.has_login:
            return False
        self.rows[(user_id, site)] = replace(
            row, has_login=False, login_hint=None, status="untested", last_error=None
        )
        self.secrets.pop((user_id, site), None)
        return True

    def fail_testing(self, reason):
        n = 0
        for key, row in list(self.rows.items()):
            if row.status == "testing":
                self.rows[key] = replace(row, status="failed", last_error=reason)
                n += 1
        return n

    @staticmethod
    def _apply(row: UserLogin, fields: dict) -> UserLogin:
        values = dict(fields)
        for key in ("last_tested_at", "last_ok_at", "test_window_start"):
            if isinstance(values.get(key), str):
                values[key] = datetime.fromisoformat(values[key])
        values.pop("updated_at", None)
        return replace(row, **values)


class ManualRunner:
    """Holds the work until a test says run(): lets a test look at the 'testing' state."""

    def __init__(self) -> None:
        self.queue: list = []

    def submit(self, work):
        self.queue.append(work)

    def run(self):
        while self.queue:
            self.queue.pop(0)()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)
