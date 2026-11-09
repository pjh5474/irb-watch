from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import get_member_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.members import Member, MemberService

router = APIRouter(prefix="/admin/sources/{source_id}/members", tags=["admin-members"])


class MemberOut(BaseModel):
    user_id: str
    email: str | None
    display_name: str | None
    added_at: str | None

    @classmethod
    def from_member(cls, m: Member) -> "MemberOut":
        return cls(
            user_id=m.user_id, email=m.email, display_name=m.display_name, added_at=m.added_at
        )


class AddMemberIn(BaseModel):
    email: str


@router.get("", response_model=list[MemberOut])
def list_members(
    source_id: int,
    _: AuthUser = Depends(require_admin),
    service: MemberService = Depends(get_member_service),
) -> list[MemberOut]:
    """Users an admin assigned to this members-only source."""
    return [MemberOut.from_member(m) for m in service.list(source_id)]


@router.put("", response_model=MemberOut)
def add_member(
    source_id: int,
    body: AddMemberIn,
    admin: AuthUser = Depends(require_admin),
    service: MemberService = Depends(get_member_service),
) -> MemberOut:
    """Let the user with this email read the source. Idempotent. 422 for a public source,
    404 for an unknown or deleted user. Logged first; if the log fails nothing changes."""
    return MemberOut.from_member(service.add(admin.id, source_id, body.email))


@router.delete("/{user_id}", status_code=204)
def remove_member(
    source_id: int,
    user_id: str,
    admin: AuthUser = Depends(require_admin),
    service: MemberService = Depends(get_member_service),
) -> None:
    service.remove(admin.id, source_id, user_id)
