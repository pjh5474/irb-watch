"""The user's open IRB work over all projects (IRB pipeline redesign, phase A2)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import (
    current_user,
    get_docgen_service,
    get_draft_job_service,
    get_pipeline_service,
)
from irb_watch.api.docgen import JobOut
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.docgen import DocgenService
from irb_watch.services.draft_jobs import DraftJobService, job_step
from irb_watch.services.pipeline_work import DUE_WINDOW_DAYS, days_left, work_lists
from irb_watch.services.pipelines import PipelineService, WorkStep

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

    @classmethod
    def of(cls, s: WorkStep, today) -> "WorkStepOut":
        return cls(
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
    user: AuthUser = Depends(current_user),
    pipelines: PipelineService = Depends(get_pipeline_service),
    docgen: DocgenService = Depends(get_docgen_service),
    jobs: DraftJobService = Depends(get_draft_job_service),
) -> WorkOut:
    """Overdue and due-soon steps, steps waiting for the IRB, steps missing required documents,
    and AI drafts being written or failed, over all the user's own live pipelines."""
    steps = pipelines.work_steps(user.id)  # first: the account check
    today = pipelines.today()
    lists = work_lists(steps, today)
    by_id = {s.step_id: s for s in steps}
    draft_steps = {d.id: d.pipeline_step_id for d in docgen.step_drafts(user.id, list(by_id))}
    drafts = []
    for j in jobs.list(user.id):
        step_id = job_step(j, draft_steps)
        if step_id in by_id:
            drafts.append(WorkJobOut(job=JobOut.of(j), step=WorkStepOut.of(by_id[step_id], today)))
    return WorkOut(
        today=today.isoformat(),
        window_days=DUE_WINDOW_DAYS,
        due=[WorkStepOut.of(s, today) for s in lists.due],
        waiting=[WorkStepOut.of(s, today) for s in lists.waiting],
        missing=[WorkStepOut.of(s, today) for s in lists.missing],
        drafts=drafts,
    )
