"""Docs generator: drafts of IRB submission/amendment forms, and the admin's guide library.

Rules (owner decisions 2026-11-10, see docs/decisions.md "Docs generator"):
  * Inputs are stored document versions the user may read (same rule as everywhere: admins
    read every source, anyone a public one, a study only its assigned members). A document the
    user may not read answers "not found".
  * Guides: an admin keeps a library and links guides to sources. A draft uses the guides of its
    documents' sources by default; the user may leave any of them out (guide_ids) and attach up
    to MAX_EXTRA_GUIDES one-off guide files.
  * A source with AI switched off may be used only when the user ticked the warning box for
    that source (ai_override_source_ids). Every use is in the audit log with the source ids.
  * Every AI call (first draft and each revision) counts toward a per-user daily limit and is
    logged BEFORE the model is called (fail closed). A failed call is not saved.
  * Each change makes a new draft version: the AI's draft, a hand edit or an AI revision.
  * Drafts are deleted 30 days after their last change (purge_old, run daily).
"""

import base64
import binascii
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from irb_watch.docgen.docx_build import build_docx
from irb_watch.docgen.prompt import build_input
from irb_watch.docgen.types import (
    DocgenStore,
    DocRef,
    DocVersion,
    Draft,
    DraftVersion,
    DraftWriter,
    ExtraGuide,
    Guide,
    GuideRevision,
    Inputs,
    NewDraft,
    SourceRef,
    VersionConflictError,
)
from irb_watch.parsers.errors import ParseError
from irb_watch.parsers.formats import extract_text
from irb_watch.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.users import AuditSink, UserStore, active_profile

log = logging.getLogger(__name__)

MAX_DOCUMENTS = 10
MAX_EXTRA_GUIDES = 3
MAX_GUIDE_BYTES = 5_000_000
MAX_INSTRUCTION_CHARS = 2_000
MAX_MARKDOWN_CHARS = 100_000
MAX_TITLE_CHARS = 200
MAX_VERSIONS = 50
RETENTION_DAYS = 30
REFUSAL = "this account cannot use the docs generator"
NO_AI = "the docs generator is not set up on this server (OPENAI_API_KEY and OPENAI_MODEL)"
GUIDE_TYPES = ("pdf", "hwp", "hwpx", "docx", "md", "txt")


@dataclass(frozen=True)
class DocChoice:
    """One post to draft from. files None = every attached file; page_text False leaves the
    post's own text out. compare_with None = compare with the version just before;
    compare False = send no list of changes."""

    document_id: int
    version_no: int | None = None
    files: tuple[str, ...] | None = None
    page_text: bool = True
    compare_with: int | None = None
    compare: bool = True


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content_base64: str


@dataclass(frozen=True)
class InputChange:
    """New inputs for an AI revision (the side panel). keep_extra_guides: names of the draft's
    attached guide files to keep; extra_files: new ones (together at most MAX_EXTRA_GUIDES)."""

    documents: list[DocChoice]
    guide_ids: list[int] | None = None
    keep_extra_guides: list[str] = field(default_factory=list)
    extra_files: list[UploadedFile] = field(default_factory=list)


@dataclass(frozen=True)
class DraftDetail:
    draft: Draft
    versions: list[DraftVersion]
    guides: list[Guide]


def _decode(upload: UploadedFile) -> bytes:
    try:
        content = base64.b64decode(upload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(f"{upload.filename}: the file content is not valid base64") from exc
    if not content:
        raise ValidationError(f"{upload.filename}: the file is empty")
    if len(content) > MAX_GUIDE_BYTES:
        raise ValidationError(
            f"{upload.filename}: the file is too large (at most {MAX_GUIDE_BYTES // 1_000_000} MB)"
        )
    return content


def _clean_name(filename: str) -> str:
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:200]


def read_guide_file(upload: UploadedFile) -> tuple[str, str, str]:
    """Returns (clean filename, text, sha256). Refuses files without text, with the reason."""
    name = _clean_name(upload.filename)
    if not name:
        raise ValidationError("the guide file needs a file name")
    ext = name.lower().rpartition(".")[2]
    if ext not in GUIDE_TYPES:
        raise ValidationError(
            f"{name}: guide files must be one of {', '.join('.' + t for t in GUIDE_TYPES)}"
        )
    content = _decode(upload)
    sha = hashlib.sha256(content).hexdigest()
    if ext in ("md", "txt"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"{name}: the text file is not UTF-8") from exc
    else:
        try:
            result = extract_text(name, content)
        except ParseError as exc:
            raise ValidationError(f"{name}: {exc}") from exc
        if result.document is None:
            raise ValidationError(
                f"{name}: no text could be read from this file ({result.note or result.hash_only_reason})"
            )
        text = result.document.text
    text = text.replace(chr(0), "").strip()
    if not text:
        raise ValidationError(f"{name}: the file has no text")
    return name, text, sha


