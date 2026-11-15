"""Project tasks (IRB calendar, CAL-1): dated items of a project that are not steps (an internal
review day, a sponsor meeting).

Owner decisions (2026-11-15; docs/decisions.md "IRB calendar, CAL-1"):
  * Title, date, optional time, note, optional site or step of the project, optional assignee
    (someone who may work on the project), done / not done.
  * Whoever may open the project reads them; whoever may edit it creates and changes them
    (phase B1's rule).
  * Removed tasks can be restored and are purged 30 days after removal. Changes are logged with
    ids and field names only (typed titles never go into the append-only log).
  * Reminders: A2's days before the date, then every 7 days overdue while not done, to the
    assignee, or the project owner and PM when there is none (services/calendar.py).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.services.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from irb_watch.services.project_access import NEEDS, REFUSALS
from irb_watch.services.sessions import clock_time
from irb_watch.services.users import AuditSink, UserStore, active_profile

log = logging.getLogger(__name__)

MAX_TITLE = 200
MAX_NOTE = 4000
MAX_TASKS = 500
PURGE_DAYS = 30


@dataclass(frozen=True)
class Task:
    id: int
    project_id: int
    title: str
    due_on: str
    due_at: str | None = None
    note: str = ""
    site_id: int | None = None
    step_id: int | None = None
    assignee_id: str | None = None
    done_at: str | None = None
    done_by: str | None = None
    created_by: str | None = None
    created_at: str = ""
    updated_at: str = ""
    deleted_at: str | None = None

    @property
    def done(self) -> bool:
        return self.done_at is not None


class TaskLinkError(Exception):
    """Raised by the store when the database refuses the site or step (another project)."""


class TaskStore(Protocol):
    def tasks_of_project(self, project_id: int) -> list[Task]: ...
    def tasks_between(self, project_ids: list[int], start: str, end: str) -> list[Task]:
        """Live tasks (done ones too) dated in [start, end]."""
        ...
    def due_tasks(self, until: str) -> list[Task]:
        """Live, not done tasks dated on or before `until` (for reminders)."""
        ...
    def get(self, task_id: int) -> Task | None: ...
    def create(self, values: dict) -> Task: ...
    def update(self, task_id: int, values: dict) -> Task: ...
    def purge_removed(self, cutoff: str) -> int: ...


class TaskRules(Protocol):
    """services.pipelines.PipelineService: the project access rule and where sites and steps
    belong."""

    def level(self, user_id: str, project_id: int) -> str | None: ...
    def site_project(self, site_id: int) -> int | None: ...
    def step_project(self, step_id: int) -> int | None: ...


def _date(value, what: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"the {what} must be a date (YYYY-MM-DD)") from exc




class TaskService:
    def __init__(
        self,
        users: UserStore,
        store: TaskStore,
        rules: TaskRules,
        audit: AuditSink,
        *,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._store = store
        self._rules = rules
        self._audit = audit
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    def today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    def _now(self) -> str:
        return self._clock().isoformat()

    def _check(self, user_id: str, project_id: int, need: str, missing: str) -> str:
        active_profile(self._users, user_id, "your account is not active")
        level = self._rules.level(user_id, project_id)
        if level is None:
            raise NotFoundError(missing)
        if level not in NEEDS[need]:
            raise ForbiddenError(REFUSALS[need])
        return level

    def _task(self, user_id: str, task_id: int, need: str) -> Task:
        task = self._store.get(task_id)
        if task is None:
            raise NotFoundError("task not found")
        self._check(user_id, task.project_id, need, "task not found")
        return task

    def _event(self, actor: str, kind: str, task: Task, fields: list[str] | None = None) -> None:
        payload: dict = {"task_id": task.id, "project_id": task.project_id}
        if fields is not None:
            payload["fields"] = sorted(fields)
        self._audit.record("user", kind, actor, title="Project task " + kind.split(".")[1], payload=payload)

    # -- reading -------------------------------------------------------------------------------

    def list_tasks(self, user_id: str, project_id: int, include_removed: bool = False) -> list[Task]:
        self._check(user_id, project_id, "read", "project not found")
        rows = [t for t in self._store.tasks_of_project(project_id) if include_removed or t.deleted_at is None]
        return sorted(rows, key=lambda t: (t.deleted_at is not None, t.done, t.due_on, t.due_at or "", t.id))

    # -- values --------------------------------------------------------------------------------

    def _values(self, project_id: int, data: dict) -> dict:
        values: dict = {}
        if "title" in data:
            title = " ".join((data["title"] or "").split())
            if not 1 <= len(title) <= MAX_TITLE:
                raise ValidationError(f"the title must have 1 to {MAX_TITLE} characters")
            values["title"] = title
        if "due_on" in data:
            values["due_on"] = _date(data["due_on"], "date")
        if "due_at" in data:
            values["due_at"] = clock_time(data["due_at"])
        if "note" in data:
            note = (data["note"] or "").strip()
            if len(note) > MAX_NOTE:
                raise ValidationError(f"the note may have at most {MAX_NOTE:,} characters")
            values["note"] = note
        if "site_id" in data:
            site = data["site_id"]
            if site is not None and self._rules.site_project(int(site)) != project_id:
                raise ValidationError("that site is not in this project")
            values["site_id"] = site
        if "step_id" in data:
            step = data["step_id"]
            if step is not None and self._rules.step_project(int(step)) != project_id:
                raise ValidationError("that step is not in this project")
            values["step_id"] = step
        if "assignee_id" in data:
            who = data["assignee_id"] or None
            if who is not None and self._rules.level(who, project_id) not in NEEDS["edit"]:
                raise ValidationError("the assignee must be someone who may work on this project")
            values["assignee_id"] = who
        return values

    def _write(self, fn: Callable[[], Task]) -> Task:
        try:
            return fn()
        except TaskLinkError as exc:
            raise ConflictError("the site or step is no longer in this project; reload the page") from exc
        except LookupError as exc:
            raise NotFoundError("this task no longer exists; reload the page") from exc

    # -- changes -------------------------------------------------------------------------------

    def create(self, user_id: str, project_id: int, data: dict) -> Task:
        self._check(user_id, project_id, "edit", "project not found")
        if "title" not in data or "due_on" not in data:
            raise ValidationError("a task needs a title and a date")
        values = self._values(project_id, data)
        live = sum(1 for t in self._store.tasks_of_project(project_id) if t.deleted_at is None)
        if live >= MAX_TASKS:
            raise ConflictError(f"a project can have at most {MAX_TASKS} tasks; remove old ones first")
        task = self._write(lambda: self._store.create({**values, "project_id": project_id, "created_by": user_id}))
        self._event(user_id, "task.created", task)
        return task

    def update(self, user_id: str, task_id: int, changes: dict) -> Task:
        """Fields sent only; `done` true/false marks it done or open again."""
        task = self._task(user_id, task_id, "edit")
        if task.deleted_at is not None:
            raise ConflictError("this task is removed; restore it first")
        if "done" in changes and not isinstance(changes["done"], bool):
            raise ValidationError("done must be true or false")
        values = self._values(task.project_id, changes)
        if "done" in changes and changes["done"] != task.done:
            values["done_at"] = self._now() if changes["done"] else None
            values["done_by"] = user_id if changes["done"] else None
        values = {k: v for k, v in values.items() if getattr(task, k) != v}
        if not values:
            return task
        updated = self._write(lambda: self._store.update(task.id, values))
        self._event(user_id, "task.updated", updated, [k for k in values if k != "done_by"])
        return updated

    def remove(self, user_id: str, task_id: int) -> Task:
        task = self._task(user_id, task_id, "edit")
        if task.deleted_at is not None:
            return task
        updated = self._write(lambda: self._store.update(task.id, {"deleted_at": self._now()}))
        self._event(user_id, "task.removed", updated)
        return updated

    def restore(self, user_id: str, task_id: int) -> Task:
        task = self._task(user_id, task_id, "edit")
        if task.deleted_at is None:
            return task
        live = sum(1 for t in self._store.tasks_of_project(task.project_id) if t.deleted_at is None)
        if live >= MAX_TASKS:
            raise ConflictError(f"a project can have at most {MAX_TASKS} tasks; remove old ones first")
        updated = self._write(lambda: self._store.update(task.id, {"deleted_at": None}))
        self._event(user_id, "task.restored", updated)
        return updated

    # -- for the calendar and reminders (callers checked access) --------------------------------

    def between(self, project_ids: list[int], start: str, end: str) -> list[Task]:
        return self._store.tasks_between(sorted(set(project_ids)), start, end) if project_ids else []

    def due(self, until: str) -> list[Task]:
        return self._store.due_tasks(until)

    def purge_old(self) -> int:
        """Never raises (runs from the scheduler)."""
        try:
            return self._store.purge_removed((self._clock() - timedelta(days=PURGE_DAYS)).isoformat())
        except Exception:
            log.exception("removed tasks could not be purged; the next run tries again")
            return 0
