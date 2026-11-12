"""The docs generator's API: a user's own drafts (login token of any active user).

Files are sent as base64 inside JSON (no form-parsing package is needed). The DOCX comes back
as a file download from POST /docgen/drafts/{id}/export.
"""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from irb_watch.api.deps import (
    current_user,
    get_docgen_service,
    get_draft_job_service,
    get_pipeline_service,
)
from irb_watch.auth.jwt import AuthUser
from irb_watch.docgen.types import (
    DocRef,
    Draft,
    DraftTemplate,
    DraftVersion,
    FormField,
    FormTemplate,
    Guide,
)
from irb_watch.services.docgen import (
    MAX_EXTRA_GUIDES,
    DocChoice,
    DocgenService,
    DraftDetail,
    InputChange,
    UploadedFile,
)
from irb_watch.services.draft_jobs import DraftJobService, Job
from irb_watch.services.pipelines import PipelineService

router = APIRouter(prefix="/docgen", tags=["docgen"])

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class StatusOut(BaseModel):
    enabled: bool
    used: int
    limit: int
    reason: str | None = None


class GuideOut(BaseModel):
    id: int
    title: str
    filename: str
    chars: int
    hospital_ids: list[int]
    created_at: str
    archived_at: str | None = None
    is_global: bool = False  # a default of every draft; hospital_ids are unused while true
    text_updated_at: str | None = None  # when the file was last replaced

    @classmethod
    def of(cls, g: Guide) -> "GuideOut":
        return cls(
            id=g.id,
            title=g.title,
            filename=g.filename,
            chars=len(g.body_text),
            hospital_ids=list(g.hospital_ids),
            created_at=g.created_at,
            archived_at=g.archived_at,
            is_global=g.is_global,
            text_updated_at=g.text_updated_at,
        )


class DocRefOut(BaseModel):
    document_id: int
    version_no: int
    source_id: int
    files: list[str] | None = None  # None = every attached file
    page_text: bool = True
    compare_with: int | None = None  # None = the version just before
    compare: bool = True

    @classmethod
    def of(cls, r: DocRef) -> "DocRefOut":
        return cls(
            document_id=r.document_id,
            version_no=r.version_no,
            source_id=r.source_id,
            files=list(r.files) if r.files is not None else None,
            page_text=r.page_text,
            compare_with=r.compare_with,
            compare=r.compare,
        )


class FormFieldOut(BaseModel):
    key: str
    label: str
    kind: str  # text | choice
    source: str  # marker ({{name}} typed in the file) | auto (found by the server)
    where: str
    options: list[str]
    multi: bool
    hint: str

    @classmethod
    def of(cls, f: FormField) -> "FormFieldOut":
        return cls(
            key=f.key,
            label=f.label,
            kind=f.kind,
            source=f.source,
            where=f.where,
            options=list(f.options),
            multi=f.multi,
            hint=f.hint,
        )


class DraftTemplateOut(BaseModel):
    template_id: int | None  # None = a one-off file attached to this draft
    title: str
    filename: str
    fields: list[FormFieldOut]

    @classmethod
    def of(cls, t: DraftTemplate) -> "DraftTemplateOut":
        return cls(
            template_id=t.template_id,
            title=t.title,
            filename=t.filename,
            fields=[FormFieldOut.of(f) for f in t.fields],
        )


class TemplateOut(BaseModel):
    id: int
    title: str
    doc_type: str
    filename: str
    is_global: bool
    status: str  # review | ready
    hospital_ids: list[int]
    fields: list[FormFieldOut]
    created_at: str
    updated_at: str
    archived_at: str | None = None

    @classmethod
    def of(cls, t: FormTemplate) -> "TemplateOut":
        return cls(
            id=t.id,
            title=t.title,
            doc_type=t.doc_type,
            filename=t.filename,
            is_global=t.is_global,
            status=t.status,
            hospital_ids=list(t.hospital_ids),
            fields=[FormFieldOut.of(f) for f in t.fields],
            created_at=t.created_at,
            updated_at=t.updated_at,
            archived_at=t.archived_at,
        )


class JobOut(BaseModel):
    """A background draft or revision job. done: draft_id is the draft to open. failed: error
    is the reason, written for the user."""

    id: str
    kind: str  # draft | revision
    status: str  # queued | running | done | failed
    error: str | None = None
    draft_id: str | None = None
    pipeline_step_id: int | None = None
    created_at: str
    finished_at: str | None = None

    @classmethod
    def of(cls, j: Job) -> "JobOut":
        return cls(
            id=j.id, kind=j.kind, status=j.status, error=j.error, draft_id=j.draft_id,
            pipeline_step_id=j.pipeline_step_id, created_at=j.created_at, finished_at=j.finished_at,
        )


