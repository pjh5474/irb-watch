"""Project tasks (CAL-1): dated items of a project that are not steps. Whoever may open the
project reads them; whoever may edit it changes them (phase B1)."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from irb_watch.api.deps import current_user, get_project_access_service, get_task_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.project_access import ProjectAccessService
from irb_watch.services.tasks import MAX_NOTE, MAX_TITLE, Task, TaskService

router = APIRouter(tags=["tasks"])
Service = TaskService


class TaskOut(BaseModel):
    id: int
    project_id: int
    title: str
    due_on: str
    due_at: str | None
    note: str
    site_id: int | None
    step_id: int | None
    assignee_id: str | None
    assignee_name: str | None
    done: bool
    done_at: str | None
    done_by: str | None  # a name
    created_by: str | None  # a name
    created_at: str
    updated_at: str
    deleted_at: str | None

    @classmethod
    def of(cls, t: Task, names: dict[str, str] | None = None) -> "TaskOut":
        names = names or {}
        return cls(
            id=t.id, project_id=t.project_id, title=t.title, due_on=t.due_on, due_at=t.due_at, note=t.note,
            site_id=t.site_id, step_id=t.step_id, assignee_id=t.assignee_id,
            assignee_name=names.get(t.assignee_id) if t.assignee_id else None, done=t.done, done_at=t.done_at,
            done_by=names.get(t.done_by) if t.done_by else None,
            created_by=names.get(t.created_by) if t.created_by else None,
            created_at=t.created_at, updated_at=t.updated_at, deleted_at=t.deleted_at,
        )


def _user_id(value: str | None) -> str | None:
    """An account id is a UUID; anything else is a 422 here, not a database error later.
    None and an empty string still mean "nobody"."""
    if value is None or not value.strip():
        return None
    try:
        return str(UUID(value.strip()))
    except ValueError:
        raise ValueError("assignee_id must be a user id") from None


class TaskIn(BaseModel):
    title: str = Field(max_length=MAX_TITLE + 50)
    due_on: str = Field(max_length=10)
    due_at: str | None = Field(None, max_length=8)
    note: str = Field("", max_length=MAX_NOTE + 100)
    site_id: int | None = None
    step_id: int | None = None
    assignee_id: str | None = Field(None, max_length=64)

    @field_validator("assignee_id")
    @classmethod
    def _assignee(cls, value: str | None) -> str | None:
        return _user_id(value)


class TaskPatch(BaseModel):
    title: str | None = Field(None, max_length=MAX_TITLE + 50)
    due_on: str | None = Field(None, max_length=10)
    due_at: str | None = Field(None, max_length=8)
    note: str | None = Field(None, max_length=MAX_NOTE + 100)
    site_id: int | None = None
    step_id: int | None = None
    assignee_id: str | None = Field(None, max_length=64)
    done: bool | None = None  # null is refused (422 by the service), it never reopens a task

    @field_validator("assignee_id")
    @classmethod
    def _assignee(cls, value: str | None) -> str | None:
        return _user_id(value)


def _names(tasks: list[Task], access: ProjectAccessService) -> dict[str, str]:
    people = [x for t in tasks for x in (t.assignee_id, t.done_by, t.created_by) if x]
    return access.names(people) if people else {}


@router.get("/projects/{project_id}/tasks", response_model=list[TaskOut])
def list_tasks(
    project_id: int,
    include_removed: bool = False,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_task_service),
    access: ProjectAccessService = Depends(get_project_access_service),
):
    tasks = service.list_tasks(user.id, project_id, include_removed)
    names = _names(tasks, access)
    return [TaskOut.of(t, names) for t in tasks]


@router.post("/projects/{project_id}/tasks", status_code=201, response_model=TaskOut)
def create_task(
    project_id: int, body: TaskIn, user: AuthUser = Depends(current_user), service: Service = Depends(get_task_service),
    access: ProjectAccessService = Depends(get_project_access_service),
):
    data = {k: getattr(body, k) for k in body.model_fields_set | {"title", "due_on"}}
    task = service.create(user.id, project_id, data)
    return TaskOut.of(task, _names([task], access))


@router.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task(
    task_id: int, body: TaskPatch, user: AuthUser = Depends(current_user), service: Service = Depends(get_task_service),
    access: ProjectAccessService = Depends(get_project_access_service),
):
    task = service.update(user.id, task_id, {k: getattr(body, k) for k in body.model_fields_set})
    return TaskOut.of(task, _names([task], access))


@router.post("/tasks/{task_id}/remove", response_model=TaskOut)
def remove_task(task_id: int, user: AuthUser = Depends(current_user), service: Service = Depends(get_task_service)):
    return TaskOut.of(service.remove(user.id, task_id))


@router.post("/tasks/{task_id}/restore", response_model=TaskOut)
def restore_task(task_id: int, user: AuthUser = Depends(current_user), service: Service = Depends(get_task_service)):
    return TaskOut.of(service.restore(user.id, task_id))
