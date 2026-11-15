"""The IRB calendar (CAL-1): one view over step dates, hospital sessions and project tasks, and
the cutoff and task lines of the daily reminder mail.

Owner decisions (2026-11-15; docs/decisions.md "IRB calendar, CAL-1"):
  * The calendar covers the projects the user may work on (as /work). Step dates are read
    where they are (pipeline_steps), never copied.
  * Sessions are shown for the hospitals of those projects, plus any hospital asked for; every
    session except rejected and removed ones, proposals marked.
  * Reminders reuse A2's days (services/pipeline_work.py): a step aiming for a meeting gets a
    cutoff line 14/7/3/1/0 days before the session's cutoff (its meeting day when no cutoff is
    set) while the step is open and not submitted, to the project owner and PM; proposals too,
    marked "not confirmed"; cancelled sessions send nothing. A task reminds on the same days and
    then every 7 days overdue while not done, to its assignee, or the owner and PM.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.mail.templates import DigestReminder
from irb_watch.services.errors import ValidationError
from irb_watch.services.pipeline_work import DUE_WINDOW_DAYS, days_left, is_reminder_day
from irb_watch.services.pipelines import WorkStep
from irb_watch.services.project_access import NEEDS
from irb_watch.services.sessions import CANCELLED, PROPOSED, Session, SessionKind
from irb_watch.services.tasks import Task

log = logging.getLogger(__name__)

MAX_DAYS = 62
WORKERS = NEEDS["edit"]  # who may work on a project (owner, manage, pm, edit)
MAX_HOSPITALS = 50  # hospitals asked for besides the user's own


class CalendarSteps(Protocol):
    """services.pipelines.PipelineService."""

    def calendar_scope(self, user_id: str) -> tuple[list[WorkStep], list[int], dict[int, set[int]]]:
        """(steps, workable project ids, project -> hospitals); checks the account."""
        ...
    def cutoff_steps(self) -> list[WorkStep]: ...
    def workable_project_ids(self, user_id: str) -> list[int]: ...
    def project_owners(self, project_ids: list[int]) -> dict[int, tuple[str, str | None]]: ...
    def project_pms(self, project_ids: list[int]) -> dict[int, str]: ...
    def hospital_names(self, hospital_ids: list[int]) -> dict[int, str]: ...
    def site_labels(self, site_ids: list[int]) -> dict[int, tuple[str, int]]: ...
    def level(self, user_id: str, project_id: int) -> str | None: ...


class CalendarSessions(Protocol):
    """services.sessions.SessionService."""

    def between(self, hospital_ids: list[int], start: str, end: str) -> list[Session]: ...
    def get_many(self, session_ids: list[int]) -> dict[int, Session]: ...
    def kinds(self, include_archived: bool = False) -> list[SessionKind]: ...


class CalendarTasks(Protocol):
    """services.tasks.TaskService."""

    def between(self, project_ids: list[int], start: str, end: str) -> list[Task]: ...
    def due(self, until: str) -> list[Task]: ...


@dataclass(frozen=True)
class Item:
    type: str  # step_due | step_submitted | step_decided | session_meeting | session_cutoff | task
    date: str
    title: str
    time: str | None = None
    project_id: int | None = None
    project_name: str | None = None
    hospital_id: int | None = None
    hospital_name: str | None = None
    site_id: int | None = None
    site_label: str = ""
    pipeline_id: int | None = None
    step_id: int | None = None
    status_label: str = ""
    closed: bool = False
    session_id: int | None = None
    session_state: str | None = None
    kind_key: str | None = None
    target_step_ids: tuple[int, ...] = ()  # the user's steps aiming for this session
    task_id: int | None = None
    done: bool = False
    assignee_id: str | None = None


@dataclass(frozen=True)
class CalendarView:
    start: str
    end: str
    items: list[Item]
    projects: dict[int, str]
    hospitals: dict[int, str]
    sites: dict[int, tuple[str, int]]  # site -> (label, hospital id)
    kinds: list[SessionKind] = field(default_factory=list)
    project_hospitals: dict[int, list[int]] = field(default_factory=dict)  # for the filters


def _range(start: str, end: str) -> tuple[str, str]:
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValidationError("from and to must be dates (YYYY-MM-DD)") from exc
    if e < s:
        raise ValidationError("'to' is before 'from'")
    if (e - s).days > MAX_DAYS:
        raise ValidationError(f"ask for at most {MAX_DAYS} days at a time")
    return s.isoformat(), e.isoformat()


def _in(day: str | None, start: str, end: str) -> bool:
    return day is not None and start <= day <= end


def step_items(steps: list[WorkStep], start: str, end: str) -> list[Item]:
    out = []
    for w in steps:
        for kind, day in (("step_due", w.due_on), ("step_submitted", w.submitted_on), ("step_decided", w.decided_on)):
            if not _in(day, start, end):
                continue
            # A submitted or closed step's due date stays visible (the page greys it).
            out.append(Item(
                kind, day, w.step_name, project_id=w.project_id, project_name=w.project_name,  # type: ignore[arg-type]
                hospital_id=w.hospital_id, hospital_name=w.hospital_name, site_id=w.site_id,
                site_label=w.site_label, pipeline_id=w.pipeline_id, step_id=w.step_id,
                status_label=w.status_label, closed=w.closed,
            ))
    return out


def session_items(
    sessions: list[Session], start: str, end: str, names: dict[int, str], kinds: dict[str, str],
    targets: dict[int, list[int]],
) -> list[Item]:
    out = []
    for s in sessions:
        label = kinds.get(s.kind_key, s.kind_key)
        common = {
            "hospital_id": s.hospital_id, "hospital_name": names.get(s.hospital_id), "session_id": s.id,
            "session_state": s.state, "kind_key": s.kind_key, "target_step_ids": tuple(sorted(targets.get(s.id, []))),
        }
        if _in(s.meets_on, start, end):
            out.append(Item("session_meeting", s.meets_on, label, time=s.meets_at, **common))  # type: ignore[arg-type]
        if _in(s.cutoff_on, start, end):
            out.append(Item("session_cutoff", s.cutoff_on, label, time=s.cutoff_at, **common))  # type: ignore[arg-type]
    return out


def task_items(
    tasks: list[Task], projects: dict[int, str], sites: dict[int, tuple[str, int]], hospitals: dict[int, str],
    step_sites: dict[int, int | None],
) -> list[Item]:
    out = []
    for t in tasks:
        site_id = t.site_id if t.site_id is not None else step_sites.get(t.step_id) if t.step_id else None
        label, hospital_id = sites.get(site_id, ("", None)) if site_id is not None else ("", None)
        out.append(Item(
            "task", t.due_on, t.title, time=t.due_at, project_id=t.project_id, project_name=projects.get(t.project_id),
            hospital_id=hospital_id, hospital_name=hospitals.get(hospital_id) if hospital_id else None,  # type: ignore[arg-type]
            site_id=site_id, site_label=label, step_id=t.step_id, task_id=t.id, done=t.done,
            assignee_id=t.assignee_id,
        ))
    return out


_ORDER = {"session_cutoff": 0, "step_due": 1, "task": 2, "session_meeting": 3, "step_submitted": 4, "step_decided": 5}


class CalendarService:
    def __init__(
        self,
        steps: CalendarSteps,
        sessions: CalendarSessions,
        tasks: CalendarTasks,
        *,
        app_url: str = "",
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._steps = steps
        self._sessions = sessions
        self._tasks = tasks
        self._app_url = app_url.rstrip("/")
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    def today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    # -- the page ------------------------------------------------------------------------------

    def view(self, user_id: str, start: str, end: str, hospital_ids: list[int] | None = None) -> CalendarView:
        """Every item between start and end (at most 62 days). Fails whole with a reason rather
        than showing a calendar with a part silently missing."""
        s, e = _range(start, end)
        extra = sorted(set(hospital_ids or []))
        if len(extra) > MAX_HOSPITALS:
            raise ValidationError(f"ask for at most {MAX_HOSPITALS} hospitals")
        steps, project_ids, by_project = self._steps.calendar_scope(user_id)  # checks the account
        own_hospitals: set[int] = set().union(set(), *by_project.values())
        hospitals = sorted(own_hospitals | set(extra))
        sessions = self._sessions.between(hospitals, s, e)
        tasks = self._tasks.between(project_ids, s, e)
        projects = {pid: info[0] for pid, info in self._steps.project_owners(project_ids).items()}
        site_ids = sorted({t.site_id for t in tasks if t.site_id is not None} | {w.site_id for w in steps if w.site_id is not None})
        sites = self._steps.site_labels(site_ids) if site_ids else {}
        names = self._steps.hospital_names(sorted(set(hospitals) | {h for _, h in sites.values()}))
        kinds = self._sessions.kinds(include_archived=True)
        targets: dict[int, list[int]] = {}
        for w in steps:
            if w.target_session_id is not None:
                targets.setdefault(w.target_session_id, []).append(w.step_id)
        step_sites = {w.step_id: w.site_id for w in steps}
        items = (
            step_items(steps, s, e)
            + session_items(sessions, s, e, names, {k.key: k.label for k in kinds}, targets)
            + task_items(tasks, projects, sites, names, step_sites)
        )
        items.sort(key=lambda i: (i.date, i.time or "99", _ORDER.get(i.type, 9), i.title))
        return CalendarView(
            s, e, items, projects, names, sites, kinds, {p: sorted(h) for p, h in by_project.items()},
        )

    # -- the daily mail ------------------------------------------------------------------------

    def reminder_lines(self, on_failure: Callable[[str], None] | None = None) -> dict[str, list[DigestReminder]]:
        """Today's cutoff and task lines, per recipient. Each part fails on its own (logged, and
        reported to on_failure with the part's name), so one broken read leaves the other lines
        and the step reminders in the mail."""
        today = self.today()
        out: dict[str, list[DigestReminder]] = {}
        for name, part in (("cutoffs", self._cutoff_lines), ("tasks", self._task_lines)):
            try:
                for user_id, lines in part(today).items():
                    out.setdefault(user_id, []).extend(lines)
            except Exception:
                log.exception("calendar reminders (%s) could not be read; today's mails go without them", name)
                if on_failure is not None:
                    on_failure(name)
        return out

    def _recipients(self, project_ids: list[int]) -> tuple[dict[int, tuple[str, str | None]], dict[int, str]]:
        return self._steps.project_owners(project_ids), self._steps.project_pms(project_ids)

    def _cutoff_lines(self, today: date) -> dict[str, list[DigestReminder]]:
        steps = self._steps.cutoff_steps()
        if not steps:
            return {}
        sessions = self._sessions.get_many([w.target_session_id for w in steps if w.target_session_id is not None])
        kinds = {k.key: k.label for k in self._sessions.kinds(include_archived=True)}
        due: list[tuple[WorkStep, Session, int]] = []
        for w in steps:
            session = sessions.get(w.target_session_id)  # type: ignore[arg-type]
            if session is None or not session.visible or session.state == CANCELLED:
                continue
            left = days_left(session.deadline, today)
            if left >= 0 and is_reminder_day(left):  # a passed cutoff is not repeated
                due.append((w, session, left))
        if not due:
            return {}
        owners, pms = self._recipients(sorted({w.project_id for w, _s, _l in due}))
        out: dict[str, list[DigestReminder]] = {}
        for w, session, left in sorted(due, key=lambda x: (x[2], x[0].project_name or "", x[0].step_id)):
            what = "접수 마감" if session.cutoff_on else "심의일"
            line = DigestReminder(
                w.project_name or "", w.hospital_name or "", w.step_name, session.deadline, left,
                f"{self._app_url}/ko/pipelines/{w.pipeline_id}/steps/{w.step_id}", w.site_label,
                kind="cutoff", unconfirmed=session.state == PROPOSED,
                detail=f"{kinds.get(session.kind_key, session.kind_key)} {session.meets_on} {what}",
            )
            owner = owners.get(w.project_id, ("", None))[1]
            for recipient in {owner, pms.get(w.project_id)} - {None}:
                out.setdefault(recipient, []).append(line)  # type: ignore[arg-type]
        return out

    def _task_lines(self, today: date) -> dict[str, list[DigestReminder]]:
        tasks = [t for t in self._tasks.due((today + timedelta(days=DUE_WINDOW_DAYS)).isoformat()) if not t.done]
        due = [(t, days_left(t.due_on, today)) for t in tasks]
        due = [(t, left) for t, left in due if is_reminder_day(left)]
        if not due:
            return {}
        project_ids = sorted({t.project_id for t, _ in due})
        owners, pms = self._recipients(project_ids)
        # One access read per assignee, not per task (owner, manage, pm, edit = WORKERS).
        workable = {a: set(self._steps.workable_project_ids(a)) for a in {t.assignee_id for t, _ in due if t.assignee_id}}
        out: dict[str, list[DigestReminder]] = {}
        for t, left in sorted(due, key=lambda x: (x[1], x[0].id)):
            name, owner = owners.get(t.project_id, (None, None))
            if name is None:
                continue  # the project is gone
            line = DigestReminder(
                name, "", t.title, t.due_on, left, f"{self._app_url}/ko/projects/{t.project_id}", "", kind="task",
            )
            # An assignee who may no longer work on the project gets nothing (the title is the
            # project's); the owner and PM are reminded instead.
            assignee = t.assignee_id if t.assignee_id and t.project_id in workable.get(t.assignee_id, ()) else None
            recipients = {assignee} if assignee else {owner, pms.get(t.project_id)}
            for recipient in recipients - {None}:
                out.setdefault(recipient, []).append(line)  # type: ignore[arg-type]
        return out
