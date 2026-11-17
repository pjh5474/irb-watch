"""How many database calls a request makes and how long they take (option C, round two).

Every call to Supabase goes through one httpx session. `install()` adds two hooks to it that count
each call of the request being served and add up the time until the answer's headers arrived. A
middleware (`timing_middleware`) starts the count for each API request, adds a `Server-Timing`
header (shown in the browser's network tab) and logs a warning for a slow request with its call
count. Nothing here changes what a request does.

Not counted: the time to read a big body (a file download), and calls made outside a request (the
scheduler, background jobs).
"""

import logging
import threading
import time
from contextvars import ContextVar

import httpx
from fastapi import Request

log = logging.getLogger(__name__)

SLOW_REQUEST_SECONDS = 1.0
_CALLED = "irb_watch_call_started"


class Stats:
    def __init__(self) -> None:
        self.calls = 0
        self.seconds = 0.0
        self._lock = threading.Lock()

    def add(self, seconds: float) -> None:
        with self._lock:  # a request may run calls on several threads
            self.calls += 1
            self.seconds += seconds


_current: ContextVar[Stats | None] = ContextVar("irb_watch_db_stats", default=None)


def _before(request: httpx.Request) -> None:
    request.extensions[_CALLED] = time.perf_counter()


def _after(response: httpx.Response) -> None:
    stats = _current.get()
    started = response.request.extensions.get(_CALLED)
    if stats is not None and started is not None:
        stats.add(time.perf_counter() - started)


def install(session: httpx.Client) -> None:
    """Count the calls made through this httpx session (once; a second call changes nothing)."""
    if _before in session.event_hooks["request"]:
        return
    session.event_hooks["request"] = [*session.event_hooks["request"], _before]
    session.event_hooks["response"] = [*session.event_hooks["response"], _after]


def server_timing(stats: Stats, total: float) -> str:
    return f'app;dur={total * 1000:.0f}, db;dur={stats.seconds * 1000:.0f};desc="{stats.calls} calls"'


async def timing_middleware(request: Request, call_next):
    stats = Stats()
    token = _current.set(stats)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        _current.reset(token)
    total = time.perf_counter() - started
    response.headers["Server-Timing"] = server_timing(stats, total)
    if total >= SLOW_REQUEST_SECONDS:
        log.warning(
            "slow request %s %s: %.2f s, %d database calls took %.2f s",
            request.method, request.url.path, total, stats.calls, stats.seconds,
        )
    return response
