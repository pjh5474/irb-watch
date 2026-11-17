"""Independent reads run together (services/parallel.py)."""

import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from irb_watch.db import timing
from irb_watch.services.parallel import together


def test_results_come_back_in_the_order_given():
    def slow():
        time.sleep(0.05)
        return "slow"

    assert together(slow, lambda: "fast", lambda: 3) == ["slow", "fast", 3]


def test_calls_really_overlap():
    barrier = threading.Barrier(3, timeout=2)  # passes only when all three run at the same time

    def wait():
        barrier.wait()
        return True

    assert together(wait, wait, wait) == [True, True, True]


def test_one_call_runs_inline():
    here = threading.get_ident()
    assert together(threading.get_ident) == [here]


def test_the_first_failure_in_order_is_raised_after_the_others_finished():
    done = []

    def first():
        time.sleep(0.05)
        raise ValueError("first")

    def second():
        raise KeyError("second")

    def third():
        time.sleep(0.1)
        done.append(True)
        return 1

    with pytest.raises(ValueError, match="first"):  # the order given, not the order they failed
        together(first, second, third)
    assert done == [True]  # the others were not abandoned


def test_calls_are_counted_by_the_request_timing():
    s = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])))
    timing.install(s)
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=timing.timing_middleware)

    @app.get("/x")
    def x():
        together(lambda: s.get("http://x/a"), lambda: s.get("http://x/b"), lambda: s.get("http://x/c"))
        return {}

    assert 'desc="3 calls"' in TestClient(app).get("/x").headers["Server-Timing"]
