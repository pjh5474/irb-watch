from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, SecretStr

from irb_watch.api.deps import get_organization_service, get_user_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.organizations import (
    DEFAULT_ORGANIZATION_ID,
    Organization,
    OrganizationService,
)
from irb_watch.services.users import Profile, UserService

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class UserOut(BaseModel):
    id: str
    email: str | None
    display_name: str | None
    role: str
    created_at: str
    deleted_at: str | None
    organization_id: str | None = None
    organization_name: str | None = None

    @classmethod
    def from_profile(cls, p: Profile, org: Organization | None = None) -> "UserOut":
        return cls(
            **p.__dict__,
            organization_id=org.id if org else None,
            organization_name=org.name if org else None,
        )


class UserList(BaseModel):
    items: list[UserOut]
    total: int
    limit: int
    offset: int


class CreateUserIn(BaseModel):
    email: EmailStr
    # SecretStr keeps the password out of validation-error echoes and logs.
    password: SecretStr
    display_name: str | None = None
    role: Literal["user", "admin"] = "user"
    # None: the Default organization.
    organization_id: str | None = None


class RoleIn(BaseModel):
    role: Literal["user", "admin"]


class OrganizationIn(BaseModel):
    organization_id: str


def _with_org(profile: Profile, orgs: OrganizationService) -> UserOut:
    return UserOut.from_profile(profile, orgs.organization_names([profile.id]).get(profile.id))


@router.post("", status_code=201, response_model=UserOut)
def create_user(
    body: CreateUserIn,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
    orgs: OrganizationService = Depends(get_organization_service),
) -> UserOut:
    org_id = body.organization_id or DEFAULT_ORGANIZATION_ID
    orgs.require_live(org_id)  # checked before the account exists
    profile = service.create_user(
        admin.id, body.email, body.password.get_secret_value(), body.display_name, body.role
    )
    orgs.place_new_user(admin.id, profile.id, org_id)
    return _with_org(profile, orgs)


@router.get("", response_model=UserList)
def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_deleted: bool = False,
    _: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
    orgs: OrganizationService = Depends(get_organization_service),
) -> UserList:
    profiles, total = service.list_users(limit, offset, include_deleted)
    names = orgs.organization_names([p.id for p in profiles])
    return UserList(
        items=[UserOut.from_profile(p, names.get(p.id)) for p in profiles],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.put("/{user_id}/organization", response_model=UserOut)
def move_user(
    user_id: str,
    body: OrganizationIn,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
    orgs: OrganizationService = Depends(get_organization_service),
) -> UserOut:
    """Moves the user to another organization; their usage counts there from now on."""
    org = orgs.move_user(admin.id, user_id, body.organization_id)
    return UserOut.from_profile(service.get_user(user_id), org)


@router.patch("/{user_id}/role", response_model=UserOut)
def change_role(
    user_id: str,
    body: RoleIn,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
    orgs: OrganizationService = Depends(get_organization_service),
) -> UserOut:
    return _with_org(service.change_role(admin.id, user_id, body.role), orgs)


@router.delete("/{user_id}", response_model=UserOut)
def delete_user(
    user_id: str,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
    orgs: OrganizationService = Depends(get_organization_service),
) -> UserOut:
    """Soft delete: sets deleted_at and blocks the user's login. Reversible."""
    return _with_org(service.delete_user(admin.id, user_id), orgs)


@router.post("/{user_id}/restore", response_model=UserOut)
def restore_user(
    user_id: str,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
    orgs: OrganizationService = Depends(get_organization_service),
) -> UserOut:
    return _with_org(service.restore_user(admin.id, user_id), orgs)
