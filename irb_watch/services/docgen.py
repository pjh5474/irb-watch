"""Docs generator: drafts of IRB submission/amendment forms, and the admin's guide library.

Rules (owner decisions 2026-11-10, see docs/decisions.md "Docs generator"):
  * Inputs are stored document versions the user may read (same rule as everywhere: admins
    read every source, anyone a public one, a study only its assigned members). A document the
    user may not read answers "not found".
  * Guides: an admin keeps a library and links guides to hospitals (since 2026-11-12; a
    hospital groups its boards). A draft uses the guides of its documents' hospitals by default;
    the user may leave any of them out (guide_ids) and attach up to MAX_EXTRA_GUIDES one-off
    guide files. Form templates are offered per hospital the same way.
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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from irb_watch.docgen.docx_build import build_docx
from irb_watch.docgen.form_fields import FormError, detect_fields
from irb_watch.docgen.form_fill import fill, placeholder, render_markdown
from irb_watch.docgen.prompt import build_input
from irb_watch.docgen.types import (
    DocgenStore,
    DocRef,
    DocVersion,
    Draft,
    DraftTemplate,
    DraftVersion,
    DraftWriter,
    ExtraGuide,
    FieldValue,
    FormField,
    FormTemplate,
    Guide,
    GuideRevision,
    Inputs,
    NewDraft,
    SourceRef,
    StepContext,
    StepReader,
    TemplateFiles,
    VersionConflictError,
    picked_list,
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
from irb_watch.services.project_access import NEEDS, REFUSALS
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
MAX_ANSWER_CHARS = 4_000  # one field's answer
MAX_ANSWERS_CHARS = 100_000  # all answers of a version
MAX_HINT_CHARS = 300
MAX_DOC_TYPE_CHARS = 100
NO_TEMPLATES = "form templates are not set up on this server (no file storage)"


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


@dataclass(frozen=True)
class FieldEdit:
    """An admin's review of one detected field: kept (by being in the list), its label, a note
    for the AI and, for tick boxes, whether several options may be ticked."""

    key: str
    label: str
    hint: str = ""
    multi: bool = False


def read_template_file(upload: UploadedFile) -> tuple[str, bytes, str, tuple[FormField, ...]]:
    """Returns (clean filename, bytes, sha256, detected fields), or refuses with the reason."""
    name = _clean_name(upload.filename)
    if not name:
        raise ValidationError("the form file needs a file name")
    content = _decode(upload)
    try:
        fields = detect_fields(name, content)
    except FormError as exc:
        raise ValidationError(str(exc)) from exc
    return name, content, hashlib.sha256(content).hexdigest(), fields


def _picked(f: FormField, v: FieldValue | None) -> list[str]:
    picked = [o for o in dict.fromkeys(picked_list(v)) if o in f.options]
    return picked if f.multi else picked[:1]


def ai_answers(form: DraftTemplate, raw: dict[str, FieldValue]) -> dict[str, FieldValue]:
    """The model's answers checked against the form: unknown keys dropped, a missing or empty
    text answer becomes a visible placeholder, options not in the list are ignored."""
    out: dict[str, FieldValue] = {}
    for f in form.fields:
        v = raw.get(f.key)
        if f.kind == "choice":
            out[f.key] = _picked(f, v)
        else:
            text = (v if isinstance(v, str) else ", ".join(v or [])).strip()
            out[f.key] = text[:MAX_ANSWER_CHARS] or placeholder(f.label)
    return out


def user_answers(form: DraftTemplate, raw: dict[str, FieldValue]) -> dict[str, FieldValue]:
    """A hand edit's answers: refused (with the reason) for an unknown key, a too long answer or
    an option that is not on the form. A field left out is saved as unanswered."""
    known = {f.key: f for f in form.fields}
    unknown = [k for k in raw if k not in known]
    if unknown:
        raise ValidationError(f'this form has no field "{unknown[0]}"')
    out: dict[str, FieldValue] = {}
    total = 0
    for key, v in raw.items():
        f = known[key]
        if f.kind == "choice":
            picked = picked_list(v)  # "" = nothing picked, as everywhere
            bad = [o for o in picked if o not in f.options]
            if bad:
                raise ValidationError(f'"{f.label}" has no option "{bad[0]}"')
            if not f.multi and len(set(picked)) > 1:
                raise ValidationError(f'"{f.label}" takes only one option')
            out[key] = list(dict.fromkeys(picked))
        else:
            if not isinstance(v, str):
                raise ValidationError(f'"{f.label}" takes text')
            text = v.replace("\r\n", "\n").strip()
            if len(text) > MAX_ANSWER_CHARS:
                raise ValidationError(f'"{f.label}" is too long (at most {MAX_ANSWER_CHARS} characters)')
            total += len(text)
            out[key] = text
    if total > MAX_ANSWERS_CHARS:
        raise ValidationError(f"the answers are too long together (at most {MAX_ANSWERS_CHARS} characters)")
    return out


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


def _payload(
    refs, used, guides, extra, override, form: DraftTemplate | None = None,
    step: StepContext | None = None,
) -> dict:
    """The audit entry of an AI call: what was sent (ids and file names, never text). For a
    pipeline draft it names each of the user's own files that went to the AI service."""
    out = {
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
    if form is not None:
        out["template"] = {"template_id": form.template_id, "filename": form.filename}
    if step is not None:
        out["pipeline_step_id"] = step.step_id
        out["step_files"] = [{**f.ref.to_json(), "filename": f.filename} for f in step.files]
    return out


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
        files: TemplateFiles | None = None,
        steps: StepReader | None = None,
    ) -> None:
        self._steps = steps  # the pipeline side: a step's files and AI settings (None: off)
        self._users = users
        self._store = store
        self._writer = writer
        self._audit = audit
        self._daily_limit = daily_limit
        self._clock = clock
        self._extra = extra_instructions  # the admins' added instructions, read per call
        self._files = files  # the form-template bucket; None = templates switched off

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
            hospital_id=sources[doc.source_id].hospital_id,
        )

    @staticmethod
    def _hospitals(used: set[int], sources: dict[int, SourceRef]) -> set[int]:
        """The hospitals of these (readable) sources."""
        return {
            h for h in (sources[s].hospital_id for s in used if s in sources) if h is not None
        }

    def _guides(
        self,
        hospitals: set[int],
        guide_ids: list[int] | None,
        is_admin: bool,
        already: tuple[int, ...] = (),
        also: tuple[int, ...] = (),
    ) -> list[Guide]:
        """By default all of the hospitals' guides. A user may leave some out; only an admin may
        add a library guide that is not linked to these hospitals. `already`: guides the draft
        uses now, which may stay even if they were unlinked or archived since."""
        defaults = {g.id: g for g in self._store.guides_for_hospitals(sorted(hospitals))}
        # `also`: the default guides of the pipeline step's type (live ones), after the others.
        missing = [g for g in also if g not in defaults]
        for g in self._store.get_guides(missing) if missing else []:
            if g.archived_at is None:
                defaults[g.id] = g
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

    def _write(self, docs, guides, extra, request, current=None, admin_text=None, step=None):
        assert self._writer is not None
        text_of_admins = self._read_extra() if admin_text is None else admin_text
        instructions, text = build_input(
            docs, guides, extra, request, current, text_of_admins, step=step
        )
        try:
            return self._writer.write(instructions, text)
        except Exception as exc:
            log.warning("docs generator failed: %s", type(exc).__name__)
            raise UpstreamError(
                f"the AI could not write the draft ({type(exc).__name__}); please try again"
            ) from exc

    def _write_fields(
        self, docs, guides, extra, request, form: DraftTemplate, current=None, step=None
    ):
        """A template draft: one answer per field, checked against the form."""
        assert self._writer is not None
        instructions, text = build_input(
            docs, guides, extra, request, admin_text=self._read_extra(), form=form,
            current_answers=current, step=step,
        )
        try:
            written = self._writer.write_fields(instructions, text)
        except Exception as exc:
            log.warning("docs generator (form) failed: %s", type(exc).__name__)
            raise UpstreamError(
                f"the AI could not fill the form ({type(exc).__name__}); please try again"
            ) from exc
        return written.title, ai_answers(form, written.values)

    def _need_files(self) -> TemplateFiles:
        if self._files is None:
            raise UnavailableError(NO_TEMPLATES)
        return self._files

    def _template(self, template_id: int) -> FormTemplate:
        found = self._store.get_template(template_id)
        if found is None:
            raise NotFoundError("form template not found")
        return found

    def _draft_template(
        self, template_id: int, hospitals: set[int], is_admin: bool, also: int | None = None
    ) -> DraftTemplate:
        """A library template the user may fill for documents from these hospitals: ready, not
        archived, and global or offered by one of the hospitals (an admin may use any ready one)."""
        self._need_files()
        t = self._store.get_template(template_id)
        offered = t is not None and t.archived_at is None and (
            is_admin or t.is_global or bool(set(t.hospital_ids) & hospitals)
            or template_id == also  # the default form of the pipeline step's type
        )
        if t is None or not offered or (t.status != "ready" and not is_admin):
            raise NotFoundError(f"form template {template_id} not found")
        if t.status != "ready":
            raise ConflictError(f'the form template "{t.title}" is waiting for its field check; save its fields first')
        return DraftTemplate(t.id, t.title, t.filename, t.storage_path, t.fields)

    def _read_form(self, form: DraftTemplate) -> bytes:
        try:
            return self._need_files().get(form.storage_path)
        except UnavailableError:
            raise
        except Exception as exc:
            log.warning("form file %s could not be read: %s", form.storage_path, type(exc).__name__)
            raise UnavailableError("the form file could not be read from storage; please try again") from exc

    def _drop_files(self, paths: list[str]) -> None:
        """Best effort: a file left behind is only wasted space (logged)."""
        if not paths or self._files is None:
            return
        try:
            self._files.delete(paths)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not delete %d form file(s): %s", len(paths), type(exc).__name__)

    def _own(self, user_id: str, draft_id: str, need: str = "edit") -> Draft:
        """A standalone draft is its author's only. A draft made in a pipeline step belongs to
        the project (phase B1, D13): whoever may open the project reads it, and whoever may edit
        the project edits, revises and removes it. Its author keeps it if the step is gone."""
        draft = self._store.get_draft(draft_id)
        if draft is None:
            raise NotFoundError("draft not found")
        if draft.pipeline_step_id is not None and self._steps is not None:
            found = self._steps.step_access(user_id, draft.pipeline_step_id)
            if found is not None:
                if found[1] not in NEEDS[need]:
                    raise ForbiddenError(REFUSALS[need])
                return draft
            # The step still exists but the user may no longer open its project (removed from
            # it, moved to another organization): the draft goes with the project, even for
            # its author. Only a draft whose step is gone stays with its author.
            if self._steps.step_exists(draft.pipeline_step_id):
                raise NotFoundError("draft not found")
        if draft.user_id != user_id:
            raise NotFoundError("draft not found")
        return draft

    def check_own_draft(self, user_id: str, draft_id: str) -> None:
        """Raises unless the active user may change the draft (before a job row names it)."""
        self._profile(user_id)
        self._own(user_id, draft_id)

    def can_edit_draft(self, user_id: str, draft_id: str) -> bool:
        try:
            self._own(user_id, draft_id)
            return True
        except (ForbiddenError, NotFoundError):
            return False

    def preview(self, admin_id: str, extra: str, document_id: int | None, request: str) -> str:
        """Admin test run with UNSAVED instructions on one document (its newest version, with
        its source's guides). Nothing is saved or counted here; the caller limits and logs test
        runs. Sources with AI off are allowed (owner's choice for admin tests)."""
        docs = self.check_preview(admin_id, document_id)
        ref = self._store.get_source(docs[0].source_id)
        hospital = ref.hospital_id if ref is not None else None
        guides = self._store.guides_for_hospitals([hospital]) if hospital is not None else []
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
        """The guides a draft from these sources would use by default (their hospitals')."""
        profile = self._profile(user_id)
        readable = self._store.readable_sources(user_id, profile.role == "admin")
        hospitals = self._hospitals(set(source_ids), readable)
        return self._store.guides_for_hospitals(sorted(hospitals)) if hospitals else []

    # -- form templates for users -------------------------------------------------------

    def templates_for(self, user_id: str, source_ids: list[int]) -> list[FormTemplate]:
        """The ready forms a draft from these sources may fill: global ones and those the
        sources' hospitals offer. An admin also gets every other ready one (after these)."""
        profile = self._profile(user_id)
        if self._files is None:
            return []
        is_admin = profile.role == "admin"
        readable = self._store.readable_sources(user_id, is_admin)
        hospitals = self._hospitals(set(source_ids), readable)
        found = self._store.templates_for_hospitals(sorted(hospitals)) if hospitals else []
        if is_admin:
            ids = {t.id for t in found}
            found += [
                t for t in self._store.list_templates(False) if t.status == "ready" and t.id not in ids
            ]
        return found

    def check_template_file(self, user_id: str, upload: UploadedFile) -> tuple[str, tuple[FormField, ...]]:
        """The fields a one-off form would have, before a draft is made from it. Nothing is
        stored or counted."""
        self._profile(user_id)
        self._need_files()
        name, _, _, fields = read_template_file(upload)
        return name, fields

    # -- drafts ------------------------------------------------------------------------

    def create_draft(
        self,
        user_id: str,
        documents: list[DocChoice],
        guide_ids: list[int] | None,
        extra_files: list[UploadedFile],
        instruction: str,
        ai_override_source_ids: list[int],
        template_id: int | None = None,
        template_file: UploadedFile | None = None,
        step_id: int | None = None,
        step_files: list[tuple[int, int | None]] | None = None,
    ) -> DraftDetail:
        """Checks, counts and writes in one call (tests and scripts). The API runs the two
        halves apart: prepare_draft in the request, its result as a background job."""
        return self.prepare_draft(
            user_id, documents, guide_ids, extra_files, instruction, ai_override_source_ids,
            template_id, template_file, step_id, step_files,
        )()

    def prepare_draft(
        self,
        user_id: str,
        documents: list[DocChoice],
        guide_ids: list[int] | None,
        extra_files: list[UploadedFile],
        instruction: str,
        ai_override_source_ids: list[int],
        template_id: int | None = None,
        template_file: UploadedFile | None = None,
        step_id: int | None = None,
        step_files: list[tuple[int, int | None]] | None = None,
    ) -> Callable[[], DraftDetail]:
        """Everything a draft can be refused for, the daily limit and the audit entry that
        counts the call. Returns the work that calls the model and saves the draft.

        template_id (a library form) or template_file (a one-off DOCX for this draft): the
        draft is then one answer per field of that form, fixed for the draft's life.
        step_id: the pipeline step the draft is made in; step_files: the user's own step
        documents it is written from, as (document id, version or None for the newest). Board
        documents are then optional."""
        profile = self._profile(user_id)
        is_admin = profile.role == "admin"
        if template_id is not None and template_file is not None:
            raise ValidationError("choose a form from the list or attach one, not both")
        step: StepContext | None = None
        if step_id is not None:
            if self._steps is None:
                raise UnavailableError("pipeline drafts are not set up on this server")
            if template_file is not None:
                # A step's drafts are deleted with the step (database cascade), which would
                # leave a one-off form file behind; the step type's form or a library form is
                # used instead.
                raise ValidationError(
                    "a pipeline draft fills a form from the list, not an attached file"
                )
            step = self._steps.context(user_id, step_id, list(step_files or []))
            if documents and not step.allow_board_documents:
                raise ValidationError("this step's type does not use hospital board documents")
            if not documents and not step.files:
                raise ValidationError("choose at least one file or document")
        elif step_files:
            raise ValidationError("step files need a pipeline step")
        elif not documents:
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
        hospitals = self._hospitals(used, sources)
        if step is not None:
            hospitals.add(step.hospital_id)  # the pipeline's hospital, even with no board document
        guides = self._guides(
            hospitals, guide_ids, is_admin, also=step.guide_ids if step is not None else ()
        )
        form = (
            self._draft_template(
                template_id, hospitals, is_admin, also=step.form_template_id if step else None
            )
            if template_id is not None
            else None
        )

        # The limit is checked before the attached files are read (reading a PDF or HWP costs
        # time and memory); the log entry that counts the call comes after, just before it.
        now = self._clock()
        self._check_limit(user_id, now)
        extra = [ExtraGuide(*read_guide_file(upload)[:2]) for upload in extra_files]
        one_off: list[str] = []
        if template_file is not None:
            files = self._need_files()
            name, content, sha, fields = read_template_file(template_file)
            form = DraftTemplate(None, name, name, f"drafts/{uuid.uuid4().hex}/{sha}.docx", fields)
            # Stored before the paid call, so a storage failure costs nothing; removed again if
            # the draft is not saved.
            try:
                files.put(form.storage_path, content)
            except Exception as exc:
                log.warning("one-off form file could not be stored: %s", type(exc).__name__)
                raise UnavailableError("the form file could not be stored; please try again") from exc
            one_off.append(form.storage_path)

        try:
            # The log is also the counter: no entry, no model call.
            self._audit.record_required(
                "user",
                "docgen.generate",
                user_id,
                payload=_payload(refs, used, guides, extra, override, form, step),
            )
        except Exception:
            self._drop_files(one_off)
            raise
        fallback_title = docs[0].title if docs else (step.step_name if step else "")
        step_refs = tuple(f.ref for f in step.files) if step is not None else ()

        def work() -> DraftDetail:
            """The model call and the save. May run in a background job."""
            try:
                if form is None:
                    written = self._write(docs, guides, extra, request, step=step)
                    ai_title, markdown, values = written.title, written.markdown, None
                else:
                    ai_title, values = self._write_fields(docs, guides, extra, request, form, step=step)
                    markdown = ""
                title = (ai_title or fallback_title)[:MAX_TITLE_CHARS]
                if form is not None:
                    markdown = render_markdown(title, form.fields, values)
                new = NewDraft(
                    user_id=user_id,
                    title=title,
                    instruction=request,
                    document_refs=refs,
                    guide_ids=tuple(g.id for g in guides),
                    extra_guides=tuple(extra),
                    ai_override_source_ids=tuple(sorted(override)),
                    template=form,
                    pipeline_step_id=step.step_id if step is not None else None,
                    step_files=step_refs,
                )
                first = DraftVersion("", 1, markdown[:MAX_MARKDOWN_CHARS], "ai", request, fields=values)
                draft = self._store.create_draft(new, first, now)
            except Exception:
                self._drop_files(one_off)
                raise
            return DraftDetail(draft, self._store.list_versions(draft.id), guides)

        return work

    def list_drafts(self, user_id: str) -> list[Draft]:
        self._profile(user_id)
        return self._store.list_drafts(user_id, 100)

    def get_draft(self, user_id: str, draft_id: str) -> DraftDetail:
        self._profile(user_id)
        draft = self._own(user_id, draft_id, "read")
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
        self,
        user_id: str,
        draft_id: str,
        markdown: str,
        base_version_no: int | None = None,
        fields: dict[str, FieldValue] | None = None,
    ) -> DraftVersion:
        """base_version_no: the version the user's edit started from. When a newer version
        exists (another tab, or an AI revision), the save is refused instead of silently
        building on old text. A template draft is saved as `fields` (one answer per field);
        its markdown is then made from them."""
        self._profile(user_id)
        draft = self._own(user_id, draft_id)
        answers = None
        if draft.template is not None:
            if fields is None:
                raise ValidationError("this draft fills a form; send its answers (fields)")
            answers = user_answers(draft.template, fields)
            text = render_markdown(draft.title, draft.template.fields, answers)
        else:
            if fields is not None:
                raise ValidationError("this draft has no form; send its text (markdown)")
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
        if answers is not None and versions[-1].fields == answers:
            return versions[-1]
        if answers is None and versions[-1].markdown == text:
            return versions[-1]
        try:
            return self._store.add_version(
                draft, text[:MAX_MARKDOWN_CHARS], "user_edit", "", self._clock(), fields=answers
            )
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
        """Checks, counts and writes in one call (tests and scripts); see prepare_revision."""
        return self.prepare_revision(user_id, draft_id, instruction, ai_override_source_ids, change)()

    def prepare_revision(
        self,
        user_id: str,
        draft_id: str,
        instruction: str,
        ai_override_source_ids: list[int] | None = None,
        change: InputChange | None = None,
    ) -> Callable[[], DraftVersion]:
        """Everything a revision can be refused for, the daily limit and the audit entry that
        counts the call. Returns the work that calls the model and saves the new version.

        ai_override_source_ids: AI-off sources the user ticked now (needed when an admin
        switched AI off for a source after the draft was made, or a new one was added).
        change: new documents and guides from the side panel; None keeps the draft's current
        ones. Either way the inputs are saved on the new version. A pipeline draft reads its
        own step files again (the versions it was written from); they cannot be changed here."""
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
            if not choices and not draft.step_files:
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

        # A pipeline draft: the step must still be the user's own and live, and its files are
        # read again at the versions the draft was written from.
        step: StepContext | None = None
        if draft.pipeline_step_id is not None:
            if self._steps is None:
                raise UnavailableError("pipeline drafts are not set up on this server")
            try:
                step = self._steps.context(
                    user_id, draft.pipeline_step_id,
                    [(f.document_id, f.version_no) for f in draft.step_files],
                )
            except (NotFoundError, ValidationError) as exc:
                # The draft's own files cannot be changed in a revision, so say what to do.
                raise ConflictError(
                    f"this draft's own files can no longer be used ({exc.message}); "
                    "edit the draft by hand or start a new draft for the step"
                ) from exc
            if change is not None and choices and not step.allow_board_documents:
                raise ValidationError("this step's type does not use hospital board documents")

        # Access is checked again: the user may have lost a study since the draft was made.
        sources = self._store.readable_sources(user_id, is_admin)
        docs, refs = self._load_docs(choices, sources)
        used = {d.source_id for d in docs}
        override = (set(draft.ai_override_source_ids) | set(ai_override_source_ids or [])) & used
        self._ai_check(sources, used, override)
        if change is None:
            guides = self._store.get_guides(list(draft.guide_ids)) if draft.guide_ids else []
        else:
            hospitals = self._hospitals(used, sources)
            if step is not None:
                hospitals.add(step.hospital_id)
            guides = self._guides(
                hospitals, change.guide_ids, is_admin, draft.guide_ids,
                also=step.guide_ids if step is not None else (),
            )

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
                **_payload(refs, used, guides, extra, override, draft.template, step),
            },
        )
        form = draft.template
        inputs = Inputs(
            refs, tuple(g.id for g in guides), tuple(extra), tuple(sorted(override)),
            tuple(f.ref for f in step.files) if step is not None else (),
        )
        # The draft row (with the attached guides' text) is rewritten only when its selection
        # changed: new inputs from the side panel, or a newly ticked AI-off source.
        changed = change is not None or set(override) != set(draft.ai_override_source_ids)

        def work() -> DraftVersion:
            """The model call and the save. May run in a background job, minutes after the
            checks: the newest version is read now, so a hand edit saved while the job waited
            is the text that gets revised (never an older one)."""
            latest = self._latest(draft_id)[-1]
            values = None
            if form is None:
                written = self._write(docs, guides, extra, request, current=latest.markdown, step=step)
                markdown = written.markdown[:MAX_MARKDOWN_CHARS]
            else:
                # The form never changes for a draft; the current answers go in place of the text.
                current = latest.fields or {}
                _, values = self._write_fields(
                    docs, guides, extra, request, form, current=current, step=step
                )
                markdown = render_markdown(draft.title, form.fields, values)[:MAX_MARKDOWN_CHARS]
            try:
                return self._store.add_version(
                    draft, markdown, "ai_revision", request, now, inputs, changed, values
                )
            except VersionConflictError:
                # A hand edit was saved while the AI was writing. The revision was paid for, so
                # it is kept as the next version rather than thrown away; the user sees both.
                try:
                    return self._store.add_version(
                        draft, markdown, "ai_revision", request, now, inputs, changed, values
                    )
                except VersionConflictError as exc:
                    raise ConflictError(
                        "the draft kept changing while the AI was writing; reload it and try again"
                    ) from exc

        return work

    def step_drafts(self, user_id: str, step_ids: list[int]) -> list[Draft]:
        """The drafts made in these pipeline steps by anyone (shared with the project, phase B1),
        newest first. The caller has checked that the user may open the steps."""
        self._profile(user_id)
        if not step_ids:
            return []
        return self._store.drafts_of_steps(step_ids)

    def export_docx(
        self, user_id: str, draft_id: str, version_no: int | None
    ) -> tuple[bytes, str]:
        self._profile(user_id)
        draft = self._own(user_id, draft_id, "read")
        versions = self._store.list_versions(draft_id)
        if version_no is None:
            chosen = versions[-1] if versions else None
        else:
            chosen = next((v for v in versions if v.version_no == version_no), None)
        if chosen is None:
            raise NotFoundError("that draft version does not exist")
        form = draft.template
        if form is None:
            content = build_docx(chosen.markdown)
        else:
            template = self._read_form(form)
            try:
                content = fill(form.filename, template, form.fields, chosen.fields or {})
            except FormError as exc:
                raise UnavailableError(f"the form file could not be filled: {exc}") from exc
        self._audit.record(
            "user",
            "docgen.export",
            user_id,
            payload={
                "draft_id": draft_id,
                "version_no": chosen.version_no,
                **({"template_id": form.template_id, "filename": form.filename} if form else {}),
            },
        )
        return content, docx_filename(draft.title, chosen.version_no)

    def delete_draft(self, user_id: str, draft_id: str) -> None:
        self._profile(user_id)
        draft = self._own(user_id, draft_id)
        # Deleting cannot be undone: a shared pipeline draft goes only by its author, the
        # project's owner or an organization admin (members edit and revise it).
        if draft.user_id != user_id and draft.pipeline_step_id is not None and self._steps is not None:
            found = self._steps.step_access(user_id, draft.pipeline_step_id)
            if found is None or found[1] not in NEEDS["manage"]:
                raise ForbiddenError(
                    "only the draft's author, the project's owner or an organization admin can delete it"
                )
        self._store.delete_draft(draft_id)
        # A one-off form file belongs to this draft only; a library file is shared.
        if draft.template is not None and draft.template.template_id is None:
            self._drop_files([draft.template.storage_path])

    def purge_old(self, days: int = RETENTION_DAYS) -> int:
        """Delete drafts unchanged for `days`, with their one-off form files. Never raises: it
        runs from the scheduler."""
        try:
            count, paths = self._store.purge_before(self._clock() - timedelta(days=days))
            self._drop_files(paths)  # the one-off form files of exactly the deleted drafts
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
            payload={"guide_id": guide_id, "unlinked_hospital_ids": list(guide.hospital_ids)},
        )

    def hospital_guides(self, admin_id: str, hospital_id: int) -> list[Guide]:
        self._admin(admin_id)
        if not self._store.hospital_exists(hospital_id):
            raise NotFoundError("hospital not found")
        return self._store.guides_for_hospitals([hospital_id])

    def link_guide(self, admin_id: str, hospital_id: int, guide_id: int) -> None:
        self._admin(admin_id)
        if not self._store.hospital_exists(hospital_id):
            raise NotFoundError("hospital not found")
        guide = self._guide(guide_id)
        if guide.archived_at is not None:
            raise ConflictError("this guide is archived; upload it again to use it")
        if guide.is_global:
            raise ConflictError("this guide is global and applies to every hospital already")
        if hospital_id in guide.hospital_ids:
            return
        self._store.link_guide(hospital_id, guide_id, self._clock())
        self._audit.record(
            "admin",
            "docgen.guide_linked",
            admin_id,
            title=guide.title,
            payload={"guide_id": guide_id, "hospital_id": hospital_id},
        )

    def guide(self, admin_id: str, guide_id: int) -> Guide:
        self._admin(admin_id)
        return self._guide(guide_id)

    def update_guide(
        self, admin_id: str, guide_id: int, title: str | None, is_global: bool | None
    ) -> Guide:
        """Rename a guide and/or make it global (a default of every draft for every source) or
        hospital-specific again. Its hospital links are kept either way (unused while global)."""
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

    def set_guide_hospitals(self, admin_id: str, guide_id: int, hospital_ids: list[int]) -> Guide:
        """The full list of hospitals that use this guide by default (added and removed in one
        call, one audit entry). Refused for a global or archived guide."""
        self._admin(admin_id)
        guide = self._guide(guide_id)
        if guide.archived_at is not None:
            raise ConflictError("this guide is archived; upload it again to use it")
        if guide.is_global:
            raise ConflictError("this guide is global and applies to every hospital; make it hospital-specific first")
        known = self._store.hospital_ids()
        wanted = set(hospital_ids)
        unknown = sorted(wanted - known)
        if unknown:
            raise NotFoundError(f"hospital {unknown[0]} not found")
        current = set(guide.hospital_ids)
        added, removed = sorted(wanted - current), sorted(current - wanted)
        if not added and not removed:
            return guide
        # Written first, fail closed (as for study members): no entry, no change. The links are
        # separate writes; if one fails part-way, the entry already names every intended change.
        self._audit.record_required(
            "admin",
            "docgen.guide_hospitals_set",
            admin_id,
            title=guide.title,
            payload={"guide_id": guide_id, "added": added, "removed": removed},
        )
        now = self._clock()
        for hid in added:
            self._store.link_guide(hid, guide_id, now)
        for hid in removed:
            self._store.unlink_guide(hid, guide_id)
        return self._guide(guide_id)

    def unlink_guide(self, admin_id: str, hospital_id: int, guide_id: int) -> None:
        self._admin(admin_id)
        guide = self._guide(guide_id)
        if hospital_id not in guide.hospital_ids:
            raise NotFoundError("this guide is not linked to that hospital")
        self._store.unlink_guide(hospital_id, guide_id)
        self._audit.record(
            "admin",
            "docgen.guide_unlinked",
            admin_id,
            title=guide.title,
            payload={"guide_id": guide_id, "hospital_id": hospital_id},
        )

    # -- the admin's form templates -----------------------------------------------------
    # A new or replaced file starts in 'review': the admin checks the detected fields (keeps,
    # renames, adds a note for the AI) and saving them makes the template 'ready'. Files are
    # never deleted, because drafts made from a template keep pointing at its file.

    def list_templates(self, admin_id: str, include_archived: bool) -> list[FormTemplate]:
        self._admin(admin_id)
        return self._store.list_templates(include_archived)

    def template(self, admin_id: str, template_id: int) -> FormTemplate:
        self._admin(admin_id)
        return self._template(template_id)

    @staticmethod
    def _clean_title(title: str) -> str:
        clean = (title or "").strip()
        if not clean or len(clean) > MAX_TITLE_CHARS:
            raise ValidationError(f"the title must be 1 to {MAX_TITLE_CHARS} characters")
        return clean

    @staticmethod
    def _clean_doc_type(doc_type: str) -> str:
        clean = " ".join((doc_type or "").split())
        if len(clean) > MAX_DOC_TYPE_CHARS:
            raise ValidationError(f"the document type is too long (at most {MAX_DOC_TYPE_CHARS} characters)")
        return clean

    def _store_file(self, content: bytes, sha: str) -> str:
        path = f"library/{uuid.uuid4().hex}/{sha}.docx"
        try:
            self._need_files().put(path, content)
        except UnavailableError:
            raise
        except Exception as exc:
            log.warning("form template file could not be stored: %s", type(exc).__name__)
            raise UnavailableError("the form file could not be stored; please try again") from exc
        return path

    def create_template(
        self, admin_id: str, title: str, doc_type: str, upload: UploadedFile
    ) -> FormTemplate:
        self._admin(admin_id)
        self._need_files()
        clean = self._clean_title(title)
        kind = self._clean_doc_type(doc_type)
        name, content, sha, fields = read_template_file(upload)
        path = self._store_file(content, sha)
        try:
            t = self._store.create_template(clean, kind, name, path, sha, fields, admin_id, self._clock())
        except Exception:
            self._drop_files([path])
            raise
        self._audit.record(
            "admin",
            "docgen.template_created",
            admin_id,
            title=clean,
            payload={"template_id": t.id, "filename": name, "fields": len(fields)},
        )
        return t

    def detected_fields(self, admin_id: str, template_id: int) -> tuple[FormField, ...]:
        """Every field found in the template's current file, including ones the admin removed
        (so they can be added back)."""
        self._admin(admin_id)
        t = self._template(template_id)
        content = self._read_form(DraftTemplate(t.id, t.title, t.filename, t.storage_path, ()))
        try:
            return detect_fields(t.filename, content)
        except FormError as exc:
            raise UnavailableError(f"the form file could not be read again: {exc}") from exc

    def save_template_fields(
        self, admin_id: str, template_id: int, edits: list[FieldEdit]
    ) -> FormTemplate:
        """The checked list of fields, in the admin's order; the template becomes ready."""
        t = self._template_open(admin_id, template_id)
        detected = {f.key: f for f in self.detected_fields(admin_id, template_id)}
        if not edits:
            raise ValidationError("keep at least one field")
        out: list[FormField] = []
        seen: set[str] = set()
        for e in edits:
            f = detected.get(e.key)
            if f is None:
                raise ValidationError(f'the form has no field "{e.key}"')
            if e.key in seen:
                raise ValidationError(f'the field "{e.key}" is listed twice')
            seen.add(e.key)
            label = " ".join((e.label or "").split())
            if not label or len(label) > 120:
                raise ValidationError(f'the label of "{e.key}" must be 1 to 120 characters')
            hint = " ".join((e.hint or "").split())
            if len(hint) > MAX_HINT_CHARS:
                raise ValidationError(f'the note of "{label}" is too long (at most {MAX_HINT_CHARS} characters)')
            out.append(replace(f, label=label, hint=hint, multi=e.multi if f.kind == "choice" else False))
        self._store.update_template(template_id, {"fields": tuple(out), "status": "ready"}, self._clock())
        self._audit.record(
            "admin",
            "docgen.template_fields_saved",
            admin_id,
            title=t.title,
            payload={"template_id": template_id, "fields": len(out), "detected": len(detected)},
        )
        return self._template(template_id)

    def _template_open(self, admin_id: str, template_id: int) -> FormTemplate:
        self._admin(admin_id)
        t = self._template(template_id)
        if t.archived_at is not None:
            raise ConflictError("this form template is archived; upload it again to use it")
        return t

    def update_template(
        self,
        admin_id: str,
        template_id: int,
        title: str | None,
        doc_type: str | None,
        is_global: bool | None,
    ) -> FormTemplate:
        t = self._template_open(admin_id, template_id)
        changes: dict = {}
        if title is not None and self._clean_title(title) != t.title:
            changes["title"] = self._clean_title(title)
        if doc_type is not None and self._clean_doc_type(doc_type) != t.doc_type:
            changes["doc_type"] = self._clean_doc_type(doc_type)
        if is_global is not None and is_global != t.is_global:
            changes["is_global"] = is_global
        if not changes:
            return t
        self._store.update_template(template_id, changes, self._clock())
        self._audit.record(
            "admin",
            "docgen.template_updated",
            admin_id,
            title=changes.get("title", t.title),
            payload={"template_id": template_id, **changes, **({"old_title": t.title} if "title" in changes else {})},
        )
        return self._template(template_id)

    def replace_template_file(self, admin_id: str, template_id: int, upload: UploadedFile) -> FormTemplate:
        """A new file; its fields are found again and the template goes back to 'review'.
        Drafts already made keep the old file. The admin's earlier check is reused only for the
        SAME field: a marker of the same name, or a found blank whose place, detected label and
        options are unchanged (a found blank's key is its position, which moves when the form
        is edited). Such a field keeps its label, note and 'several allowed', or stays dropped;
        everything else starts as detected."""
        t = self._template_open(admin_id, template_id)
        self._need_files()
        name, content, sha, fields = read_template_file(upload)
        if sha == t.sha256:
            raise ConflictError("this is the same file as the template's current file; nothing changed")
        carried = self._carry_review(t, fields)
        path = self._store_file(content, sha)
        try:
            self._store.update_template(
                template_id,
                {"filename": name, "storage_path": path, "sha256": sha, "fields": carried, "status": "review"},
                self._clock(),
            )
        except Exception:
            self._drop_files([path])
            raise
        self._audit.record(
            "admin",
            "docgen.template_replaced",
            admin_id,
            title=t.title,
            payload={"template_id": template_id, "filename": name, "old_filename": t.filename, "fields": len(fields)},
        )
        return self._template(template_id)

    def _carry_review(self, t: FormTemplate, fields: tuple[FormField, ...]) -> tuple[FormField, ...]:
        kept = {f.key: f for f in t.fields}
        try:
            old_file = self._read_form(DraftTemplate(t.id, t.title, t.filename, t.storage_path, ()))
            before = {f.key: f for f in detect_fields(t.filename, old_file)}
        except (UnavailableError, FormError) as exc:
            # Without the old file only markers can be matched safely (by name).
            log.warning("template %s: old file not re-read (%s); only markers keep their review", t.id, exc)
            before = {f.key: f for f in t.fields if f.source == "marker"}

        def same(f: FormField) -> bool:
            b = before.get(f.key)
            if b is None:
                return False
            return f.source == "marker" or (b.label == f.label and b.options == f.options and b.kind == f.kind)

        out = []
        for f in fields:
            if not same(f):
                out.append(f)  # new, or moved: as detected
            elif f.key in kept:
                k = kept[f.key]
                out.append(replace(f, label=k.label, hint=k.hint, multi=k.multi and f.kind == "choice"))
            # else: the admin dropped this same field before; it stays dropped
        return tuple(out) or fields  # never an empty list: the admin reviews it anyway

    def set_template_hospitals(self, admin_id: str, template_id: int, hospital_ids: list[int]) -> FormTemplate:
        """The full list of hospitals that offer this form (one audit entry, written first)."""
        t = self._template_open(admin_id, template_id)
        if t.is_global:
            raise ConflictError("this form is global and offered to every hospital; make it hospital-specific first")
        known = self._store.hospital_ids()
        wanted = set(hospital_ids)
        unknown = sorted(wanted - known)
        if unknown:
            raise NotFoundError(f"hospital {unknown[0]} not found")
        current = set(t.hospital_ids)
        added, removed = sorted(wanted - current), sorted(current - wanted)
        if not added and not removed:
            return t
        self._audit.record_required(
            "admin",
            "docgen.template_hospitals_set",
            admin_id,
            title=t.title,
            payload={"template_id": template_id, "added": added, "removed": removed},
        )
        now = self._clock()
        for hid in added:
            self._store.link_template(hid, template_id, now)
        for hid in removed:
            self._store.unlink_template(hid, template_id)
        return self._template(template_id)

    def archive_template(self, admin_id: str, template_id: int) -> None:
        self._admin(admin_id)
        t = self._template(template_id)
        if t.archived_at is not None:
            return
        self._store.archive_template(template_id, self._clock())
        self._audit.record(
            "admin",
            "docgen.template_archived",
            admin_id,
            title=t.title,
            payload={"template_id": template_id, "unlinked_hospital_ids": list(t.hospital_ids)},
        )
