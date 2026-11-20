"""FastAPI dependencies: login-token check, admin check, service wiring."""

import logging
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from irb_watch.ai.factory import (
    build_agent_model,
    build_draft_writer,
    build_enricher,
    build_session_finder,
    build_summarizer,
)
from irb_watch.ai.openai_summarizer import OpenAISummarizer
from irb_watch.auth.jwt import AuthUnavailableError, AuthUser, InvalidTokenError, TokenVerifier
from irb_watch.auth.password_login import SupabasePasswordLogin
from irb_watch.config import get_settings
from irb_watch.crawlers.credentials import CredentialStore, KeyringCredentialStore
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.rbay_login import POOL as RBAY_POOL
from irb_watch.crawlers.rbay_login import browser_check_login
from irb_watch.crawlers.rbay_reader import RbaySourceReader
from irb_watch.crawlers.snuh import SnuhAdapter
from irb_watch.crawlers.snuh_reader import SnuhSourceReader
from irb_watch.crawlers.tls import rbay_ssl_context
from irb_watch.db.agent import SupabaseAgentData, SupabaseAgentStore
from irb_watch.db.ai_instructions import SupabaseInstructionsStore
from irb_watch.db.alerts import SupabaseAlertStore
from irb_watch.db.client import get_supabase_client
from irb_watch.db.contacts import SupabaseContactStore
from irb_watch.db.credentials import DatabaseCredentialStore
from irb_watch.db.digest import SupabaseDigestStore
from irb_watch.db.docgen import SupabaseDocgenStore, SupabaseTemplateFiles
from irb_watch.db.documents import (
    SupabaseFileStorage,
    SupabaseScanRepo,
    SupabaseSourceStore,
    SupabaseSummaryRepo,
)
from irb_watch.db.downloads import SupabaseDownloadRepo, SupabaseUrlSigner
from irb_watch.db.draft_jobs import SupabaseJobStore, ThreadRunner
from irb_watch.db.hospitals import SupabaseHospitalStore
from irb_watch.db.members import SupabaseMemberStore
from irb_watch.db.organizations import SupabaseOrganizationStore
from irb_watch.db.pipeline_admin import SupabasePipelineSetupStore
from irb_watch.db.pipelines import SupabasePipelineFiles, SupabasePipelineStore
from irb_watch.db.project_access import SupabaseProjectAccessStore
from irb_watch.db.read_cache import cached_session_store, cached_setup_store
from irb_watch.db.reviews import SupabaseReviewStore
from irb_watch.db.session_all import SupabaseAllSearchStore
from irb_watch.db.session_daily import SupabaseDailySearchStore
from irb_watch.db.sessions import SupabaseSessionStore
from irb_watch.db.sites import SupabaseSiteStore
from irb_watch.db.supabase_users import SupabaseAuditSink, SupabaseAuthAdmin, SupabaseUserStore
from irb_watch.db.tasks import SupabaseTaskStore
from irb_watch.db.user_credentials import SupabaseUserLoginStore
from irb_watch.db.weekly import SupabaseWeeklyStore
from irb_watch.mail.client import ResendMailer
from irb_watch.services.agent import AgentService
from irb_watch.services.ai_instructions import (
    InstructionsCache,
    InstructionsService,
    Preview,
    PreviewResult,
    summary_preview,
)
from irb_watch.services.alerts import AlertService
from irb_watch.services.board_sync import BoardSyncService
from irb_watch.services.calendar import CalendarService
from irb_watch.services.contacts import ContactService, StaffRead, staff_dicts
from irb_watch.services.digest import DigestService
from irb_watch.services.docgen import DocgenService
from irb_watch.services.downloads import DownloadService
from irb_watch.services.draft_jobs import DraftJobService
from irb_watch.services.errors import ConflictError, ValidationError
from irb_watch.services.hospitals import HospitalService
from irb_watch.services.manual import ManualService
from irb_watch.services.members import MemberService
from irb_watch.services.organizations import OrganizationService
from irb_watch.services.pipeline_admin import PipelineSetupService
from irb_watch.services.pipelines import PipelineService
from irb_watch.services.project_access import ProjectAccessService
from irb_watch.services.reviews import ReviewService
from irb_watch.services.runner import ScanRunner
from irb_watch.services.scan import ScanService, SourceReader
from irb_watch.services.session_all import AllSearchService
from irb_watch.services.session_daily import DailySessionSearch
from irb_watch.services.sessions import SessionService
from irb_watch.services.sites import SiteService
from irb_watch.services.source_login import cookies_for_source, login_site
from irb_watch.services.sources import RBAY_ADAPTER, Source, SourceService
from irb_watch.services.summaries import SummaryService
from irb_watch.services.tasks import TaskService
from irb_watch.services.token_login import FailureLimiter, TokenLoginService
from irb_watch.services.user_logins import UserLoginService
from irb_watch.services.user_rbay import UserRbaySessions
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


