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


@dataclass(frozen=True)
class ContactInfo:
    """One hospital's IRB office contacts as the screens show them (a typed text wins)."""

    hospital_id: int
    hospital_name: str
    phone: str
    fax: str
    email: str
    from_admin: tuple[str, ...]  # fields an admin typed (the rest were read from R-BAY)
    check_status: str  # never | ok | failed
    checked_at: str | None
    error_kind: str | None  # a short kind only; the raw reason is for admins
    # The manager page: [{"tab", "entries": [{"title", "name", "email", "tel"}]}] and its status
    # (never | none | ok | failed; failed means the staff shown is the last good read).
    staff: tuple[dict, ...] = ()
    staff_status: str = "never"


class AgentData(Protocol):
    def hospital_contacts(self, name_contains: str | None, limit: int) -> list[ContactInfo]:
        """Hospitals (by a name, short forms and other spellings tolerated, or the first ones)
        with their contacts. Not filtered by a board's AI switch or membership: contacts are
        readable by every signed-in user."""
        ...

    def hospital_names(self) -> list[tuple[int, str]]:
        """Every hospital (id, name): offered back when a name matched nothing."""
        ...

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
