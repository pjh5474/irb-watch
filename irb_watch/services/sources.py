"""Admin-managed list of sources (URLs to scan)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from irb_watch.security.url_guard import UnsafeURLError, validate_url
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.users import AuditSink

SNUH_HOST = "hrpp.snuh.org"
SNUH_LIST_PATH = "/hrpp/bbs/list.do"


@dataclass(frozen=True)
class Source:
    id: int
    name: str
    adapter: str
    url: str
    config: dict
    needs_browser: bool
    is_active: bool
    created_at: str
    last_scan_at: str | None = None
    last_scan_status: str | None = None
    last_scan_error: str | None = None
    scan_started_at: str | None = None


class DuplicateSourceError(Exception):
    """Raised by SourceStore.create when the URL is already registered."""


class SourceStore(Protocol):
    def create(
        self, name: str, adapter: str, url: str, config: dict, created_by: str | None
    ) -> Source: ...
    def get(self, source_id: int) -> Source | None: ...
    def list(self) -> list[Source]: ...
    def update(self, source_id: int, values: dict) -> Source: ...


def detect_adapter(url: str) -> tuple[str, str, dict]:
    """Return (adapter, canonical_url, config) or raise ValidationError for unknown sites."""
    parts = urlsplit(url.strip())
    if (parts.hostname or "").lower() == SNUH_HOST and parts.path == SNUH_LIST_PATH:
        key = (parse_qs(parts.query).get("key") or [""])[0]
        if key.isdigit():
            canonical = f"https://{SNUH_HOST}{SNUH_LIST_PATH}?key={key}"
            return "snuh_hrpp", canonical, {"board_key": key}
        raise ValidationError("the SNUH board link needs a numeric ?key=... parameter")
    raise ValidationError("no adapter for this site yet (supported: hrpp.snuh.org boards)")


class SourceService:
    def __init__(
        self,
        store: SourceStore,
        audit: AuditSink,
        url_checker: Callable[[str], str] = validate_url,
    ) -> None:
        self._store = store
        self._audit = audit
        self._check_url = url_checker

    def create(self, actor_id: str | None, name: str, url: str) -> Source:
        name = name.strip()
        if not name:
            raise ValidationError("name must not be empty")
        adapter, canonical, config = detect_adapter(url)
        try:
            self._check_url(canonical)
        except UnsafeURLError as exc:
            raise ValidationError(f"URL is not allowed: {exc}") from exc
        try:
            source = self._store.create(name, adapter, canonical, config, actor_id)
        except DuplicateSourceError as exc:
            raise ConflictError("this URL is already registered") from exc
        self._audit.record(
            "admin",
            "source.created",
            actor_id,
            title=f"Source {name} added",
            payload={"url": canonical, "adapter": adapter},
            source_id=source.id,
        )
        return source

    def list(self) -> list[Source]:
        return self._store.list()

    def get(self, source_id: int) -> Source:
        source = self._store.get(source_id)
        if source is None:
            raise NotFoundError("source not found")
        return source

    def update(
        self,
        actor_id: str | None,
        source_id: int,
        name: str | None = None,
        is_active: bool | None = None,
    ) -> Source:
        self.get(source_id)
        values: dict = {}
        if name is not None:
            if not name.strip():
                raise ValidationError("name must not be empty")
            values["name"] = name.strip()
        if is_active is not None:
            values["is_active"] = is_active
        if not values:
            return self.get(source_id)
        source = self._store.update(source_id, values)
        self._audit.record(
            "admin",
            "source.updated",
            actor_id,
            title=f"Source {source.name} updated",
            payload=values,
            source_id=source_id,
        )
        return source
