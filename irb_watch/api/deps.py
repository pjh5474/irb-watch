"""FastAPI dependencies: login-token check, admin check, service wiring."""

import logging
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from irb_watch.ai.factory import build_agent_model, build_enricher, build_summarizer
from irb_watch.auth.jwt import AuthUnavailableError, AuthUser, InvalidTokenError, TokenVerifier
from irb_watch.auth.password_login import SupabasePasswordLogin
from irb_watch.config import get_settings
from irb_watch.crawlers.credentials import CredentialStore, KeyringCredentialStore
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.snuh import SnuhAdapter
from irb_watch.crawlers.snuh_reader import SnuhSourceReader
from irb_watch.db.agent import SupabaseAgentData, SupabaseAgentStore
from irb_watch.db.alerts import SupabaseAlertStore
from irb_watch.db.client import get_supabase_client
from irb_watch.db.credentials import DatabaseCredentialStore
from irb_watch.db.digest import SupabaseDigestStore
from irb_watch.db.documents import (
    SupabaseFileStorage,
    SupabaseScanRepo,
    SupabaseSourceStore,
    SupabaseSummaryRepo,
)
from irb_watch.db.downloads import SupabaseDownloadRepo, SupabaseUrlSigner
from irb_watch.db.members import SupabaseMemberStore
from irb_watch.db.supabase_users import SupabaseAuditSink, SupabaseAuthAdmin, SupabaseUserStore
from irb_watch.db.weekly import SupabaseWeeklyStore
from irb_watch.mail.client import ResendMailer
from irb_watch.services.agent import AgentService
from irb_watch.services.alerts import AlertService
from irb_watch.services.digest import DigestService
from irb_watch.services.downloads import DownloadService
from irb_watch.services.errors import ConflictError, ValidationError
from irb_watch.services.members import MemberService
from irb_watch.services.runner import ScanRunner
from irb_watch.services.scan import ScanService, SourceReader
from irb_watch.services.source_login import cookies_for_source
from irb_watch.services.sources import Source, SourceService
from irb_watch.services.summaries import SummaryService
from irb_watch.services.token_login import FailureLimiter, TokenLoginService
from irb_watch.services.users import UserService, UserStore
from irb_watch.services.weekly import WeeklyStatsService

log = logging.getLogger(__name__)

# The docs page's Authorize button logs in through POST /auth/token (an OAuth2 password flow) and
# then sends "Authorization: Bearer <token>", the same header every client already uses.
_bearer = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


@lru_cache
def _verifier_for(supabase_url: str) -> TokenVerifier:
    return TokenVerifier(supabase_url)


def get_verifier() -> TokenVerifier:
    return _verifier_for(get_settings().supabase_url)


def get_user_store() -> UserStore:
    return SupabaseUserStore(get_supabase_client())


def get_user_service() -> UserService:
    client = get_supabase_client()
    return UserService(
        SupabaseUserStore(client),
        SupabaseAuthAdmin(client),
        SupabaseAuditSink(client),
        get_settings().min_password_length,
    )


def get_audit_sink() -> SupabaseAuditSink:
    return SupabaseAuditSink(get_supabase_client())


def get_credential_store() -> CredentialStore:
    """The store for the logins used by scheduled scans (CREDENTIAL_STORE)."""
    settings = get_settings()
    if settings.credential_store == "database":
        return DatabaseCredentialStore(get_supabase_client(), settings.credentials_key)
    return KeyringCredentialStore()


def get_managed_credentials() -> DatabaseCredentialStore:
    """The database store, for the admin API. Only exists when CREDENTIAL_STORE=database."""
    store = get_credential_store()
    if not isinstance(store, DatabaseCredentialStore):
        raise ConflictError(
            "managing credentials over the API needs CREDENTIAL_STORE=database on this server"
        )
    return store


def build_reader(source: Source) -> SourceReader:
    """One reader per site type; a new site gets a new adapter and one more branch here."""
    settings = get_settings()
    if source.adapter != "snuh_hrpp":
        raise ValidationError(f"no adapter named {source.adapter}")
    # One login attempt per scan; LoginRejectedError fails only this source (needs_review).
    cookies = cookies_for_source(source, get_credential_store())
    fetcher = SafeFetcher(
        max_bytes=settings.max_file_bytes,
        timeout=settings.request_timeout_seconds,
        delay=settings.per_domain_delay_seconds,
        retry_waits=settings.fetch_retry_waits,
        retry_budget=settings.fetch_retry_budget,
        cookies=cookies,
    )
    return SnuhSourceReader(
        SnuhAdapter(fetcher),
        source.config["board_key"],
        settings.max_docs_per_url,
        settings.max_file_bytes,
    )


def get_source_service() -> SourceService:
    client = get_supabase_client()
    return SourceService(SupabaseSourceStore(client), SupabaseAuditSink(client))


def get_member_service() -> MemberService:
    client = get_supabase_client()
    return MemberService(
        get_source_service(),
        SupabaseUserStore(client),
        SupabaseMemberStore(client),
        SupabaseAuditSink(client),
    )


