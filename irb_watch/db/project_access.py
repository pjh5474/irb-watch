"""Supabase-backed store for services.project_access (project sharing, phase B1)."""

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.services.project_access import MemberRow, Person, ProjectRow

UNIQUE_VIOLATION = "23505"
_IN_CHUNK = 200


def _chunks(values: list) -> list[list]:
    return [values[i : i + _IN_CHUNK] for i in range(0, len(values), _IN_CHUNK)]


def _project(r: dict) -> ProjectRow:
    return ProjectRow(int(r["id"]), str(r["user_id"]), str(r["organization_id"]), r["name"])


class SupabaseProjectAccessStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    # the rule (database functions, service_role only)
    def access_level(self, project_id: int, user_id: str) -> str | None:
        data = self._c.rpc("project_access", {"p_project": project_id, "p_user": user_id}).execute().data
        return data if isinstance(data, str) and data else None

    def workable_project_ids(self, user_id: str) -> list[int]:
        rows = self._c.rpc("workable_project_ids", {"p_user": user_id}).execute().data or []
        return [int(r) if not isinstance(r, dict) else int(next(iter(r.values()))) for r in rows]

    # projects
    def project(self, project_id: int) -> ProjectRow | None:
        rows = (
            self._c.table("projects").select("id, user_id, organization_id, name").eq("id", project_id)
            .limit(1).execute().data
        )
        return _project(rows[0]) if rows else None

    def projects(self, project_ids: list[int]) -> list[ProjectRow]:
        out: list[ProjectRow] = []
        for chunk in _chunks(sorted(set(project_ids))):
            rows = self._c.table("projects").select("id, user_id, organization_id, name").in_("id", chunk).execute().data
            out += [_project(r) for r in rows]
        return out

    def organization_projects(self, organization_id: str) -> list[ProjectRow]:
        rows = (
            self._c.table("projects").select("id, user_id, organization_id, name")
            .eq("organization_id", organization_id).order("id").limit(1000).execute().data
        )
        return [_project(r) for r in rows]

    def owned_projects(self, user_id: str) -> list[ProjectRow]:
        rows = (
            self._c.table("projects").select("id, user_id, organization_id, name").eq("user_id", user_id)
            .execute().data
        )
        return [_project(r) for r in rows]

    def hand_over(self, project_id: int, user_id: str, keep_old_owner: bool, actor: str) -> None:
        try:
            self._c.rpc(
                "hand_over_project",
                {"p_project": project_id, "p_user": user_id, "p_keep": keep_old_owner, "p_actor": actor},
            ).execute()
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise LookupError("name taken") from exc
            raise

    def set_pm(self, project_id: int, user_id: str | None, added_by: str) -> None:
        try:
            self._c.rpc(
                "set_project_pm", {"p_project": project_id, "p_user": user_id, "p_actor": added_by}
            ).execute()
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise LookupError("taken") from exc
            raise

    # members
    def members(self, project_ids: list[int]) -> dict[int, list[MemberRow]]:
        out: dict[int, list[MemberRow]] = {}
        for chunk in _chunks(sorted(set(project_ids))):
            rows = (
                self._c.table("project_members").select("project_id, user_id, role, added_at")
                .in_("project_id", chunk).execute().data
            )
            for r in rows:
                out.setdefault(int(r["project_id"]), []).append(MemberRow(str(r["user_id"]), r["role"], r["added_at"]))
        return out

    def memberships(self, user_id: str) -> list[tuple[int, str]]:
        rows = self._c.table("project_members").select("project_id, role").eq("user_id", user_id).execute().data
        return [(int(r["project_id"]), r["role"]) for r in rows]

    def add_member(self, project_id: int, user_id: str, role: str, added_by: str) -> None:
        try:
            self._c.table("project_members").insert(
                {"project_id": project_id, "user_id": user_id, "role": role, "added_by": added_by}
            ).execute()
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise LookupError("taken") from exc
            raise

    def remove_member(self, project_id: int, user_id: str) -> None:
        self._c.table("project_members").delete().eq("project_id", project_id).eq("user_id", user_id).execute()

    # people
    def _people(self, rows: list[dict], orgs: dict[str, str]) -> list[Person]:
        return [
            Person(
                str(r["id"]), (r.get("display_name") or r.get("email") or "").strip(),
                orgs.get(str(r["id"])), r.get("deleted_at") is None,
            )
            for r in rows
        ]

    def _orgs(self, user_ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for chunk in _chunks(user_ids):
            rows = (
                self._c.table("organization_members").select("user_id, organization_id").in_("user_id", chunk)
                .execute().data
            )
            out.update({str(r["user_id"]): str(r["organization_id"]) for r in rows})
        return out

    def people(self, user_ids: list[str]) -> dict[str, Person]:
        ids = sorted({u for u in user_ids if u})
        if not ids:
            return {}
        rows: list[dict] = []
        for chunk in _chunks(ids):
            rows += (
                self._c.table("profiles").select("id, email, display_name, deleted_at").in_("id", chunk)
                .execute().data
            )
        return {p.id: p for p in self._people(rows, self._orgs(ids))}

    def organization_people(self, organization_id: str) -> list[Person]:
        ids = [
            str(r["user_id"])
            for r in self._c.table("organization_members").select("user_id")
            .eq("organization_id", organization_id).execute().data
        ]
        return list(self.people(ids).values())

    def organization_role(self, user_id: str) -> tuple[str | None, str | None]:
        rows = (
            self._c.table("organization_members").select("organization_id, role").eq("user_id", user_id)
            .limit(1).execute().data
        )
        return (str(rows[0]["organization_id"]), rows[0]["role"]) if rows else (None, None)
