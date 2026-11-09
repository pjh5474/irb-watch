"""Public one-click unsubscribe from the daily digest.

No login: the signed token in the link is the proof (see mail/unsubscribe.py). It can only turn
off the digest of the one user it was made for. GET changes nothing (a link scanner or a mail
preview must never unsubscribe anyone); POST does, and is what mail apps send for one-click.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from irb_watch.api.deps import get_audit_sink, get_digest_store
from irb_watch.config import get_settings
from irb_watch.db.digest import SupabaseDigestStore
from irb_watch.mail.unsubscribe import read_token
from irb_watch.services.users import AuditSink

router = APIRouter(prefix="/unsubscribe", tags=["unsubscribe"])


def _user_of(token: str) -> str:
    user_id = read_token(token, get_settings().unsubscribe_key.strip())
    if user_id is None:
        raise HTTPException(400, "this unsubscribe link is not valid")  # no detail on purpose
    return user_id


@router.get("")
def check_link(token: str) -> dict:
    """Is this link valid? Changes nothing."""
    _user_of(token)
    return {"valid": True}


@router.post("")
def unsubscribe(
    token: str,
    store: SupabaseDigestStore = Depends(get_digest_store),
    audit: AuditSink = Depends(get_audit_sink),
) -> dict:
    user_id = _user_of(token)
    store.set_digest_enabled(user_id, False, datetime.now(UTC).isoformat())
    audit.record("user", "digest.unsubscribed", user_id, title="Daily digest switched off by link")
    return {"status": "unsubscribed"}
