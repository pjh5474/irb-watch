"""Plain types and the storage and model protocols of the docs generator.

The Supabase implementation is in db/docgen.py; tests use in-memory fakes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from irb_watch.core.state import VersionContent

ORIGINS = ("ai", "user_edit", "ai_revision")


class VersionConflictError(Exception):
    """Another save of the same draft won the race for the next version number."""


@dataclass(frozen=True)
class SourceRef:
    id: int
    name: str
    ai_enabled: bool


@dataclass(frozen=True)
class DocVersion:
    """One stored document version, and the one before it (for the list of changes)."""

    document_id: int
    source_id: int
    title: str
    version_no: int
    content: VersionContent
    previous: VersionContent | None


@dataclass(frozen=True)
class Guide:
    id: int
    title: str
    filename: str
    body_text: str
    created_at: str
    archived_at: str | None = None
    source_ids: tuple[int, ...] = ()  # sources that use it by default


@dataclass(frozen=True)
class DocRef:
    document_id: int
    version_no: int
    source_id: int


@dataclass(frozen=True)
class ExtraGuide:
    """A guide file the user attached to one draft (text only)."""

    filename: str
    text: str


@dataclass(frozen=True)
class Draft:
    id: str
    user_id: str
    title: str
    instruction: str
    document_refs: tuple[DocRef, ...]
    guide_ids: tuple[int, ...]
    extra_guides: tuple[ExtraGuide, ...]
    ai_override_source_ids: tuple[int, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DraftVersion:
    draft_id: str
    version_no: int
    markdown: str
    origin: str  # one of ORIGINS
    instruction: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class NewDraft:
    user_id: str
    title: str
    instruction: str
    document_refs: tuple[DocRef, ...]
    guide_ids: tuple[int, ...]
    extra_guides: tuple[ExtraGuide, ...] = ()
    ai_override_source_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Written:
    title: str
    markdown: str


class DocgenStore(Protocol):
    # -- what a user may read ------------------------------------------------------
    def readable_sources(self, user_id: str, is_admin: bool) -> dict[int, SourceRef]:
        """Every source the user may read (same rule as private.can_read_source), INCLUDING
        sources with AI off; the service decides what to do with those."""
        ...

    def get_source(self, source_id: int) -> SourceRef | None: ...

    def get_doc_version(self, document_id: int, version_no: int | None) -> DocVersion | None:
        """version_no None = the newest."""
        ...

    # -- guides ----------------------------------------------------------------------
    def create_guide(
        self, title: str, filename: str, body_text: str, sha256: str, created_by: str, now: datetime
    ) -> Guide: ...
    def list_guides(self, include_archived: bool) -> list[Guide]: ...
    def get_guides(self, guide_ids: list[int]) -> list[Guide]: ...
    def guides_for_sources(self, source_ids: list[int]) -> list[Guide]:
        """Non-archived guides linked to any of these sources."""
        ...

    def archive_guide(self, guide_id: int, now: datetime) -> None:
        """Set archived_at and remove all its links."""
        ...

    def link_guide(self, source_id: int, guide_id: int, now: datetime) -> None: ...
    def unlink_guide(self, source_id: int, guide_id: int) -> None: ...

    # -- drafts ----------------------------------------------------------------------
    def create_draft(self, new: NewDraft, first: DraftVersion, now: datetime) -> Draft: ...
    def get_draft(self, draft_id: str) -> Draft | None: ...
    def list_drafts(self, user_id: str, limit: int) -> list[Draft]: ...
    def list_versions(self, draft_id: str) -> list[DraftVersion]: ...
    def add_version(
        self, draft: Draft, markdown: str, origin: str, instruction: str, now: datetime
    ) -> DraftVersion: ...
    def delete_draft(self, draft_id: str) -> None: ...
    def count_requests_since(self, user_id: str, since: datetime) -> int:
        """AI calls (docgen.generate + docgen.revise audit entries) since `since`."""
        ...

    def purge_before(self, cutoff: datetime) -> int: ...


class DraftWriter(Protocol):
    def write(self, instructions: str, input_text: str) -> Written: ...
