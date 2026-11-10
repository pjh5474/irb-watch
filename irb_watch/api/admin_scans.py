from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_runner, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.errors import ConflictError
from irb_watch.services.runner import MANUAL, ScanRunner

router = APIRouter(prefix="/admin/scans", tags=["admin-scans"])


@router.post("/run-all", status_code=202)
def run_all(
    background: BackgroundTasks,
    admin: AuthUser = Depends(require_admin),
    runner: ScanRunner = Depends(get_runner),
) -> dict:
    """Scan every active source now, with the same rules as the daily run.

    One worker per site, sites in parallel. Sources already scanning, or paused after a
    rejected login, are skipped and the reason is recorded. Returns at once; poll
    GET /admin/sources for each source's result.
    """
    if runner.is_running():
        raise ConflictError("a full run is already in progress")
    background.add_task(runner.run_all, MANUAL, admin.id)
    return {"status": "started"}


class SelectedIn(BaseModel):
    source_ids: list[int] = Field(min_length=1, max_length=500)


@router.post("/run-selected", status_code=202)
def run_selected(
    body: SelectedIn,
    background: BackgroundTasks,
    admin: AuthUser = Depends(require_admin),
    runner: ScanRunner = Depends(get_runner),
) -> dict:
    """Scan the chosen sources now, with the daily run's rules: one worker per site (a site's
    boards one after another, never in parallel), inactive, running and paused sources skipped
    with the reason recorded. Refused while another full run is going."""
    if runner.is_running():
        raise ConflictError("a full run is already in progress")
    background.add_task(runner.run_all, MANUAL, admin.id, frozenset(body.source_ids))
    return {"status": "started", "count": len(set(body.source_ids))}
