"""Hospital IRB review sessions (IRB calendar, CAL-1): meetings with their submission cutoff.

Owner decisions (2026-11-15; docs/decisions.md "IRB calendar, CAL-1", pipeline-design.md 14.5):
  * Platform-wide on purpose, like hospitals: every organization reads the same rows.
  * Platform admins enter, edit, approve, reject, cancel and remove sessions. Users who work
    on a project with that hospital propose one (state 'proposed'); a proposer may withdraw
    their own proposal while it is not reviewed. An AI job a platform admin starts per
    hospital reads its stored posts and writes proposals only, never sessions: a date read
    from a notice never becomes a session without a person (handoff section 11).
  * Everyone signed in sees every session except rejected and removed ones; proposals are
    marked "proposed, not confirmed" by the pages.
  * A time may be unknown (None); it is never made up.
  * States: proposed -> tentative | confirmed | rejected; tentative <-> confirmed; tentative or
    confirmed -> cancelled; cancelled -> tentative; rejected -> tentative | confirmed.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from irb_watch.ai.digest import document_digest
from irb_watch.core.state import VersionContent
from irb_watch.services.draft_jobs import OPEN, JobRunner
from irb_watch.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceError,
    TooManyRequestsError,
    UnavailableError,
    ValidationError,
)
from irb_watch.services.users import AuditSink, UserStore, active_profile

log = logging.getLogger(__name__)

PROPOSED, TENTATIVE, CONFIRMED, CANCELLED, REJECTED = "proposed", "tentative", "confirmed", "cancelled", "rejected"
STATES = (PROPOSED, TENTATIVE, CONFIRMED, CANCELLED, REJECTED)
APPROVED = frozenset({TENTATIVE, CONFIRMED})
# Which states an admin may move a session to, from each state.
MOVES = {
    PROPOSED: {TENTATIVE, CONFIRMED, REJECTED},
    TENTATIVE: {CONFIRMED, CANCELLED},
    CONFIRMED: {TENTATIVE, CANCELLED},
    CANCELLED: {TENTATIVE},
    REJECTED: {TENTATIVE, CONFIRMED},
}
OTHER_KIND = "other"
MAX_NOTE = 2000
MAX_URL = 1000
MAX_QUOTE = 300
MAX_LABEL = 60
MAX_UPCOMING = 20
MAX_RANGE_DAYS = 400  # sessions read in one call
# The AI job.
SCAN_POSTS = 60  # newest live posts looked at (titles only)
FIND_POSTS = 15  # posts sent
POST_CHARS = 8_000
TOTAL_CHARS = 60_000
MAX_FOUND = 40
PAST_DAYS = 30
FUTURE_DAYS = 400
MAX_OPEN_JOBS_PER_USER = 2
JOB_RETENTION_DAYS = 30
SHOW_JOBS = 20
RESTARTED = "the server restarted while the posts were being read; start it again"
SCHEDULE_WORDS = ("일정", "심의", "회의", "개최", "마감", "접수", "schedule", "meeting", "deadline")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(:00)?$")


@dataclass(frozen=True)
class SessionKind:
    key: str
    label: str
    position: int
    archived_at: str | None = None


@dataclass(frozen=True)
class Session:
    id: int
    hospital_id: int
    kind_key: str
    meets_on: str
    state: str
    origin: str  # admin | user | ai
    meets_at: str | None = None  # "HH:MM"; None = time unknown
    cutoff_on: str | None = None
    cutoff_at: str | None = None
    timezone: str = "Asia/Seoul"
    note: str = ""
    evidence_version_id: int | None = None
    evidence_url: str | None = None
    evidence_quote: str | None = None
    proposed_by: str | None = None
    proposed_at: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    job_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    deleted_at: str | None = None

    @property
    def visible(self) -> bool:
        """Shown to users: not removed, not rejected (proposals are shown, marked)."""
        return self.deleted_at is None and self.state != REJECTED

    @property
    def deadline(self) -> str:
        """The day work must be in: the cutoff, else the meeting day."""
        return self.cutoff_on or self.meets_on


@dataclass(frozen=True)
class SessionJob:
    id: str
    hospital_id: int
    user_id: str
    status: str  # queued | running | done | failed
    created_at: str
    error: str | None = None
    documents_read: int = 0
    proposed: int = 0
    skipped: int = 0
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def open(self) -> bool:
        return self.status in OPEN


@dataclass(frozen=True)
class PostBrief:
    """A stored post of one of the hospital's boards with AI on (titles only)."""

    document_id: int
    version_id: int
    title: str
    posted_on: str | None


