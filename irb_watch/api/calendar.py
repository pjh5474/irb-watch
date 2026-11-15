"""The IRB calendar (CAL-1): step dates, hospital sessions and project tasks in one list over
the projects the user may work on (as /work)."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from irb_watch.api.deps import current_user, get_calendar_service
from irb_watch.api.sessions import KindOut
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.calendar import CalendarService, Item

router = APIRouter(tags=["calendar"])


class ItemOut(BaseModel):
    type: str  # step_due | step_submitted | step_decided | session_meeting | session_cutoff | task
    date: str
    time: str | None  # null: no time (or time unknown)
    title: str  # step label, session kind label or task title
    project_id: int | None
    project_name: str | None
    hospital_id: int | None
    hospital_name: str | None
    site_id: int | None
    site_label: str
    pipeline_id: int | None
    step_id: int | None
    status_label: str
    closed: bool
    session_id: int | None
    session_state: str | None  # proposed (not confirmed) | tentative | confirmed | cancelled
    kind_key: str | None
    target_step_ids: list[int]  # the user's steps aiming for this session
    task_id: int | None
    done: bool
    assignee_id: str | None

    @classmethod
    def of(cls, i: Item) -> "ItemOut":
        return cls(**{**i.__dict__, "target_step_ids": list(i.target_step_ids)})


class SiteRef(BaseModel):
    label: str
    hospital_id: int


class CalendarOut(BaseModel):
    start: str
    end: str
    items: list[ItemOut]
    projects: dict[int, str]
    hospitals: dict[int, str]
    sites: dict[int, SiteRef]
    kinds: list[KindOut]
    project_hospitals: dict[int, list[int]]  # project -> its hospitals (the filters)


@router.get("/calendar", response_model=CalendarOut)
def calendar(
    start: str,
    end: str,
    hospital_id: list[int] = Query(default_factory=list),
    user: AuthUser = Depends(current_user),
    service: CalendarService = Depends(get_calendar_service),
):
    """Items dated start..end (at most 62 days). Sessions of the hospitals of the user's projects,
    plus any hospital_id asked for. A failure answers with its reason; it never looks empty."""
    view = service.view(user.id, start, end, hospital_id)
    return CalendarOut(
        start=view.start, end=view.end, items=[ItemOut.of(i) for i in view.items], projects=view.projects,
        hospitals=view.hospitals,
        sites={sid: SiteRef(label=label, hospital_id=h) for sid, (label, h) in view.sites.items()},
        kinds=[KindOut.of(k) for k in view.kinds], project_hospitals=view.project_hospitals,
    )
