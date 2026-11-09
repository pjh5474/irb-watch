from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from irb_watch.api import (
    admin_credentials,
    admin_documents,
    admin_mail,
    admin_members,
    admin_scans,
    admin_sources,
    admin_users,
    documents,
    files,
    health,
)
from irb_watch.config import get_settings
from irb_watch.services.errors import ServiceError


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the daily scan schedule when SCHEDULER_ENABLED=true; stop it on shutdown."""
    settings = get_settings()
    scheduler = None
    if settings.scheduler_enabled:
        from irb_watch.api import deps
        from irb_watch.scheduler import ScanScheduler

        scheduler = ScanScheduler(deps.get_runner(), settings.scan_cron, settings.scan_timezone)
        deps.retry_hook[0] = scheduler.schedule_retry
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()


app = FastAPI(title="IRB Watch", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().frontend_origin_list,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


app.include_router(health.router)
app.include_router(admin_users.router)
app.include_router(admin_sources.router)
app.include_router(admin_scans.router)
app.include_router(admin_credentials.router)
app.include_router(admin_documents.router)
app.include_router(admin_members.router)
app.include_router(admin_mail.router)
app.include_router(documents.router)
app.include_router(files.router)
