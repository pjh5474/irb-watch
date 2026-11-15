"""IRB review sessions of hospitals (CAL-1). Platform-wide: every signed-in user reads them
(except rejected and removed ones); users of a hospital propose; platform admins enter,
review and remove them, and start the AI search that writes proposals."""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import (
    current_user,
    get_project_access_service,
    get_session_service,
    require_admin,
)
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.project_access import ProjectAccessService
from irb_watch.services.sessions import (
    MAX_LABEL,
    MAX_NOTE,
    MAX_URL,
    Session,
    SessionJob,
    SessionKind,
    SessionService,
)

router = APIRouter(tags=["sessions"])
admin_router = APIRouter(prefix="/admin", tags=["admin-sessions"])
Service = SessionService


class KindOut(BaseModel):
    key: str
    label: str
    position: int
    archived_at: str | None

    @classmethod
    def of(cls, k: SessionKind) -> "KindOut":
        return cls(key=k.key, label=k.label, position=k.position, archived_at=k.archived_at)


class SessionOut(BaseModel):
    id: int
    hospital_id: int
    kind_key: str
    meets_on: str
    meets_at: str | None  # null: time unknown
    cutoff_on: str | None
    cutoff_at: str | None
    timezone: str
    state: str  # proposed | tentative | confirmed | cancelled | rejected
    origin: str  # admin | user | ai
    note: str
    evidence_version_id: int | None
    evidence_url: str | None
    evidence_quote: str | None  # untrusted text from a post: show as plain text
    proposed_by: str | None  # a name
    proposed_by_me: bool
    proposed_at: str | None
    reviewed_by: str | None  # a name
    reviewed_at: str | None
    job_id: str | None
    deleted_at: str | None

    @classmethod
    def of(
        cls, s: Session, user_id: str | None = None, names: dict[str, str] | None = None, *, evidence: bool = False
    ) -> "SessionOut":
        """evidence: include the AI's quote and the stored post (platform admins only: the post
        may be on a members-only board, and every user reads sessions)."""
        names = names or {}
        return cls(
            id=s.id, hospital_id=s.hospital_id, kind_key=s.kind_key, meets_on=s.meets_on, meets_at=s.meets_at,
            cutoff_on=s.cutoff_on, cutoff_at=s.cutoff_at, timezone=s.timezone, state=s.state, origin=s.origin,
            note=s.note, evidence_version_id=s.evidence_version_id if evidence else None, evidence_url=s.evidence_url,
            evidence_quote=s.evidence_quote if evidence else None,
            proposed_by=names.get(s.proposed_by) if s.proposed_by else None,
            proposed_by_me=user_id is not None and s.proposed_by == user_id,
            proposed_at=s.proposed_at,
            reviewed_by=names.get(s.reviewed_by) if s.reviewed_by else None, reviewed_at=s.reviewed_at,
            job_id=s.job_id, deleted_at=s.deleted_at,
        )


def sessions_out(
    sessions: list[Session], user_id: str, access: ProjectAccessService, *, evidence: bool = False
) -> list[SessionOut]:
    people = [x for s in sessions for x in (s.proposed_by, s.reviewed_by) if x]
    names = access.names(people) if people else {}
    return [SessionOut.of(s, user_id, names, evidence=evidence) for s in sessions]


class JobOut(BaseModel):
    id: str
    hospital_id: int
    status: str
    error: str | None
    documents_read: int
    proposed: int
    skipped: int
    created_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def of(cls, j: SessionJob) -> "JobOut":
        return cls(
            id=j.id, hospital_id=j.hospital_id, status=j.status, error=j.error, documents_read=j.documents_read,
            proposed=j.proposed, skipped=j.skipped, created_at=j.created_at, started_at=j.started_at,
            finished_at=j.finished_at,
        )


class SessionIn(BaseModel):
    kind_key: str | None = Field(None, max_length=40)  # default: other
    meets_on: str = Field(max_length=10)
    meets_at: str | None = Field(None, max_length=8)
    cutoff_on: str | None = Field(None, max_length=10)
    cutoff_at: str | None = Field(None, max_length=8)
    timezone: str | None = Field(None, max_length=60)
    note: str = Field("", max_length=MAX_NOTE + 100)
    evidence_url: str | None = Field(None, max_length=MAX_URL + 50)


class AdminSessionIn(SessionIn):
    state: Literal["tentative", "confirmed"] = "tentative"
    evidence_version_id: int | None = None


class SessionPatch(BaseModel):
    kind_key: str | None = Field(None, max_length=40)
    meets_on: str | None = Field(None, max_length=10)
    meets_at: str | None = Field(None, max_length=8)
    cutoff_on: str | None = Field(None, max_length=10)
    cutoff_at: str | None = Field(None, max_length=8)
    timezone: str | None = Field(None, max_length=60)
    note: str | None = Field(None, max_length=MAX_NOTE + 100)
    evidence_url: str | None = Field(None, max_length=MAX_URL + 50)
    evidence_version_id: int | None = None
    state: Literal["proposed", "tentative", "confirmed", "cancelled", "rejected"] | None = None


