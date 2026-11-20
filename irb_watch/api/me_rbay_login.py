"""The signed-in user's own R-BAY login (docs/decisions.md "User's own R-BAY login").

The password is accepted once, encrypted, and never returned, logged or echoed back: answers
carry the status only. Saving and testing answer 202 with status 'testing'; the page polls GET.
Use over HTTPS only.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, SecretStr

from irb_watch.api.deps import current_user, get_user_login_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.user_logins import UserLogin, UserLoginService

router = APIRouter(prefix="/me/rbay-login", tags=["me"])


class RbayLoginIn(BaseModel):
    login_id: str = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=200)


class RbayLoginOut(BaseModel):
    has_login: bool
    login_hint: str | None  # e.g. "ab***"
    status: str  # untested, testing, ok, rejected, failed
    last_error: str | None
    last_tested_at: datetime | None
    last_ok_at: datetime | None


def _out(row: UserLogin) -> RbayLoginOut:
    return RbayLoginOut(
        has_login=row.has_login, login_hint=row.login_hint, status=row.status,
        last_error=row.last_error, last_tested_at=row.last_tested_at, last_ok_at=row.last_ok_at,
    )


@router.get("", response_model=RbayLoginOut)
def get_login(
    user: AuthUser = Depends(current_user),
    service: UserLoginService = Depends(get_user_login_service),
) -> RbayLoginOut:
    """Whether a login is saved and how its last test went (never the login or password)."""
    return _out(service.status(user.id))


@router.put("", response_model=RbayLoginOut, status_code=202)
def save_login(
    body: RbayLoginIn,
    user: AuthUser = Depends(current_user),
    service: UserLoginService = Depends(get_user_login_service),
) -> RbayLoginOut:
    """Save (or replace) the login and test it once in the background. Poll GET for the result."""
    return _out(service.save(user.id, body.login_id, body.password.get_secret_value()))


@router.post("/test", response_model=RbayLoginOut, status_code=202)
def test_login(
    user: AuthUser = Depends(current_user),
    service: UserLoginService = Depends(get_user_login_service),
) -> RbayLoginOut:
    """Test the saved login again: one R-BAY login, at most 1 per 10 minutes and 10 a day."""
    return _out(service.test(user.id))


@router.delete("", status_code=204)
def delete_login(
    user: AuthUser = Depends(current_user),
    service: UserLoginService = Depends(get_user_login_service),
) -> None:
    service.delete(user.id)
