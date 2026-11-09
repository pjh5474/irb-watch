from fastapi import APIRouter, Depends

from irb_watch.api.deps import (
    get_alert_service,
    get_audit_sink,
    get_user_store,
    get_weekly_service,
    require_admin,
)
from irb_watch.auth.jwt import AuthUser
from irb_watch.mail.client import MailError
from irb_watch.services.alerts import AlertService
from irb_watch.services.errors import ConflictError, UpstreamError, ValidationError
from irb_watch.services.users import AuditSink, UserStore
from irb_watch.services.weekly import WeeklyStatsService

router = APIRouter(prefix="/admin/mail", tags=["admin-mail"])


@router.post("/test")
def send_test_mail(
    admin: AuthUser = Depends(require_admin),
    alerts: AlertService = Depends(get_alert_service),
    users: UserStore = Depends(get_user_store),
    audit: AuditSink = Depends(get_audit_sink),
) -> dict:
    """Send a short test mail to YOUR OWN address, to check the key, the sender domain and DNS.

    409 when mail is not set up on this server (RESEND_API_KEY, MAIL_FROM and APP_URL).
    502 with the mail service's reason when the send fails (for example an unverified domain).
    """
    if not alerts.enabled:
        raise ConflictError("email is not set up on this server (RESEND_API_KEY, MAIL_FROM, APP_URL)")
    profile = users.get_profile(admin.id)
    if profile is None or not profile.email:
        raise ValidationError("your account has no email address")
    try:
        alerts.send_test(profile.email)
    except MailError as exc:
        audit.record("admin", "mail.test_failed", admin.id, title="Mail test failed", payload={"error": str(exc)})
        raise UpstreamError(str(exc)) from exc
    audit.record("admin", "mail.test_sent", admin.id, title="Mail test sent")
    return {"status": "sent"}


@router.post("/weekly-preview")
def send_weekly_preview(
    admin: AuthUser = Depends(require_admin),
    weekly: WeeklyStatsService = Depends(get_weekly_service),
    users: UserStore = Depends(get_user_store),
    audit: AuditSink = Depends(get_audit_sink),
) -> dict:
    """Send the weekly usage report for the last 7 days to YOUR OWN address only, so it can be
    looked at before Monday. It never goes to the other admins and does not use up the week's
    once-only claim, so Monday's real mail is not affected."""
    if not weekly.enabled:
        raise ConflictError("email is not set up on this server (RESEND_API_KEY, MAIL_FROM, APP_URL)")
    profile = users.get_profile(admin.id)
    if profile is None or not profile.email:
        raise ValidationError("your account has no email address")
    try:
        weekly.preview(profile.email)
    except MailError as exc:
        audit.record("admin", "mail.test_failed", admin.id, title="Weekly preview failed", payload={"error": str(exc)})
        raise UpstreamError(str(exc)) from exc
    audit.record("admin", "mail.weekly_preview_sent", admin.id, title="Weekly preview sent")
    return {"status": "sent"}
