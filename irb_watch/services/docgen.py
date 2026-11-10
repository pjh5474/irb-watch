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
from dataclasses import dataclass
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
    document_id: int
    version_no: int | None = None


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content_base64: str


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
        self, refs: list[tuple[int, int | None]], sources: dict[int, SourceRef]
    ) -> list[DocVersion]:
        docs = []
        for document_id, version_no in refs:
            doc = self._store.get_doc_version(document_id, version_no)
            if doc is None or doc.source_id not in sources:
                raise NotFoundError(f"document {document_id} not found")
            docs.append(doc)
        return docs

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
        return self._load_docs([(document_id, None)], sources)

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

        seen = list(dict.fromkeys((d.document_id, d.version_no) for d in documents))
        sources = self._store.readable_sources(user_id, is_admin)
        docs = self._load_docs(seen, sources)
        used = {d.source_id for d in docs}
        override = set(ai_override_source_ids) & used
        self._ai_check(sources, used, override)

        # Guides: by default all of the sources' guides. A user may leave some out; only an
        # admin may add a library guide that is not linked to these sources.
        defaults = {g.id: g for g in self._store.guides_for_sources(sorted(used))}
        if guide_ids is None:
            guides = list(defaults.values())
        else:
            wanted = list(dict.fromkeys(guide_ids))
            extra_ids = [g for g in wanted if g not in defaults]
            if extra_ids and not is_admin:
                raise NotFoundError(f"guide {extra_ids[0]} not found")
            found = {g.id: g for g in self._store.get_guides(extra_ids)} if extra_ids else {}
            for gid in extra_ids:
                if gid not in found or found[gid].archived_at is not None:
                    raise NotFoundError(f"guide {gid} not found")
            guides = [defaults.get(g) or found[g] for g in wanted]

        # The limit is checked before the attached files are read (reading a PDF or HWP costs
        # time and memory); the log entry that counts the call comes after, just before it.
        now = self._clock()
        self._check_limit(user_id, now)
        extra = []
        for upload in extra_files:
            name, text, _ = read_guide_file(upload)
            extra.append(ExtraGuide(name, text))

        refs = tuple(DocRef(d.document_id, d.version_no, d.source_id) for d in docs)
        # The log is also the counter: no entry, no model call.
        self._audit.record_required(
            "user",
            "docgen.generate",
            user_id,
            payload={
                "documents": [{"document_id": r.document_id, "version_no": r.version_no} for r in refs],
                "source_ids": sorted(used),
                "guide_ids": [g.id for g in guides],
                "extra_guides": [e.filename for e in extra],
                "ai_override_source_ids": sorted(override),
            },
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

    def save_edit(self, user_id: str, draft_id: str, markdown: str) -> DraftVersion:
        self._profile(user_id)
        draft = self._own(user_id, draft_id)
        text = (markdown or "").strip()
        if not text:
            raise ValidationError("the draft is empty")
        if len(text) > MAX_MARKDOWN_CHARS:
            raise ValidationError(f"the draft is too long (at most {MAX_MARKDOWN_CHARS} characters)")
        versions = self._latest(draft_id)
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
    ) -> DraftVersion:
        """ai_override_source_ids: AI-off sources the user ticked now (needed when an admin
        switched AI off for a source after the draft was made)."""
        profile = self._profile(user_id)
        draft = self._own(user_id, draft_id)
        request = (instruction or "").strip()
        if not request:
            raise ValidationError("say what to change")
        if len(request) > MAX_INSTRUCTION_CHARS:
            raise ValidationError(f"the request is too long (at most {MAX_INSTRUCTION_CHARS} characters)")
        if self._writer is None:
            raise UnavailableError(NO_AI)
        versions = self._latest(draft_id)

        # Access is checked again: the user may have lost a study since the draft was made.
        sources = self._store.readable_sources(user_id, profile.role == "admin")
        docs = self._load_docs([(r.document_id, r.version_no) for r in draft.document_refs], sources)
        used = {d.source_id for d in docs}
        override = (set(draft.ai_override_source_ids) | set(ai_override_source_ids or [])) & used
        self._ai_check(sources, used, override)
        guides = self._store.get_guides(list(draft.guide_ids)) if draft.guide_ids else []

        now = self._clock()
        self._check_limit(user_id, now)
        self._audit.record_required(
            "user",
            "docgen.revise",
            user_id,
            payload={
                "draft_id": draft_id,
                "from_version": versions[-1].version_no,
                "source_ids": sorted(used),
                "ai_override_source_ids": sorted(override),
            },
        )
        written = self._write(
            docs, guides, list(draft.extra_guides), request, current=versions[-1].markdown
        )
        markdown = written.markdown[:MAX_MARKDOWN_CHARS]
        try:
            return self._store.add_version(draft, markdown, "ai_revision", request, now)
        except VersionConflictError:
            # A hand edit was saved while the AI was writing. The revision was paid for, so it is
            # kept as the next version rather than thrown away; the user sees both.
            try:
                return self._store.add_version(draft, markdown, "ai_revision", request, now)
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