class StepFileOut(BaseModel):
    document_id: int
    version_no: int
    name: str
    step_id: int
    step_name: str


class DraftOut(BaseModel):
    id: str
    title: str
    instruction: str
    documents: list[DocRefOut]
    guide_ids: list[int]
    extra_guides: list[str]  # file names
    ai_override_source_ids: list[int]
    created_at: str
    updated_at: str
    template: DraftTemplateOut | None = None  # None = a Markdown draft
    pipeline_step_id: int | None = None  # made in this pipeline step
    step_files: list[StepFileOut] = Field(default_factory=list)  # the user's own files it uses

    @classmethod
    def of(cls, d: Draft) -> "DraftOut":
        return cls(
            id=d.id,
            title=d.title,
            instruction=d.instruction,
            documents=[DocRefOut.of(r) for r in d.document_refs],
            guide_ids=list(d.guide_ids),
            extra_guides=[e.filename for e in d.extra_guides],
            ai_override_source_ids=list(d.ai_override_source_ids),
            created_at=d.created_at,
            updated_at=d.updated_at,
            template=DraftTemplateOut.of(d.template) if d.template else None,
            pipeline_step_id=d.pipeline_step_id,
            step_files=[StepFileOut(**f.to_json()) for f in d.step_files],
        )


class VersionOut(BaseModel):
    version_no: int
    markdown: str
    origin: str
    instruction: str
    created_at: str
    # What an AI version was written from (documents with their files, guide ids, attached
    # guide file names); None for hand edits and versions saved before this was recorded.
    inputs: dict | None = None
    # Template drafts: one answer per field key (text, or the picked options).
    fields: dict[str, str | list[str]] | None = None

    @classmethod
    def of(cls, v: DraftVersion) -> "VersionOut":
        return cls(
            version_no=v.version_no,
            markdown=v.markdown,
            origin=v.origin,
            instruction=v.instruction,
            created_at=v.created_at,
            inputs=v.inputs,
            fields=v.fields,
        )


class DraftDetailOut(BaseModel):
    draft: DraftOut
    versions: list[VersionOut]
    guides: list[GuideOut]

    @classmethod
    def of(cls, d: DraftDetail) -> "DraftDetailOut":
        return cls(
            draft=DraftOut.of(d.draft),
            versions=[VersionOut.of(v) for v in d.versions],
            guides=[GuideOut.of(g) for g in d.guides],
        )


class DocChoiceIn(BaseModel):
    document_id: int
    version_no: int | None = None  # None = the newest version
    # Attached files to send, by name; None = all of them.
    files: list[str] | None = Field(default=None, max_length=200)
    page_text: bool = True  # False leaves the post's own text out
    compare_with: int | None = None  # an earlier version; None = the one just before
    compare: bool = True  # False sends no list of changes

    def choice(self) -> DocChoice:
        return DocChoice(
            self.document_id,
            self.version_no,
            tuple(self.files) if self.files is not None else None,
            self.page_text,
            self.compare_with,
            self.compare,
        )


class FileIn(BaseModel):
    filename: str = Field(max_length=300)
    # 5 MB of file is about 6.7 million base64 characters; the real limit is in the service.
    content_base64: str = Field(max_length=7_000_000)


class DraftIn(BaseModel):
    documents: list[DocChoiceIn] = Field(max_length=50)
    # None: every guide linked to the documents' sources. A list: exactly these (leave some out).
    guide_ids: list[int] | None = Field(default=None, max_length=50)
    extra_guides: list[FileIn] = Field(default_factory=list, max_length=MAX_EXTRA_GUIDES)
    instruction: str = Field(default="", max_length=20_000)
    # Sources with AI switched off whose text the user agreed to send to OpenAI for this draft.
    ai_override_source_ids: list[int] = Field(default_factory=list, max_length=50)
    # A form to fill (one answer per field): a library template, or a one-off .docx file.
    template_id: int | None = None
    template_file: FileIn | None = None


class StepFileIn(BaseModel):
    document_id: int  # a step document of the user's pipeline
    version_no: int | None = None  # None = the newest version


