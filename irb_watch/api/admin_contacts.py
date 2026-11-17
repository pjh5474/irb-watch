"""Admin: a hospital's IRB contacts, with the raw reason of a failed check.

Users read the contacts directly from Supabase (table hospital_contacts, readable by every
signed-in user, without the raw reason). The scan fills the scanned texts; an admin may type a
field here, which the scan never overwrites (see services/contacts.py).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_contact_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.contacts import FIELDS, MAX_FIELD_CHARS, ContactService, HospitalContacts

router = APIRouter(prefix="/admin/hospitals", tags=["admin-hospitals"])


class ContactsOut(BaseModel):
    hospital_id: int
    scanned: dict[str, str]
    manual: dict[str, str | None]
    shown: dict[str, str]
    shown_from: dict[str, str]  # admin | r-bay | none
    check_status: str
    checked_at: str | None
    last_ok_at: str | None
    error_kind: str | None
    error_detail: str | None
    staff: list[dict]
    staff_status: str
    staff_error_kind: str | None
    staff_error_detail: str | None

    @classmethod
    def of(cls, c: HospitalContacts) -> "ContactsOut":
        shown = {f: c.shown(f) for f in FIELDS}
        return cls(
            hospital_id=c.hospital_id, scanned=c.scanned, manual=c.manual,
            shown={f: shown[f][0] for f in FIELDS}, shown_from={f: shown[f][1] for f in FIELDS},
            check_status=c.check_status, checked_at=c.checked_at, last_ok_at=c.last_ok_at,
            error_kind=c.error_kind, error_detail=c.error_detail, staff=list(c.staff),
            staff_status=c.staff_status, staff_error_kind=c.staff_error_kind,
            staff_error_detail=c.staff_error_detail,
        )


class ContactsIn(BaseModel):
    """Only the fields sent change; null or blank clears a typed field."""

    phone: str | None = Field(default=None, max_length=MAX_FIELD_CHARS + 200)
    fax: str | None = Field(default=None, max_length=MAX_FIELD_CHARS + 200)
    email: str | None = Field(default=None, max_length=MAX_FIELD_CHARS + 200)


@router.get("/{hospital_id}/contacts", response_model=ContactsOut)
def get_contacts(
    hospital_id: int,
    _: AuthUser = Depends(require_admin),
    service: ContactService = Depends(get_contact_service),
) -> ContactsOut:
    return ContactsOut.of(service.get(hospital_id))


@router.put("/{hospital_id}/contacts", response_model=ContactsOut)
def set_contacts(
    hospital_id: int,
    body: ContactsIn,
    admin: AuthUser = Depends(require_admin),
    service: ContactService = Depends(get_contact_service),
) -> ContactsOut:
    return ContactsOut.of(service.set_manual(admin.id, hospital_id, body.model_dump(exclude_unset=True)))