def get_organization_service() -> OrganizationService:
    client = get_supabase_client()
    return OrganizationService(
        SupabaseOrganizationStore(client),
        SupabaseAuditSink(client),
        timezone=get_settings().scan_timezone,
    )


def get_pipeline_setup_service() -> PipelineSetupService:
    client = get_supabase_client()
    # The lists are cached for 10 minutes and cleared by every admin write (db/read_cache.py).
    return PipelineSetupService(cached_setup_store(SupabasePipelineSetupStore(client)), SupabaseAuditSink(client))


def get_project_access_service() -> ProjectAccessService:
    """Who may open a project and its members (phase B1)."""
    client = get_supabase_client()
    return ProjectAccessService(
        SupabaseUserStore(client), SupabaseProjectAccessStore(client), SupabaseAuditSink(client)
    )


def get_pipeline_service() -> PipelineService:
    client = get_supabase_client()
    settings = get_settings()
    return PipelineService(
        SupabaseUserStore(client),
        SupabasePipelineStore(client),
        get_pipeline_setup_service(),
        SupabasePipelineFiles(client),
        max_file_bytes=settings.max_file_bytes,
        link_ttl_seconds=settings.download_link_ttl_seconds,
        timezone=settings.scan_timezone,
        access=get_project_access_service(),
        sites=SupabaseSiteStore(client),
        sessions=_session_service(),  # a step's target meeting (CAL-1)
    )


def _session_service(rules=None) -> SessionService:
    """IRB sessions (CAL-1). `rules` (the pipeline service) is needed only for user proposals;
    the pipeline service itself gets one without, so neither builds the other in a loop."""
    client = get_supabase_client()
    settings = get_settings()
    hospitals = SupabaseHospitalStore(client)
    finder = build_session_finder()
    return SessionService(
        SupabaseUserStore(client),
        cached_session_store(SupabaseSessionStore(client, runner=settings.app_env)),
        SupabaseAuditSink(client),
        finder=finder,
        runner=_session_job_runner() if finder is not None else None,
        rules=rules,
        hospital_exists=lambda hospital_id: hospitals.get(hospital_id) is not None,
        timezone=settings.scan_timezone,
    )


def get_session_service() -> SessionService:
    return _session_service(rules=get_pipeline_service())


def get_task_service() -> TaskService:
    return _task_service(get_pipeline_service())


def _task_service(pipelines: PipelineService) -> TaskService:
    client = get_supabase_client()
    return TaskService(
        SupabaseUserStore(client), SupabaseTaskStore(client), pipelines, SupabaseAuditSink(client),
        timezone=get_settings().scan_timezone,
    )


def get_calendar_service() -> CalendarService:
    settings = get_settings()
    pipelines = get_pipeline_service()  # built once, shared with the task service
    return CalendarService(
        pipelines, _session_service(), _task_service(pipelines),
        app_url=settings.app_url.strip(), timezone=settings.scan_timezone,
    )


def get_site_service() -> SiteService:
    """Sites per PI and their dated details (phase B2a)."""
    client = get_supabase_client()
    return SiteService(
        SupabaseUserStore(client), SupabaseSiteStore(client), get_pipeline_service(),
        timezone=get_settings().scan_timezone,
    )


@lru_cache
def _job_runner() -> ThreadRunner:
    """One small worker pool for the whole process (AI drafts and revisions)."""
    return ThreadRunner(workers=2)


@lru_cache
def _session_job_runner() -> ThreadRunner:
    """A separate one-worker pool for the IRB session searches (CAL-1), so a long search never
    holds a draft worker. Searches queue behind each other."""
    return ThreadRunner(workers=1, name="session-job")


def get_draft_job_service() -> DraftJobService:
    # runner = APP_ENV: at its start a server fails only the open jobs it made itself.
    store = SupabaseJobStore(get_supabase_client(), runner=get_settings().app_env)
    return DraftJobService(store, _job_runner())


def get_audit_sink() -> SupabaseAuditSink:
    return SupabaseAuditSink(get_supabase_client())


@lru_cache
def _login_test_runner() -> ThreadRunner:
    """Its own small pool: a login test (a headless browser, 5-20 s) never holds a draft worker."""
    return ThreadRunner(workers=2, name="rbay-login-test")


def get_user_login_service() -> UserLoginService:
    """A person's own R-BAY login (user_site_credentials). Separate from the platform's logins."""
    store = SupabaseUserLoginStore(get_supabase_client(), get_settings().credentials_key)
    return UserLoginService(store, get_audit_sink(), browser_check_login, _login_test_runner())