class KindIn(BaseModel):
    key: str = Field(max_length=40)
    label: str = Field(max_length=MAX_LABEL + 20)


class KindPatch(BaseModel):
    label: str | None = Field(None, max_length=MAX_LABEL + 20)
    position: int | None = None
    archived: bool | None = None


def _given(body: BaseModel) -> dict:
    return {k: getattr(body, k) for k in body.model_fields_set}


# -- everyone ----------------------------------------------------------------------------------


@router.get("/session-kinds", response_model=list[KindOut])
def kinds(include_archived: bool = False, _: AuthUser = Depends(current_user), service: Service = Depends(get_session_service)):
    return [KindOut.of(k) for k in service.kinds(include_archived)]


@router.get("/hospitals/{hospital_id}/sessions", response_model=list[SessionOut])
def hospital_sessions(
    hospital_id: int,
    start: str | None = None,
    end: str | None = None,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_session_service),
    access: ProjectAccessService = Depends(get_project_access_service),
):
    """The hospital's sessions (default: 30 days back to 400 ahead). Proposals are included and
    marked by their state; rejected and removed ones are left out (admins also get rejected, and
    the AI's quote with its stored post)."""
    rows = service.for_hospital(user.id, hospital_id, start, end)
    return sessions_out(rows, user.id, access, evidence=service.sees_evidence(user.id))


@router.post("/hospitals/{hospital_id}/sessions", status_code=201, response_model=SessionOut)
def propose(
    hospital_id: int, body: SessionIn, user: AuthUser = Depends(current_user), service: Service = Depends(get_session_service)
):
    """A proposal (state proposed) by a user who works on a project with this hospital. An admin
    reviews it."""
    return SessionOut.of(service.propose(user.id, hospital_id, _given(body)), user.id)


@router.post("/sessions/{session_id}/withdraw", response_model=SessionOut)
def withdraw(session_id: int, user: AuthUser = Depends(current_user), service: Service = Depends(get_session_service)):
    """The proposer takes back an unreviewed proposal."""
    return SessionOut.of(service.withdraw(user.id, session_id), user.id)


# -- platform admins ---------------------------------------------------------------------------


@admin_router.get("/sessions/proposals", response_model=list[SessionOut])
def proposals(
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_session_service),
    access: ProjectAccessService = Depends(get_project_access_service),
):
    return sessions_out(service.proposals(), admin.id, access, evidence=True)


@admin_router.post("/hospitals/{hospital_id}/sessions", status_code=201, response_model=SessionOut)
def admin_create(
    hospital_id: int, body: AdminSessionIn, admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_session_service),
):
    data = _given(body)
    state = data.pop("state", "tentative")
    return SessionOut.of(service.create(admin.id, hospital_id, data, state), admin.id, evidence=True)


@admin_router.patch("/sessions/{session_id}", response_model=SessionOut)
def admin_update(
    session_id: int, body: SessionPatch, admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_session_service),
):
    """Fields and the state: approve a proposal (tentative or confirmed), reject it, cancel a
    session or undo a cancel."""
    return SessionOut.of(service.update(admin.id, session_id, _given(body)), admin.id, evidence=True)


@admin_router.post("/sessions/{session_id}/remove", response_model=SessionOut)
def admin_remove(session_id: int, admin: AuthUser = Depends(require_admin), service: Service = Depends(get_session_service)):
    return SessionOut.of(service.remove(admin.id, session_id), admin.id, evidence=True)


@admin_router.post("/hospitals/{hospital_id}/sessions/find", status_code=202, response_model=JobOut)
def find(hospital_id: int, admin: AuthUser = Depends(require_admin), service: Service = Depends(get_session_service)):
    """Start the AI search over the hospital's stored posts (boards with AI on). It writes
    proposals only; poll GET /admin/session-jobs/{id}."""
    return JobOut.of(service.start_find(admin.id, hospital_id))


@admin_router.get("/session-jobs", response_model=list[JobOut])
def jobs(hospital_id: int | None = None, _: AuthUser = Depends(require_admin), service: Service = Depends(get_session_service)):
    return [JobOut.of(j) for j in service.jobs(hospital_id)]


@admin_router.get("/session-jobs/{job_id}", response_model=JobOut)
def job(job_id: str, _: AuthUser = Depends(require_admin), service: Service = Depends(get_session_service)):
    return JobOut.of(service.job(job_id))


@admin_router.get("/session-finder", response_model=dict)
def finder(_: AuthUser = Depends(require_admin), service: Service = Depends(get_session_service)):
    """Whether the AI search is set up on this server (the page hides or explains the button)."""
    return {"enabled": service.finder_enabled}


@admin_router.post("/session-kinds", status_code=201, response_model=KindOut)
def add_kind(body: KindIn, admin: AuthUser = Depends(require_admin), service: Service = Depends(get_session_service)):
    return KindOut.of(service.add_kind(admin.id, body.key, body.label))


@admin_router.patch("/session-kinds/{key}", response_model=KindOut)
def update_kind(key: str, body: KindPatch, admin: AuthUser = Depends(require_admin), service: Service = Depends(get_session_service)):
    return KindOut.of(service.update_kind(admin.id, key, _given(body)))
