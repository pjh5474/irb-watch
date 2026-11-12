import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from irb_watch.api import (
    admin_agent,
    admin_ai,
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
    docgen,
    documents,
    files,
    health,
    pipelines,
    unsubscribe,
)
from irb_watch.config import get_settings
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
    if settings.scheduler_enabled:
        from irb_watch.scheduler import ScanScheduler

        weekly = deps.get_weekly_service()
        agent_service = deps.get_agent_service()
        docgen_service = deps.get_docgen_service()
        pipeline_service = deps.get_pipeline_service()
        job_service = deps.get_draft_job_service()

        def purge_old() -> None:
            # All run every time; each one logs and swallows its own failure.
            agent_service.purge_old()
            docgen_service.purge_old()
            pipeline_service.purge_old()
            job_service.purge_old()

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().frontend_origin_list,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


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
app.include_router(pipelines.router)
app.include_router(unsubscribe.router)
