"""FastAPI dependencies: login-token check, admin check, service wiring."""

import logging
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from irb_watch.ai.factory import build_enricher, build_summarizer
from irb_watch.auth.jwt import AuthUnavailableError, AuthUser, InvalidTokenError, TokenVerifier
from irb_watch.config import get_settings
from irb_watch.crawlers.credentials import KeyringCredentialStore
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.snuh import SnuhAdapter
from irb_watch.crawlers.snuh_reader import SnuhSourceReader
from irb_watch.db.access import SupabaseAccessStore
from irb_watch.db.client import get_supabase_client
from irb_watch.db.documents import (
    SupabaseFileStorage,
    SupabaseScanRepo,
    SupabaseSourceStore,
    SupabaseSummaryRepo,
)
from irb_watch.db.downloads import SupabaseDownloadRepo, SupabaseUrlSigner
from irb_watch.db.supabase_users import SupabaseAuditSink, SupabaseAuthAdmin, SupabaseUserStore
from irb_watch.services.access import AccessService
from irb_watch.services.downloads import DownloadService
from irb_watch.services.errors import ValidationError
from irb_watch.services.runner import ScanRunner
from irb_watch.services.scan import ScanService, SourceReader
from irb_watch.services.source_login import cookies_for_source, verify_user_access
from irb_watch.services.sources import Source, SourceService
from irb_watch.services.summaries import SummaryService
from irb_watch.services.users import UserService, UserStore

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


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


def build_reader(source: Source) -> SourceReader:
    """One reader per site type; a new site gets a new adapter and one more branch here."""
    settings = get_settings()
    if source.adapter != "snuh_hrpp":
        raise ValidationError(f"no adapter named {source.adapter}")
    # One login attempt per scan; LoginRejectedError fails only this source (needs_review).
    cookies = cookies_for_source(source, KeyringCredentialStore())
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


def get_access_service() -> AccessService:
    client = get_supabase_client()
    settings = get_settings()
    return AccessService(
        SupabaseUserStore(client),
        SupabaseAccessStore(client),
        SupabaseAuditSink(client),
        verify_user_access,
        settings.access_ttl_days,
        settings.access_attempts_per_hour,
        id_hash_key=settings.access_id_hash_key,
        id_failures_per_hour=settings.access_id_failures_per_hour,
    )


# The scheduler (if running) replaces this so retries of failed scans get scheduled.
retry_hook: list[Callable[[int, datetime], None]] = [
    lambda source_id, due: log.warning(
        "no scheduler running: retry of source %s at %s was not scheduled", source_id, due
    )
]


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


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    verifier: TokenVerifier = Depends(get_verifier),
) -> AuthUser:
    if credentials is None:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        return verifier.verify(credentials.credentials)
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
