"""Admin: ALL-SEARCH, the hospital's whole backlog and the AI search over it (see
services/session_all.py). Two jobs per hospital, each with a status, progress, a readable reason
when it fails and a stop."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import get_all_search_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.session_all import AllSearchService, Preview, SearchJob

router = APIRouter(prefix="/admin", tags=["admin-all-search"])
Service = AllSearchService


class SearchJobOut(BaseModel):
    id: str
    hospital_id: int
    stage: str  # read | ai
    status: str  # queued | running | done | stopped | failed
    stop_requested: bool
    error: str | None
    progress: dict  # read: boards -> counts; ai: batches, posts, proposed, skipped
    created_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def of(cls, j: SearchJob) -> "SearchJobOut":
        progress = {k: v for k, v in j.progress.items() if k != "read_versions"}  # ids stay on the server
        return cls(
            id=j.id, hospital_id=j.hospital_id, stage=j.stage, status=j.status, stop_requested=j.stop_requested,
            error=j.error, progress=progress, created_at=j.created_at, started_at=j.started_at, finished_at=j.finished_at,
        )


class PreviewOut(BaseModel):
    stored_posts: int
    posts: int
    chars: int
    batches: int
    run_batches: int
    left_over: int
    enabled: bool

    @classmethod
    def of(cls, p: Preview) -> "PreviewOut":
        return cls(**p.__dict__)


@router.get("/hospitals/{hospital_id}/all-search", response_model=list[SearchJobOut])
def jobs(hospital_id: int, _: AuthUser = Depends(require_admin), service: Service = Depends(get_all_search_service)):
    """The hospital's latest ALL-SEARCH jobs, newest first."""
    return [SearchJobOut.of(j) for j in service.jobs(hospital_id)]


@router.post("/hospitals/{hospital_id}/all-search/read", status_code=202, response_model=SearchJobOut)
def start_read(hospital_id: int, admin: AuthUser = Depends(require_admin), service: Service = Depends(get_all_search_service)):
    """Step 1: read the hospital's backlog (all its own boards, at most 500 posts per board),
    quietly. Poll the job; stop it any time; start it again to continue."""
    return SearchJobOut.of(service.start_read(admin.id, hospital_id))


@router.get("/hospitals/{hospital_id}/all-search/preview", response_model=PreviewOut)
def preview(hospital_id: int, admin: AuthUser = Depends(require_admin), service: Service = Depends(get_all_search_service)):
    """Step 2, before it starts: what the AI would read, in how many calls and characters."""
    return PreviewOut.of(service.preview(admin.id, hospital_id))


@router.post("/hospitals/{hospital_id}/all-search/ai", status_code=202, response_model=SearchJobOut)
def start_ai(hospital_id: int, admin: AuthUser = Depends(require_admin), service: Service = Depends(get_all_search_service)):
    """Step 2: the AI reads the candidate posts and writes NEW proposals only."""
    return SearchJobOut.of(service.start_ai(admin.id, hospital_id))


@router.get("/all-search-jobs/{job_id}", response_model=SearchJobOut)
def job(job_id: str, _: AuthUser = Depends(require_admin), service: Service = Depends(get_all_search_service)):
    return SearchJobOut.of(service.job(job_id))


@router.post("/all-search-jobs/{job_id}/stop", response_model=SearchJobOut)
def stop(job_id: str, admin: AuthUser = Depends(require_admin), service: Service = Depends(get_all_search_service)):
    """Ask a running job to stop: a read stops before its next post (what it read is kept)."""
    return SearchJobOut.of(service.stop(admin.id, job_id))