def get_user_rbay_sessions() -> UserRbaySessions:
    """For IRB jobs a person starts: a session on R-BAY as that person, logged out afterwards."""
    return UserRbaySessions(get_user_login_service())


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


def _scan_fetcher(**extra) -> SafeFetcher:
    """A fetcher with the scan settings (size, timeout, politeness delay, retries)."""
    settings = get_settings()
    return SafeFetcher(
        max_bytes=settings.max_file_bytes,
        timeout=settings.request_timeout_seconds,
        delay=settings.per_domain_delay_seconds,
        retry_waits=settings.fetch_retry_waits,
        retry_budget=settings.fetch_retry_budget,
        **extra,
    )


def build_reader(source: Source) -> SourceReader:
    """One reader per site type; a new site gets a new adapter and one more branch here."""
    settings = get_settings()
    if source.adapter == RBAY_ADAPTER:
        # The login happens inside read(), shared by the hospital's boards (rbay_login.POOL).
        return RbaySourceReader(
            RBAY_POOL,
            get_credential_store(),
            source.config["login_user"],
            source.config["agency"],
            login_site(source),
            source.config["board"],
            settings.max_docs_per_url,
            settings.max_file_bytes,
            lambda session: _scan_fetcher(
                cookies=session.jar(), headers=session.headers(), verify=rbay_ssl_context()
            ),
            _contacts_hook(source),
        )
    if source.adapter != "snuh_hrpp":
        raise ValidationError(f"no adapter named {source.adapter}")
    # One login attempt per scan; LoginRejectedError fails only this source (needs_review).
    fetcher = _scan_fetcher(cookies=cookies_for_source(source, get_credential_store()))
    return SnuhSourceReader(
        SnuhAdapter(fetcher),
        source.config["board_key"],
        settings.max_docs_per_url,
        settings.max_file_bytes,
    )


def get_contact_service() -> ContactService:
    client = get_supabase_client()
    return ContactService(SupabaseContactStore(client), SupabaseAuditSink(client))


def _contacts_hook(source: Source):
    """What an R-BAY reader runs after its board read fine: read the hospital's contact cards
    once per run (a hospital's boards share one run) and save the result, a failure included."""
    hospital_id = source.hospital_id
    if hospital_id is None:
        return None

    def refresh(adapter) -> None:
        service = get_contact_service()  # built only when a reader actually reaches this step
        if not service.should_check(hospital_id):
            return
        try:
            found = adapter.contacts()
        except Exception as exc:  # noqa: BLE001 - recorded with its kind; the scan goes on
            service.record_check(hospital_id, None, exc)
            return
        staff = StaffRead(present=found.has_staff_page)
        if found.has_staff_page:  # the page exists only for a hospital whose start page lists it
            try:
                staff = StaffRead(True, staff_dicts(adapter.staff()))
            except Exception as exc:  # noqa: BLE001 - the cards are still saved; the failure is recorded
                staff = StaffRead(True, error=exc)
        service.record_check(hospital_id, {"phone": found.phone, "fax": found.fax, "email": found.email}, None, staff)

    return refresh


def get_hospital_service() -> HospitalService:
    client = get_supabase_client()
    return HospitalService(SupabaseHospitalStore(client), SupabaseAuditSink(client))


def get_source_service() -> SourceService:
    client = get_supabase_client()
    return SourceService(
        SupabaseSourceStore(client), SupabaseAuditSink(client), hospitals=get_hospital_service()
    )


def _read_rbay_menu(user: str, agency: str, site: str) -> dict[str, str]:
    """The boards of a hospital's e-IRB menu (code -> label), read in a pooled session. An admin
    started this, so a remembered rejected login is tried once more."""
    lease = RBAY_POOL.acquire(user, agency, site, get_credential_store(), person_started=True)
    try:
        return {code: menu.name for code, menu in lease.session.menus.items()}
    finally:
        RBAY_POOL.release(lease.session)


def get_board_sync_service() -> BoardSyncService:
    return BoardSyncService(get_source_service(), get_hospital_service(), _read_rbay_menu)


def get_member_service() -> MemberService:
    client = get_supabase_client()
    return MemberService(
        get_source_service(),
        SupabaseUserStore(client),
        SupabaseMemberStore(client),
        SupabaseAuditSink(client),
    )


@lru_cache
def get_instructions_cache() -> InstructionsCache:
    """One per process: the admins' added AI instructions, re-read at most every 30 seconds."""
    return InstructionsCache(SupabaseInstructionsStore(get_supabase_client()))


def _extra(feature: str) -> Callable[[], str]:
    return get_instructions_cache().provider(feature)


