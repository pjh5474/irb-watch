"""Supabase-backed hospital contacts (table public.hospital_contacts)."""

from datetime import UTC, datetime

from supabase import Client

from irb_watch.services.contacts import FIELDS, HospitalContacts

_COLUMNS = (
    "hospital_id, scanned_phone, scanned_fax, scanned_email, manual_phone, manual_fax, manual_email, "
    "check_status, checked_at, last_ok_at, error_kind, error_detail, updated_at, "
    "staff, staff_status, staff_error_kind, staff_error_detail"
)


def _row(r: dict) -> HospitalContacts:
    return HospitalContacts(
        hospital_id=int(r["hospital_id"]),
        scanned={f: r.get(f"scanned_{f}") or "" for f in FIELDS},
        manual={f: r.get(f"manual_{f}") or None for f in FIELDS},
        check_status=r.get("check_status") or "never",
        checked_at=r.get("checked_at"),
        last_ok_at=r.get("last_ok_at"),
        error_kind=r.get("error_kind"),
        error_detail=r.get("error_detail"),
        updated_at=r.get("updated_at"),
        staff=tuple(r.get("staff") or ()),
        staff_status=r.get("staff_status") or "never",
        staff_error_kind=r.get("staff_error_kind"),
        staff_error_detail=r.get("staff_error_detail"),
    )


class SupabaseContactStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def get(self, hospital_id: int) -> HospitalContacts | None:
        rows = (
            self._c.table("hospital_contacts").select(_COLUMNS).eq("hospital_id", hospital_id).limit(1).execute().data
        )
        return _row(rows[0]) if rows else None

    def save_scan(self, row: HospitalContacts) -> None:
        # Only the scan's columns are in the payload: an upsert updates just the columns it
        # names, so a typed text (manual_*) written meanwhile by an admin is left alone.
        values: dict = {
            "hospital_id": row.hospital_id,
            "check_status": row.check_status,
            "checked_at": row.checked_at,
            "last_ok_at": row.last_ok_at,
            "error_kind": row.error_kind,
            "error_detail": row.error_detail,
            "staff": list(row.staff),
            "staff_status": row.staff_status,
            "staff_error_kind": row.staff_error_kind,
            "staff_error_detail": row.staff_error_detail,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        for f in FIELDS:
            values[f"scanned_{f}"] = row.scanned.get(f, "")
        self._c.table("hospital_contacts").upsert(values, on_conflict="hospital_id").execute()

    def save_manual(self, row: HospitalContacts, actor_id: str) -> None:
        # Only the typed columns and who typed them: a scan's result is left alone.
        values: dict = {
            "hospital_id": row.hospital_id,
            "updated_by": actor_id,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        for f in FIELDS:
            values[f"manual_{f}"] = row.manual.get(f)
        self._c.table("hospital_contacts").upsert(values, on_conflict="hospital_id").execute()

    def hospital_exists(self, hospital_id: int) -> bool:
        rows = self._c.table("hospitals").select("id").eq("id", hospital_id).limit(1).execute().data
        return bool(rows)

    def checked_since(self, hospital_id: int, since: str) -> bool:
        rows = (
            self._c.table("hospital_contacts")
            .select("hospital_id")
            .eq("hospital_id", hospital_id)
            .gte("checked_at", since)
            .limit(1)
            .execute()
            .data
        )
        return bool(rows)
