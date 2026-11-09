from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, SecretStr

from irb_watch.api.deps import current_user, get_access_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.access import AccessService

router = APIRouter(prefix="/sources", tags=["access"])


class AccessIn(BaseModel):
    login_id: str = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=200)


class AccessOut(BaseModel):
    expires_at: str


@router.post("/{source_id}/access", response_model=AccessOut)
def prove_access(
    source_id: int,
    body: AccessIn,
    user: AuthUser = Depends(current_user),
    service: AccessService = Depends(get_access_service),
) -> AccessOut:
    """Prove with your OWN hospital login that you may read a members-only source.

    The password is used once, in memory, and never stored or logged. Access then lasts
    30 days.
    """
    expires = service.prove_access(
        user.id, source_id, body.login_id, body.password.get_secret_value()
    )
    return AccessOut(expires_at=expires.isoformat())
