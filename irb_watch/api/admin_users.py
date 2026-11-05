from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, SecretStr

from irb_watch.api.deps import get_user_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.users import Profile, UserService

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class UserOut(BaseModel):
    id: str
    email: str | None
    display_name: str | None
    role: str
    created_at: str
    deleted_at: str | None

    @classmethod
    def from_profile(cls, p: Profile) -> "UserOut":
        return cls(**p.__dict__)


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


class RoleIn(BaseModel):
    role: Literal["user", "admin"]


@router.post("", status_code=201, response_model=UserOut)
def create_user(
    body: CreateUserIn,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
) -> UserOut:
    profile = service.create_user(
        admin.id, body.email, body.password.get_secret_value(), body.display_name, body.role
    )
    return UserOut.from_profile(profile)


@router.get("", response_model=UserList)
def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_deleted: bool = False,
    _: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
) -> UserList:
    profiles, total = service.list_users(limit, offset, include_deleted)
    return UserList(
        items=[UserOut.from_profile(p) for p in profiles], total=total, limit=limit, offset=offset
    )


@router.patch("/{user_id}/role", response_model=UserOut)
def change_role(
    user_id: str,
    body: RoleIn,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
) -> UserOut:
    return UserOut.from_profile(service.change_role(admin.id, user_id, body.role))


@router.delete("/{user_id}", response_model=UserOut)
def delete_user(
    user_id: str,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
) -> UserOut:
    """Soft delete: sets deleted_at and blocks the user's login. Reversible."""
    return UserOut.from_profile(service.delete_user(admin.id, user_id))


@router.post("/{user_id}/restore", response_model=UserOut)
def restore_user(
    user_id: str,
    admin: AuthUser = Depends(require_admin),
    service: UserService = Depends(get_user_service),
) -> UserOut:
    return UserOut.from_profile(service.restore_user(admin.id, user_id))
