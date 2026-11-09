"""POST /auth/token: sign in and get a bearer token, for the Authorize button of the API docs.

The docs page (/docs) declares this as the token URL of an OAuth2 "password" flow, so its
Authorize button asks for an email and a password, calls this endpoint, and then sends the token
with every Try-it-out request. The same email and password as the web app's login.

The body is a standard form (application/x-www-form-urlencoded). It is read by hand, small and
size-capped, so no form-parsing package is needed.
"""

from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from irb_watch.api.deps import get_token_login_service
from irb_watch.services.errors import ValidationError
from irb_watch.services.token_login import TokenLoginService

router = APIRouter(tags=["auth"])

MAX_BODY_BYTES = 4_096

_FORM_SCHEMA = {
    "required": True,
    "content": {
        "application/x-www-form-urlencoded": {
            "schema": {
                "type": "object",
                "required": ["username", "password"],
                "properties": {
                    "grant_type": {"type": "string", "default": "password"},
                    "username": {"type": "string", "description": "your email address"},
                    "password": {"type": "string", "format": "password"},
                },
            }
        }
    },
}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def client_ip(request: Request) -> str:
    """The caller's address. Behind Caddy the last X-Forwarded-For entry is the real client."""
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip()
    if last:
        return last
    return request.client.host if request.client else "unknown"


@router.post("/auth/token", response_model=TokenOut, openapi_extra={"requestBody": _FORM_SCHEMA})
async def issue_token(
    request: Request, service: TokenLoginService = Depends(get_token_login_service)
) -> JSONResponse:
    """Sign in with your email (username) and password; returns a bearer token (about 1 hour).

    Wrong guesses are limited: after 5 failures the answer is 429 with Retry-After."""
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise ValidationError("send the email and password as a form (application/x-www-form-urlencoded)")
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise ValidationError("the request is too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValidationError("the request is too large")
    try:
        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        raise ValidationError("the form is not valid UTF-8") from None

    token = await run_in_threadpool(
        service.issue,
        form.get("username", [""])[0],
        form.get("password", [""])[0],
        client_ip(request),
    )
    return JSONResponse(
        TokenOut(access_token=token.access_token, expires_in=token.expires_in).model_dump(),
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
