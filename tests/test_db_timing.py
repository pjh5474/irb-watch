"""Per-request database call counting (db/timing.py)."""

import logging

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from irb_watch.db import timing


def session():
    s = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])))
    timing.install(s)
    return s


def make_app(db, sleep=0.0):
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=timing.timing_middleware)

    @app.get("/two")
    def two():  # a sync endpoint, like the real ones: it runs in a worker thread
        db.get("http://x/a")
        db.get("http://x/b")
        return {"ok": True}

    @app.get("/none")
    def none():
        return {"ok": True}

    return app


def test_calls_of_a_request_are_counted_and_reported():
    db = session()
    response = TestClient(make_app(db)).get("/two")
    header = response.headers["Server-Timing"]
    assert 'desc="2 calls"' in header and header.startswith("app;dur=") and "db;dur=" in header


def test_a_request_with_no_database_call_says_zero():
    response = TestClient(make_app(session())).get("/none")
    assert 'desc="0 calls"' in response.headers["Server-Timing"]


def test_requests_do_not_share_counts():
    db = session()
    client = TestClient(make_app(db))
    client.get("/two")
    assert 'desc="2 calls"' in client.get("/two").headers["Server-Timing"]  # not 4


def test_calls_outside_a_request_are_ignored_and_do_not_fail():
    db = session()
    assert db.get("http://x/a").status_code == 200  # no request context: nothing to count, no error


def test_installing_twice_counts_each_call_once():
    db = session()
    timing.install(db)
    assert 'desc="2 calls"' in TestClient(make_app(db)).get("/two").headers["Server-Timing"]


def test_a_slow_request_is_logged_with_its_call_count(caplog, monkeypatch):
    monkeypatch.setattr(timing, "SLOW_REQUEST_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger="irb_watch.db.timing"):
        TestClient(make_app(session())).get("/two")
    assert any("slow request GET /two" in m and "2 database calls" in m for m in caplog.messages)


def test_a_fast_request_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="irb_watch.db.timing"):
        TestClient(make_app(session())).get("/none")
    assert caplog.messages == []


def test_the_header_text_format():
    stats = timing.Stats()
    stats.add(0.25)
    stats.add(0.05)
    assert timing.server_timing(stats, 0.4) == 'app;dur=400, db;dur=300;desc="2 calls"'