def _payload(refs, used, guides, extra, override) -> dict:
    """The audit entry of an AI call: what was sent (ids and file names, never text)."""
    return {
        "documents": [
            {
                "document_id": r.document_id,
                "version_no": r.version_no,
                "files": list(r.files) if r.files is not None else None,
                "page_text": r.page_text,
                "compare_with": r.compare_with,
                "compare": r.compare,
            }
            for r in refs
        ],
        "source_ids": sorted(used),
        "guide_ids": [g.id for g in guides],
        "extra_guides": [e.filename for e in extra],
        "ai_override_source_ids": sorted(override),
    }


_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def docx_filename(title: str, version_no: int) -> str:
    base = _FILENAME_BAD.sub(" ", title).strip()[:80] or "draft"
    return f"{base} v{version_no}.docx"


class DocgenService:
    def __init__(
        self,
        users: UserStore,
        store: DocgenStore,
        writer: DraftWriter | None,
        audit: AuditSink,
        daily_limit: int = 10,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        extra_instructions: Callable[[], str] = lambda: "",
    ) -> None:
        self._users = users
        self._store = store
        self._writer = writer
        self._audit = audit
        self._daily_limit = daily_limit
        self._clock = clock
        self._extra = extra_instructions  # the admins' added instructions, read per call

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    # -- status ------------------------------------------------------------------------

    def status(self, user_id: str) -> dict:
        active_profile(self._users, user_id, REFUSAL)
        used = self._store.count_requests_since(user_id, self._clock() - timedelta(days=1))
        on = self.enabled and self._daily_limit > 0
        return {
            "enabled": on,
            "used": used,
            "limit": self._daily_limit,
            "reason": None if on else (NO_AI if not self.enabled else "the daily limit is 0"),
        }

    # -- helpers -----------------------------------------------------------------------

    def _profile(self, user_id: str):
        return active_profile(self._users, user_id, REFUSAL)

    def _admin(self, user_id: str) -> None:
        if self._profile(user_id).role != "admin":
            raise ForbiddenError("admin access required")

    def _check_limit(self, user_id: str, now: datetime) -> None:
        if self._writer is None:
            raise UnavailableError(NO_AI)
        if self._daily_limit <= 0:
            raise ForbiddenError("the docs generator is switched off (daily limit 0)")
        used = self._store.count_requests_since(user_id, now - timedelta(days=1))
        if used >= self._daily_limit:
            raise TooManyRequestsError(
                f"you have used your {self._daily_limit} AI drafts and revisions for the last 24 hours"
            )

    def _ai_check(
        self, sources: dict[int, SourceRef], used: set[int], override: set[int]
    ) -> None:
        blocked = sorted(sid for sid in used if not sources[sid].ai_enabled and sid not in override)
        if blocked:
            names = ", ".join(f"{sources[s].name} (#{s})" for s in blocked)
            raise ConflictError(
                f"AI is switched off for {names}; tick the box to confirm its text may be sent "
                "to OpenAI for this draft"
            )

    def _load_docs(
        self, choices: list[DocChoice], sources: dict[int, SourceRef]
    ) -> tuple[list[DocVersion], tuple[DocRef, ...]]:
        """The chosen versions with only the chosen files, and the refs to save. The same post
        and version chosen twice counts once (the first choice wins), also when one says
        "newest" and the other names the newest number."""
        docs, refs, seen = [], [], set()
        for c in choices:
            doc = self._load_one(c, sources)
            if (doc.document_id, doc.version_no) in seen:
                continue
            seen.add((doc.document_id, doc.version_no))
            docs.append(doc)
            refs.append(
                DocRef(
                    doc.document_id,
                    doc.version_no,
                    doc.source_id,
                    doc.files,
                    doc.page_text,
                    c.compare_with if c.compare else None,
                    c.compare,
                )
            )
        return docs, tuple(refs)

    def _load_one(self, c: DocChoice, sources: dict[int, SourceRef]) -> DocVersion:
        # The version just before is read only when it is the one compared with.
        doc = self._store.get_doc_version(
            c.document_id, c.version_no, previous=c.compare and c.compare_with is None
        )
        if doc is None or doc.source_id not in sources:
            raise NotFoundError(f"document {c.document_id} not found")
        label = f"document {doc.document_id} version {doc.version_no}"
        names = [f.filename for f in doc.content.files]
        files = None
        if c.files is not None:
            files = tuple(dict.fromkeys(c.files))
            missing = [f for f in files if f not in names]
            if missing:
                raise ValidationError(f'{label} has no file named "{missing[0]}"')
        sent_files = names if files is None else files
        if not c.page_text and not sent_files:
            raise ValidationError(f"{label}: choose its page text or at least one file")

        previous, previous_no = doc.previous, doc.version_no - 1 if doc.previous else None
        if not c.compare:
            previous, previous_no = None, None
        elif c.compare_with is not None:
            if not 1 <= c.compare_with < doc.version_no:
                raise ValidationError(f"{label}: compare it with an earlier version")
            previous = self._store.get_version_content(doc.document_id, c.compare_with)
            if previous is None:
                raise NotFoundError(f"document {doc.document_id} has no version {c.compare_with}")
            previous_no = c.compare_with
        return replace(
            doc,
            files=files,
            page_text=c.page_text,
            previous=previous,
            previous_no=previous_no,
            compare=c.compare,
        )

    def _guides(
        self,
        used: set[int],
        guide_ids: list[int] | None,
        is_admin: bool,
        already: tuple[int, ...] = (),
    ) -> list[Guide]:
        """By default all of the sources' guides. A user may leave some out; only an admin may
        add a library guide that is not linked to these sources. `already`: guides the draft
        uses now, which may stay even if they were unlinked or archived since."""
        defaults = {g.id: g for g in self._store.guides_for_sources(sorted(used))}
        if guide_ids is None:
            return list(defaults.values())
        wanted = list(dict.fromkeys(guide_ids))
        extra_ids = [g for g in wanted if g not in defaults]
        new_ids = [g for g in extra_ids if g not in already]
        if new_ids and not is_admin:
            raise NotFoundError(f"guide {new_ids[0]} not found")
        found = {g.id: g for g in self._store.get_guides(extra_ids)} if extra_ids else {}
        for gid in extra_ids:
            if gid not in found or (gid in new_ids and found[gid].archived_at is not None):
                raise NotFoundError(f"guide {gid} not found")
        return [defaults.get(g) or found[g] for g in wanted]

    def _read_extra(self) -> str:
        try:
            return self._extra() or ""
        except Exception as exc:  # noqa: BLE001  the fixed rules still apply without it
            log.warning("could not read the admins' docs instructions: %s", type(exc).__name__)
            return ""

    def _write(self, docs, guides, extra, request, current=None, admin_text=None):
        assert self._writer is not None
        text_of_admins = self._read_extra() if admin_text is None else admin_text
        instructions, text = build_input(docs, guides, extra, request, current, text_of_admins)
        try:
            return self._writer.write(instructions, text)
        except Exception as exc:
            log.warning("docs generator failed: %s", type(exc).__name__)
            raise UpstreamError(
                f"the AI could not write the draft ({type(exc).__name__}); please try again"
            ) from exc

    def _own(self, user_id: str, draft_id: str) -> Draft:
        draft = self._store.get_draft(draft_id)
        if draft is None or draft.user_id != user_id:
            raise NotFoundError("draft not found")
        return draft

    def preview(self, admin_id: str, extra: str, document_id: int | None, request: str) -> str:
        """Admin test run with UNSAVED instructions on one document (its newest version, with
        its source's guides). Nothing is saved or counted here; the caller limits and logs test
        runs. Sources with AI off are allowed (owner's choice for admin tests)."""
        docs = self.check_preview(admin_id, document_id)
        guides = self._store.guides_for_sources([docs[0].source_id])
        return self._write(docs, guides, [], request, admin_text=extra).markdown

    def check_preview(self, admin_id: str, document_id: int | None) -> list[DocVersion]:
        """Everything a test run can be refused for, checked before the run is counted."""
        if self._writer is None:
            raise UnavailableError(NO_AI)
        if document_id is None:
            raise ValidationError("pick a document to test with")
        self._admin(admin_id)
        sources = self._store.readable_sources(admin_id, True)
        return self._load_docs([DocChoice(document_id)], sources)[0]

    # -- guides for users -------------------------------------------------------------

    def default_guides(self, user_id: str, source_ids: list[int]) -> list[Guide]:
        """The guides a draft from these sources would use by default."""
        profile = self._profile(user_id)
        readable = self._store.readable_sources(user_id, profile.role == "admin")
        wanted = [s for s in dict.fromkeys(source_ids) if s in readable]
        return self._store.guides_for_sources(wanted) if wanted else []

    # -- drafts ------------------------------------------------------------------------

    def create_draft(
        self,
        user_id: str,
        documents: list[DocChoice],
        guide_ids: list[int] | None,
        extra_files: list[UploadedFile],
        instruction: str,
        ai_override_source_ids: list[int],
    ) -> DraftDetail:
        profile = self._profile(user_id)
        is_admin = profile.role == "admin"
        if not documents:
            raise ValidationError("choose at least one document")
        if len(documents) > MAX_DOCUMENTS:
            raise ValidationError(f"choose at most {MAX_DOCUMENTS} documents")
        if len(extra_files) > MAX_EXTRA_GUIDES:
            raise ValidationError(f"attach at most {MAX_EXTRA_GUIDES} guide files")
        request = (instruction or "").strip()
        if len(request) > MAX_INSTRUCTION_CHARS:
            raise ValidationError(f"the request is too long (at most {MAX_INSTRUCTION_CHARS} characters)")
        if self._writer is None:
            raise UnavailableError(NO_AI)

        sources = self._store.readable_sources(user_id, is_admin)
        docs, refs = self._load_docs(documents, sources)
        used = {d.source_id for d in docs}
        override = set(ai_override_source_ids) & used
        self._ai_check(sources, used, override)
        guides = self._guides(used, guide_ids, is_admin)

        # The limit is checked before the attached files are read (reading a PDF or HWP costs
        # time and memory); the log entry that counts the call comes after, just before it.
        now = self._clock()
        self._check_limit(user_id, now)
        extra = [ExtraGuide(*read_guide_file(upload)[:2]) for upload in extra_files]

        # The log is also the counter: no entry, no model call.
        self._audit.record_required(
            "user",
            "docgen.generate",
            user_id,
            payload=_payload(refs, used, guides, extra, override),
        )
        written = self._write(docs, guides, extra, request)
        title = written.title or docs[0].title
        new = NewDraft(
            user_id=user_id,
            title=title[:MAX_TITLE_CHARS],
            instruction=request,
            document_refs=refs,
            guide_ids=tuple(g.id for g in guides),
            extra_guides=tuple(extra),
            ai_override_source_ids=tuple(sorted(override)),
        )
        first = DraftVersion("", 1, written.markdown[:MAX_MARKDOWN_CHARS], "ai", request)
        draft = self._store.create_draft(new, first, now)
        return DraftDetail(draft, self._store.list_versions(draft.id), guides)

    def list_drafts(self, user_id: str) -> list[Draft]:
        self._profile(user_id)
        return self._store.list_drafts(user_id, 100)

    def get_draft(self, user_id: str, draft_id: str) -> DraftDetail:
        self._profile(user_id)
        draft = self._own(user_id, draft_id)
        guides = self._store.get_guides(list(draft.guide_ids)) if draft.guide_ids else []
        return DraftDetail(draft, self._store.list_versions(draft_id), guides)

    def _latest(self, draft_id: str) -> list[DraftVersion]:
        versions = self._store.list_versions(draft_id)
        if not versions:
            raise NotFoundError("draft not found")
        if len(versions) >= MAX_VERSIONS:
            raise ValidationError(
                f"this draft has {MAX_VERSIONS} versions; export it or start a new draft"
            )
        return versions

    def save_edit(
        self, user_id: str, draft_id: str, markdown: str, base_version_no: int | None = None
    ) -> DraftVersion:
        """base_version_no: the version the user's edit started from. When a newer version
        exists (another tab, or an AI revision), the save is refused instead of silently
        building on old text."""
        self._profile(user_id)
        draft = self._own(user_id, draft_id)
        text = (markdown or "").strip()
        if not text:
            raise ValidationError("the draft is empty")
        if len(text) > MAX_MARKDOWN_CHARS:
            raise ValidationError(f"the draft is too long (at most {MAX_MARKDOWN_CHARS} characters)")
        versions = self._latest(draft_id)
        if base_version_no is not None and base_version_no != versions[-1].version_no:
            raise ConflictError(
                f"the draft has a newer version ({versions[-1].version_no}) than the one you "
                f"edited ({base_version_no}); reload it and save again"
            )
        if versions[-1].markdown == text:
            return versions[-1]
        try:
            return self._store.add_version(draft, text, "user_edit", "", self._clock())
        except VersionConflictError as exc:
            raise ConflictError(
                "the draft was changed at the same moment (another save or an AI revision); "
                "reload it and save again"
            ) from exc

    def revise(
        self,
        user_id: str,
        draft_id: str,
        instruction: str,
        ai_override_source_ids: list[int] | None = None,
        change: InputChange | None = None,
    ) -> DraftVersion:
        """ai_override_source_ids: AI-off sources the user ticked now (needed when an admin
        switched AI off for a source after the draft was made, or a new one was added).
        change: new documents and guides from the side panel; None keeps the draft's current
        ones. Either way the inputs are saved on the new version."""
        profile = self._profile(user_id)
        is_admin = profile.role == "admin"
        draft = self._own(user_id, draft_id)
        request = (instruction or "").strip()
        if not request:
            raise ValidationError("say what to change")
        if len(request) > MAX_INSTRUCTION_CHARS:
            raise ValidationError(f"the request is too long (at most {MAX_INSTRUCTION_CHARS} characters)")
        if self._writer is None:
            raise UnavailableError(NO_AI)
        versions = self._latest(draft_id)

        if change is None:
            choices = [
                DocChoice(r.document_id, r.version_no, r.files, r.page_text, r.compare_with, r.compare)
                for r in draft.document_refs
            ]
            keep = list(draft.extra_guides)
            uploads: list[UploadedFile] = []
        else:
            choices = change.documents
            if not choices:
                raise ValidationError("choose at least one document")
            if len(choices) > MAX_DOCUMENTS:
                raise ValidationError(f"choose at most {MAX_DOCUMENTS} documents")
            held = {e.filename: e for e in draft.extra_guides}
            names = list(dict.fromkeys(change.keep_extra_guides))
            unknown = [n for n in names if n not in held]
            if unknown:
                raise NotFoundError(f'this draft has no attached guide file "{unknown[0]}"')
            keep = [held[n] for n in names]
            uploads = change.extra_files
            if len(keep) + len(uploads) > MAX_EXTRA_GUIDES:
                raise ValidationError(f"attach at most {MAX_EXTRA_GUIDES} guide files")

        # Access is checked again: the user may have lost a study since the draft was made.
        sources = self._store.readable_sources(user_id, is_admin)
        docs, refs = self._load_docs(choices, sources)
        used = {d.source_id for d in docs}
        override = (set(draft.ai_override_source_ids) | set(ai_override_source_ids or [])) & used
        self._ai_check(sources, used, override)
        if change is None:
            guides = self._store.get_guides(list(draft.guide_ids)) if draft.guide_ids else []
        else:
            guides = self._guides(used, change.guide_ids, is_admin, draft.guide_ids)

        now = self._clock()
        self._check_limit(user_id, now)
        extra = keep + [ExtraGuide(*read_guide_file(u)[:2]) for u in uploads]
        self._audit.record_required(
            "user",
            "docgen.revise",
            user_id,
            payload={
                "draft_id": draft_id,
                "from_version": versions[-1].version_no,
                "inputs_changed": change is not None,
                **_payload(refs, used, guides, extra, override),
            },
        )
        written = self._write(docs, guides, extra, request, current=versions[-1].markdown)
        markdown = written.markdown[:MAX_MARKDOWN_CHARS]
        inputs = Inputs(refs, tuple(g.id for g in guides), tuple(extra), tuple(sorted(override)))
        # The draft row (with the attached guides' text) is rewritten only when its selection
        # changed: new inputs from the side panel, or a newly ticked AI-off source.
        changed = change is not None or set(override) != set(draft.ai_override_source_ids)
        try:
            return self._store.add_version(
                draft, markdown, "ai_revision", request, now, inputs, changed
            )
        except VersionConflictError:
            # A hand edit was saved while the AI was writing. The revision was paid for, so it is
            # kept as the next version rather than thrown away; the user sees both.
            try:
                return self._store.add_version(
                    draft, markdown, "ai_revision", request, now, inputs, changed
                )
            except VersionConflictError as exc:
                raise ConflictError(
                    "the draft kept changing while the AI was writing; reload it and try again"
                ) from exc

    def export_docx(
        self, user_id: str, draft_id: str, version_no: int | None
    ) -> tuple[bytes, str]:
        self._profile(user_id)
        draft = self._own(user_id, draft_id)
        versions = self._store.list_versions(draft_id)
        if version_no is None:
            chosen = versions[-1] if versions else None
        else:
            chosen = next((v for v in versions if v.version_no == version_no), None)
        if chosen is None:
            raise NotFoundError("that draft version does not exist")
        content = build_docx(chosen.markdown)
        self._audit.record(
            "user",
            "docgen.export",
            user_id,
            payload={"draft_id": draft_id, "version_no": chosen.version_no},
        )
        return content, docx_filename(draft.title, chosen.version_no)

    def delete_draft(self, user_id: str, draft_id: str) -> None:
        self._profile(user_id)
        self._own(user_id, draft_id)
        self._store.delete_draft(draft_id)

    def purge_old(self, days: int = RETENTION_DAYS) -> int:
        """Delete drafts unchanged for `days`. Never raises: it runs from the scheduler."""
        try:
            count = self._store.purge_before(self._clock() - timedelta(days=days))
            if count:
                self._audit.record(
                    "admin", "docgen.purged", None, payload={"drafts": count, "days": days}
                )
            return count
        except Exception:
            log.exception("could not delete old drafts")
            return 0

    # -- the admin's guide library ------------------------------------------------------

    def list_guides(self, admin_id: str, include_archived: bool) -> list[Guide]:
        self._admin(admin_id)
        return self._store.list_guides(include_archived)

    def create_guide(self, admin_id: str, title: str, upload: UploadedFile) -> Guide:
        self._admin(admin_id)
        clean_title = (title or "").strip()
        if not clean_title or len(clean_title) > MAX_TITLE_CHARS:
            raise ValidationError(f"the title must be 1 to {MAX_TITLE_CHARS} characters")
        name, text, sha = read_guide_file(upload)
        guide = self._store.create_guide(clean_title, name, text, sha, admin_id, self._clock())
        self._audit.record(
            "admin",
            "docgen.guide_created",
            admin_id,
            title=clean_title,
            payload={"guide_id": guide.id, "filename": name, "chars": len(text)},
        )
        return guide

    def _guide(self, guide_id: int) -> Guide:
        found = self._store.get_guides([guide_id])
        if not found:
            raise NotFoundError("guide not found")
        return found[0]

    def archive_guide(self, admin_id: str, guide_id: int) -> None:
        self._admin(admin_id)
        guide = self._guide(guide_id)
        if guide.archived_at is not None:
            return
        self._store.archive_guide(guide_id, self._clock())
        self._audit.record(
            "admin",
            "docgen.guide_archived",
            admin_id,
            title=guide.title,
            payload={"guide_id": guide_id, "unlinked_source_ids": list(guide.source_ids)},
        )

    def source_guides(self, admin_id: str, source_id: int) -> list[Guide]:
        self._admin(admin_id)
        if self._store.get_source(source_id) is None:
            raise NotFoundError("source not found")
        return self._store.guides_for_sources([source_id])

    def link_guide(self, admin_id: str, source_id: int, guide_id: int) -> None:
        self._admin(admin_id)
        if self._store.get_source(source_id) is None:
            raise NotFoundError("source not found")
        guide = self._guide(guide_id)
        if guide.archived_at is not None:
            raise ConflictError("this guide is archived; upload it again to use it")
        if guide.is_global:
            raise ConflictError("this guide is global and applies to every site already")
        if source_id in guide.source_ids:
            return
        self._store.link_guide(source_id, guide_id, self._clock())
        self._audit.record(
            "admin",
            "docgen.guide_linked",
            admin_id,
            title=guide.title,
            payload={"guide_id": guide_id},
            source_id=source_id,
        )

    def guide(self, admin_id: str, guide_id: int) -> Guide:
        self._admin(admin_id)
        return self._guide(guide_id)

    def update_guide(
        self, admin_id: str, guide_id: int, title: str | None, is_global: bool | None
    ) -> Guide:
        """Rename a guide and/or make it global (a default of every draft for every source) or
        site-specific again. Its source links are kept either way (unused while global)."""
        self._admin(admin_id)
        guide = self._guide(guide_id)
        if guide.archived_at is not None:
            raise ConflictError("this guide is archived; upload it again to use it")
        clean = None
        if title is not None:
            clean = title.strip()
            if not clean or len(clean) > MAX_TITLE_CHARS:
                raise ValidationError(f"the title must be 1 to {MAX_TITLE_CHARS} characters")
            if clean == guide.title:
                clean = None
        flag = None if is_global is None or is_global == guide.is_global else is_global
        if clean is None and flag is None:
            return guide
        self._store.update_guide(guide_id, clean, flag)
        self._audit.record(
            "admin",
            "docgen.guide_updated",
            admin_id,
            title=clean or guide.title,
            payload={
                "guide_id": guide_id,
                **({"old_title": guide.title} if clean is not None else {}),
                **({"is_global": flag} if flag is not None else {}),
            },
        )
        return self._guide(guide_id)

    def replace_guide_file(self, admin_id: str, guide_id: int, upload: UploadedFile) -> Guide:
        """New text for a guide; the old text is kept in its history. Links and the global flag
        stay; drafts use the new text from their next AI call."""
        self._admin(admin_id)
        guide = self._guide(guide_id)
        if guide.archived_at is not None:
            raise ConflictError("this guide is archived; upload it again to use it")
        name, text, sha = read_guide_file(upload)
        if text == guide.body_text:
            # The same file again (a double click, a retry): history rows cannot be removed, so
            # nothing is written; the admin is told why.
            raise ConflictError("this file has the same text as the guide's current text; nothing changed")
        try:
            self._store.replace_guide_text(guide, name, text, sha, admin_id, self._clock())
        except VersionConflictError as exc:
            raise ConflictError(
                "the guide's file was replaced by someone else at the same moment; reload and try again"
            ) from exc
        self._audit.record(
            "admin",
            "docgen.guide_replaced",
            admin_id,
            title=guide.title,
            payload={"guide_id": guide_id, "filename": name, "old_filename": guide.filename, "chars": len(text)},
        )
        return self._guide(guide_id)

    def guide_history(self, admin_id: str, guide_id: int) -> list[GuideRevision]:
        self._admin(admin_id)
        self._guide(guide_id)
        return self._store.list_guide_revisions(guide_id)

    def set_guide_sources(self, admin_id: str, guide_id: int, source_ids: list[int]) -> Guide:
        """The full list of sources that use this guide by default (added and removed in one
        call, one audit entry). Refused for a global or archived guide."""
        self._admin(admin_id)
        guide = self._guide(guide_id)
        if guide.archived_at is not None:
            raise ConflictError("this guide is archived; upload it again to use it")
        if guide.is_global:
            raise ConflictError("this guide is global and applies to every site; make it site-specific first")
        known = {s.id for s in self._store.list_sources()}
        wanted = set(source_ids)
        unknown = sorted(wanted - known)
        if unknown:
            raise NotFoundError(f"source {unknown[0]} not found")
        current = set(guide.source_ids)
        added, removed = sorted(wanted - current), sorted(current - wanted)
        if not added and not removed:
            return guide
        # Written first, fail closed (as for study members): no entry, no change. The links are
        # separate writes; if one fails part-way, the entry already names every intended change.
        self._audit.record_required(
            "admin",
            "docgen.guide_sources_set",
            admin_id,
            title=guide.title,
            payload={"guide_id": guide_id, "added": added, "removed": removed},
        )
        now = self._clock()
        for sid in added:
            self._store.link_guide(sid, guide_id, now)
        for sid in removed:
            self._store.unlink_guide(sid, guide_id)
        return self._guide(guide_id)

    def unlink_guide(self, admin_id: str, source_id: int, guide_id: int) -> None:
        self._admin(admin_id)
        guide = self._guide(guide_id)
        if source_id not in guide.source_ids:
            raise NotFoundError("this guide is not linked to that source")
        self._store.unlink_guide(source_id, guide_id)
        self._audit.record(
            "admin",
            "docgen.guide_unlinked",
            admin_id,
            title=guide.title,
            payload={"guide_id": guide_id},
            source_id=source_id,
        )
