"""The user's open IRB work over all projects (IRB pipeline redesign, phase A2)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import (
    current_user,
    get_docgen_service,
    get_draft_job_service,
    get_pipeline_service,
    get_project_access_service,
)
from irb_watch.api.docgen import JobOut
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.docgen import DocgenService
from irb_watch.services.draft_jobs import DraftJobService, job_step
from irb_watch.services.pipeline_work import DUE_WINDOW_DAYS, days_left, work_lists
from irb_watch.services.pipelines import PipelineService, WorkStep
from irb_watch.services.project_access import ProjectAccessService

router = APIRouter(tags=["work"])


class WorkStepOut(BaseModel):
    pipeline_id: int
    project_id: int
    project_name: str | None
    hospital_id: int
    hospital_name: str | None
    step_id: int
    step_name: str
    status_key: str
    status_label: str
    due_on: str | None
    submitted_on: str | None
    days_left: int | None  # to the due date; negative = overdue
    required: int
    required_missing: int
    mine: bool  # in a project the user owns (the "mine only" filter)
    site_label: str = ""  # "01 · PI 김○○"; empty for an unnamed site

    @classmethod
    def of(cls, s: WorkStep, today, user_id: str) -> "WorkStepOut":
        return cls(
            mine=s.project_owner_id == user_id,
            site_label=s.site_label,
            pipeline_id=s.pipeline_id, project_id=s.project_id, project_name=s.project_name,
            hospital_id=s.hospital_id, hospital_name=s.hospital_name, step_id=s.step_id,
            step_name=s.step_name, status_key=s.status_key, status_label=s.status_label,
            due_on=s.due_on, submitted_on=s.submitted_on,
            days_left=days_left(s.due_on, today) if s.due_on else None,
            required=s.required, required_missing=s.required_missing,
        )


class WorkJobOut(BaseModel):
    job: JobOut
    step: WorkStepOut


class WorkOut(BaseModel):
    today: str  # in the server's time zone (Asia/Seoul)
    window_days: int
    due: list[WorkStepOut]
    waiting: list[WorkStepOut]
    missing: list[WorkStepOut]
    drafts: list[WorkJobOut]  # AI drafts and revisions of pipeline steps: open, or failed in 24 h


@router.get("/work", response_model=WorkOut)
def my_work(
    mine: bool = False,
    user: AuthUser = Depends(current_user),
    pipelines: PipelineService = Depends(get_pipeline_service),
    docgen: DocgenService = Depends(get_docgen_service),
    jobs: DraftJobService = Depends(get_draft_job_service),
    access: ProjectAccessService = Depends(get_project_access_service),
) -> WorkOut:
    """Overdue and due-soon steps, steps waiting for the IRB, steps missing required documents,
    and AI drafts being written or failed, over every project the user may work on (own,
    shared, and an organization admin's organization). mine=true: projects the user owns."""
    steps = pipelines.work_steps(user.id)  # first: the account check
    if mine:
        steps = [s for s in steps if s.project_owner_id == user.id]
    today = pipelines.today()
    lists = work_lists(steps, today)
    by_id = {s.step_id: s for s in steps}
    draft_steps = {d.id: d.pipeline_step_id for d in docgen.step_drafts(user.id, list(by_id))}
    found = [(j, job_step(j, draft_steps)) for j in jobs.for_steps(list(by_id), list(draft_steps))]
    found = [(j, s) for j, s in found if s in by_id]
    names = access.names([j.user_id for j, _ in found if j.user_id != user.id])
    def out(s: WorkStep) -> WorkStepOut:
        return WorkStepOut.of(s, today, user.id)

    return WorkOut(
        today=today.isoformat(),
        window_days=DUE_WINDOW_DAYS,
        due=[out(s) for s in lists.due],
        waiting=[out(s) for s in lists.waiting],
        missing=[out(s) for s in lists.missing],
        drafts=[WorkJobOut(job=JobOut.of(j, user.id, names), step=out(by_id[s])) for j, s in found],
    )