class StepDraftIn(DraftIn):
    """A draft made in a pipeline step: the user's own step files (named in the dialog, so
    starting is the consent to send them to the AI service) and, when the step's type allows,
    hospital board documents."""

    documents: list[DocChoiceIn] = Field(default_factory=list, max_length=50)
    step_files: list[StepFileIn] = Field(default_factory=list, max_length=50)


class MarkdownIn(BaseModel):
    markdown: str = Field(default="", max_length=500_000)
    # The version the edit started from; a newer one on the server answers 409.
    base_version_no: int | None = None
    # A template draft sends its answers instead of markdown.
    fields: dict[str, str | list[str]] | None = Field(default=None, max_length=500)


class TemplateFileIn(BaseModel):
    file: FileIn


class TemplateCheckOut(BaseModel):
    filename: str
    fields: list[FormFieldOut]


class InputsIn(BaseModel):
    """New documents and guides for a revision (they replace the draft's current ones)."""

    documents: list[DocChoiceIn] = Field(max_length=50)
    # None: every guide linked to the documents' sources. A list: exactly these.
    guide_ids: list[int] | None = Field(default=None, max_length=50)
    # Names of the draft's attached guide files to keep, plus new files.
    keep_extra_guides: list[str] = Field(default_factory=list, max_length=MAX_EXTRA_GUIDES)
    extra_guides: list[FileIn] = Field(default_factory=list, max_length=MAX_EXTRA_GUIDES)


class ReviseIn(BaseModel):
    instruction: str = Field(max_length=20_000)
    # Needed when AI was switched off for a source after the draft was made, or for a newly
    # added AI-off source (409 names it).
    ai_override_source_ids: list[int] = Field(default_factory=list, max_length=50)
    inputs: InputsIn | None = None  # None: the draft's current documents and guides


@router.get("/status", response_model=StatusOut)
def status(
    user: AuthUser = Depends(current_user), service: DocgenService = Depends(get_docgen_service)
) -> StatusOut:
    return StatusOut(**service.status(user.id))


@router.get("/guides", response_model=list[GuideOut])
def default_guides(
    source_id: list[int] = Query(default_factory=list),
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> list[GuideOut]:
    """The guides a draft from these sources uses by default (sources the user may read)."""
    return [GuideOut.of(g) for g in service.default_guides(user.id, source_id)]


@router.post("/drafts", status_code=202, response_model=JobOut)
def create_draft(
    body: DraftIn,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
    jobs: DraftJobService = Depends(get_draft_job_service),
) -> JobOut:
    """Checks the request and starts a background job that writes the first draft with the AI
    (up to a few minutes). Refusals are answered at once (409 names any AI-off source that was
    not ticked); afterwards poll GET /docgen/jobs/{id}: `done` carries the new draft's id,
    `failed` the reason."""
    job = jobs.start(
        user.id,
        "draft",
        lambda: service.prepare_draft(
            user.id,
            [d.choice() for d in body.documents],
            body.guide_ids,
            [UploadedFile(f.filename, f.content_base64) for f in body.extra_guides],
            body.instruction,
            body.ai_override_source_ids,
            body.template_id,
            UploadedFile(body.template_file.filename, body.template_file.content_base64)
            if body.template_file
            else None,
        ),
    )
    return JobOut.of(job)


@router.post("/steps/{step_id}/drafts", status_code=202, response_model=JobOut)
def create_step_draft(
    step_id: int,
    body: StepDraftIn,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
    jobs: DraftJobService = Depends(get_draft_job_service),
    pipelines: PipelineService = Depends(get_pipeline_service),
) -> JobOut:
    """A draft for one step of the user's own IRB pipeline, as a background job (one open job
    per step). It is written from the chosen step files and board documents with the step
    type's instructions, guides and form."""
    job = jobs.start(
        user.id,
        "draft",
        lambda: service.prepare_draft(
            user.id,
            [d.choice() for d in body.documents],
            body.guide_ids,
            [UploadedFile(f.filename, f.content_base64) for f in body.extra_guides],
            body.instruction,
            body.ai_override_source_ids,
            body.template_id,
            UploadedFile(body.template_file.filename, body.template_file.content_base64)
            if body.template_file
            else None,
            step_id=step_id,
            step_files=[(f.document_id, f.version_no) for f in body.step_files],
        ),
        step_id=step_id,
        owns=lambda: pipelines.check_own_step(user.id, step_id),
    )
    pipelines.draft_started(user.id, step_id, "draft", job.id)
    return JobOut.of(job)


@router.get("/pipelines/{pipeline_id}/drafts", response_model=list[DraftOut])
def pipeline_drafts(
    pipeline_id: int,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
    pipelines: PipelineService = Depends(get_pipeline_service),
) -> list[DraftOut]:
    """The drafts made in the steps of the user's own pipeline, newest first (each names its
    step in pipeline_step_id)."""
    step_ids = pipelines.step_ids(user.id, pipeline_id)
    return [DraftOut.of(d) for d in service.step_drafts(user.id, step_ids)]


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    user: AuthUser = Depends(current_user),
    jobs: DraftJobService = Depends(get_draft_job_service),
) -> list[JobOut]:
    """The user's open draft jobs, and those that failed in the last 24 hours."""
    return [JobOut.of(j) for j in jobs.list(user.id)]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: UUID,
    user: AuthUser = Depends(current_user),
    jobs: DraftJobService = Depends(get_draft_job_service),
) -> JobOut:
    return JobOut.of(jobs.get(user.id, str(job_id)))


