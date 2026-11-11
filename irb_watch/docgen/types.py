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
    """One stored document version, and the version it is compared with (for the list of
    changes): by default the one just before it.

    files / page_text: what of the post the user chose to send (files None = every file).
    previous_no: the number of `previous`; compare False = the user chose no comparison."""

    document_id: int
    source_id: int
    title: str
    version_no: int
    content: VersionContent
    previous: VersionContent | None
    files: tuple[str, ...] | None = None
    page_text: bool = True
    previous_no: int | None = None
    compare: bool = True


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
    """One chosen post of a draft. files None = every attached file. compare_with None = the
    version just before; compare False = no list of changes."""

    document_id: int
    version_no: int
    source_id: int
    files: tuple[str, ...] | None = None
    page_text: bool = True
    compare_with: int | None = None
    compare: bool = True


def ref_to_json(r: DocRef) -> dict:
    return {
        "document_id": r.document_id,
        "version_no": r.version_no,
        "source_id": r.source_id,
        "files": list(r.files) if r.files is not None else None,
        "page_text": r.page_text,
        "compare_with": r.compare_with,
        "compare": r.compare,
    }


def ref_from_json(r: dict) -> DocRef:
    """Rows saved before file choice existed have only the first three keys."""
    files = r.get("files")
    compare_with = r.get("compare_with")
    return DocRef(
        int(r["document_id"]),
        int(r["version_no"]),
        int(r["source_id"]),
        tuple(str(f) for f in files) if files is not None else None,
        bool(r.get("page_text", True)),
        int(compare_with) if compare_with is not None else None,
        bool(r.get("compare", True)),
    )


@dataclass(frozen=True)
class ExtraGuide:
    """A guide file the user attached to one draft (text only)."""

    filename: str
    text: str


@dataclass(frozen=True)
class Inputs:
    """What one AI version was written from. Saved on that version (guide files by name only)
    and, for the newest AI version, as the draft's current selection (with guide file text)."""

    document_refs: tuple[DocRef, ...]
    guide_ids: tuple[int, ...]
    extra_guides: tuple[ExtraGuide, ...]
    ai_override_source_ids: tuple[int, ...]

    def to_json(self) -> dict:
        return {
            "documents": [ref_to_json(r) for r in self.document_refs],
            "guide_ids": list(self.guide_ids),
            "extra_guides": [e.filename for e in self.extra_guides],
            "ai_override_source_ids": list(self.ai_override_source_ids),
        }


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
    inputs: dict | None = None  # Inputs.to_json() for AI versions; None for hand edits and old rows


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

    def get_doc_version(
        self, document_id: int, version_no: int | None, previous: bool = True
    ) -> DocVersion | None:
        """version_no None = the newest. previous True also reads the version just before it
        into `previous` (None when there is none or previous is False)."""
        ...

    def get_version_content(self, document_id: int, version_no: int) -> VersionContent | None:
        """One stored version's content (to compare with a version other than the one before)."""
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
        self,
        draft: Draft,
        markdown: str,
        origin: str,
        instruction: str,
        now: datetime,
        inputs: Inputs | None = None,
        update_selection: bool = False,
    ) -> DraftVersion:
        """inputs (AI versions): saved on the version; with update_selection also written to
        the draft row as its current selection. Once the version is saved this never raises
        for the draft row: the paid version stands (the failure is logged)."""
        ...
    def delete_draft(self, draft_id: str) -> None: ...
    def count_requests_since(self, user_id: str, since: datetime) -> int:
        """AI calls (docgen.generate + docgen.revise audit entries) since `since`."""
        ...

    def purge_before(self, cutoff: datetime) -> int: ...


class DraftWriter(Protocol):
    def write(self, instructions: str, input_text: str) -> Written: ...
