"""Sites per PI (IRB pipeline redesign, phase B2a): one IRB track of a project, its details
with a dated history.

Owner decisions (2026-11-13; docs/decisions.md "IRB pipeline redesign, phase B2a: sites per PI"):
  * A site is a hospital (the IRB it submits to), an optional PI name and an optional site
    number. A PI name once per hospital of a project (an empty one too: one unnamed site); a
    site number, when given, once per project. At most 100 live sites per project.
  * Other details are free facts (CRA, CRC, Sub-I, 증례당 연구비, ...) with a dated history, at
    most 40 live names per site. Rows are only added. A typo is fixed by a correction: a new row
    that names the row it replaces. The current value of a fact is the newest row dated today or
    earlier that no other row corrects; later rows are "upcoming". An empty value means the fact
    was removed.
  * "PI" is reserved: a PI change goes through change_pi, which writes pi_name and the PI fact in
    one database function, so the site keeps its pipeline and the history shows the change.
  * Whoever may open the project reads its sites; whoever may edit it changes them (phase B1).
  * A site with a live pipeline cannot be removed. Removed sites are purged after 30 days, once
    no pipeline row points at them.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.services.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from irb_watch.services.project_access import NEEDS, REFUSALS
from irb_watch.services.users import UserStore, active_profile

log = logging.getLogger(__name__)

MAX_SITES = 100
MAX_FACT_NAMES = 40
MAX_SITE_NO = 20
MAX_PI = 100
MAX_NOTE = 4000
MAX_FACT_NAME = 100
MAX_FACT_VALUE = 2000
PURGE_DAYS = 30
PI = "PI"
START_DATE = "1900-01-01"  # "from the start": the PI a site is created with
# Suggestions only (owner, O26: fixed in code); any name may be typed.
SUGGESTED_FACTS = ("PI 직위", "진료과", "Sub-I", "CRC", "CRA", "IRB 과제번호", "증례당 연구비")


@dataclass(frozen=True)
class Site:
    id: int
    project_id: int
    hospital_id: int
    user_id: str | None
    site_no: str
    pi_name: str
    note: str
    created_at: str
    updated_at: str
    deleted_at: str | None = None

    @property
    def label(self) -> str:
        """"01 · PI 김○○" / "PI 김○○" / "01" / "" (unnamed)."""
        parts = [p for p in (self.site_no.strip(), f"PI {self.pi_name.strip()}" if self.pi_name.strip() else "") if p]
        return " · ".join(parts)


@dataclass(frozen=True)
class Fact:
    id: int
    site_id: int
    name: str
    value: str
    valid_from: str  # YYYY-MM-DD
    step_id: int | None
    corrects: int | None
    changed_by: str | None
    created_at: str


@dataclass(frozen=True)
class FactView:
    name: str
    current: Fact | None  # None: not set yet, or only upcoming
    upcoming: list[Fact]  # dated after today, oldest first
    history: list[Fact]  # every row of the name, newest first (corrected rows included)
    corrected: frozenset[int]  # ids of rows another row replaces


class SiteStore(Protocol):
    def sites_of_project(self, project_id: int) -> list[Site]: ...
    def sites(self, site_ids: list[int]) -> list[Site]: ...
    def get_site(self, site_id: int) -> Site | None: ...
    def create_site(self, project_id: int, hospital_id: int, user_id: str, site_no: str, pi_name: str, note: str) -> Site:
        """LookupError when a unique index refuses (PI name or site number taken)."""
        ...
    def update_site(self, site_id: int, values: dict) -> Site:
        """LookupError when a unique index refuses."""
        ...
    def set_site_pi(self, site_id: int, name: str, valid_from: str, step_id: int | None, actor: str, apply: bool) -> None:
        """The PI fact and (apply) pi_name in one transaction. LookupError when the name is taken."""
        ...
    def apply_due_site_pi(self, today: str) -> tuple[int, int]:
        """Scheduled PI changes whose day has come: (changed, skipped on a name clash)."""
        ...
    def set_site_deleted(self, site_id: int, deleted_at: str | None) -> Site:
        """LookupError when restoring clashes with a live site."""
        ...
    def facts(self, site_ids: list[int]) -> list[Fact]: ...
    def add_fact(
        self, site_id: int, name: str, value: str, valid_from: str, step_id: int | None, corrects: int | None, actor: str
    ) -> Fact: ...
    def purge_candidates(self, cutoff: str) -> list[int]:
        """Sites removed before cutoff that no pipeline row points at."""
        ...
    def purge_sites(self, site_ids: list[int]) -> None: ...


class ProjectRules(Protocol):
    """services.pipelines.PipelineService: the project access rule, the project's hospitals and
    whether a site has a live pipeline."""

    def level(self, user_id: str, project_id: int) -> str | None: ...
    def project_hospitals(self, project_id: int) -> set[int]: ...
    def live_pipeline_of_site(self, site_id: int) -> int | None: ...
    def step_site(self, step_id: int) -> int | None: ...


def _key(name: str) -> str:
    return " ".join(name.split()).lower()


def fact_views(facts: list[Fact], today: date) -> list[FactView]:
    """Per fact name (case and spaces ignored): current value, upcoming values and history."""
    corrected = frozenset(f.corrects for f in facts if f.corrects is not None)
    by_name: dict[str, list[Fact]] = {}
    for f in facts:
        by_name.setdefault(_key(f.name), []).append(f)
    out = []
    for rows in by_name.values():
        # The PI too: its first entry is dated "from the start" (START_DATE), so a change dated
        # back still counts after it; a change dated later is upcoming until its day.
        rows = sorted(rows, key=lambda f: (f.valid_from, f.id), reverse=True)
        valid = [f for f in rows if f.id not in corrected]
        current = next((f for f in valid if date.fromisoformat(f.valid_from) <= today), None)
        upcoming = sorted((f for f in valid if date.fromisoformat(f.valid_from) > today), key=lambda f: (f.valid_from, f.id))
        out.append(FactView(rows[0].name, current, upcoming, rows, corrected))
    return sorted(out, key=lambda v: (v.name != PI, v.name.lower()))


def current_facts(facts: list[Fact], today: date) -> dict[str, str]:
    """name -> current non-empty value (removed facts and the PI fact left out): what a draft
    of the site is told (D18)."""
    return {
        v.name: v.current.value
        for v in fact_views(facts, today)
        if v.current is not None and v.current.value.strip() and _key(v.name) != PI.lower()
    }


def audit_site_details(names: list[str]) -> dict:
    """What the append-only audit log keeps of the site details sent: how many, and the names
    that are one of the suggestions. A typed name may hold a person's name ("CRA 홍길동"), and
    the log can never be cleaned, so any other name is only counted."""
    known = {_key(n) for n in (*SUGGESTED_FACTS, "site number", PI)}
    listed = [n for n in names if _key(n) in known]
    return {"count": len(names), "names": listed, "other": len(names) - len(listed)}


def _clean(value: str, limit: int, what: str) -> str:
    value = " ".join((value or "").split())
    if len(value) > limit:
        raise ValidationError(f"the {what} may have at most {limit} characters")
    return value


def _date(value: str, what: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"the {what} must be a date (YYYY-MM-DD)") from exc


class SiteService:
    def __init__(
        self,
        users: UserStore,
        store: SiteStore,
        rules: ProjectRules,
        *,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._store = store
        self._rules = rules
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    def today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    # -- access --------------------------------------------------------------------------------

    def _check(self, user_id: str, project_id: int, need: str, missing: str) -> None:
        active_profile(self._users, user_id, "your account is not active")
        level = self._rules.level(user_id, project_id)
        if level is None:
            raise NotFoundError(missing)
        if level not in NEEDS[need]:
            raise ForbiddenError(REFUSALS[need])

    def _site(self, user_id: str, site_id: int, need: str, *, live: bool = True) -> Site:
        site = self._store.get_site(site_id)
        if site is None:
            raise NotFoundError("site not found")
        self._check(user_id, site.project_id, need, "site not found")
        if live and site.deleted_at is not None:
            raise ConflictError("this site is removed; restore it first")
        return site

    # -- sites ---------------------------------------------------------------------------------

    def list_sites(self, user_id: str, project_id: int, include_removed: bool = False) -> list[Site]:
        self._check(user_id, project_id, "read", "project not found")
        sites = [s for s in self._store.sites_of_project(project_id) if include_removed or s.deleted_at is None]
        return sorted(sites, key=lambda s: (s.deleted_at is not None, s.hospital_id, s.site_no or "~", s.pi_name.lower(), s.id))

    def create(self, user_id: str, project_id: int, hospital_id: int, pi_name: str, site_no: str, note: str = "") -> Site:
        self._check(user_id, project_id, "edit", "project not found")
        if hospital_id not in self._rules.project_hospitals(project_id):
            raise ValidationError("this hospital is not in the project; add it first")
        pi = _clean(pi_name, MAX_PI, "PI name")
        number = _clean(site_no, MAX_SITE_NO, "site number")
        if sum(1 for s in self._store.sites_of_project(project_id) if s.deleted_at is None) >= MAX_SITES:
            raise ConflictError(f"a project can have at most {MAX_SITES} sites")
        try:
            site = self._store.create_site(project_id, hospital_id, user_id, number, pi, (note or "")[:MAX_NOTE])
        except LookupError as exc:
            raise ConflictError(self._taken(pi, number)) from exc
        if pi:  # the PI's history starts with the site, "from the start"
            self._store.add_fact(site.id, PI, pi, START_DATE, None, None, user_id)
        return site

    @staticmethod
    def _taken(pi: str, number: str) -> str:
        if not pi:
            return "this hospital already has a site without a PI name; name its PI first"
        return f'PI "{pi}" already has a site at this hospital' + (f', or site number "{number}" is taken' if number else "")

    def update(self, user_id: str, site_id: int, changes: dict) -> Site:
        """site_no and note. The PI is changed with change_pi (it keeps a history)."""
        site = self._site(user_id, site_id, "edit")
        values: dict = {}
        if "site_no" in changes:
            values["site_no"] = _clean(changes["site_no"] or "", MAX_SITE_NO, "site number")
        if "note" in changes:
            note = changes["note"] or ""
            if len(note) > MAX_NOTE:
                raise ValidationError(f"the note may have at most {MAX_NOTE} characters")
            values["note"] = note
        if not values:
            return site
        try:
            return self._store.update_site(site.id, values)
        except LookupError as exc:
            raise ConflictError(f'site number "{values.get("site_no")}" is already used in this project') from exc

    def change_pi(self, user_id: str, site_id: int, name: str, valid_from: str | None, step_id: int | None) -> Site:
        """The site keeps its pipeline and files; the history shows "PI A until <date>". A change
        dated today or earlier applies now; a later one is scheduled (upcoming) and the daily
        job switches the PI on its day (owner, 2026-11-13)."""
        site = self._site(user_id, site_id, "edit")
        pi = _clean(name, MAX_PI, "PI name")
        when = _date(valid_from, "date") if valid_from else self.today().isoformat()
        applies = date.fromisoformat(when) <= self.today()
        if applies and pi == site.pi_name.strip():
            return site
        self._step_of(site, step_id)
        if not applies and any(
            s.id != site.id and s.deleted_at is None and s.hospital_id == site.hospital_id
            and _key(s.pi_name) == _key(pi)
            for s in self._store.sites_of_project(site.project_id)
        ):
            raise ConflictError(self._taken(pi, ""))
        try:
            self._store.set_site_pi(site.id, pi, when, step_id, user_id, applies)
        except LookupError as exc:
            raise ConflictError(self._taken(pi, "")) from exc
        updated = self._store.get_site(site.id)
        return updated or site

    def apply_due_pi(self) -> tuple[int, int]:
        """The daily job: scheduled PI changes whose day has come. Never raises. Returns
        (changed, skipped: a name now used by another site of the hospital)."""
        try:
            changed, skipped = self._store.apply_due_site_pi(self.today().isoformat())
        except Exception:
            log.exception("scheduled PI changes could not be applied; the next run tries again")
            return 0, 0
        if skipped:
            log.warning("%d scheduled PI change(s) were skipped: the name is used by another site", skipped)
        return changed, skipped

    def _step_of(self, site: Site, step_id: int | None) -> None:
        if step_id is not None and self._rules.step_site(step_id) != site.id:
            raise ValidationError("that step is not in this site's pipeline")

    def remove(self, user_id: str, site_id: int) -> Site:
        site = self._site(user_id, site_id, "edit")
        if self._rules.live_pipeline_of_site(site.id) is not None:
            raise ConflictError("this site has a pipeline; remove the pipeline first")
        return self._store.set_site_deleted(site.id, self._clock().isoformat())

    def restore(self, user_id: str, site_id: int) -> Site:
        site = self._site(user_id, site_id, "edit", live=False)
        if site.deleted_at is None:
            return site
        live = sum(1 for s in self._store.sites_of_project(site.project_id) if s.deleted_at is None)
        if live >= MAX_SITES:
            raise ConflictError(f"a project can have at most {MAX_SITES} sites")
        try:
            return self._store.set_site_deleted(site.id, None)
        except LookupError as exc:
            raise ConflictError("another site of the hospital now uses this PI name or site number") from exc

    # -- facts ---------------------------------------------------------------------------------

    def facts(self, user_id: str, site_id: int) -> tuple[Site, list[FactView]]:
        site = self._site(user_id, site_id, "read", live=False)
        return site, fact_views(self._store.facts([site.id]), self.today())

    def add_fact(
        self,
        user_id: str,
        site_id: int,
        name: str,
        value: str,
        valid_from: str | None,
        step_id: int | None = None,
        corrects: int | None = None,
    ) -> Fact:
        """A new value from a day on (an empty value removes the fact), or with `corrects` a fix
        of a wrongly typed row (it then takes that row's place in the history)."""
        site = self._site(user_id, site_id, "edit")
        clean_name = _clean(name, MAX_FACT_NAME, "detail name")
        if not clean_name:
            raise ValidationError("the detail needs a name")
        if _key(clean_name) == PI.lower():
            raise ValidationError("change the PI with the PI field (it keeps the site's pipeline)")
        text = (value or "").strip()
        if len(text) > MAX_FACT_VALUE:
            raise ValidationError(f"the value may have at most {MAX_FACT_VALUE} characters")
        self._step_of(site, step_id)
        existing = self._store.facts([site.id])
        if corrects is not None:
            target = next((f for f in existing if f.id == corrects), None)
            if target is None or _key(target.name) != _key(clean_name):
                raise ValidationError("a correction must replace an entry of the same detail of this site")
            if any(f.corrects == corrects for f in existing):
                raise ConflictError("that entry was corrected already; correct the newest one")
            clean_name = target.name
            # A correction takes the corrected entry's place: its date unless another is given.
            when = _date(valid_from, "date") if valid_from else target.valid_from
        else:
            when = _date(valid_from, "date") if valid_from else self.today().isoformat()
            names = {_key(f.name) for f in existing if _key(f.name) != PI.lower()}
            if _key(clean_name) not in names and len(names) >= MAX_FACT_NAMES:
                raise ConflictError(f"a site can have at most {MAX_FACT_NAMES} details")
            same = next((f.name for f in existing if _key(f.name) == _key(clean_name)), None)
            clean_name = same or clean_name  # one spelling per detail
        return self._store.add_fact(site.id, clean_name, text, when, step_id, corrects, user_id)

    # -- for drafts and pages ------------------------------------------------------------------

    def details(self, site_id: int) -> tuple[Site, dict[str, str]] | None:
        """The site and its current details (no access check: the caller has one)."""
        site = self._store.get_site(site_id)
        if site is None:
            return None
        return site, current_facts(self._store.facts([site.id]), self.today())

    # -- purge ---------------------------------------------------------------------------------

    def purge_old(self) -> int:
        """Never raises (runs from the scheduler, after the pipeline purge)."""
        cutoff = (self._clock() - timedelta(days=PURGE_DAYS)).isoformat()
        try:
            ids = self._store.purge_candidates(cutoff)
            if ids:
                self._store.purge_sites(ids)
            return len(ids)
        except Exception:
            log.exception("site purge failed; the next run tries again")
            return 0
