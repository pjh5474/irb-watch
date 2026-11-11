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
    source_ids: tuple[int, ...] = ()  # sources that use it by default (unused while global)
    is_global: bool = False  # a default of every draft, for every source
    text_updated_at: str | None = None  # when the file was last replaced; None = never


@dataclass(frozen=True)
class GuideRevision:
    """An old text of a guide, kept when an admin replaced its file."""

    id: int
    guide_id: int
    filename: str
    body_text: str
    valid_from: str
    replaced_by: str | None
    replaced_at: str


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


FIELD_KINDS = ("text", "choice")
FIELD_SOURCES = ("marker", "auto")
TEMPLATE_STATUSES = ("review", "ready")

# A field's answer: text, or the picked options of a tick-box group.
FieldValue = str | list[str]


def picked_list(v: FieldValue | None) -> list[str]:
    """A tick-box answer as a list of options. One option given as text counts as that option;
    "" and None mean nothing picked. The one place this is decided."""
    if isinstance(v, str):
        return [v] if v else []
    return list(v or [])


@dataclass(frozen=True)
class FormField:
    """One place of a form template that the AI fills.

    key: "m:<name>" for a {{name}} marker (filled everywhere it appears), "a:<where>" for a
    detected blank, "c:<where>" for a tick-box group. where: the place in the file (stable for
    the same file). options/multi: tick-box groups only. hint: the admin's note for the AI."""

    key: str
    label: str
    kind: str  # one of FIELD_KINDS
    source: str  # one of FIELD_SOURCES
    where: str
    options: tuple[str, ...] = ()
    multi: bool = False
    hint: str = ""


def field_to_json(f: FormField) -> dict:
    return {
        "key": f.key,
        "label": f.label,
        "kind": f.kind,
        "source": f.source,
        "where": f.where,
        "options": list(f.options),
        "multi": f.multi,
        "hint": f.hint,
    }


def field_from_json(r: dict) -> FormField:
    return FormField(
        key=str(r["key"]),
        label=str(r.get("label") or ""),
        kind=str(r.get("kind") or "text"),
        source=str(r.get("source") or "auto"),
        where=str(r.get("where") or ""),
        options=tuple(str(o) for o in r.get("options") or []),
        multi=bool(r.get("multi", False)),
        hint=str(r.get("hint") or ""),
    )


@dataclass(frozen=True)
class FormTemplate:
    """An admin's blank form (DOCX) in the library. Only 'ready' templates are offered."""

    id: int
    title: str
    doc_type: str
    filename: str
    storage_path: str
    sha256: str
    is_global: bool
    status: str  # one of TEMPLATE_STATUSES
    fields: tuple[FormField, ...]
    created_at: str
    updated_at: str
    archived_at: str | None = None
    source_ids: tuple[int, ...] = ()  # sites that offer it (unused while global)


@dataclass(frozen=True)
class DraftTemplate:
    """The template a draft fills, copied at creation (a later change of the library template
    changes nothing for the draft). template_id None = a one-off file the user attached."""

    template_id: int | None
    title: str
    filename: str
    storage_path: str
    fields: tuple[FormField, ...]


def draft_template_to_json(t: DraftTemplate) -> dict:
    return {
        "template_id": t.template_id,
        "title": t.title,
        "filename": t.filename,
        "storage_path": t.storage_path,
        "fields": [field_to_json(f) for f in t.fields],
    }


def draft_template_from_json(r: dict) -> DraftTemplate:
    tid = r.get("template_id")
    return DraftTemplate(
        int(tid) if tid is not None else None,
        str(r.get("title") or ""),
        str(r.get("filename") or ""),
        str(r.get("storage_path") or ""),
        tuple(field_from_json(f) for f in r.get("fields") or []),
    )


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
    template: DraftTemplate | None = None  # None = a Markdown draft


@dataclass(frozen=True)
class DraftVersion:
    draft_id: str
    version_no: int
    markdown: str
    origin: str  # one of ORIGINS
    instruction: str = ""
    created_at: str = ""
    inputs: dict | None = None  # Inputs.to_json() for AI versions; None for hand edits and old rows
    # Template drafts: one answer per field key; markdown then holds a readable copy.
    fields: dict[str, FieldValue] | None = None


