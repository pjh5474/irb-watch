"""In-memory stand-ins for the mail service and the alert state."""

from irb_watch.mail.client import Mail, MailError


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[Mail] = []
        self.fail: str | None = None  # a MailError message to raise, or None

    def send(self, mail: Mail) -> str:
        if self.fail:
            raise MailError(self.fail)
        self.sent.append(mail)
        return f"msg-{len(self.sent)}"


class FakeDigestStore:
    """Users, sources, memberships and cards for the daily digest, in memory."""

    def __init__(self) -> None:
        self.user_list = []
        self.source_list = []
        self.members: dict[str, set[int]] = {}
        self.cards = []
        self.cursors: dict[str, str] = {}
        self.enabled: dict[str, bool] = {}
        self.claims: set[str] = set()
        self.fail_cursor = False

    def users(self):
        return list(self.user_list)

    def sources(self):
        return list(self.source_list)

    def memberships(self):
        return {k: set(v) for k, v in self.members.items()}

    def cards_since(self, since, limit):
        from datetime import datetime

        floor = datetime.fromisoformat(since)
        return [c for c in self.cards if datetime.fromisoformat(c.created_at) > floor][:limit]

    def set_cursor(self, user_id, at):
        if self.fail_cursor:
            raise RuntimeError("database down")
        self.cursors[user_id] = at

    def set_digest_enabled(self, user_id, enabled, at):
        self.enabled[user_id] = enabled

    def claim(self, key):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    def release(self, key):
        self.claims.discard(key)


class FakeAlertStore:
    def __init__(self, admins: tuple[str, ...] = ("admin@example.com",)) -> None:
        self.levels: dict[int, str] = {}
        self.claims: set[str] = set()
        self.admins = admins
        self.fail_reads = False

    def get_levels(self):
        if self.fail_reads:
            raise RuntimeError("database down")
        return dict(self.levels)

    def set_level(self, source_id, level, reason, at):
        self.levels[source_id] = level

    def clear_level(self, source_id):
        self.levels.pop(source_id, None)

    def claim(self, key):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    def release(self, key):
        self.claims.discard(key)

    def admin_emails(self):
        return self.admins
