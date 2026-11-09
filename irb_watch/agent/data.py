"""What the assistant's tools may read, as small plain types and one protocol.

The Supabase implementation is in db/agent.py; tests use an in-memory fake.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from irb_watch.core.state import VersionContent


@dataclass(frozen=True)
class SourceInfo:
    id: int
    name: str
    is_active: bool
    last_scan_at: str | None
    last_scan_status: str | None
    requires_login: bool


@dataclass(frozen=True)
class VersionInfo:
    version_no: int
    created_at: str
    change_type: str
    summary: str | None
    priority: str | None


@dataclass(frozen=True)
class DocumentInfo:
    id: int
    source_id: int
    title: str
    status: str
    posted_on: str | None
    source_url: str
    versions: tuple[VersionInfo, ...]
    file_names: tuple[str, ...]  # files of the newest version


@dataclass(frozen=True)
class DocumentBrief:
    id: int
    title: str
    status: str
    posted_on: str | None
    first_seen_at: str


@dataclass(frozen=True)
class ChangeInfo:
    created_at: str
    event_type: str
    source_id: int | None
    document_id: int | None
    title: str | None
    summary: str | None
    priority: str | None


class AgentData(Protocol):
    def visible_sources(self, user_id: str, is_admin: bool) -> list[SourceInfo]:
        """Sources the assistant may use for this user: readable by them AND with AI switched
        on. A source with AI off is hidden here entirely, so its text never reaches the model."""
        ...

    def get_source(self, source_id: int) -> SourceInfo | None:
        """Any source, with no access or AI filter. Only used to name an admin's proposed scan."""
        ...

    def get_document(self, document_id: int) -> DocumentInfo | None: ...

    def get_version(self, document_id: int, version_no: int | None) -> VersionContent | None:
        """The text of one version (None = the newest)."""
        ...

    def list_documents(
        self, source_id: int, title_contains: str | None, limit: int, offset: int
    ) -> list[DocumentBrief]: ...

    def changes(self, source_ids: list[int], since: datetime, limit: int) -> list[ChangeInfo]: ...