class SessionTakenError(Exception):
    """Raised by the store when a live session of that hospital, kind and day exists."""


class SessionJobTakenError(Exception):
    """Raised by the store when the hospital already has an open job (unique index)."""


class SessionStore(Protocol):
    def kinds(self) -> list[SessionKind]: ...
    def add_kind(self, key: str, label: str, position: int) -> SessionKind:
        """LookupError when the key exists."""
        ...
    def update_kind(self, key: str, values: dict) -> SessionKind: ...
    def get(self, session_id: int) -> Session | None: ...
    def sessions(self, session_ids: list[int]) -> list[Session]: ...
    def between(self, hospital_ids: list[int], start: str, end: str) -> list[Session]:
        """Not removed (any state), whose meeting or cutoff falls in [start, end]."""
        ...
    def same_day(self, hospital_id: int, kind_key: str, meets_on: str) -> list[Session]:
        """Every session (any state, removed too) of that hospital, kind and meeting day."""
        ...
    def proposals(self, limit: int) -> list[Session]: ...
    def create(self, values: dict) -> Session:
        """SessionTakenError on the day's unique index."""
        ...
    def update(self, session_id: int, values: dict) -> Session:
        """SessionTakenError on the day's unique index."""
        ...
    def version_hospital(self, version_id: int) -> int | None:
        """The hospital of the board a stored document version is on."""
        ...
    # the AI job
    def posts_for(self, hospital_id: int, limit: int) -> list[PostBrief]:
        """Newest posts of the hospital's AI-on boards, login boards included."""
        ...
    def version_content(self, version_id: int) -> VersionContent | None: ...
    def create_job(self, hospital_id: int, user_id: str) -> SessionJob:
        """SessionJobTakenError when the hospital has an open job."""
        ...
    def get_job(self, job_id: str) -> SessionJob | None: ...
    def jobs(self, hospital_id: int | None, limit: int) -> list[SessionJob]: ...
    def open_jobs_of_user(self, user_id: str) -> list[SessionJob]: ...
    def update_job(self, job_id: str, values: dict) -> None:
        """Changes an OPEN job only."""
        ...
    def delete_job(self, job_id: str) -> None: ...
    def fail_open_jobs(self, error: str, now: datetime) -> int: ...
    def purge_jobs(self, cutoff: datetime) -> int: ...


class SessionFinder(Protocol):
    """ai.session_finder.OpenAISessionFinder."""

    model: str

    def find(self, posts: list[str], kinds: dict[str, str], today: str) -> list: ...


class HospitalRules(Protocol):
    """services.pipelines.PipelineService: the hospitals of the projects a user works on."""

    def workable_hospital_ids(self, user_id: str) -> set[int]: ...


# -- checks ------------------------------------------------------------------------------------


