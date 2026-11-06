from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from irb_watch.api.deps import get_scan_service, get_source_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.scan import ScanService
from irb_watch.services.sources import Source, SourceService

router = APIRouter(prefix="/admin/sources", tags=["admin-sources"])


class SourceOut(BaseModel):
    id: int
    name: str
    adapter: str
    url: str
    needs_browser: bool
    is_active: bool
    created_at: str
    last_scan_at: str | None
    last_scan_status: str | None
    last_scan_error: str | None
    scan_in_progress: bool
    login_user: str | None = None  # whose stored login this source scans with

    @classmethod
    def from_source(cls, s: Source) -> "SourceOut":
        return cls(
            id=s.id,
            name=s.name,
            adapter=s.adapter,
            url=s.url,
            needs_browser=s.needs_browser,
            is_active=s.is_active,
            created_at=s.created_at,
            last_scan_at=s.last_scan_at,
            last_scan_status=s.last_scan_status,
            last_scan_error=s.last_scan_error,
            scan_in_progress=s.scan_started_at is not None,
            login_user=s.config.get("login_user"),
        )


class CreateSourceIn(BaseModel):
    name: str
    url: str
    login_user: str | None = None


class UpdateSourceIn(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    login_user: str | None = None  # "" removes the login


@router.post("", status_code=201, response_model=SourceOut)
def create_source(
    body: CreateSourceIn,
    admin: AuthUser = Depends(require_admin),
    service: SourceService = Depends(get_source_service),
) -> SourceOut:
    return SourceOut.from_source(service.create(admin.id, body.name, body.url, body.login_user))


@router.get("", response_model=list[SourceOut])
def list_sources(
    _: AuthUser = Depends(require_admin),
    service: SourceService = Depends(get_source_service),
) -> list[SourceOut]:
    return [SourceOut.from_source(s) for s in service.list()]


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(
    source_id: int,
    body: UpdateSourceIn,
    admin: AuthUser = Depends(require_admin),
    service: SourceService = Depends(get_source_service),
) -> SourceOut:
    return SourceOut.from_source(
        service.update(admin.id, source_id, body.name, body.is_active, body.login_user)
    )


@router.post("/{source_id}/scan", status_code=202)
def start_scan(
    source_id: int,
    background: BackgroundTasks,
    admin: AuthUser = Depends(require_admin),
    scans: ScanService = Depends(get_scan_service),
) -> dict:
    """Starts a scan in the background. Poll GET /admin/sources for the result."""
    source = scans.begin_scan(source_id)  # 404 / 409 are raised here, before 202
    background.add_task(scans.run_scan, source, admin.id)
    return {"status": "started", "source_id": source.id}
