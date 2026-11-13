"""Sites per PI and their dated details (IRB pipeline redesign, phase B2a). Whoever may open
the project reads them; whoever may edit it changes them (phase B1)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import current_user, get_project_access_service, get_site_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.project_access import ProjectAccessService
from irb_watch.services.sites import (
    MAX_FACT_NAME,
    MAX_FACT_VALUE,
    MAX_NOTE,
    MAX_PI,
    MAX_SITE_NO,
    SUGGESTED_FACTS,
    Fact,
    FactView,
    Site,
    SiteService,
)

router = APIRouter(tags=["sites"])
Service = SiteService


class SiteOut(BaseModel):
    id: int
    project_id: int
    hospital_id: int
    site_no: str
    pi_name: str
    note: str
    created_at: str
    updated_at: str
    deleted_at: str | None

    @classmethod
    def of(cls, s: Site) -> "SiteOut":
        return cls(
            id=s.id, project_id=s.project_id, hospital_id=s.hospital_id, site_no=s.site_no, pi_name=s.pi_name,
            note=s.note, created_at=s.created_at, updated_at=s.updated_at, deleted_at=s.deleted_at,
        )


class FactOut(BaseModel):
    id: int
    value: str
    valid_from: str
    step_id: int | None
    corrects: int | None  # this entry fixes that one
    corrected: bool  # another entry replaces this one
    changed_by: str | None  # a name
    created_at: str

    @classmethod
    def of(cls, f: Fact, corrected: frozenset[int], names: dict[str, str]) -> "FactOut":
        return cls(
            id=f.id, value=f.value, valid_from=f.valid_from, step_id=f.step_id, corrects=f.corrects,
            corrected=f.id in corrected, changed_by=names.get(f.changed_by or "") if f.changed_by else None,
            created_at=f.created_at,
        )


class FactViewOut(BaseModel):
    name: str
    current: FactOut | None
    upcoming: list[FactOut]
    history: list[FactOut]

    @classmethod
    def of(cls, v: FactView, names: dict[str, str]) -> "FactViewOut":
        def out(f: Fact) -> FactOut:
            return FactOut.of(f, v.corrected, names)

        return cls(
            name=v.name, current=out(v.current) if v.current else None,
            upcoming=[out(f) for f in v.upcoming], history=[out(f) for f in v.history],
        )


class FactsOut(BaseModel):
    site: SiteOut
    facts: list[FactViewOut]
    suggested: list[str]  # names offered for a new detail


class NewSiteIn(BaseModel):
    hospital_id: int
    pi_name: str = Field("", max_length=MAX_PI + 50)
    site_no: str = Field("", max_length=MAX_SITE_NO + 20)
    note: str = Field("", max_length=MAX_NOTE + 100)


class SitePatch(BaseModel):
    site_no: str | None = Field(None, max_length=MAX_SITE_NO + 20)
    note: str | None = Field(None, max_length=MAX_NOTE + 100)


class PiIn(BaseModel):
    pi_name: str = Field(max_length=MAX_PI + 50)
    valid_from: str | None = Field(None, max_length=10)  # default: today
    step_id: int | None = None  # the step that reported the change


class FactIn(BaseModel):
    name: str = Field(max_length=MAX_FACT_NAME + 50)
    value: str = Field("", max_length=MAX_FACT_VALUE + 100)  # empty: the detail is removed
    valid_from: str | None = Field(None, max_length=10)
    step_id: int | None = None
    corrects: int | None = None  # fixes that entry (a typo) instead of a new value


@router.get("/projects/{project_id}/sites", response_model=list[SiteOut])
def list_sites(
    project_id: int,
    include_removed: bool = False,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_site_service),
):
    return [SiteOut.of(s) for s in service.list_sites(user.id, project_id, include_removed)]


@router.post("/projects/{project_id}/sites", status_code=201, response_model=SiteOut)
def create_site(
    project_id: int, body: NewSiteIn, user: AuthUser = Depends(current_user), service: Service = Depends(get_site_service)
):
    return SiteOut.of(service.create(user.id, project_id, body.hospital_id, body.pi_name, body.site_no, body.note))


@router.patch("/sites/{site_id}", response_model=SiteOut)
def update_site(site_id: int, body: SitePatch, user: AuthUser = Depends(current_user), service: Service = Depends(get_site_service)):
    return SiteOut.of(service.update(user.id, site_id, {k: getattr(body, k) for k in body.model_fields_set}))


@router.put("/sites/{site_id}/pi", response_model=SiteOut)
def change_pi(site_id: int, body: PiIn, user: AuthUser = Depends(current_user), service: Service = Depends(get_site_service)):
    """A PI change keeps the site, its pipeline and files; the history shows it."""
    return SiteOut.of(service.change_pi(user.id, site_id, body.pi_name, body.valid_from, body.step_id))


@router.post("/sites/{site_id}/remove", response_model=SiteOut)
def remove_site(site_id: int, user: AuthUser = Depends(current_user), service: Service = Depends(get_site_service)):
    """Refused while the site has a live pipeline. Purged after 30 days."""
    return SiteOut.of(service.remove(user.id, site_id))


@router.post("/sites/{site_id}/restore", response_model=SiteOut)
def restore_site(site_id: int, user: AuthUser = Depends(current_user), service: Service = Depends(get_site_service)):
    return SiteOut.of(service.restore(user.id, site_id))


@router.get("/sites/{site_id}/facts", response_model=FactsOut)
def facts(
    site_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_site_service),
    access: ProjectAccessService = Depends(get_project_access_service),
):
    """The site's details: each one's current value (dated today or earlier), upcoming values
    and its whole history (who, when, which step, corrections)."""
    site, views = service.facts(user.id, site_id)  # first: access check
    names = access.names([f.changed_by for v in views for f in v.history if f.changed_by])
    return FactsOut(site=SiteOut.of(site), facts=[FactViewOut.of(v, names) for v in views], suggested=list(SUGGESTED_FACTS))


@router.post("/sites/{site_id}/facts", status_code=201, response_model=FactOut)
def add_fact(site_id: int, body: FactIn, user: AuthUser = Depends(current_user), service: Service = Depends(get_site_service)):
    fact = service.add_fact(user.id, site_id, body.name, body.value, body.valid_from, body.step_id, body.corrects)
    return FactOut.of(fact, frozenset(), {})
