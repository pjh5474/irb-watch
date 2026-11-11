"""Supabase-backed store for services.organizations."""

from datetime import datetime

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.core.org_stats import UsageRow
from irb_watch.core.weekly import UserInfo
from irb_watch.db.weekly import SupabaseWeeklyStore
from irb_watch.services.organizations import Member, NameTakenError, Organization

UNIQUE_VIOLATION = "23505"
_COLUMNS = "id, name, created_at, deleted_at"


def _org(row: dict, member_count: int) -> Organization:
    return Organization(
        id=str(row["id"]),
        name=row["name"],
        created_at=row["created_at"],
        deleted_at=row.get("deleted_at"),
        member_count=member_count,
    )


class SupabaseOrganizationStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def _counts(self) -> dict[str, int]:
        rows = self._c.table("organization_members").select("organization_id").execute().data
        counts: dict[str, int] = {}
        for r in rows:
            counts[str(r["organization_id"])] = counts.get(str(r["organization_id"]), 0) + 1
        return counts

    def _count(self, organization_id: str) -> int:
        result = (
            self._c.table("organization_members")
            .select("user_id", count="exact")
            .eq("organization_id", organization_id)
            .limit(1)
            .execute()
        )
        return result.count or 0

    def list_organizations(self) -> list[Organization]:
        counts = self._counts()
        rows = self._c.table("organizations").select(_COLUMNS).execute().data
        return [_org(r, counts.get(str(r["id"]), 0)) for r in rows]

    def get_organization(self, organization_id: str) -> Organization | None:
        try:
            rows = (
                self._c.table("organizations").select(_COLUMNS).eq("id", organization_id)
                .limit(1).execute().data
            )
        except APIError as exc:
            if exc.code == "22P02":  # not a uuid
                return None
            raise
        return _org(rows[0], self._count(organization_id)) if rows else None

    def _write(self, query) -> dict:
        try:
            rows = query.execute().data
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise NameTakenError from exc
            raise
        if not rows:
            raise LookupError("organization not found")
        return rows[0]

    def create_organization(self, name: str) -> Organization:
        return _org(self._write(self._c.table("organizations").insert({"name": name})), 0)

    def rename_organization(self, organization_id: str, name: str) -> Organization:
        row = self._write(
            self._c.table("organizations").update({"name": name}).eq("id", organization_id)
        )
        return _org(row, self._count(organization_id))

    def set_deleted_at(self, organization_id: str, deleted_at: str | None) -> Organization:
        row = self._write(
            self._c.table("organizations").update({"deleted_at": deleted_at}).eq("id", organization_id)
        )
        return _org(row, self._count(organization_id))

    def members(self, organization_id: str) -> list[Member]:
        rows = (
            self._c.table("organization_members")
            .select("user_id, profiles(email, display_name, role, created_at, deleted_at)")
            .eq("organization_id", organization_id)
            .execute()
            .data
        )
        result = []
        for r in rows:
            p = r.get("profiles") or {}
            result.append(
                Member(
                    user_id=str(r["user_id"]),
                    email=p.get("email"),
                    display_name=p.get("display_name"),
                    role=p.get("role") or "user",
                    created_at=p.get("created_at") or "",
                    deleted_at=p.get("deleted_at"),
                )
            )
        result.sort(key=lambda m: (m.deleted_at is not None, (m.email or "~").lower()))
        return result

    def organization_of(self, user_ids: list[str]) -> dict[str, str]:
        rows = (
            self._c.table("organization_members")
            .select("user_id, organization_id")
            .in_("user_id", user_ids)
            .execute()
            .data
        )
        return {str(r["user_id"]): str(r["organization_id"]) for r in rows}

    def all_memberships(self) -> dict[str, str]:
        rows = self._c.table("organization_members").select("user_id, organization_id").execute().data
        return {str(r["user_id"]): str(r["organization_id"]) for r in rows}

    def user_exists(self, user_id: str) -> bool:
        try:
            rows = self._c.table("profiles").select("id").eq("id", user_id).limit(1).execute().data
        except APIError as exc:
            if exc.code == "22P02":
                return False
            raise
        return bool(rows)

    def set_user_organization(self, user_id: str, organization_id: str) -> None:
        # One row per user (unique user_id): update it, or add it if the user has none.
        rows = (
            self._c.table("organization_members")
            .update({"organization_id": organization_id})
            .eq("user_id", user_id)
            .execute()
            .data
        )
        if not rows:
            self._c.table("organization_members").insert(
                {"organization_id": organization_id, "user_id": user_id}
            ).execute()

    def usage(self, start: datetime, end: datetime, types: list[str]) -> list[UsageRow]:
        rows = (
            self._c.rpc(
                "organization_usage",
                {"p_start": start.isoformat(), "p_end": end.isoformat(), "p_types": types},
            )
            .execute()
            .data
        ) or []
        return [
            UsageRow(str(r["organization_id"]), str(r["actor_id"]), r["event_type"], int(r["n"]))
            for r in rows
        ]

    def accounts(self) -> list[UserInfo]:
        return SupabaseWeeklyStore(self._c).users()
