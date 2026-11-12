"""Supabase-backed hospital catalog (table public.hospitals)."""

from datetime import UTC, datetime

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.services.hospitals import DuplicateHospitalError, Hospital

UNIQUE_VIOLATION = "23505"
_COLUMNS = "id, name, note, created_at, updated_at"


def _hospital(r: dict) -> Hospital:
    return Hospital(
        id=int(r["id"]), name=r["name"], note=r.get("note") or "",
        created_at=r.get("created_at") or "", updated_at=r.get("updated_at") or "",
    )


class SupabaseHospitalStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def list(self) -> list[Hospital]:
        rows = self._c.table("hospitals").select(_COLUMNS).order("id").execute().data
        return [_hospital(r) for r in rows]

    def get(self, hospital_id: int) -> Hospital | None:
        rows = (
            self._c.table("hospitals").select(_COLUMNS).eq("id", hospital_id).limit(1).execute().data
        )
        return _hospital(rows[0]) if rows else None

    def create(self, name: str, note: str, created_by: str | None) -> Hospital:
        try:
            rows = (
                self._c.table("hospitals")
                .insert({"name": name, "note": note, "created_by": created_by})
                .execute()
                .data
            )
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise DuplicateHospitalError from exc
            raise
        return _hospital(rows[0])

    def delete(self, hospital_id: int) -> None:
        # sources.hospital_id is ON DELETE RESTRICT: a hospital with boards cannot be removed.
        self._c.table("hospitals").delete().eq("id", hospital_id).execute()

    def update(self, hospital_id: int, values: dict) -> Hospital:
        try:
            rows = (
                self._c.table("hospitals")
                .update({**values, "updated_at": datetime.now(UTC).isoformat()})
                .eq("id", hospital_id)
                .execute()
                .data
            )
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise DuplicateHospitalError from exc
            raise
        if not rows:
            raise LookupError(hospital_id)
        return _hospital(rows[0])
