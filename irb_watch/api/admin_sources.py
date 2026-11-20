import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import (
    get_alert_service,
    get_board_sync_service,
    get_scan_service,
    get_source_service,
    require_admin,
)
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.alerts import AlertService
from irb_watch.services.board_sync import BoardSyncService
from irb_watch.services.scan import ScanService
from irb_watch.services.source_login import login_site
from irb_watch.services.sources import Source, SourceService

log = logging.getLogger(__name__)

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
    requires_login: bool = False  # members-only (a study): readable only by assigned users
    scans_paused_reason: str | None = None  # why daily scans skip this source, if they do
    ai_enabled: bool = True  # False: no text of this source goes to the AI services
    # The site name its stored login is filed under (PUT /admin/credentials/{label}/{site}):
    # snuh-<board key>, or rbay-<hospital code> (one login per R-BAY hospital).
    login_site: str | None = None
    hospital_id: int | None = None  # the hospital this board belongs to
    board_kind: str = "other"  # notice | forms | regulations | committee_members | news | faq | other

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
            requires_login=s.requires_login,
            scans_paused_reason=s.scans_paused_reason,
            ai_enabled=s.ai_enabled,
            login_site=login_site(s),
            hospital_id=s.hospital_id,
            board_kind=s.board_kind,
        )


class CreateSourceIn(BaseModel):
    name: str
    url: str
    login_user: str | None = None
    # Default: off for a members-only source (login_user set), on for a public one.
    ai_enabled: bool | None = None
    # Default: the hospital of another board on the same site (R-BAY hospital code, SNUH),
    # else the hospital named like the board (R-BAY: the part before " : "), created if missing.
    hospital_id: int | None = None
    board_kind: str | None = None  # default: from the R-BAY board code, else "other"


class UpdateSourceIn(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    login_user: str | None = None  # "" removes the login
    ai_enabled: bool | None = None
    hospital_id: int | None = None  # move the board to another hospital
    board_kind: str | None = None


@router.post("", status_code=201, response_model=SourceOut)
def create_source(
    body: CreateSourceIn,
    admin: AuthUser = Depends(require_admin),
    service: SourceService = Depends(get_source_service),
) -> SourceOut:
    return SourceOut.from_source(
        service.create(
            admin.id, body.name, body.url, body.login_user, body.ai_enabled,
            body.hospital_id, body.board_kind,
        )
    )


class RbayHospitalIn(BaseModel):
    address: str  # https://www.r-bay.co.kr/agency/main/<code>
    hospital_name: str
    login_user: str
    boards: list[str] = Field(default_factory=lambda: ["NOTI", "FORM_BOX", "REGLA"])
    ai_enabled: bool | None = None  # default: on


class RbayHospitalOut(BaseModel):
    hospital_id: int
    hospital_made: bool
    sources: list[SourceOut]


@router.post("/rbay-hospital", status_code=201, response_model=RbayHospitalOut)
def create_rbay_hospital(
    body: RbayHospitalIn,
    admin: AuthUser = Depends(require_admin),
    service: SourceService = Depends(get_source_service),
) -> RbayHospitalOut:
    """One R-BAY hospital's boards in one step: one source per board under one hospital."""
    sources, hospital_id, made = service.create_rbay_hospital(
        admin.id, body.address, body.hospital_name, body.login_user, body.boards, body.ai_enabled
    )
    return RbayHospitalOut(
        hospital_id=hospital_id, hospital_made=made, sources=[SourceOut.from_source(s) for s in sources]
    )


class SyncBoardsIn(BaseModel):
    hospital_id: int
    dry_run: bool = True  # default: only show what would be made


class SyncGroupOut(BaseModel):
    agency: str
    login_user: str
    error: str | None  # set: the menu could not be read and nothing was changed
    menu: dict[str, str]  # every board of the e-IRB menu: code -> label
    already: list[str]
    to_create: list[str]
    not_in_menu: list[str]
    unknown: list[str]
    created: list[SourceOut]
    failed: dict[str, str]


class SyncBoardsOut(BaseModel):
    hospital_id: int
    hospital_name: str
    dry_run: bool
    groups: list[SyncGroupOut]


@router.post("/rbay-sync", response_model=SyncBoardsOut)
def sync_rbay_boards(
    body: SyncBoardsIn,
    admin: AuthUser = Depends(require_admin),
    service: BoardSyncService = Depends(get_board_sync_service),
) -> SyncBoardsOut:
    """Add the boards this R-BAY hospital has (IRB committee list, news, other, FAQ, ...) but the
    server does not read yet. One login per call, so call it once per hospital. Default is a dry
    run; send dry_run=false to create the sources (the first scan of each is a silent baseline)."""
    r = service.sync(admin.id, body.hospital_id, body.dry_run)
    return SyncBoardsOut(
        hospital_id=r.hospital_id,
        hospital_name=r.hospital_name,
        dry_run=r.dry_run,
        groups=[
            SyncGroupOut(
                agency=g.agency, login_user=g.login_user, error=g.error, menu=g.menu, already=g.already,
                to_create=g.to_create, not_in_menu=g.not_in_menu, unknown=g.unknown,
                created=[SourceOut.from_source(s) for s in g.created], failed=g.failed,
            )
            for g in r.groups
        ],
    )


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
        service.update(
            admin.id, source_id, body.name, body.is_active, body.login_user, body.ai_enabled,
            body.hospital_id, body.board_kind,
        )
    )


@router.post("/{source_id}/scan", status_code=202)
def start_scan(
    source_id: int,
    background: BackgroundTasks,
    admin: AuthUser = Depends(require_admin),
    scans: ScanService = Depends(get_scan_service),
    alerts: AlertService = Depends(get_alert_service),
) -> dict:
    """Starts a scan in the background. Poll GET /admin/sources for the result."""
    source = scans.begin_scan(source_id)  # 404 / 409 are raised here, before 202
    background.add_task(_scan_then_alert, scans, alerts, source, admin.id)
    return {"status": "started", "source_id": source.id}


def _scan_then_alert(
    scans: ScanService, alerts: AlertService, source: Source, actor_id: str | None
) -> None:
    """A manual scan, then look at this source's health so admins are mailed if it changed.
    A mail problem never turns into a scan problem."""
    scans.run_scan(source, actor_id, person_started=True)
    try:
        alerts.evaluate_source(source.id)
    except Exception:
        log.exception("alarm check after a manual scan of source %s failed", source.id)
