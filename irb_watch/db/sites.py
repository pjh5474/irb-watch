"""Supabase-backed store for services.sites (sites per PI, phase B2a)."""

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.services.sites import Fact, Site

UNIQUE_VIOLATION = "23505"
_SITE_COLUMNS = "id, project_id, hospital_id, user_id, site_no, pi_name, note, created_at, updated_at, deleted_at"
_FACT_COLUMNS = "id, site_id, name, value, valid_from, step_id, corrects, changed_by, created_at"
_CHUNK = 200
_PAGE = 1000


def _site(r: dict) -> Site:
    return Site(
        id=int(r["id"]), project_id=int(r["project_id"]), hospital_id=int(r["hospital_id"]),
        user_id=str(r["user_id"]) if r.get("user_id") else None, site_no=r.get("site_no") or "",
        pi_name=r.get("pi_name") or "", note=r.get("note") or "", created_at=r["created_at"],
        updated_at=r["updated_at"], deleted_at=r.get("deleted_at"),
    )


def _fact(r: dict) -> Fact:
    return Fact(
        id=int(r["id"]), site_id=int(r["site_id"]), name=r["name"], value=r.get("value") or "",
        valid_from=str(r["valid_from"])[:10], step_id=r.get("step_id"), corrects=r.get("corrects"),
        changed_by=str(r["changed_by"]) if r.get("changed_by") else None, created_at=r["created_at"],
    )


def _taken(exc: APIError) -> bool:
    return exc.code == UNIQUE_VIOLATION


class SupabaseSiteStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def sites_of_project(self, project_id: int) -> list[Site]:
        rows = self._c.table("project_sites").select(_SITE_COLUMNS).eq("project_id", project_id).order("id").execute().data
        return [_site(r) for r in rows]

    def sites(self, site_ids: list[int]) -> list[Site]:
        ids = sorted(set(site_ids))
        out: list[Site] = []
        for i in range(0, len(ids), _CHUNK):
            rows = self._c.table("project_sites").select(_SITE_COLUMNS).in_("id", ids[i : i + _CHUNK]).execute().data
            out += [_site(r) for r in rows]
        return out

    def get_site(self, site_id: int) -> Site | None:
        rows = self._c.table("project_sites").select(_SITE_COLUMNS).eq("id", site_id).limit(1).execute().data
        return _site(rows[0]) if rows else None

    def create_site(self, project_id: int, hospital_id: int, user_id: str, site_no: str, pi_name: str, note: str) -> Site:
        try:
            rows = (
                self._c.table("project_sites")
                .insert({
                    "project_id": project_id, "hospital_id": hospital_id, "user_id": user_id,
                    "site_no": site_no, "pi_name": pi_name, "note": note,
                })
                .execute().data
            )
        except APIError as exc:
            if _taken(exc):
                raise LookupError("taken") from exc
            raise
        return _site(rows[0])

    def update_site(self, site_id: int, values: dict) -> Site:
        try:
            rows = self._c.table("project_sites").update(values).eq("id", site_id).execute().data
        except APIError as exc:
            if _taken(exc):
                raise LookupError("taken") from exc
            raise
        return _site(rows[0])

    def set_site_pi(self, site_id: int, name: str, valid_from: str, step_id: int | None, actor: str, apply: bool) -> None:
        try:
            self._c.rpc(
                "set_site_pi",
                {
                    "p_site": site_id, "p_name": name, "p_valid_from": valid_from, "p_step": step_id,
                    "p_actor": actor, "p_apply": apply,
                },
            ).execute()
        except APIError as exc:
            if _taken(exc):
                raise LookupError("taken") from exc
            raise

    def apply_due_site_pi(self, today: str) -> tuple[int, int]:
        data = self._c.rpc("apply_due_site_pi", {"p_today": today}).execute().data
        row = data[0] if isinstance(data, list) and data else (data or {})
        return int(row.get("changed") or 0), int(row.get("skipped") or 0)

    def set_site_deleted(self, site_id: int, deleted_at: str | None) -> Site:
        return self.update_site(site_id, {"deleted_at": deleted_at})

    def facts(self, site_ids: list[int]) -> list[Fact]:
        # Chunked and paged: the API answers at most 1,000 rows per request, and a cut answer
        # would leave newer values out without any error.
        ids = sorted(set(site_ids))
        out: list[Fact] = []
        for i in range(0, len(ids), _CHUNK):
            start = 0
            while True:
                rows = (
                    self._c.table("site_facts").select(_FACT_COLUMNS).in_("site_id", ids[i : i + _CHUNK])
                    .order("id").range(start, start + _PAGE - 1).execute().data
                )
                out += [_fact(r) for r in rows]
                if len(rows) < _PAGE:
                    break
                start += _PAGE
        return out

    def add_fact(
        self, site_id: int, name: str, value: str, valid_from: str, step_id: int | None, corrects: int | None, actor: str
    ) -> Fact:
        rows = (
            self._c.table("site_facts")
            .insert({
                "site_id": site_id, "name": name, "value": value, "valid_from": valid_from,
                "step_id": step_id, "corrects": corrects, "changed_by": actor,
            })
            .execute().data
        )
        return _fact(rows[0])

    def purge_candidates(self, cutoff: str) -> list[int]:
        removed = [
            int(r["id"])
            for r in self._c.table("project_sites").select("id").lt("deleted_at", cutoff).execute().data
        ]
        if not removed:
            return []
        used = {
            int(r["site_id"])
            for r in self._c.table("pipelines").select("site_id").in_("site_id", removed).execute().data
        }
        return [i for i in removed if i not in used]

    def purge_sites(self, site_ids: list[int]) -> None:
        if site_ids:
            self._c.table("project_sites").delete().in_("id", site_ids).execute()
