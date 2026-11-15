"""Shared helpers for long Supabase reads: id chunks for `in` filters and paged queries."""

from collections.abc import Callable
from typing import Any

PAGE = 1000  # the API answers at most 1,000 rows per request
IN_CHUNK = 200  # ids per `in` filter, so the request address stays short


def chunks(ids: list) -> list[list]:
    return [ids[i : i + IN_CHUNK] for i in range(0, len(ids), IN_CHUNK)]


def paged(make_query: Callable[[], Any]) -> list[dict]:
    """Every row of an ordered query: a cut answer would look like real data."""
    out: list[dict] = []
    start = 0
    while True:
        rows = make_query().range(start, start + PAGE - 1).execute().data
        out += rows
        if len(rows) < PAGE:
            return out
        start += PAGE
