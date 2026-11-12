"""Admin: manual sources (a hospital without an e-IRB site; documents uploaded by hand).

Files travel as base64 inside JSON, like guide files (no form-parsing package is needed).
"""

import logging
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from irb_watch.api.admin_sources import SourceOut
from irb_watch.api.deps import (
    get_alert_service,
    get_manual_service,
    get_source_service,
    require_admin,
)
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.alerts import AlertService
from irb_watch.services.manual import (
    MAX_FILES,
    MAX_NOTE_CHARS,
    MAX_TITLE_CHARS,
    MAX_UPLOAD_BYTES,
    ManualFile,
    ManualResult,
    ManualService,
)
from irb_watch.services.sources import SourceService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-manual"])

# An upload's whole request, checked from its declared size before anything is read: the files
# (MAX_UPLOAD_BYTES) as base64 (4/3 larger) plus room for the rest of the JSON.
MAX_UPLOAD_REQUEST_BYTES = MAX_UPLOAD_BYTES * 4 // 3 + 2_000_000
UPLOAD_PATH = re.compile(r"^/admin/sources/\d+/manual-documents(/\d+/versions)?/?$")
# A user's upload to an IRB pipeline step: one file of at most MAX_FILE_BYTES (20 MB by default).
PIPELINE_UPLOAD_PATH = re.compile(r"^/pipelines/\d+/steps/\d+/documents/?$")
PIPELINE_FILE_BYTES = 20_000_000
MAX_PIPELINE_REQUEST_BYTES = PIPELINE_FILE_BYTES * 4 // 3 + 1_000_000


async def limit_upload_size(request: Request, call_next):
    """Refuse an upload request that is too big BEFORE its body is read, so one request cannot
    fill the small server's memory. A request without a declared size is refused too."""
    if request.method != "POST":
        return await call_next(request)
    path = request.url.path
    if UPLOAD_PATH.match(path):
        limit = MAX_UPLOAD_REQUEST_BYTES
        reason = (
            f"the upload is too large (at most {MAX_UPLOAD_BYTES / 1_000_000:.0f} MB of files at once); "
            "upload in two steps"
        )
    elif PIPELINE_UPLOAD_PATH.match(path):
        limit = MAX_PIPELINE_REQUEST_BYTES
        reason = f"the file is too large (at most {PIPELINE_FILE_BYTES / 1_000_000:.0f} MB)"
    else:
        return await call_next(request)
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit():
        return JSONResponse({"detail": "the upload must declare its size (Content-Length)"}, status_code=411)
    if int(declared) > limit:
        return JSONResponse({"detail": reason}, status_code=413)
    return await call_next(request)


class ManualSourceIn(BaseModel):
    name: str = Field(max_length=200)
    # True: readable only by admins and assigned members (like a study).
    members_only: bool = False
    # Default: off for a members-only source, on for a public one.
    ai_enabled: bool | None = None
    hospital_id: int | None = None  # default: the hospital with this name, created if missing
    board_kind: str | None = None  # default: "other"


class ManualFileIn(BaseModel):
    filename: str = Field(max_length=300)
    # 20 MB of file is about 27 million base64 characters; the real limits are in the service.
    content_base64: str = Field(max_length=28_000_000)


class NewDocumentIn(BaseModel):
    title: str = Field(max_length=MAX_TITLE_CHARS + 50)
    note: str = Field(default="", max_length=MAX_NOTE_CHARS + 1000)
    posted_on: str | None = None  # YYYY-MM-DD
    files: list[ManualFileIn] = Field(default_factory=list, max_length=MAX_FILES)


class NewVersionIn(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS + 50)  # None: keep
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS + 1000)  # None: keep
    posted_on: str | None = None  # None: keep; "": clear
    keep_files: list[str] = Field(default_factory=list, max_length=500)  # current files to keep
    files: list[ManualFileIn] = Field(default_factory=list, max_length=MAX_FILES)


class ManualResultOut(BaseModel):
    document_id: int
    change: str  # new | updated | resaved | details | removed | reappeared
    version_no: int
    warnings: list[str]

    @classmethod
    def of(cls, r: ManualResult) -> "ManualResultOut":
        return cls(
            document_id=r.document_id, change=r.change, version_no=r.version_no, warnings=list(r.warnings)
        )


def _files(files: list[ManualFileIn]) -> list[ManualFile]:
    return [ManualFile(f.filename, f.content_base64) for f in files]


def _checked(alerts: AlertService, source_id: int, result: ManualResult) -> ManualResultOut:
    """After a save, look at the source's health so admins are mailed if it changed (a flagged
    file, or a flag cleared), as after a manual scan. A mail problem never fails the upload."""
    try:
        alerts.evaluate_source(source_id)
    except Exception:
        log.exception("alarm check after a manual upload to source %s failed", source_id)
    return ManualResultOut.of(result)


@router.post("/sources/manual", status_code=201, response_model=SourceOut)
def create_manual_source(
    body: ManualSourceIn,
    admin: AuthUser = Depends(require_admin),
    service: SourceService = Depends(get_source_service),
) -> SourceOut:
    """A source an admin fills by hand. It is never scanned."""
    return SourceOut.from_source(
        service.create_manual(
            admin.id, body.name, body.members_only, body.ai_enabled, body.hospital_id, body.board_kind
        )
    )


@router.post("/sources/{source_id}/manual-documents", status_code=201, response_model=ManualResultOut)
def create_document(
    source_id: int,
    body: NewDocumentIn,
    admin: AuthUser = Depends(require_admin),
    service: ManualService = Depends(get_manual_service),
    alerts: AlertService = Depends(get_alert_service),
) -> ManualResultOut:
    """A new document with its first version (saved like a scan: card, AI summary if on).
    409 when an identical document (same title, note and files) already exists."""
    result = service.create_document(
        admin.id, source_id, body.title, body.note, body.posted_on, _files(body.files)
    )
    return _checked(alerts, source_id, result)


@router.post(
    "/sources/{source_id}/manual-documents/{document_id}/versions",
    status_code=201,
    response_model=ManualResultOut,
)
def add_version(
    source_id: int,
    document_id: int,
    body: NewVersionIn,
    admin: AuthUser = Depends(require_admin),
    service: ManualService = Depends(get_manual_service),
    alerts: AlertService = Depends(get_alert_service),
) -> ManualResultOut:
    """The next version: keep some current files, add or replace others, change the title,
    note or date. 409 when nothing changed."""
    result = service.add_version(
        admin.id,
        source_id,
        document_id,
        body.title,
        body.note,
        body.posted_on,
        body.keep_files,
        _files(body.files),
    )
    return _checked(alerts, source_id, result)


@router.post("/sources/{source_id}/manual-documents/{document_id}/remove", response_model=ManualResultOut)
def remove_document(
    source_id: int,
    document_id: int,
    admin: AuthUser = Depends(require_admin),
    service: ManualService = Depends(get_manual_service),
    alerts: AlertService = Depends(get_alert_service),
) -> ManualResultOut:
    """Mark removed (a 'removed' card). History and files stay."""
    return _checked(alerts, source_id, service.set_removed(admin.id, source_id, document_id, True))


@router.post("/sources/{source_id}/manual-documents/{document_id}/restore", response_model=ManualResultOut)
def restore_document(
    source_id: int,
    document_id: int,
    admin: AuthUser = Depends(require_admin),
    service: ManualService = Depends(get_manual_service),
    alerts: AlertService = Depends(get_alert_service),
) -> ManualResultOut:
    return _checked(alerts, source_id, service.set_removed(admin.id, source_id, document_id, False))
