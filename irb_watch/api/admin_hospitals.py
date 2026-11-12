"""Admin: the hospital catalog (create, rename) with each hospital's boards.

Moving a board to another hospital is PATCH /admin/sources/{id} with hospital_id. Users read
the catalog directly from Supabase (table hospitals, readable by every signed-in user).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_hospital_service, get_source_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.hospitals import (
    BOARD_KINDS,
    MAX_NAME_CHARS,
    MAX_NOTE_CHARS,
    Hospital,
    HospitalService,
)
from irb_watch.services.sources import Source, SourceService

router = APIRouter(prefix="/admin/hospitals", tags=["admin-hospitals"])


class BoardOut(BaseModel):
    id: int
    name: str
    adapter: str
    board_kind: str
    is_active: bool
    requires_login: bool

    @classmethod
    def of(cls, s: Source) -> "BoardOut":
        return cls(
            id=s.id, name=s.name, adapter=s.adapter, board_kind=s.board_kind,
            is_active=s.is_active, requires_login=s.requires_login,
        )


class HospitalOut(BaseModel):
    id: int
    name: str
    note: str
    created_at: str
    updated_at: str
    boards: list[BoardOut] = Field(default_factory=list)

    @classmethod
    def of(cls, h: Hospital, boards: list[Source]) -> "HospitalOut":
        return cls(
            id=h.id, name=h.name, note=h.note, created_at=h.created_at, updated_at=h.updated_at,
            boards=[BoardOut.of(s) for s in sorted(boards, key=lambda s: s.id)],
        )


class HospitalListOut(BaseModel):
    hospitals: list[HospitalOut]
    board_kinds: list[str]


class CreateHospitalIn(BaseModel):
    name: str = Field(max_length=MAX_NAME_CHARS + 50)
    note: str = Field(default="", max_length=MAX_NOTE_CHARS + 100)


class UpdateHospitalIn(BaseModel):
    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS + 50)
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS + 100)


def _boards_of(sources: SourceService, hospital_id: int) -> list[Source]:
    return [s for s in sources.list() if s.hospital_id == hospital_id]


@router.get("", response_model=HospitalListOut)
def list_hospitals(
    _: AuthUser = Depends(require_admin),
    service: HospitalService = Depends(get_hospital_service),
    sources: SourceService = Depends(get_source_service),
) -> HospitalListOut:
    boards: dict[int, list[Source]] = {}
    for s in sources.list():
        if s.hospital_id is not None:
            boards.setdefault(s.hospital_id, []).append(s)
    return HospitalListOut(
        hospitals=[HospitalOut.of(h, boards.get(h.id, [])) for h in service.list()],
        board_kinds=list(BOARD_KINDS),
    )


@router.post("", status_code=201, response_model=HospitalOut)
def create_hospital(
    body: CreateHospitalIn,
    admin: AuthUser = Depends(require_admin),
    service: HospitalService = Depends(get_hospital_service),
) -> HospitalOut:
    return HospitalOut.of(service.create(admin.id, body.name, body.note), [])


@router.patch("/{hospital_id}", response_model=HospitalOut)
def update_hospital(
    hospital_id: int,
    body: UpdateHospitalIn,
    admin: AuthUser = Depends(require_admin),
    service: HospitalService = Depends(get_hospital_service),
    sources: SourceService = Depends(get_source_service),
) -> HospitalOut:
    updated = service.update(admin.id, hospital_id, body.name, body.note)
    return HospitalOut.of(updated, _boards_of(sources, hospital_id))