@router.get("/templates", response_model=list[TemplateOut])
def templates(
    source_id: list[int] = Query(default_factory=list),
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> list[TemplateOut]:
    """The forms a draft from these sources may fill (global ones and the sources' own)."""
    return [TemplateOut.of(t) for t in service.templates_for(user.id, source_id)]


@router.post("/templates/check", response_model=TemplateCheckOut)
def check_template(
    body: TemplateFileIn,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> TemplateCheckOut:
    """The fields the server finds in a .docx form, before a draft is made from it. Nothing is
    stored or counted."""
    name, fields = service.check_template_file(
        user.id, UploadedFile(body.file.filename, body.file.content_base64)
    )
    return TemplateCheckOut(filename=name, fields=[FormFieldOut.of(f) for f in fields])


@router.get("/drafts", response_model=list[DraftOut])
def list_drafts(
    user: AuthUser = Depends(current_user), service: DocgenService = Depends(get_docgen_service)
) -> list[DraftOut]:
    return [DraftOut.of(d) for d in service.list_drafts(user.id)]


@router.get("/drafts/{draft_id}", response_model=DraftDetailOut)
def get_draft(
    draft_id: UUID,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> DraftDetailOut:
    return DraftDetailOut.of(service.get_draft(user.id, str(draft_id)))


@router.put("/drafts/{draft_id}/markdown", response_model=VersionOut)
def save_edit(
    draft_id: UUID,
    body: MarkdownIn,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> VersionOut:
    """Saves a hand edit as a new version (no AI, not counted). A template draft sends
    `fields` (its answers) instead of markdown."""
    return VersionOut.of(
        service.save_edit(user.id, str(draft_id), body.markdown, body.base_version_no, body.fields)
    )


@router.post("/drafts/{draft_id}/revise", status_code=202, response_model=JobOut)
def revise(
    draft_id: UUID,
    body: ReviseIn,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
    jobs: DraftJobService = Depends(get_draft_job_service),
) -> JobOut:
    """Checks the request and starts a background job in which the AI changes the newest
    version as instructed; the result is saved as a new version. With `inputs`, the revision
    reads those documents and guides, and they become the draft's. Poll GET /docgen/jobs/{id}."""
    change = None
    if body.inputs is not None:
        change = InputChange(
            [d.choice() for d in body.inputs.documents],
            body.inputs.guide_ids,
            body.inputs.keep_extra_guides,
            [UploadedFile(f.filename, f.content_base64) for f in body.inputs.extra_guides],
        )
    job = jobs.start(
        user.id,
        "revision",
        lambda: service.prepare_revision(
            user.id, str(draft_id), body.instruction, body.ai_override_source_ids, change
        ),
        draft_id=str(draft_id),
        owns=lambda: service.check_own_draft(user.id, str(draft_id)),
    )
    return JobOut.of(job)


@router.post("/drafts/{draft_id}/export")
def export_docx(
    draft_id: UUID,
    version_no: int | None = None,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> Response:
    """A DOCX of one version (default: the newest): the filled form for a template draft,
    otherwise a plain document."""
    content, filename = service.export_docx(user.id, str(draft_id), version_no)
    ascii_name = filename.encode("ascii", "ignore").decode() or "draft.docx"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content, media_type=DOCX_TYPE, headers={"Content-Disposition": disposition})


@router.delete("/drafts/{draft_id}", status_code=204)
def delete_draft(
    draft_id: UUID,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    service.delete_draft(user.id, str(draft_id))
