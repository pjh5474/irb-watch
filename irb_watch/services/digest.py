"""The daily digest: one mail per user listing the new and updated documents of the sources the
user can read, and (phase A2) reminders of the user's IRB steps whose reminder day is today
(services/pipeline_work.py). News and reminders have their own switches; a mail goes out when
either has something.

  * Runs after the SCHEDULED scan run (see services/alerts.py). Plain server code, no AI.
  * Who: every active user with an email address who has the news or the reminders switched on
    (both default on). Admins are users too and read every source. The finish entry counts
    `news_users` and `reminder_users` (switched on) besides `users` (either), and `reminders`
    (lines sent).
  * The window below is for the news only; reminders look at today alone.
  * What a user may see follows the database rule private.can_read_source: public sources, plus
    the members-only sources an admin assigned (admins: everything). A members-only source
    (a study) appears as a COUNT only: its name and titles never go into an email.
  * Window: everything since the user's previous digest, at most 7 days back; a user's first
    digest covers the last 24 hours. No change = no mail and the cursor does not move.
  * The cursor moves only AFTER a successful send, so a failed day is not lost. One mail per
    user per day (a claim key), and one failing user never stops the others.
  * Each mail is its own message to one recipient and carries one-click unsubscribe headers.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.mail import templates as t
from irb_watch.mail.client import Mailer, MailError
from irb_watch.mail.unsubscribe import make_token
from irb_watch.services.pipeline_work import DUE_WINDOW_DAYS, reminders_for
from irb_watch.services.pipelines import WorkStep
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

FIRST_WINDOW = timedelta(hours=24)
MAX_LOOKBACK = timedelta(days=7)
MAX_LISTED = 30  # items listed in one mail; the rest is a count
MAX_CARDS = 2000
KINDS = {"document.new": "new", "document.reappeared": "reappeared", "document.updated": "updated"}
KIND_ORDER = {"new": 0, "reappeared": 1, "updated": 2}  # when a document has several events


@dataclass(frozen=True)
class DigestUser:
    id: str
    email: str
    role: str
    enabled: bool  # the document news (digest_enabled)
    last_digest_at: str | None
    reminders_enabled: bool = True  # IRB step reminders (phase A2); its own switch


@dataclass(frozen=True)
class Card:
    created_at: str
    event_type: str
    source_id: int
    document_id: int | None
    title: str | None


@dataclass(frozen=True)
class SourceInfo:
    id: int
    name: str
    requires_login: bool


class DigestStore(Protocol):
    def users(self) -> list[DigestUser]: ...
    def sources(self) -> list[SourceInfo]: ...
    def memberships(self) -> dict[str, set[int]]: ...
    def cards_since(self, since: str, limit: int) -> list[Card]: ...
    def set_cursor(self, user_id: str, at: str) -> None: ...
    def claim(self, key: str) -> bool: ...
    def release(self, key: str) -> None: ...


class ReminderSource(Protocol):
    """services.pipelines.PipelineService: due steps of live pipelines, per owner."""

    def reminder_steps(self, until: str) -> dict[str, list[WorkStep]]: ...


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class DigestService:
    def __init__(
        self,
        store: DigestStore,
        mailer: Mailer,
        audit: AuditSink,
        *,
        app_url: str,
        api_domain: str,
        unsubscribe_key: str,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reminders: ReminderSource | None = None,
    ) -> None:
        self._reminders = reminders
        self._store = store
        self._mailer = mailer
        self._audit = audit
        self._app_url = app_url.rstrip("/")
        self._api_domain = api_domain.strip().strip("/")
        self._key = unsubscribe_key
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    def send_all(self) -> dict[str, int]:
        now = self._clock()
        day = now.astimezone(self._tz).date().isoformat()
        users = [u for u in self._store.users() if (u.enabled or u.reminders_enabled) and u.email]
        counts = {
            "users": len(users), "sent": 0, "failed": 0, "quiet": 0, "reminders": 0,
            "news_users": sum(1 for u in users if u.enabled),
            "reminder_users": sum(1 for u in users if u.reminders_enabled),
        }
        if not users:
            return counts

        since = {u.id: self._since(u, now) for u in users}
        news_since = [since[u.id] for u in users if u.enabled]
        cards = self._store.cards_since(min(news_since).isoformat(), MAX_CARDS) if news_since else []
        if len(cards) >= MAX_CARDS:
            log.warning("the digest read %d cards (the limit); older ones may be missing", MAX_CARDS)
        sources = {s.id: s for s in self._store.sources()}
        memberships = self._store.memberships()
        due = self._due_steps(now) if any(u.reminders_enabled for u in users) else {}

        for user in users:
            try:
                outcome = self._digest_one(
                    user, since[user.id], now, day, cards, sources, memberships, due.get(user.id, []), counts
                )
            except Exception:  # one user must never stop the others
                log.exception("the digest crashed for one user")
                outcome = "failed"
            counts[outcome] += 1
        self._audit.record(
            "admin", "mail.digest_finished", None, title="Daily digest finished",
            payload={"day": day, **counts},
        )
        return counts

    def _due_steps(self, now: datetime) -> dict[str, list[WorkStep]]:
        """Steps due within the window (and overdue), per owner. A failure leaves reminders out
        of today's mails (logged) instead of stopping the document news."""
        if self._reminders is None:
            return {}
        until = (now.astimezone(self._tz).date() + timedelta(days=DUE_WINDOW_DAYS)).isoformat()
        try:
            return self._reminders.reminder_steps(until)
        except Exception:
            log.exception("the IRB step reminders could not be read; today's mails go without them")
            try:  # best effort: a failing audit write must not stop the document news either
                self._audit.record(
                    "admin", "mail.reminders_failed", None, title="IRB step reminders could not be read",
                    payload={"until": until},
                )
            except Exception:
                log.exception("the reminder failure could not be written to the audit log")
            return {}

    # -- one user ---------------------------------------------------------------------------------

    @staticmethod
    def _since(user: DigestUser, now: datetime) -> datetime:
        floor = now - MAX_LOOKBACK
        if user.last_digest_at is None:
            return now - FIRST_WINDOW
        return max(floor, _dt(user.last_digest_at))

    def _digest_one(
        self,
        user: DigestUser,
        since: datetime,
        now: datetime,
        day: str,
        cards: list[Card],
        sources: dict[int, SourceInfo],
        memberships: dict[str, set[int]],
        due_steps: list[WorkStep],
        counts: dict[str, int],
    ) -> str:
        readable = (
            set(sources)
            if user.role == "admin"
            else {s.id for s in sources.values() if not s.requires_login} | memberships.get(user.id, set())
        )
        # IRB step reminders: the user's steps whose reminder day is today (phase A2; a day
        # without a mail is not caught up later).
        reminders = []
        if user.reminders_enabled:
            reminders = [
                t.DigestReminder(
                    w.project_name or "", w.hospital_name or "", w.step_name, w.due_on or "", left,
                    f"{self._app_url}/ko/pipelines/{w.pipeline_id}/steps/{w.step_id}",
                    w.site_label,  # the PI is named in the mail (owner, 2026-11-13)
                )
                for w, left in reminders_for(due_steps, now.astimezone(self._tz).date())
            ]
        # One entry per document: the newest title, and the most telling kind of its events.
        docs: dict[int, tuple[int, str, str, int, datetime]] = {}  # id -> (order, kind, title, source, newest)
        for card in cards if user.enabled else ():
            kind = KINDS.get(card.event_type)
            if kind is None or card.document_id is None or card.source_id not in readable:
                continue
            created = _dt(card.created_at)
            if created <= since:
                continue
            previous = docs.get(card.document_id)
            order = KIND_ORDER[kind]
            title = card.title or ""
            if previous is None:
                docs[card.document_id] = (order, kind, title, card.source_id, created)
            else:
                best = min(previous[0], order)
                best_kind = previous[1] if previous[0] <= order else kind
                newest_title = title if created > previous[4] else previous[2]
                docs[card.document_id] = (best, best_kind, newest_title, previous[3], max(created, previous[4]))
        if not docs and not reminders:
            return "quiet"

        # List the newest documents of public sources, up to the cap; count the rest.
        public = sorted(
            ((did, d) for did, d in docs.items() if not sources[d[3]].requires_login),
            key=lambda pair: pair[1][4],
            reverse=True,
        )
        listed_ids = {did for did, _ in public[:MAX_LISTED]}
        digest_sources = []
        for source_id in sorted({d[3] for d in docs.values()}):
            info = sources[source_id]
            of_source = [(did, d) for did, d in docs.items() if d[3] == source_id]
            items = ()
            if not info.requires_login:
                items = tuple(
                    t.DigestItem(did, d[2], d[1])
                    for did, d in sorted(of_source, key=lambda pair: pair[1][4], reverse=True)
                    if did in listed_ids
                )
            digest_sources.append(
                t.DigestSource(source_id, info.name, info.requires_login, len(of_source), items)
            )

        key = f"digest:{user.id}:{day}"
        if not self._store.claim(key):
            return "quiet"  # already mailed today
        token = make_token(user.id, self._key)
        mail = t.digest_mail(
            (user.email,),
            day,
            digest_sources,
            self._app_url,
            f"{self._app_url}/ko/unsubscribe?token={token}",
            f"https://{self._api_domain}/unsubscribe?token={token}",
            reminders=reminders,
        )
        try:
            self._mailer.send(mail)
        except MailError as exc:
            self._store.release(key)  # tomorrow's digest will still cover these documents
            log.warning("digest mail failed: %s", exc)
            self._audit.record(
                "admin", "mail.failed", None, title="Mail (digest) failed",
                payload={"kind": "digest", "error": str(exc)},
            )
            return "failed"
        counts["reminders"] += len(reminders)
        try:
            self._store.set_cursor(user.id, now.isoformat())
        except Exception:
            log.exception("the digest was sent but its cursor could not be saved")
        return "sent"
