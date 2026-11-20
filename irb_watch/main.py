import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from irb_watch.api import (
    admin_agent,
    admin_ai,
    admin_all_search,
    admin_contacts,
    admin_credentials,
    admin_documents,
    admin_guides,
    admin_hospitals,
    admin_mail,
    admin_manual,
    admin_members,
    admin_organizations,
    admin_pipeline,
    admin_scans,
    admin_sources,
    admin_templates,
    admin_users,
    agent,
    auth_token,
    calendar,
    docgen,
    documents,
    files,
    health,
    me_rbay_login,
    pipelines,
    projects,
    sessions,
    sites,
    tasks,
    unsubscribe,
    work,
)
from irb_watch.config import get_settings
from irb_watch.db.timing import timing_middleware
from irb_watch.services.errors import ServiceError


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the daily scan schedule when SCHEDULER_ENABLED=true; stop it on shutdown."""
    settings = get_settings()
    scheduler = None
    from irb_watch.api import deps

    # Draft jobs run inside this process and keep their inputs in memory: one that was open
    # when the last run stopped cannot continue and is marked failed with a reason. This
    # assumes ONE API process; a second one would fail the first one's jobs at its start.
    try:
        deps.get_draft_job_service().fail_unfinished()
    except Exception:
        logging.getLogger(__name__).exception("open draft jobs could not be checked at start")
    try:  # the same for the AI session searches (CAL-1)
        deps.get_session_service().fail_unfinished()
    except Exception:
        logging.getLogger(__name__).exception("open session jobs could not be checked at start")
    try:  # and for the users' R-BAY login tests
        deps.get_user_login_service().fail_unfinished()
    except Exception:
        logging.getLogger(__name__).exception("open R-BAY login tests could not be checked at start")
    try:  # and for ALL-SEARCH (the posts a read stored are kept)
        deps.get_all_search_service().fail_unfinished()
    except Exception:
        logging.getLogger(__name__).exception("open all-search jobs could not be checked at start")
    if settings.scheduler_enabled:
        from irb_watch.scheduler import ScanScheduler

        weekly = deps.get_weekly_service()
        agent_service = deps.get_agent_service()
        docgen_service = deps.get_docgen_service()
        pipeline_service = deps.get_pipeline_service()
        site_service = deps.get_site_service()
        job_service = deps.get_draft_job_service()
        session_service = deps.get_session_service()
        all_search_service = deps.get_all_search_service()
        task_service = deps.get_task_service()

        def purge_old() -> None:
            # All run every time; each one logs and swallows its own failure. Sites after
            # pipelines: a removed site goes only once no pipeline row points at it.
            agent_service.purge_old()
            docgen_service.purge_old()
            pipeline_service.purge_old()
            site_service.purge_old()
            site_service.apply_due_pi()  # scheduled PI changes whose day has come
            job_service.purge_old()
            session_service.purge_old()  # finished session searches after 30 days
            all_search_service.purge_old()  # finished ALL-SEARCH jobs after 30 days
            task_service.purge_old()  # removed project tasks after 30 days

        scheduler = ScanScheduler(
            deps.get_runner(),
            settings.scan_cron,
            settings.scan_timezone,
            # The weekly stats mail only exists when mail is set up.
            weekly=weekly.send_scheduled if weekly.enabled else None,
            weekly_cron=settings.weekly_stats_cron,
            # Old assistant conversations and drafts are deleted after 30 days (needs no model
            # or mail).
            purge_chats=purge_old,
        )
        deps.retry_hook[0] = scheduler.schedule_retry
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()


app = FastAPI(title="IRB Watch", lifespan=lifespan)

# Added before CORS so CORS wraps it: a refused upload still reaches the browser with its reason.
app.add_middleware(BaseHTTPMiddleware, dispatch=admin_manual.limit_upload_size)
# Counts each request's database calls: a Server-Timing header and a warning for slow requests.
app.add_middleware(BaseHTTPMiddleware, dispatch=timing_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().frontend_origin_list,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 echoes each bad value back ("input"). That would send a password
    (e.g. one that is too long) straight back in the body, so the values are left out: the
    answer names the field and the problem only."""
    detail = [{"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message},
        headers=getattr(exc, "headers", None),
    )


app.include_router(health.router)
app.include_router(auth_token.router)
app.include_router(admin_users.router)
app.include_router(admin_organizations.router)
app.include_router(admin_pipeline.router)
# Before admin_sources: "/admin/sources/manual" must not be read as a source id.
app.include_router(admin_manual.router)
app.include_router(admin_sources.router)
app.include_router(admin_hospitals.router)
app.include_router(admin_contacts.router)
app.include_router(admin_scans.router)
app.include_router(admin_credentials.router)
app.include_router(admin_documents.router)
app.include_router(admin_members.router)
app.include_router(admin_mail.router)
app.include_router(admin_agent.router)
app.include_router(agent.router)
app.include_router(docgen.router)
app.include_router(admin_guides.router)
app.include_router(admin_templates.router)
app.include_router(admin_ai.router)
app.include_router(documents.router)
app.include_router(files.router)
app.include_router(me_rbay_login.router)
app.include_router(pipelines.router)
app.include_router(work.router)
app.include_router(projects.router)
app.include_router(sites.router)
app.include_router(sessions.router)
app.include_router(sessions.admin_router)
app.include_router(admin_all_search.router)
app.include_router(tasks.router)
app.include_router(calendar.router)
app.include_router(unsubscribe.router)