def _date(value, what: str, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValidationError(f"the {what} is required")
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValidationError(f"the {what} must be a date (YYYY-MM-DD)") from exc


def clock_time(value, what: str = "time") -> str | None:
    """"HH:MM" (or "HH:MM:00") -> "HH:MM"; empty -> None. Also used by services.tasks."""
    if value in (None, ""):
        return None
    match = _TIME.match(str(value).strip())
    if not match:
        raise ValidationError(f"the {what} must be HH:MM")
    return f"{match.group(1)}:{match.group(2)}"


_time = clock_time


def _url(value) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > MAX_URL or not re.match(r"^https?://\S+$", text, re.IGNORECASE):
        raise ValidationError("the evidence link must be a web address (http or https)")
    return text


def _timezone(value) -> str:
    name = (value or "Asia/Seoul").strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("unknown time zone") from exc
    return name


def ranked_posts(posts: list[PostBrief], limit: int = FIND_POSTS) -> list[PostBrief]:
    """Titles with schedule words first, then the newest; at most `limit`."""

    def schedule_title(p: PostBrief) -> bool:
        title = p.title.lower()
        return any(w in title for w in SCHEDULE_WORDS)

    newest_first = sorted(posts, key=lambda p: (p.posted_on or "", p.document_id), reverse=True)
    return sorted(newest_first, key=lambda p: 0 if schedule_title(p) else 1)[:limit]  # stable


def upcoming(sessions: list[Session], today: date) -> list[Session]:
    """Sessions a step may aim for: shown (not rejected or removed), not cancelled, meeting
    today or later; by meeting day."""
    day = today.isoformat()
    return sorted(
        (s for s in sessions if s.visible and s.state != CANCELLED and s.meets_on >= day),
        key=lambda s: (s.meets_on, s.meets_at or "", s.id),
    )[:MAX_UPCOMING]


def suggested_session(sessions: list[Session], today: date) -> Session | None:
    """The hint on the step page: the next approved session whose cutoff (else its meeting day)
    is today or later; else the next such proposal. Nothing is saved until the user picks."""
    day = today.isoformat()
    open_ones = [s for s in upcoming(sessions, today) if s.deadline >= day]
    return next((s for s in open_ones if s.state in APPROVED), None) or next(
        (s for s in open_ones if s.state == PROPOSED), None
    )


def target_warning(session: Session | None) -> str | None:
    """Why the step's target needs a look; None when it is fine."""
    if session is None:
        return None
    if session.deleted_at is not None:
        return "removed"
    if session.state in (CANCELLED, REJECTED):
        return session.state
    return None


# -- the service --------------------------------------------------------------------------------


class SessionService:
    def __init__(
        self,
        users: UserStore,
        store: SessionStore,
        audit: AuditSink,
        *,
        finder: SessionFinder | None = None,
        runner: JobRunner | None = None,
        rules: HospitalRules | None = None,
        hospital_exists: Callable[[int], bool] = lambda _id: True,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._store = store
        self._audit = audit
        self._finder = finder
        self._runner = runner
        self._rules = rules
        self._hospital_exists = hospital_exists
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    @property
    def finder_enabled(self) -> bool:
        return self._finder is not None and self._runner is not None

    def today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    def _now(self) -> str:
        return self._clock().isoformat()

    def _is_admin(self, user_id: str) -> bool:
        return active_profile(self._users, user_id, "your account is not active").role == "admin"

    def sees_evidence(self, user_id: str) -> bool:
        """Whether the AI's quote and the stored post it came from are shown: platform admins
        only. The post may be on a members-only board, and sessions are read by everyone."""
        try:
            return self._is_admin(user_id)
        except ForbiddenError:
            return False

    def _hospital(self, hospital_id: int) -> None:
        if not self._hospital_exists(hospital_id):
            raise NotFoundError("hospital not found")

    # -- kinds ---------------------------------------------------------------------------------

    def kinds(self, include_archived: bool = False) -> list[SessionKind]:
        rows = sorted(self._store.kinds(), key=lambda k: (k.position, k.key))
        return [k for k in rows if include_archived or k.archived_at is None]

    def _kind(self, key) -> str:
        key = (key or "").strip()
        if key not in {k.key for k in self.kinds()}:
            raise ValidationError("unknown or archived session kind")
        return key

    def add_kind(self, admin_id: str, key: str, label: str) -> SessionKind:
        key = (key or "").strip()
        if not _KEY.match(key):
            raise ValidationError("the key must be lower-case letters, digits or _ and start with a letter")
        label = " ".join((label or "").split())
        if not 1 <= len(label) <= MAX_LABEL:
            raise ValidationError(f"the label must have 1 to {MAX_LABEL} characters")
        existing = self._store.kinds()
        if any(k.label.lower() == label.lower() and k.archived_at is None for k in existing):
            raise ConflictError("a live session kind already has this label")
        try:
            kind = self._store.add_kind(key, label, max((k.position for k in existing), default=0) + 1)
        except LookupError as exc:
            raise ConflictError("this key is taken") from exc
        self._audit.record("admin", "session_kind.created", admin_id, title="Session kind added", payload={"key": key})
        return kind

    def update_kind(self, admin_id: str, key: str, changes: dict) -> SessionKind:
        kind = next((k for k in self._store.kinds() if k.key == key), None)
        if kind is None:
            raise NotFoundError("session kind not found")
        values: dict = {}
        if "label" in changes:
            label = " ".join((changes["label"] or "").split())
            if not 1 <= len(label) <= MAX_LABEL:
                raise ValidationError(f"the label must have 1 to {MAX_LABEL} characters")
            if any(k.key != key and k.archived_at is None and k.label.lower() == label.lower() for k in self._store.kinds()):
                raise ConflictError("a live session kind already has this label")
            values["label"] = label
        if "position" in changes and changes["position"] is not None:
            values["position"] = int(changes["position"])
        if "archived" in changes:
            if changes["archived"] and key == OTHER_KIND:
                raise ConflictError("'other' is used for AI proposals of an unknown kind; it cannot be archived")
            values["archived_at"] = (kind.archived_at or self._now()) if changes["archived"] else None
        if not values:
            return kind
        updated = self._store.update_kind(key, values)
        self._audit.record(
            "admin", "session_kind.updated", admin_id, title="Session kind changed",
            payload={"key": key, "fields": sorted(values)},
        )
        return updated

    # -- reading -------------------------------------------------------------------------------

    def _range(self, start: str | None, end: str | None) -> tuple[str, str]:
        today = self.today()
        s = _date(start, "start date") or (today - timedelta(days=PAST_DAYS)).isoformat()
        e = _date(end, "end date") or (today + timedelta(days=FUTURE_DAYS)).isoformat()
        if e < s:
            raise ValidationError("the end date is before the start date")
        if (date.fromisoformat(e) - date.fromisoformat(s)).days > MAX_RANGE_DAYS + PAST_DAYS:
            raise ValidationError(f"ask for at most {MAX_RANGE_DAYS + PAST_DAYS} days at a time")
        return s, e

    def for_hospital(self, user_id: str, hospital_id: int, start: str | None = None, end: str | None = None) -> list[Session]:
        """Any signed-in user: the hospital's sessions in the range, except rejected and removed
        ones (proposals included). Admins also get rejected ones (the page can hide them)."""
        admin = self._is_admin(user_id)
        self._hospital(hospital_id)
        s, e = self._range(start, end)
        rows = self._store.between([hospital_id], s, e)
        return sorted(
            (x for x in rows if x.visible or (admin and x.deleted_at is None)),
            key=lambda x: (x.meets_on, x.meets_at or "", x.id),
        )

    def between(self, hospital_ids: list[int], start: str, end: str) -> list[Session]:
        """For the calendar (the caller checked the user): shown sessions only."""
        if not hospital_ids:
            return []
        return [x for x in self._store.between(sorted(set(hospital_ids)), start, end) if x.visible]

    def get_many(self, session_ids: list[int]) -> dict[int, Session]:
        ids = sorted({i for i in session_ids if i is not None})
        return {s.id: s for s in self._store.sessions(ids)} if ids else {}

    def upcoming_for(self, hospital_id: int) -> tuple[list[Session], Session | None]:
        """For the step page: the sessions a step may aim for and the hint."""
        today = self.today()
        rows = self._store.between([hospital_id], today.isoformat(), (today + timedelta(days=FUTURE_DAYS)).isoformat())
        return upcoming(rows, today), suggested_session(rows, today)

    def check_target(self, session_id: int, hospital_id: int) -> Session:
        """A step's new target: a shown, not cancelled session of the step's hospital."""
        session = self._store.get(session_id)
        if session is None or not session.visible or session.hospital_id != hospital_id:
            raise ValidationError("pick a session of this step's hospital")
        if session.state == CANCELLED:
            raise ValidationError("this session is cancelled; pick another one")
        return session

    # -- values --------------------------------------------------------------------------------

    def _values(self, data: dict, current: Session | None = None) -> dict:
        """Checked fields from `data` (only the keys given), merged rules with `current`."""
        values: dict = {}
        if "kind_key" in data:
            values["kind_key"] = self._kind(data["kind_key"])
        if "meets_on" in data:
            values["meets_on"] = _date(data["meets_on"], "meeting date", required=True)
        if "meets_at" in data:
            values["meets_at"] = _time(data["meets_at"], "meeting time")
        if "cutoff_on" in data:
            values["cutoff_on"] = _date(data["cutoff_on"], "cutoff date")
        if "cutoff_at" in data:
            values["cutoff_at"] = _time(data["cutoff_at"], "cutoff time")
        if "timezone" in data:
            values["timezone"] = _timezone(data["timezone"])
        if "note" in data:
            note = (data["note"] or "").strip()
            if len(note) > MAX_NOTE:
                raise ValidationError(f"the note may have at most {MAX_NOTE} characters")
            values["note"] = note
        if "evidence_url" in data:
            values["evidence_url"] = _url(data["evidence_url"])
        meets = values.get("meets_on", current.meets_on if current else None)
        cutoff = values.get("cutoff_on", current.cutoff_on if current else None)
        cutoff_at = values.get("cutoff_at", current.cutoff_at if current else None)
        if cutoff and meets and cutoff > meets:
            raise ValidationError("the cutoff cannot be after the meeting")
        if cutoff_at and not cutoff:
            raise ValidationError("a cutoff time needs a cutoff date")
        return values

    def _create(self, values: dict) -> Session:
        try:
            return self._store.create(values)
        except SessionTakenError as exc:
            raise ConflictError("this hospital already has a session of this kind on that day") from exc

    def _save(self, session_id: int, values: dict) -> Session:
        try:
            return self._store.update(session_id, values)
        except SessionTakenError as exc:
            raise ConflictError("this hospital already has a session of this kind on that day") from exc

    @staticmethod
    def _payload(s: Session, **extra) -> dict:
        # Ids, dates and states only: notes and links are typed text and the log is append-only.
        return {
            "session_id": s.id, "hospital_id": s.hospital_id, "kind": s.kind_key, "meets_on": s.meets_on,
            "state": s.state, "origin": s.origin, **extra,
        }

    # -- users: proposals ----------------------------------------------------------------------

    def propose(self, user_id: str, hospital_id: int, data: dict) -> Session:
        """A user who works on a project with this hospital (or an admin) proposes a session.
        Evidence is a link and a note; stored document versions are linked by admins and the AI
        job (a user's reading rights differ per board)."""
        admin = self._is_admin(user_id)
        self._hospital(hospital_id)
        if not admin and (self._rules is None or hospital_id not in self._rules.workable_hospital_ids(user_id)):
            raise ForbiddenError("you can propose sessions only for hospitals of projects you work on")
        values = self._values({**data, "kind_key": data.get("kind_key") or OTHER_KIND})
        if "meets_on" not in values:
            raise ValidationError("the meeting date is required")
        now = self._now()
        session = self._create({
            **values, "hospital_id": hospital_id, "state": PROPOSED, "origin": "user",
            "proposed_by": user_id, "proposed_at": now,
        })
        self._audit.record("user", "session.proposed", user_id, title="IRB session proposed", payload=self._payload(session))
        return session

    def withdraw(self, user_id: str, session_id: int) -> Session:
        self._is_admin(user_id)
        session = self._store.get(session_id)
        if session is None or session.deleted_at is not None or session.proposed_by != user_id or session.origin != "user":
            raise NotFoundError("proposal not found")
        if session.state != PROPOSED:
            raise ConflictError("an admin has reviewed this proposal already; ask an admin to change it")
        updated = self._save(session.id, {"deleted_at": self._now()})
        self._audit.record("user", "session.withdrawn", user_id, title="IRB session proposal withdrawn", payload=self._payload(updated))
        return updated

    # -- admins --------------------------------------------------------------------------------

    def proposals(self, limit: int = 200) -> list[Session]:
        return sorted(self._store.proposals(limit), key=lambda s: (s.hospital_id, s.meets_on, s.id))

    def create(self, admin_id: str, hospital_id: int, data: dict, state: str = TENTATIVE) -> Session:
        self._hospital(hospital_id)
        if state not in APPROVED:
            raise ValidationError("a new session is tentative or confirmed")
        values = self._values({**data, "kind_key": data.get("kind_key") or OTHER_KIND})
        if "meets_on" not in values:
            raise ValidationError("the meeting date is required")
        if data.get("evidence_version_id") is not None:
            values["evidence_version_id"] = self._version(int(data["evidence_version_id"]), hospital_id)
        now = self._now()
        session = self._create({
            **values, "hospital_id": hospital_id, "state": state, "origin": "admin",
            "proposed_by": admin_id, "proposed_at": now, "reviewed_by": admin_id, "reviewed_at": now,
        })
        self._audit.record("admin", "session.created", admin_id, title="IRB session added", payload=self._payload(session))
        return session

    def _version(self, version_id: int, hospital_id: int) -> int:
        if self._store.version_hospital(version_id) != hospital_id:
            raise ValidationError("the evidence must be a stored document of this hospital's boards")
        return version_id

    def update(self, admin_id: str, session_id: int, changes: dict) -> Session:
        """Fields and the state (see MOVES). Approving or rejecting records who and when."""
        session = self._store.get(session_id)
        if session is None or session.deleted_at is not None:
            raise NotFoundError("session not found")
        values = self._values(changes, session)
        if "evidence_version_id" in changes:
            vid = changes["evidence_version_id"]
            values["evidence_version_id"] = None if vid is None else self._version(int(vid), session.hospital_id)
        if "state" in changes and changes["state"] != session.state:
            state = changes["state"]
            if state not in STATES:
                raise ValidationError("unknown state")
            if state not in MOVES[session.state]:
                raise ConflictError(f"a {session.state} session cannot become {state}")
            values["state"] = state
            values["reviewed_by"] = admin_id
            values["reviewed_at"] = self._now()
        values = {k: v for k, v in values.items() if getattr(session, k, None) != v or k in ("reviewed_by", "reviewed_at")}
        if not values or set(values) <= {"reviewed_by", "reviewed_at"}:
            return session
        updated = self._save(session.id, values)
        self._audit.record(
            "admin", "session.updated", admin_id, title="IRB session changed",
            payload=self._payload(updated, fields=sorted(k for k in values if k not in ("reviewed_by", "reviewed_at")),
                                  from_state=session.state),
        )
        return updated

    def remove(self, admin_id: str, session_id: int) -> Session:
        session = self._store.get(session_id)
        if session is None or session.deleted_at is not None:
            raise NotFoundError("session not found")
        updated = self._save(session.id, {"deleted_at": self._now()})
        self._audit.record("admin", "session.removed", admin_id, title="IRB session removed", payload=self._payload(updated))
        return updated

    # -- the AI job ----------------------------------------------------------------------------

    def jobs(self, hospital_id: int | None = None) -> list[SessionJob]:
        return self._store.jobs(hospital_id, SHOW_JOBS)

    def job(self, job_id: str) -> SessionJob:
        job = self._store.get_job(job_id)
        if job is None:
            raise NotFoundError("job not found")
        return job

    def _posts(self, hospital_id: int) -> list[PostBrief]:
        return ranked_posts(self._store.posts_for(hospital_id, SCAN_POSTS))

    def start_find(self, admin_id: str, hospital_id: int) -> SessionJob:
        """A platform admin starts reading the hospital's stored posts (boards with AI on). The
        refusals come at once: AI not set up, no posts, a job already open."""
        if not self.finder_enabled:
            raise UnavailableError("finding sessions with AI is not set up on this server (OPENAI_API_KEY, OPENAI_MODEL)")
        self._hospital(hospital_id)
        posts = self._posts(hospital_id)
        if not posts:
            raise ConflictError(
                "this hospital has no stored posts on boards with AI on; nothing can be read (turn AI on for a board first)"
            )
        if len(self._store.open_jobs_of_user(admin_id)) >= MAX_OPEN_JOBS_PER_USER:
            raise TooManyRequestsError(f"{MAX_OPEN_JOBS_PER_USER} of your session searches are running; wait for one")
        try:
            job = self._store.create_job(hospital_id, admin_id)
        except SessionJobTakenError as exc:
            raise ConflictError("sessions of this hospital are being looked for already") from exc
        # Counted before the call (usage per user and action).
        self._audit.record(
            "admin", "session.find_started", admin_id, title="IRB session search started",
            payload={"hospital_id": hospital_id, "job_id": job.id, "posts": len(posts)},
        )
        try:
            self._runner.submit(lambda: self._run(job, admin_id, posts))  # type: ignore[union-attr]
        except Exception as exc:
            log.exception("session job %s could not be queued", job.id)
            self._finish(job.id, "failed", error="the search could not be started; please try again")
            raise ServiceError("the search could not be started; please try again") from exc
        return self._store.get_job(job.id) or job

    def _finish(self, job_id: str, status: str, **values) -> None:
        try:
            self._store.update_job(job_id, {"status": status, "finished_at": self._now(), **values})
        except Exception:
            log.exception("session job %s could not be marked %s", job_id, status)

    def _run(self, job: SessionJob, admin_id: str, posts: list[PostBrief]) -> None:
        """Never raises (a worker thread). Every outcome ends in done or failed with a reason."""
        try:
            self._store.update_job(job.id, {"status": "running", "started_at": self._now()})
        except Exception:
            log.exception("session job %s could not be marked running", job.id)
        try:
            counts = self._find(job, admin_id, posts)
        except ServiceError as exc:
            self._finish(job.id, "failed", error=exc.message[:1000])
            counts = None
        except Exception as exc:
            log.exception("session job %s failed", job.id)
            self._finish(job.id, "failed", error=f"the AI service or the database failed ({type(exc).__name__}); try again later")
            counts = None
        if counts is not None:
            self._finish(job.id, "done", **counts)
        try:
            self._audit.record(
                "admin", "session.find_finished", admin_id, title="IRB session search finished",
                payload={"hospital_id": job.hospital_id, "job_id": job.id, "ok": counts is not None, **(counts or {})},
            )
        except Exception:
            log.exception("the end of session job %s could not be logged", job.id)

    def _texts(self, posts: list[PostBrief]) -> tuple[list[str], list[PostBrief]]:
        texts: list[str] = []
        sent: list[PostBrief] = []
        total = 0
        for p in posts:
            content = self._store.version_content(p.version_id)
            if content is None:
                continue
            text = document_digest(content).text[:POST_CHARS]
            if not text.strip():
                continue  # a scanned or hash-only post; later posts may still have text
            text = text[: TOTAL_CHARS - total]
            texts.append(text)
            sent.append(p)
            total += len(text)
            if total >= TOTAL_CHARS:
                break
        return texts, sent

    def _find(self, job: SessionJob, admin_id: str, posts: list[PostBrief]) -> dict:
        texts, sent = self._texts(posts)
        if not texts:
            raise ServiceError("the stored posts of this hospital have no readable text; nothing was sent")
        kinds = {k.key: k.label for k in self.kinds()}
        today = self.today()
        found = self._finder.find(texts, kinds, today.isoformat())  # type: ignore[union-attr]
        proposed = skipped = 0
        seen: set[tuple[str, str]] = set()
        for item in found[:MAX_FOUND]:
            values = self._checked_found(item, sent, kinds, today)
            if values is None:
                skipped += 1
                continue
            key = (values["kind_key"], values["meets_on"])
            if key in seen or self._store.same_day(job.hospital_id, *key):
                skipped += 1  # listed already, or an admin rejected it before
                continue
            seen.add(key)
            try:
                self._store.create({
                    **values, "hospital_id": job.hospital_id, "state": PROPOSED, "origin": "ai",
                    "proposed_by": admin_id, "proposed_at": self._now(), "job_id": job.id,
                })
            except SessionTakenError:
                skipped += 1
                continue
            proposed += 1
        skipped += max(0, len(found) - MAX_FOUND)
        return {"documents_read": len(sent), "proposed": proposed, "skipped": skipped}

    @staticmethod
    def _checked_found(item, sent: list[PostBrief], kinds: dict[str, str], today: date) -> dict | None:
        """One model answer row as session values, or None when any part is not usable. The
        answer is untrusted: every field is checked again."""
        try:
            post = int(getattr(item, "post", 0))
            if not 1 <= post <= len(sent):
                return None
            meets = date.fromisoformat(str(item.meets_on))
            if not today - timedelta(days=PAST_DAYS) <= meets <= today + timedelta(days=FUTURE_DAYS):
                return None
            cutoff = date.fromisoformat(str(item.cutoff_on)) if item.cutoff_on else None
            if cutoff is not None and cutoff > meets:
                return None
            meets_at = _time(item.meets_at, "time") if item.meets_at else None
            cutoff_at = _time(item.cutoff_at, "time") if item.cutoff_at and cutoff else None
        except (ValueError, TypeError, ValidationError):
            return None
        quote = " ".join(str(getattr(item, "quote", "") or "").split())[:MAX_QUOTE] or None
        kind = item.kind if item.kind in kinds else OTHER_KIND
        if kind not in kinds:
            return None  # 'other' archived: cannot happen (update_kind refuses it)
        return {
            "kind_key": kind, "meets_on": meets.isoformat(), "meets_at": meets_at,
            "cutoff_on": cutoff.isoformat() if cutoff else None, "cutoff_at": cutoff_at,
            "evidence_version_id": sent[post - 1].version_id, "evidence_quote": quote,
        }

    def fail_unfinished(self) -> int:
        try:
            count = self._store.fail_open_jobs(RESTARTED, self._clock())
        except Exception:
            log.exception("open session jobs could not be marked failed at start")
            return 0
        if count:
            log.warning("%d session job(s) were open at start and are marked failed", count)
        return count

    def purge_old(self) -> int:
        """Never raises: it runs from the scheduler."""
        try:
            return self._store.purge_jobs(self._clock() - timedelta(days=JOB_RETENTION_DAYS))
        except Exception:
            log.exception("old session jobs could not be deleted")
            return 0