def get_scan_service() -> ScanService:
    client = get_supabase_client()
    return ScanService(
        SupabaseScanRepo(client, get_settings().max_scan_payload_bytes),
        SupabaseFileStorage(client),
        build_reader,
        SupabaseAuditSink(client),
        enricher=build_enricher(extra=_extra("summary")),
    )


def get_manual_service() -> ManualService:
    client = get_supabase_client()
    settings = get_settings()
    return ManualService(
        SupabaseScanRepo(client, settings.max_scan_payload_bytes),
        SupabaseFileStorage(client),
        SupabaseAuditSink(client),
        SupabaseSourceStore(client).get,
        enricher=build_enricher(extra=_extra("summary")),
        max_file_bytes=settings.max_file_bytes,
    )


def get_review_service() -> ReviewService:
    client = get_supabase_client()
    return ReviewService(SupabaseReviewStore(client), SupabaseAuditSink(client))


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
            reminders=get_pipeline_service(),  # IRB step reminders (phase A2)
            calendar=get_calendar_service(),  # cutoff and task lines (CAL-1)
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
        # Failed contact reads, in the daily report; the service is built only when a report runs.
        contact_problems=lambda: get_contact_service().problems(),
    )


@lru_cache
def _all_search_runner() -> ThreadRunner:
    """One worker for every ALL-SEARCH job: an hour-long read must not hold a draft or session
    worker, and two reads never log in to R-BAY at once."""
    return ThreadRunner(workers=1, name="all-search")


def get_all_search_service() -> AllSearchService:
    """ALL-SEARCH (services/session_all.py): the backlog read and the AI search over it."""
    client = get_supabase_client()
    settings = get_settings()
    hospitals = SupabaseHospitalStore(client)
    return AllSearchService(
        SupabaseUserStore(client),
        SupabaseAllSearchStore(client, runner=settings.app_env),
        get_scan_service(),
        _session_service(),
        SupabaseAuditSink(client),
        _all_search_runner(),
        scan_running=lambda: get_runner().is_running(),
        hospital_exists=lambda hospital_id: hospitals.get(hospital_id) is not None,
    )


@lru_cache
def get_daily_session_search() -> DailySessionSearch:
    """The daily AI search for IRB dates after a scan run; off without the AI key and model."""
    client = get_supabase_client()
    return DailySessionSearch(_session_service(), SupabaseDailySearchStore(client), SupabaseAuditSink(client))


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
        after_scan=lambda report: get_daily_session_search().after_run(report),
    )


def get_summary_service() -> SummaryService:
    client = get_supabase_client()
    return SummaryService(
        SupabaseUserStore(client),
        SupabaseSummaryRepo(client),
        build_summarizer(extra=_extra("summary")),
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
        extra_instructions=_extra("agent"),
    )


def get_docgen_service() -> DocgenService:
    """The docs generator. Without OPENAI_API_KEY and OPENAI_MODEL drafts answer 503; guides,
    hand edits and DOCX downloads still work."""
    client = get_supabase_client()
    return DocgenService(
        SupabaseUserStore(client),
        SupabaseDocgenStore(client),
        build_draft_writer(),
        SupabaseAuditSink(client),
        get_settings().docgen_requests_per_day,
        extra_instructions=_extra("docgen"),
        files=SupabaseTemplateFiles(client),
        steps=get_pipeline_service(),  # a pipeline step's own files and AI settings
    )


def get_instructions_service() -> InstructionsService:
    """Admin editing of the added AI instructions. Test runs exist only for the parts that are
    set up (OPENAI_API_KEY and OPENAI_MODEL)."""
    client = get_supabase_client()
    previews: dict[str, Preview] = {}
    agent = get_agent_service()
    if agent.enabled:

        def agent_preview(admin_id, extra, document_id, question):
            answer, tools = agent.preview(admin_id, extra, document_id, question)
            return PreviewResult(answer, f"tools used: {', '.join(tools) or 'none'}")

        previews["agent"] = Preview(
            lambda admin_id, document_id, question: agent.check_preview(question), agent_preview
        )
    docgen = get_docgen_service()
    if docgen.enabled:
        previews["docgen"] = Preview(
            lambda admin_id, document_id, request: docgen.check_preview(admin_id, document_id),
            lambda admin_id, extra, document_id, request: PreviewResult(
                docgen.preview(admin_id, extra, document_id, request)
            ),
        )
    summarizer = build_summarizer()  # the plain one: the test passes the unsaved text itself
    if isinstance(summarizer, OpenAISummarizer):
        previews["summary"] = summary_preview(
            summarizer, lambda document_id: SupabaseDocgenStore(client).get_doc_version(document_id, None)
        )
    return InstructionsService(
        SupabaseUserStore(client),
        SupabaseInstructionsStore(client),
        get_instructions_cache(),
        SupabaseAuditSink(client),
        previews,
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
