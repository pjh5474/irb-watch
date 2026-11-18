"""The setup-list cache (db/read_cache.py): hits, expiry, clearing on writes, and that every
store method is classified."""

import threading

from irb_watch.db import read_cache as rc
from irb_watch.services.pipeline_admin import PipelineSetupStore
from irb_watch.services.sessions import SessionStore


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeStore:
    def __init__(self) -> None:
        self.rows = ["a"]
        self.reads = 0
        self.arg_reads = 0

    def statuses(self):
        self.reads += 1
        return list(self.rows)

    def get_one(self, key):  # a read with an argument: never cached
        self.arg_reads += 1
        return key

    def add_status(self, value):
        self.rows.append(value)
        return value

    def broken_write(self):
        raise RuntimeError("boom")


def make(ttl=60.0):
    clock = Clock()
    cache = rc.ReadCache(ttl, clock)
    store = FakeStore()
    wrapped = rc.CachedStore(store, cache, frozenset({"statuses"}), frozenset({"add_status", "broken_write"}))
    return clock, cache, store, wrapped


def test_second_read_comes_from_the_cache():
    _, _, store, wrapped = make()
    assert wrapped.statuses() == ["a"]
    assert wrapped.statuses() == ["a"]
    assert store.reads == 1


def test_a_caller_cannot_change_the_cached_list():
    _, _, store, wrapped = make()
    wrapped.statuses().append("x")
    assert wrapped.statuses() == ["a"]


def test_the_cache_expires_after_the_age_limit():
    clock, _, store, wrapped = make(ttl=60)
    wrapped.statuses()
    clock.now += 59
    wrapped.statuses()
    assert store.reads == 1
    clock.now += 2
    wrapped.statuses()
    assert store.reads == 2


def test_a_write_clears_the_cache_at_once():
    _, _, store, wrapped = make()
    assert wrapped.statuses() == ["a"]
    wrapped.add_status("b")
    assert wrapped.statuses() == ["a", "b"]
    assert store.reads == 2


def test_a_failed_write_also_clears_the_cache():
    _, cache, store, wrapped = make()
    wrapped.statuses()
    try:
        wrapped.broken_write()
    except RuntimeError:
        pass
    wrapped.statuses()
    assert store.reads == 2


def test_other_methods_pass_straight_through_and_do_not_clear():
    _, _, store, wrapped = make()
    wrapped.statuses()
    assert wrapped.get_one(5) == 5
    assert wrapped.get_one(6) == 6
    wrapped.statuses()
    assert store.arg_reads == 2
    assert store.reads == 1


def test_a_write_through_another_wrapper_of_the_same_cache_clears_it():
    _, cache, store, first = make()
    second = rc.CachedStore(store, cache, frozenset({"statuses"}), frozenset({"add_status"}))
    first.statuses()
    second.add_status("b")
    assert first.statuses() == ["a", "b"]


def test_a_failed_read_is_not_cached():
    cache = rc.ReadCache(60, Clock())
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("down")
        return ["ok"]

    try:
        cache.get("x", load)
    except RuntimeError:
        pass
    assert cache.get("x", load) == ["ok"]


def test_a_read_that_started_before_a_write_cannot_store_its_old_answer():
    cache = rc.ReadCache(60, Clock())
    started = threading.Event()
    go = threading.Event()
    old = ["old"]

    def slow_load():
        started.set()
        go.wait(2)
        return old

    out = []
    reader = threading.Thread(target=lambda: out.append(cache.get("x", slow_load)))
    reader.start()
    started.wait(2)
    cache.clear()  # the write happens while the read is still running
    go.set()
    reader.join(2)
    assert out == [old]
    assert cache.get("x", lambda: ["new"]) == ["new"]  # the old answer was not kept


def _public(protocol) -> set[str]:
    return {n for n in vars(protocol) if not n.startswith("_") and callable(getattr(protocol, n))}


# Reads that stay uncached on purpose (they take arguments or are rare).
SETUP_PASS = {
    "get_step_type", "live_steps_of_type", "live_guide_ids", "form_template_ready", "hospital_exists", "document_kinds",
}
SESSION_PASS = {
    "get", "sessions", "between", "same_day", "proposals", "create", "update", "version_hospital", "version_source", "posts_for",
    "version_content", "create_job", "get_job", "jobs", "open_jobs_of_user", "update_job", "delete_job",
    "fail_open_jobs", "purge_jobs",
}


def test_every_setup_store_method_is_classified():
    known = rc.SETUP_READS | rc.SETUP_WRITES | SETUP_PASS
    assert _public(PipelineSetupStore) - known == set()
    assert (rc.SETUP_READS | rc.SETUP_WRITES) <= _public(PipelineSetupStore)


def test_every_session_store_method_is_classified():
    known = rc.SESSION_READS | rc.SESSION_WRITES | SESSION_PASS
    assert _public(SessionStore) - known == set()
    assert (rc.SESSION_READS | rc.SESSION_WRITES) <= _public(SessionStore)
