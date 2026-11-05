from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from irb_watch.api import admin_users, health
from irb_watch.config import get_settings
from irb_watch.services.users import UserServiceError

app = FastAPI(title="IRB Watch")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().frontend_origin_list,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(UserServiceError)
async def user_service_error_handler(_: Request, exc: UserServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


app.include_router(health.router)
app.include_router(admin_users.router)
