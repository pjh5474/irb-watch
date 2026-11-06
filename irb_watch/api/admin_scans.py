from fastapi import APIRouter, BackgroundTasks, Depends

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
