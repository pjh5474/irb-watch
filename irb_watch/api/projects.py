"""Sharing a project (phase B1): its members, PM and owner; projects shared with me; an
organization's projects. The project itself (name, note, hospitals) is still read and written
by the browser under RLS."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import current_user, get_project_access_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.project_access import (
    MANAGE,
    MEMBERS,
    OrgProject,
    Person,
    ProjectAccessService,
    SharedProject,
)

router = APIRouter(tags=["projects"])
Service = ProjectAccessService


class PersonOut(BaseModel):
    id: str
    name: str

    @classmethod
    def of(cls, p: Person | None) -> "PersonOut | None":
        return cls(id=p.id, name=p.name) if p else None


class MemberOut(BaseModel):
    user: PersonOut
    role: str  # pm | member
    in_organization: bool  # False: moved to another organization; the row grants nothing


class SharingOut(BaseModel):
    project_id: int
    access: str  # owner | manage | pm | edit | read
    owner: PersonOut | None
    members: list[MemberOut]
    can_manage_members: bool  # add / remove members
    can_manage: bool  # set the PM, hand over
    is_member: bool  # may leave


class UserIn(BaseModel):
    user_id: str


class PmIn(BaseModel):
    user_id: str | None = None  # None: no PM


class HandOverIn(BaseModel):
    user_id: str
    keep_as_member: bool = True  # the old owner stays as a member


class SharedOut(BaseModel):
    project_id: int
    name: str
    owner: PersonOut | None
    role: str

    @classmethod
    def of(cls, s: SharedProject) -> "SharedOut":
        return cls(project_id=s.project.id, name=s.project.name, owner=PersonOut.of(s.owner), role=s.role)


class OrgProjectOut(BaseModel):
    project_id: int
    name: str
    owner: PersonOut | None
    pm: PersonOut | None
    members: int

    @classmethod
    def of(cls, o: OrgProject) -> "OrgProjectOut":
        return cls(
            project_id=o.project.id, name=o.project.name, owner=PersonOut.of(o.owner), pm=PersonOut.of(o.pm),
            members=o.members,
        )


def _sharing(service: Service, user_id: str, project_id: int) -> SharingOut:
    s = service.sharing(user_id, project_id)
    return SharingOut(
        project_id=project_id,
        access=s.level,
        owner=PersonOut.of(s.owner),
        members=[MemberOut(user=PersonOut.of(m.person), role=m.role, in_organization=m.in_organization) for m in s.members],  # type: ignore[arg-type]
        can_manage_members=s.level in MEMBERS,
        can_manage=s.level in MANAGE,
        is_member=any(m.person.id == user_id for m in s.members),
    )


@router.get("/projects/shared", response_model=list[SharedOut])
def shared_with_me(user: AuthUser = Depends(current_user), service: Service = Depends(get_project_access_service)):
    """Projects other users shared with me (as PM or member), with their owner's name."""
    return [SharedOut.of(s) for s in service.shared_with(user.id)]


@router.get("/projects/{project_id}/sharing", response_model=SharingOut)
def sharing(project_id: int, user: AuthUser = Depends(current_user), service: Service = Depends(get_project_access_service)):
    """The project's owner, PM and members, and what the caller may change."""
    return _sharing(service, user.id, project_id)


@router.get("/projects/{project_id}/candidates", response_model=list[PersonOut])
def candidates(project_id: int, user: AuthUser = Depends(current_user), service: Service = Depends(get_project_access_service)):
    """Active users of the project's organization not in it yet."""
    return [PersonOut.of(p) for p in service.candidates(user.id, project_id)]


@router.post("/projects/{project_id}/members", response_model=SharingOut)
def add_member(
    project_id: int, body: UserIn, user: AuthUser = Depends(current_user), service: Service = Depends(get_project_access_service)
):
    service.add_member(user.id, project_id, body.user_id)
    return _sharing(service, user.id, project_id)


@router.delete("/projects/{project_id}/members/{member_id}", response_model=SharingOut)
def remove_member(
    project_id: int, member_id: str, user: AuthUser = Depends(current_user), service: Service = Depends(get_project_access_service)
):
    service.remove_member(user.id, project_id, member_id)
    return _sharing(service, user.id, project_id)


@router.put("/projects/{project_id}/pm", response_model=SharingOut)
def set_pm(project_id: int, body: PmIn, user: AuthUser = Depends(current_user), service: Service = Depends(get_project_access_service)):
    service.set_pm(user.id, project_id, body.user_id)
    return _sharing(service, user.id, project_id)


@router.post("/projects/{project_id}/hand-over", response_model=SharingOut)
def hand_over(
    project_id: int, body: HandOverIn, user: AuthUser = Depends(current_user), service: Service = Depends(get_project_access_service)
):
    service.hand_over(user.id, project_id, body.user_id, body.keep_as_member)
    return _sharing(service, user.id, project_id)


@router.post("/projects/{project_id}/leave", status_code=204)
def leave(project_id: int, user: AuthUser = Depends(current_user), service: Service = Depends(get_project_access_service)) -> None:
    service.leave(user.id, project_id)


@router.get("/organization/projects", response_model=list[OrgProjectOut])
def organization_projects(
    organization_id: str | None = None,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_project_access_service),
):
    """Organization admins: their organization's projects. Platform admins: any organization
    (organization_id), read-only outside their own."""
    return [OrgProjectOut.of(o) for o in service.organization_projects(user.id, organization_id)]
