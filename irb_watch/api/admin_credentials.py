"""Admin API for the hospital logins used by scheduled scans (CREDENTIAL_STORE=database).

The password is accepted once, encrypted, and never returned, logged or echoed back: the
endpoints answer with labels and dates only. Every change is written to the audit log
first (fail closed). Use over HTTPS only.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, SecretStr

from irb_watch.api.deps import get_audit_sink, get_managed_credentials, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.sessions import valid_name
from irb_watch.db.credentials import DatabaseCredentialStore
from irb_watch.services.errors import NotFoundError, ValidationError
from irb_watch.services.users import AuditSink

router = APIRouter(prefix="/admin/credentials", tags=["admin-credentials"])


class CredentialIn(BaseModel):
    login_id: str = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=200)


class CredentialOut(BaseModel):
    owner_label: str
    site: str
    updated_at: str | None = None


def _check_labels(owner_label: str, site: str) -> None:
    if not (valid_name(owner_label) and valid_name(site)):
        raise ValidationError("owner and site may use only letters, digits, '.', '_' and '-'")


@router.get("", response_model=list[CredentialOut])
def list_credentials(
    _: AuthUser = Depends(require_admin),
    store: DatabaseCredentialStore = Depends(get_managed_credentials),
) -> list[CredentialOut]:
    """Which logins are stored (labels and dates only; never the login or password)."""
    return [CredentialOut(**vars(e)) for e in store.list_entries()]


@router.put("/{owner_label}/{site}", response_model=CredentialOut)
def save_credential(
    owner_label: str,
    site: str,
    body: CredentialIn,
    admin: AuthUser = Depends(require_admin),
    store: DatabaseCredentialStore = Depends(get_managed_credentials),
    audit: AuditSink = Depends(get_audit_sink),
) -> CredentialOut:
    """Store or replace the hospital login used for `site` by sources whose login_user is
    `owner_label`. For a SNUH board the site is `snuh-<board_key>`."""
    _check_labels(owner_label, site)
    audit.record_required(
        "admin",
        "credentials.saved",
        admin.id,
        title=f"{owner_label}/{site}",
        payload={"owner_label": owner_label, "site": site},
    )
    store.set(
        owner_label,
        site,
        Credentials(body.login_id.strip(), body.password.get_secret_value()),
        updated_by=admin.id,
    )
    return CredentialOut(owner_label=owner_label, site=site)


@router.delete("/{owner_label}/{site}", status_code=204)
def delete_credential(
    owner_label: str,
    site: str,
    admin: AuthUser = Depends(require_admin),
    store: DatabaseCredentialStore = Depends(get_managed_credentials),
    audit: AuditSink = Depends(get_audit_sink),
) -> None:
    _check_labels(owner_label, site)
    audit.record_required(
        "admin",
        "credentials.deleted",
        admin.id,
        title=f"{owner_label}/{site}",
        payload={"owner_label": owner_label, "site": site},
    )
    if not store.delete(owner_label, site):
        raise NotFoundError("no stored login with that owner and site")
