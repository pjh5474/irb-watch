"""Supabase-backed store for services.tasks (project tasks; CAL-1)."""

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.db.paging import chunks, paged
from irb_watch.services.tasks import Task, TaskLinkError

RAISED = "P0001"  # the link check trigger
_COLUMNS = (
    "id, project_id, title, due_on, due_at, note, site_id, step_id, assignee_id, done_at, done_by, created_by, "
    "created_at, updated_at, deleted_at"
)


def _task(r: dict) -> Task:
    return Task(
        id=int(r["id"]), project_id=int(r["project_id"]), title=r["title"], due_on=str(r["due_on"])[:10],
        due_at=str(r["due_at"])[:5] if r.get("due_at") else None, note=r.get("note") or "",
        site_id=r.get("site_id"), step_id=r.get("step_id"),
        assignee_id=str(r["assignee_id"]) if r.get("assignee_id") else None, done_at=r.get("done_at"),
        done_by=str(r["done_by"]) if r.get("done_by") else None,
        created_by=str(r["created_by"]) if r.get("created_by") else None,
        created_at=r.get("created_at") or "", updated_at=r.get("updated_at") or "", deleted_at=r.get("deleted_at"),
    )


class SupabaseTaskStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def tasks_of_project(self, project_id: int) -> list[Task]:
        rows = paged(lambda: self._c.table("project_tasks").select(_COLUMNS).eq("project_id", project_id).order("id"))
        return [_task(r) for r in rows]

    def tasks_between(self, project_ids: list[int], start: str, end: str) -> list[Task]:
        ids = sorted(set(project_ids))
        out: list[Task] = []
        for chunk in chunks(ids):
            rows = paged(
                lambda chunk=chunk: self._c.table("project_tasks").select(_COLUMNS).in_("project_id", chunk)
                .is_("deleted_at", "null").gte("due_on", start).lte("due_on", end).order("id")
            )
            out += [_task(r) for r in rows]
        return out

    def due_tasks(self, until: str) -> list[Task]:
        rows = paged(
            lambda: self._c.table("project_tasks").select(_COLUMNS).is_("deleted_at", "null").is_("done_at", "null")
            .lte("due_on", until).order("id")
        )
        return [_task(r) for r in rows]

    def get(self, task_id: int) -> Task | None:
        rows = self._c.table("project_tasks").select(_COLUMNS).eq("id", task_id).limit(1).execute().data
        return _task(rows[0]) if rows else None

    def _write(self, fn):
        try:
            rows = fn().execute().data
        except APIError as exc:
            if exc.code == RAISED:
                raise TaskLinkError(str(exc.message)) from exc
            raise
        if not rows:
            raise LookupError("gone")
        return _task(rows[0])

    def create(self, values: dict) -> Task:
        return self._write(lambda: self._c.table("project_tasks").insert(values))

    def update(self, task_id: int, values: dict) -> Task:
        return self._write(lambda: self._c.table("project_tasks").update(values).eq("id", task_id))

    def purge_removed(self, cutoff: str) -> int:
        rows = self._c.table("project_tasks").delete().lt("deleted_at", cutoff).execute().data
        return len(rows or [])
