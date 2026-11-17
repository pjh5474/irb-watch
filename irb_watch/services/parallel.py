"""Independent database reads of one request, run at the same time (option C, round two).

A page that makes many reads one after another waits for each answer. Reads that do not depend
on each other can start together; the page then waits for the slowest instead of the sum.

  * `together(*calls)` runs the calls on a shared pool and returns their results in the order
    given. When some fail, all the others still finish, then the first failure in the order
    given is raised (the same error a sequential run would have raised first).
  * Each call runs in a copy of the caller's context, so the per-request call counter
    (db/timing.py) still counts them.
  * Only reads belong here. A call never starts another `together` (the pool is shared, so
    waiting inside it could stall); the callers keep to one level.
"""

import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="irb-read")


def together(*calls: Callable[[], Any]) -> list[Any]:
    if len(calls) == 1:
        return [calls[0]()]
    futures = [_POOL.submit(contextvars.copy_context().run, call) for call in calls]
    results: list[Any] = []
    failure: BaseException | None = None
    for future in futures:
        try:
            results.append(future.result())
        except BaseException as exc:  # noqa: BLE001 - re-raised below, after every call finished
            results.append(None)
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure
    return results
