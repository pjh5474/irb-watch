"""A short-lived cache for the admin setup lists that every pipeline page reads.

Statuses, results, step types, pipeline orders and session kinds change when an admin edits
them (rare), yet each step page used to read them again. `CachedStore` wraps a store and keeps
the answer of its no-argument list reads for `ttl` seconds.

  * Every write method of the store clears the cache before and after it runs, so an edit made
    through this server shows at once. A read that started before the write cannot put its old
    answer back (a generation number is checked before storing).
  * An edit made straight in the database shows after at most `ttl` seconds.
  * The cache lives in this process. With a second API process, an edit made on the other one
    shows after `ttl` seconds, not at once (one API process is assumed today).
  * The lists are platform-wide today. When setup becomes per organization, the cache key must
    get the organization id (see CLAUDE.md, SaaS-ready).
  * Callers get a copy of the list; the rows are frozen dataclasses.

Every method of a wrapped store must be named as a cached read, a write or a plain pass-through
read; tests/test_read_cache.py fails when a new store method is in none of the three.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

# 10 minutes (owner, 2026-11-17; was 60 s): an edit made through this server clears the cache at
# once, so only an edit made straight in the database waits this long. A user who pauses between
# clicks used to meet an empty cache on most pages (4 extra calls, about 0.15 s).
DEFAULT_TTL_SECONDS = 600.0


class ReadCache:
    """Values by name with an age limit; `clear()` forgets everything. Shared by the wrappers
    of one family of stores, so a write through any of them clears all."""

    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, Any]] = {}
        self._generation = 0

    def get(self, name: str, load: Callable[[], Any]) -> Any:
        now = self._clock()
        with self._lock:
            hit = self._values.get(name)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
            generation = self._generation
        value = load()  # outside the lock: a slow read must not block the others
        with self._lock:
            if generation == self._generation:  # nothing was written meanwhile
                self._values[name] = (self._clock(), value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._values.clear()


class CachedStore:
    """Wraps `store`: `reads` (no arguments, return a list) are cached, `writes` clear the cache,
    every other attribute passes straight through."""

    def __init__(self, store: Any, cache: ReadCache, reads: frozenset[str], writes: frozenset[str]) -> None:
        self._store = store
        self._cache = cache
        self._reads = reads
        self._writes = writes

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._store, name)
        if name in self._reads:
            return lambda: list(self._cache.get(name, attr))
        if name in self._writes:

            def write(*args: Any, **kwargs: Any) -> Any:
                self._cache.clear()
                try:
                    return attr(*args, **kwargs)
                finally:
                    self._cache.clear()  # also after a failed write: it may have half-applied

            return write
        return attr


# One cache per family, shared by every wrapper made in this process.
SETUP_CACHE = ReadCache()
SETUP_READS = frozenset({"statuses", "results", "step_types", "templates"})
SETUP_WRITES = frozenset({
    "add_status", "update_status", "add_result", "update_result", "create_step_type", "update_step_type",
    "save_template", "delete_template", "save_document_kinds", "delete_document_kinds",
})

SESSION_CACHE = ReadCache()
SESSION_READS = frozenset({"kinds"})
SESSION_WRITES = frozenset({"add_kind", "update_kind"})


def cached_setup_store(store: Any) -> Any:
    return CachedStore(store, SETUP_CACHE, SETUP_READS, SETUP_WRITES)


def cached_session_store(store: Any) -> Any:
    return CachedStore(store, SESSION_CACHE, SESSION_READS, SESSION_WRITES)
