"""In-memory stand-in for services.sites.SiteStore, with the unique indexes of project_sites."""

import itertools
from dataclasses import replace
from datetime import UTC, datetime

from irb_watch.services.sites import Fact, Site


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeSiteStore:
    def __init__(self, pipelines=None) -> None:
        self.rows: dict[int, Site] = {}
        self.fact_rows: list[Fact] = []
        self._ids = itertools.count(1000)
        self.pipelines = pipelines  # FakePipelineStore, for the purge check

    def _clash(self, site: Site) -> bool:
        for o in self.rows.values():
            if o.id == site.id or o.deleted_at is not None or o.project_id != site.project_id:
                continue
            if o.hospital_id == site.hospital_id and o.pi_name.strip().lower() == site.pi_name.strip().lower():
                return True
            if site.site_no.strip() and o.site_no.strip().lower() == site.site_no.strip().lower():
                return True
        return False

    def sites_of_project(self, project_id):
        return [s for s in self.rows.values() if s.project_id == project_id]

    def sites(self, site_ids):
        return [self.rows[i] for i in site_ids if i in self.rows]

    def get_site(self, site_id):
        return self.rows.get(site_id)

    def create_site(self, project_id, hospital_id, user_id, site_no, pi_name, note):
        site = Site(next(self._ids), project_id, hospital_id, user_id, site_no, pi_name, note, _now(), _now())
        if self._clash(site):
            raise LookupError("taken")
        self.rows[site.id] = site
        return site

    def update_site(self, site_id, values):
        site = replace(self.rows[site_id], **values, updated_at=_now())
        if site.deleted_at is None and self._clash(site):
            raise LookupError("taken")
        self.rows[site_id] = site
        return site

    def set_site_pi(self, site_id, name, valid_from, step_id, actor, apply):
        if apply:
            self.update_site(site_id, {"pi_name": name.strip()})
        self.add_fact(site_id, "PI", name.strip(), valid_from, step_id, None, actor)

    def apply_due_site_pi(self, today):
        changed = skipped = 0
        corrected = {f.corrects for f in self.fact_rows if f.corrects}
        for site in list(self.rows.values()):
            if site.deleted_at is not None:
                continue
            due = sorted(
                (f for f in self.fact_rows
                 if f.site_id == site.id and f.name.strip().lower() == "pi" and f.valid_from <= today and f.id not in corrected),
                key=lambda f: (f.valid_from, f.id),
            )
            if due and due[-1].value.strip() != site.pi_name.strip():
                try:
                    self.update_site(site.id, {"pi_name": due[-1].value.strip()})
                    changed += 1
                except LookupError:
                    skipped += 1
        return changed, skipped

    def set_site_deleted(self, site_id, deleted_at):
        return self.update_site(site_id, {"deleted_at": deleted_at})

    def facts(self, site_ids):
        return [f for f in self.fact_rows if f.site_id in set(site_ids)]

    def add_fact(self, site_id, name, value, valid_from, step_id, corrects, actor):
        fact = Fact(next(self._ids), site_id, name, value, valid_from, step_id, corrects, actor, _now())
        self.fact_rows.append(fact)
        return fact

    def purge_candidates(self, cutoff):
        used = {p.site_id for p in self.pipelines.pipes.values()} if self.pipelines else set()
        return [s.id for s in self.rows.values() if s.deleted_at and s.deleted_at < cutoff and s.id not in used]

    def purge_sites(self, site_ids):
        for i in site_ids:
            self.rows.pop(i, None)
        self.fact_rows = [f for f in self.fact_rows if f.site_id not in set(site_ids)]
