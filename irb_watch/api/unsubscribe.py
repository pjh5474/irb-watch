"""Public one-click unsubscribe from the daily mail: its document news, its IRB step reminders,
or both.

No login: the signed token in the link is the proof (see mail/unsubscribe.py). It can only turn
off the mail parts of the one user it was made for. GET changes nothing (a link scanner or a mail
preview must never unsubscribe anyone); POST does, and is what mail apps send for one-click.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

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


Part = Literal["news", "reminders"]


@router.post("")
def unsubscribe(
    token: str,
    stop: list[Part] = Query(default_factory=list, max_length=2),
    store: SupabaseDigestStore = Depends(get_digest_store),
    audit: AuditSink = Depends(get_audit_sink),
) -> dict:
    """stop: which parts to switch off (the page sends the user's choice). None given = both:
    that is what mail apps send for one-click unsubscribe, which must always stop the mail."""
    user_id = _user_of(token)
    parts = set(stop) or {"news", "reminders"}
    at = datetime.now(UTC).isoformat()
    if "news" in parts:
        store.set_digest_enabled(user_id, False, at)
        audit.record("user", "digest.unsubscribed", user_id, title="Daily digest switched off by link")
    if "reminders" in parts:
        store.set_reminders_enabled(user_id, False, at)
        audit.record(
            "user", "reminders.unsubscribed", user_id, title="IRB step reminders switched off by link"
        )
    return {"status": "unsubscribed", "stopped": sorted(parts)}