@dataclass(frozen=True)
class NewDraft:
    user_id: str
    title: str
    instruction: str
    document_refs: tuple[DocRef, ...]
    guide_ids: tuple[int, ...]
    extra_guides: tuple[ExtraGuide, ...] = ()
    ai_override_source_ids: tuple[int, ...] = field(default_factory=tuple)
    template: DraftTemplate | None = None


@dataclass(frozen=True)
class Written:
    title: str
    markdown: str


@dataclass(frozen=True)
class WrittenFields:
    """The AI's answer for a template draft: raw, not yet checked against the fields."""

    title: str
    values: dict[str, FieldValue]


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
        """Non-archived guides linked to any of these sources, plus every non-archived global
        guide (global ones first). Empty source list: nothing."""
        ...

    def list_sources(self) -> list[SourceRef]: ...
    def update_guide(self, guide_id: int, title: str | None, is_global: bool | None) -> None: ...
    def replace_guide_text(
        self, guide: Guide, filename: str, body_text: str, sha256: str, by: str, now: datetime
    ) -> None:
        """Make the new text current only if the guide still has the text this call read (else
        VersionConflictError, nothing written), then keep that old text in guide_revisions."""
        ...

    def list_guide_revisions(self, guide_id: int) -> list[GuideRevision]:
        """Newest first."""
        ...

    def archive_guide(self, guide_id: int, now: datetime) -> None:
        """Set archived_at and remove all its links."""
        ...

    def link_guide(self, source_id: int, guide_id: int, now: datetime) -> None: ...
    def unlink_guide(self, source_id: int, guide_id: int) -> None: ...

    # -- form templates ----------------------------------------------------------------
    def create_template(
        self,
        title: str,
        doc_type: str,
        filename: str,
        storage_path: str,
        sha256: str,
        fields: tuple[FormField, ...],
        created_by: str,
        now: datetime,
    ) -> FormTemplate:
        """A new template, status 'review'."""
        ...

    def list_templates(self, include_archived: bool) -> list[FormTemplate]: ...
    def get_template(self, template_id: int) -> FormTemplate | None: ...
    def templates_for_sources(self, source_ids: list[int]) -> list[FormTemplate]:
        """Ready, non-archived templates linked to any of these sources plus every ready,
        non-archived global one (global first)."""
        ...

    def update_template(self, template_id: int, changes: dict, now: datetime) -> None:
        """changes: any of title, doc_type, is_global, status, fields (FormField tuple),
        filename, storage_path, sha256."""
        ...

    def archive_template(self, template_id: int, now: datetime) -> None:
        """Set archived_at and remove all its links."""
        ...

    def link_template(self, source_id: int, template_id: int, now: datetime) -> None: ...
    def unlink_template(self, source_id: int, template_id: int) -> None: ...
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
        fields: dict[str, FieldValue] | None = None,
    ) -> DraftVersion:
        """inputs (AI versions): saved on the version; with update_selection also written to
        the draft row as its current selection. Once the version is saved this never raises
        for the draft row: the paid version stands (the failure is logged). fields: the
        answers of a template draft."""
        ...
    def delete_draft(self, draft_id: str) -> None: ...
    def count_requests_since(self, user_id: str, since: datetime) -> int:
        """AI calls (docgen.generate + docgen.revise audit entries) since `since`."""
        ...

    def purge_before(self, cutoff: datetime) -> tuple[int, list[str]]:
        """Delete drafts unchanged since cutoff. Returns how many, and the storage paths of the
        one-off form files of exactly those drafts (to delete them too)."""
        ...


class DraftWriter(Protocol):
    def write(self, instructions: str, input_text: str) -> Written: ...
    def write_fields(self, instructions: str, input_text: str) -> WrittenFields: ...


class TemplateFiles(Protocol):
    """The private bucket of template files."""

    def put(self, path: str, content: bytes) -> None: ...
    def get(self, path: str) -> bytes: ...
    def delete(self, paths: list[str]) -> None: ...