def get_scan_service() -> ScanService:
    client = get_supabase_client()
    return ScanService(
        SupabaseScanRepo(client, get_settings().max_scan_payload_bytes),
        SupabaseFileStorage(client),
        build_reader,
        SupabaseAuditSink(client),
        enricher=build_enricher(),
    )


def get_download_service() -> DownloadService:
    client = get_supabase_client()
    settings = get_settings()
    return DownloadService(
        SupabaseUserStore(client),
        SupabaseDownloadRepo(client),
        SupabaseUrlSigner(client),
        SupabaseAuditSink(client),
        settings.download_link_ttl_seconds,
        settings.download_links_per_day,
    )


# The scheduler (if running) replaces this so retries of failed scans get scheduled.
retry_hook: list[Callable[[int, datetime], None]] = [
    lambda source_id, due: log.warning(
        "no scheduler running: retry of source %s at %s was not scheduled", source_id, due
    )
]


def get_digest_store() -> SupabaseDigestStore:
    return SupabaseDigestStore(get_supabase_client())


def get_weekly_service() -> WeeklyStatsService:
    """The weekly usage report for admins. Without mail settings it is built without a mailer
    and does nothing."""
    client = get_supabase_client()
    settings = get_settings()
    on = settings.mail_enabled
    return WeeklyStatsService(
        SupabaseWeeklyStore(client),
        ResendMailer(settings.resend_api_key.strip(), settings.mail_from.strip()) if on else None,
        SupabaseAuditSink(client),
        SupabaseSourceStore(client).list,
        app_url=settings.app_url.strip() if on else "",
        stale_hours=settings.alert_stale_hours,
        timezone=settings.scan_timezone,
    )


def get_alert_service() -> AlertService:
    """Email alarms, the daily admin report and (when its extra settings exist) the daily digest
    for users. Without RESEND_API_KEY, MAIL_FROM and APP_URL the service is built without a
    mailer and does nothing."""
    client = get_supabase_client()
    settings = get_settings()
    sources = SupabaseSourceStore(client)
    on = settings.mail_enabled
    mailer = ResendMailer(settings.resend_api_key.strip(), settings.mail_from.strip()) if on else None
    audit = SupabaseAuditSink(client)
    digest = None
    if mailer is not None and settings.digest_enabled:
        digest = DigestService(
            SupabaseDigestStore(client),
            mailer,
            audit,
            app_url=settings.app_url.strip(),
            api_domain=settings.api_domain,
            unsubscribe_key=settings.unsubscribe_key.strip(),
            timezone=settings.scan_timezone,
        )
    return AlertService(
        SupabaseAlertStore(client),
        mailer,
        audit,
        sources.list,
        sources.get,
        app_url=settings.app_url.strip() if on else "",
        stale_hours=settings.alert_stale_hours,
        timezone=settings.scan_timezone,
        digest=digest,
    )


@lru_cache
def get_runner() -> ScanRunner:
    """One runner per process, so the daily run and "run all now" never overlap."""
    client = get_supabase_client()
    sources = SupabaseSourceStore(client)
    return ScanRunner(
        sources.list,
        sources.get,
        get_scan_service(),
        SupabaseAuditSink(client),
        lambda source_id, due: retry_hook[0](source_id, due),
        alerts=get_alert_service(),
    )


def get_summary_service() -> SummaryService:
    client = get_supabase_client()
    return SummaryService(
        SupabaseUserStore(client),
        SupabaseSummaryRepo(client),
        build_summarizer(),
        SupabaseAuditSink(client),
        get_settings().summary_requests_per_day,
    )


def get_agent_service() -> AgentService:
    """The assistant. Without OPENAI_API_KEY and OPENAI_MODEL it is built without a model and
    answers 503; the rest of the API is unaffected."""
    client = get_supabase_client()
    return AgentService(
        SupabaseUserStore(client),
        SupabaseAgentStore(client),
        SupabaseAgentData(client),
        build_agent_model(),
        SupabaseAuditSink(client),
    )


@lru_cache
def _failure_limiter() -> FailureLimiter:
    """One limiter per process, so wrong-password counts survive between requests."""
    return FailureLimiter()


def get_token_login_service() -> TokenLoginService:
    """Sign in by email and password for POST /auth/token. Without SUPABASE_PUBLISHABLE_KEY it is
    built without a login and answers 503."""
    settings = get_settings()
    key = settings.supabase_publishable_key.strip()
    login = SupabasePasswordLogin(settings.supabase_url, key) if key else None
    return TokenLoginService(
        login,
        SupabaseUserStore(get_supabase_client()),
        SupabaseAuditSink(get_supabase_client()),
        _failure_limiter(),
    )


def current_user(
    token: str | None = Depends(_bearer),
    verifier: TokenVerifier = Depends(get_verifier),
) -> AuthUser:
    if token is None:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        return verifier.verify(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    except AuthUnavailableError as exc:
        raise HTTPException(503, "login keys are temporarily unavailable") from exc


def require_admin(
    user: AuthUser = Depends(current_user),
    store: UserStore = Depends(get_user_store),
) -> AuthUser:
    """Checked against the database on every call, so demotions and deletions apply at once."""
    profile = store.get_profile(user.id)
    if profile is None or profile.deleted_at is not None or profile.role != "admin":
        raise HTTPException(403, "admin access required")
    return user
