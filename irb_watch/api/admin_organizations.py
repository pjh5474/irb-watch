"""Admin routes for organizations and their monthly statistics (platform admins only)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_organization_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.core import org_stats as s
from irb_watch.services.organizations import (
    MAX_NAME_CHARS,
    Member,
    Organization,
    OrganizationService,
)

router = APIRouter(prefix="/admin/organizations", tags=["admin-organizations"])

MONTH = Query(None, pattern=r"^\d{4}-\d{2}$", description="YYYY-MM; default: this month")


class OrganizationOut(BaseModel):
    id: str
    name: str
    created_at: str
    deleted_at: str | None
    member_count: int
    is_default: bool

    @classmethod
    def of(cls, o: Organization) -> "OrganizationOut":
        return cls(
            id=o.id,
            name=o.name,
            created_at=o.created_at,
            deleted_at=o.deleted_at,
            member_count=o.member_count,
            is_default=o.is_default,
        )


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)


class MemberOut(BaseModel):
    user_id: str
    email: str | None
    display_name: str | None
    role: str
    created_at: str
    deleted_at: str | None

    @classmethod
    def of(cls, m: Member) -> "MemberOut":
        return cls(**m.__dict__)


class Counts(BaseModel):
    chat_messages: int
    summaries: int
    drafts: int
    revisions: int
    exports: int
    downloads: int


class OrgTotalsOut(BaseModel):
    organization_id: str
    name: str
    deleted: bool
    members: int
    new_accounts: int
    signed_in: int
    counts: Counts

    @classmethod
    def of(cls, t: s.OrgTotals) -> "OrgTotalsOut":
        return cls(
            organization_id=t.organization.id,
            name=t.organization.name,
            deleted=t.organization.deleted,
            members=t.members,
            new_accounts=t.new_accounts,
            signed_in=t.signed_in,
            counts=Counts(**t.counts),
        )


class MonthStatsOut(BaseModel):
    month: str
    organizations: list[OrgTotalsOut]


class UserTotalsOut(BaseModel):
    user_id: str
    email: str | None
    member_now: bool
    last_sign_in_at: datetime | None
    counts: Counts


class OrgStatsOut(BaseModel):
    month: str
    organization: OrganizationOut
    totals: OrgTotalsOut
    users: list[UserTotalsOut]


@router.get("", response_model=list[OrganizationOut])
def list_organizations(
    include_deleted: bool = False,
    _: AuthUser = Depends(require_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> list[OrganizationOut]:
    return [OrganizationOut.of(o) for o in service.list_organizations(include_deleted)]


@router.post("", status_code=201, response_model=OrganizationOut)
def create_organization(
    body: NameIn,
    admin: AuthUser = Depends(require_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationOut:
    return OrganizationOut.of(service.create(admin.id, body.name))


# Before "/{organization_id}" so "stats" is never read as an id.
@router.get("/stats", response_model=MonthStatsOut)
def month_stats(
    month: str | None = MONTH,
    _: AuthUser = Depends(require_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> MonthStatsOut:
    stats = service.month_stats(month)
    return MonthStatsOut(
        month=stats.month, organizations=[OrgTotalsOut.of(t) for t in stats.organizations]
    )


@router.patch("/{organization_id}", response_model=OrganizationOut)
def rename_organization(
    organization_id: str,
    body: NameIn,
    admin: AuthUser = Depends(require_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationOut:
    return OrganizationOut.of(service.rename(admin.id, organization_id, body.name))


@router.delete("/{organization_id}", response_model=OrganizationOut)
def delete_organization(
    organization_id: str,
    admin: AuthUser = Depends(require_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationOut:
    """Soft delete; refused for Default and while it has users."""
    return OrganizationOut.of(service.delete(admin.id, organization_id))


@router.post("/{organization_id}/restore", response_model=OrganizationOut)
def restore_organization(
    organization_id: str,
    admin: AuthUser = Depends(require_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationOut:
    return OrganizationOut.of(service.restore(admin.id, organization_id))


@router.get("/{organization_id}/members", response_model=list[MemberOut])
def organization_members(
    organization_id: str,
    _: AuthUser = Depends(require_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> list[MemberOut]:
    return [MemberOut.of(m) for m in service.members(organization_id)]


@router.get("/{organization_id}/stats", response_model=OrgStatsOut)
def organization_stats(
    organization_id: str,
    month: str | None = MONTH,
    _: AuthUser = Depends(require_admin),
    service: OrganizationService = Depends(get_organization_service),
) -> OrgStatsOut:
    stats = service.organization_stats(organization_id, month)
    return OrgStatsOut(
        month=stats.month,
        organization=OrganizationOut.of(stats.organization),
        totals=OrgTotalsOut.of(stats.totals),
        users=[
            UserTotalsOut(
                user_id=u.user_id,
                email=u.email,
                member_now=u.member_now,
                last_sign_in_at=u.last_sign_in_at,
                counts=Counts(**u.counts),
            )
            for u in stats.users
        ],
    )
