"""Admin settings of the assistant: the daily message limits."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import get_agent_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.agent import AgentService, Limits

router = APIRouter(prefix="/admin/agent", tags=["admin-agent"])


class LimitsIO(BaseModel):
    user_daily_limit: int
    admin_daily_limit: int

    @classmethod
    def of(cls, limits: Limits) -> "LimitsIO":
        return cls(
            user_daily_limit=limits.user_daily_limit, admin_daily_limit=limits.admin_daily_limit
        )


@router.get("/settings", response_model=LimitsIO)
def get_settings(
    _: AuthUser = Depends(require_admin), service: AgentService = Depends(get_agent_service)
) -> LimitsIO:
    return LimitsIO.of(service.get_limits())


@router.put("/settings", response_model=LimitsIO)
def put_settings(
    body: LimitsIO,
    admin: AuthUser = Depends(require_admin),
    service: AgentService = Depends(get_agent_service),
) -> LimitsIO:
    """Messages per rolling 24 hours for each account type. 0 switches the assistant off for
    that type. Logged first; if the log cannot be written nothing changes."""
    return LimitsIO.of(
        service.set_limits(admin.id, body.user_daily_limit, body.admin_daily_limit)
    )
