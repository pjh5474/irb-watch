"""Supabase-backed store and file bucket for services.pipelines."""

from datetime import UTC, datetime
from typing import ClassVar

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.db.paging import chunks as _chunks
from irb_watch.db.paging import paged as _paged
from irb_watch.services.pipelines import (
    DocVersion,
    Event,
    NameTakenError,
    NewVersion,
    Pipeline,
    Step,
    StepCopyError,
    StepDocument,
    StepListError,
    StepParentError,
)

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
BUCKET = "pipeline-files"
_STEP_COLUMNS = (
    "id, pipeline_id, step_type_id, name, position, status_key, due_on, submitted_on, decided_on, "
    "note, deleted_at, updated_at, parent_step_id, sequence_no, subject, result_key, target_session_id, "
    "copied_from_step_id, custom_dates"
)
_VERSION_COLUMNS = (
    "id, document_id, version_no, filename, storage_path, sha256, size_bytes, has_text, text_note, created_at"
)


def _pipeline(r: dict) -> Pipeline:
    return Pipeline(
        id=int(r["id"]), user_id=str(r["user_id"]), project_id=int(r["project_id"]),
        hospital_id=int(r["hospital_id"]), created_at=r["created_at"], updated_at=r["updated_at"],
        deleted_at=r.get("deleted_at"), site_id=int(r["site_id"]) if r.get("site_id") is not None else None,
    )


def _step(r: dict) -> Step:
    return Step(
        id=int(r["id"]), pipeline_id=int(r["pipeline_id"]), step_type_id=r.get("step_type_id"),
        name=r["name"], position=int(r["position"]), status_key=r["status_key"],
        due_on=r.get("due_on"), submitted_on=r.get("submitted_on"), decided_on=r.get("decided_on"),
        note=r.get("note") or "", deleted_at=r.get("deleted_at"), updated_at=r.get("updated_at") or "",
        parent_step_id=int(r["parent_step_id"]) if r.get("parent_step_id") is not None else None,
        sequence_no=int(r["sequence_no"]) if r.get("sequence_no") is not None else None,
        subject=r.get("subject") or "", result_key=r.get("result_key"),
        target_session_id=int(r["target_session_id"]) if r.get("target_session_id") is not None else None,
        copied_from_step_id=int(r["copied_from_step_id"]) if r.get("copied_from_step_id") is not None else None,
        custom_dates=tuple(r.get("custom_dates") or ()),
    )


def _version(r: dict) -> DocVersion:
    return DocVersion(
        id=int(r["id"]), document_id=int(r["document_id"]), version_no=int(r["version_no"]),
        filename=r["filename"], storage_path=r["storage_path"], sha256=r["sha256"],
        size_bytes=int(r["size_bytes"]), has_text=bool(r.get("has_text")), text_note=r.get("text_note"),
        created_at=r["created_at"],
    )


_DOC_COLUMNS = (
    "id, step_id, name, created_at, deleted_at, kind_id, section, origin, required, status, position, note, "
    "updated_at, listed, copied_from_document_id"
)


def _document(r: dict, versions: tuple[DocVersion, ...] = ()) -> StepDocument:
    return StepDocument(
        id=int(r["id"]), step_id=int(r["step_id"]), name=r["name"], created_at=r["created_at"],
        deleted_at=r.get("deleted_at"), versions=versions,
        kind_id=int(r["kind_id"]) if r.get("kind_id") is not None else None,
        section=r.get("section") or "submission", origin=r.get("origin") or "upload",
        required=bool(r.get("required")), status=r.get("status") or "needed",
        position=int(r.get("position") or 0), note=r.get("note") or "", updated_at=r.get("updated_at") or "",
        listed=bool(r.get("listed")),
        copied_from_document_id=(
            int(r["copied_from_document_id"]) if r.get("copied_from_document_id") is not None else None
        ),
    )


def _unique(exc: APIError) -> bool:
    return exc.code == UNIQUE_VIOLATION



class SupabasePipelineStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    # projects and hospitals
    def project_owner(self, project_id: int) -> str | None:
        rows = self._c.table("projects").select("user_id").eq("id", project_id).limit(1).execute().data
        return str(rows[0]["user_id"]) if rows else None

    def project_hospital_ids(self, project_id: int) -> set[int]:
        rows = (
            self._c.table("project_hospitals").select("hospital_id").eq("project_id", project_id)
            .execute().data
        )
        return {int(r["hospital_id"]) for r in rows}

    def hospital_names(self, hospital_ids: list[int]) -> dict[int, str]:
        """Hospital names are readable by every signed-in user (platform-wide catalog)."""
        if not hospital_ids:
            return {}
        rows = (
            self._c.table("hospitals").select("id, name").in_("id", sorted(set(hospital_ids)))
            .execute().data
        )
        return {int(r["id"]): r["name"] for r in rows}

    def projects_info(self, project_ids: list[int]) -> dict[int, tuple[str, str]]:
        """Read by the server for the work page and reminders (the browser reads projects under
        RLS itself); the callers only ask for projects the user may open."""
        out: dict[int, tuple[str, str]] = {}
        for chunk in _chunks(sorted(set(project_ids))):
            rows = self._c.table("projects").select("id, name, user_id").in_("id", chunk).execute().data
            out.update({int(r["id"]): (r["name"], str(r["user_id"])) for r in rows})
        return out

    def live_pipelines_of_projects(self, project_ids: list[int]) -> list[Pipeline]:
        out: list[Pipeline] = []
        for chunk in _chunks(sorted(set(project_ids))):
            rows = (
                self._c.table("pipelines").select("*").in_("project_id", chunk).is_("deleted_at", "null")
                .execute().data
            )
            out += [_pipeline(r) for r in rows]
        return out

    # pipelines
    def pipelines_of_project(self, project_id: int) -> list[Pipeline]:
        rows = self._c.table("pipelines").select("*").eq("project_id", project_id).order("id").execute().data
        return [_pipeline(r) for r in rows]

    def live_pipelines_of_user(self, user_id: str) -> list[Pipeline]:
        rows = (
            self._c.table("pipelines").select("*").eq("user_id", user_id).is_("deleted_at", "null")
            .order("id").execute().data
        )
        return [_pipeline(r) for r in rows]

    def live_pipelines(self, pipeline_ids: list[int]) -> list[Pipeline]:
        out: list[Pipeline] = []
        for chunk in _chunks(sorted(set(pipeline_ids))):
            rows = (
                self._c.table("pipelines").select("*").in_("id", chunk).is_("deleted_at", "null")
                .execute().data
            )
            out += [_pipeline(r) for r in rows]
        return out

    def due_steps(self, until: str, skip_statuses: list[str]) -> list[Step]:
        def query():
            q = (
                self._c.table("pipeline_steps").select(_STEP_COLUMNS).lte("due_on", until)
                .is_("deleted_at", "null")
            )
            if skip_statuses:
                q = q.not_.in_("status_key", skip_statuses)
            return q.order("id")

        rows = _paged(query)
        return [_step(r) for r in rows]

    def targeted_steps(self, skip_statuses: list[str]) -> list[Step]:
        def query():
            q = (
                self._c.table("pipeline_steps").select(_STEP_COLUMNS).not_.is_("target_session_id", "null")
                .is_("deleted_at", "null")
            )
            if skip_statuses:
                q = q.not_.in_("status_key", skip_statuses)
            return q.order("id")

        return [_step(r) for r in _paged(query)]

    def hospitals_of_projects(self, project_ids: list[int]) -> dict[int, set[int]]:
        out: dict[int, set[int]] = {}
        for chunk in _chunks(sorted(set(project_ids))):
            rows = (
                self._c.table("project_hospitals").select("project_id, hospital_id").in_("project_id", chunk)
                .execute().data
            )
            for r in rows:
                out.setdefault(int(r["project_id"]), set()).add(int(r["hospital_id"]))
        return out

    def get_pipeline(self, pipeline_id: int) -> Pipeline | None:
        rows = self._c.table("pipelines").select("*").eq("id", pipeline_id).limit(1).execute().data
        return _pipeline(rows[0]) if rows else None

    def create_pipeline(
        self, user_id: str, project_id: int, hospital_id: int, steps: list[dict], site_id: int | None = None
    ) -> Pipeline:
        org = (
            self._c.table("organization_members").select("organization_id").eq("user_id", user_id)
            .limit(1).execute().data
        )
        values: dict = {"user_id": user_id, "project_id": project_id, "hospital_id": hospital_id}
        if site_id is not None:
            values["site_id"] = site_id  # without one, the database uses the unnamed site
        if org:
            values["organization_id"] = org[0]["organization_id"]
        try:
            row = self._c.table("pipelines").insert(values).execute().data[0]
        except APIError as exc:
            if _unique(exc):
                raise NameTakenError from exc
            raise
        try:
            if steps:
                rows = [{k: v for k, v in s.items() if k != "documents"} | {"pipeline_id": row["id"]} for s in steps]
                made = self._c.table("pipeline_steps").insert(rows).execute().data
                # Slots (phase C1): matched by position, unique in a new pipeline.
                by_position = {int(m["position"]): int(m["id"]) for m in made}
                slots = [
                    {**d, "step_id": by_position[int(s["position"])]} for s in steps for d in s.get("documents") or []
                ]
                if slots:
                    self._c.table("step_documents").insert(slots).execute()
        except Exception:
            # Do not leave a pipeline without its steps or slots (the steps and slots cascade).
            self._c.table("pipelines").delete().eq("id", row["id"]).execute()
            raise
        return _pipeline(row)

    def set_pipeline_deleted(self, pipeline_id: int, deleted_at: str | None) -> Pipeline:
        try:
            rows = (
                self._c.table("pipelines").update({"deleted_at": deleted_at}).eq("id", pipeline_id).execute().data
            )
        except APIError as exc:
            if _unique(exc):
                raise NameTakenError from exc
            raise
        return _pipeline(rows[0])

    def touch_pipeline(self, pipeline_id: int) -> None:
        self._c.table("pipelines").update({"updated_at": datetime.now(UTC).isoformat()}).eq(
            "id", pipeline_id
        ).execute()

    # steps
    def steps(self, pipeline_ids: list[int]) -> list[Step]:
        rows: list[dict] = []
        for chunk in _chunks(sorted(set(pipeline_ids))):
            rows += _paged(
                lambda chunk=chunk: self._c.table("pipeline_steps").select(_STEP_COLUMNS)
                .in_("pipeline_id", chunk).order("id")
            )
        return [_step(r) for r in rows]

    def get_step(self, step_id: int) -> Step | None:
        rows = self._c.table("pipeline_steps").select(_STEP_COLUMNS).eq("id", step_id).limit(1).execute().data
        return _step(rows[0]) if rows else None

    def version_text(self, version_id: int) -> str | None:
        rows = (
            self._c.table("step_document_versions").select("body_text").eq("id", version_id)
            .limit(1).execute().data
        )
        return rows[0].get("body_text") if rows else None

    def add_step(self, pipeline_id: int, values: dict, documents: list[dict] | None = None) -> Step:
        try:
            rows = self._c.table("pipeline_steps").insert({**values, "pipeline_id": pipeline_id}).execute().data
        except APIError as exc:
            if exc.code == "P0001":  # check_step_parent: one level
                raise StepParentError(exc.message or "the step to answer cannot take a follow-up") from exc
            if exc.code == FOREIGN_KEY_VIOLATION and values.get("parent_step_id") is not None:
                raise StepParentError("the step to answer is gone or in another pipeline") from exc
            raise
        step = _step(rows[0])
        if documents:
            try:
                self._c.table("step_documents").insert([{**d, "step_id": step.id} for d in documents]).execute()
            except Exception:
                self._c.table("pipeline_steps").delete().eq("id", step.id).execute()  # no step without its slots
                raise
        return step

    def update_step(self, step_id: int, values: dict) -> Step:
        try:
            rows = self._c.table("pipeline_steps").update(values).eq("id", step_id).execute().data
        except APIError as exc:
            if exc.code == "P0001":  # check_step_removal: a follow-up was added at the same moment
                raise StepParentError(exc.message or "this step has follow-up steps; remove them first") from exc
            if _unique(exc):  # pipeline_steps_live_copy_key: restoring a second live copy (phase C2)
                raise NameTakenError from exc
            raise
        if not rows:
            raise LookupError("step not found")
        return _step(rows[0])

    def reorder_steps(self, pipeline_id: int, step_ids: list[int]) -> None:
        try:
            self._c.rpc("reorder_pipeline_steps", {"p_pipeline_id": pipeline_id, "p_step_ids": step_ids}).execute()
        except APIError as exc:
            if exc.code == "P0001":
                raise StepListError from exc
            raise

    def copy_step(self, step_id: int, actor_id: str, targets: list[dict], skipped: list[dict]) -> list[dict]:
        try:
            data = self._c.rpc(
                "copy_pipeline_step",
                {"p_step_id": step_id, "p_actor": actor_id, "p_targets": targets, "p_skipped": skipped},
            ).execute().data
        except APIError as exc:
            if exc.code == "P0001":
                raise StepCopyError(exc.message or "something changed meanwhile") from exc
            if _unique(exc):
                raise StepCopyError("the step was just copied to one of the sites, or a document name is taken") from exc
            if exc.code == FOREIGN_KEY_VIOLATION:
                raise StepCopyError("a document list row or a meeting was just deleted") from exc
            raise
        return list(data or [])

    # documents
    def documents(self, step_ids: list[int]) -> list[StepDocument]:
        # Chunked and paged: the work page reads every step of all the user's pipelines, and a
        # silently cut answer would show files as missing.
        docs: list[dict] = []
        for chunk in _chunks(sorted(set(step_ids))):
            docs += _paged(
                lambda chunk=chunk: self._c.table("step_documents")
                .select(_DOC_COLUMNS).in_("step_id", chunk).order("id")
            )
        if not docs:
            return []
        versions: list[dict] = []
        for chunk in _chunks([int(d["id"]) for d in docs]):
            versions += _paged(
                lambda chunk=chunk: self._c.table("step_document_versions").select(_VERSION_COLUMNS)
                .in_("document_id", chunk).order("id")
            )
        by_doc: dict[int, list[DocVersion]] = {}
        for v in sorted(versions, key=lambda v: int(v["version_no"]), reverse=True):  # newest first
            by_doc.setdefault(int(v["document_id"]), []).append(_version(v))
        return [_document(d, tuple(by_doc.get(int(d["id"]), []))) for d in docs]

    def create_document(self, step_id: int, name: str, values: dict | None = None) -> StepDocument:
        try:
            row = (
                self._c.table("step_documents").insert({**(values or {}), "step_id": step_id, "name": name})
                .execute().data[0]
            )
        except APIError as exc:
            if _unique(exc):
                raise NameTakenError from exc
            raise
        return _document(row)

    def update_document(self, document_id: int, values: dict) -> StepDocument:
        try:
            rows = self._c.table("step_documents").update(values).eq("id", document_id).execute().data
        except APIError as exc:
            if _unique(exc):
                raise NameTakenError from exc
            raise
        if not rows:
            raise LookupError("document not found")
        return _document(rows[0])

    def reorder_documents(self, step_id: int, document_ids: list[int]) -> None:
        try:
            self._c.rpc("reorder_step_documents", {"p_step_id": step_id, "p_document_ids": document_ids}).execute()
        except APIError as exc:
            if exc.code == "P0001":
                raise StepListError from exc
            raise

    def delete_empty_document(self, document_id: int) -> None:
        # Only a document no version was saved for (a failed first upload).
        versions = (
            self._c.table("step_document_versions").select("id").eq("document_id", document_id).limit(1)
            .execute().data
        )
        if not versions:
            self._c.table("step_documents").delete().eq("id", document_id).execute()

    def set_document_deleted(self, document_id: int, deleted_at: str | None) -> StepDocument:
        try:
            rows = (
                self._c.table("step_documents").update({"deleted_at": deleted_at}).eq("id", document_id)
                .execute().data
            )
        except APIError as exc:
            if _unique(exc):
                raise NameTakenError from exc
            raise
        return _document(rows[0])

    def add_version(self, document_id: int, version: NewVersion) -> DocVersion:
        last = (
            self._c.table("step_document_versions").select("version_no").eq("document_id", document_id)
            .order("version_no", desc=True).limit(1).execute().data
        )
        number = (int(last[0]["version_no"]) if last else 0) + 1
        try:
            rows = self._insert_version(document_id, number, version)
        except APIError as exc:
            if _unique(exc):  # another upload took this number at the same moment
                raise NameTakenError from exc
            raise
        return _version(rows[0])

    def _insert_version(self, document_id: int, number: int, version: NewVersion) -> list[dict]:
        return (
            self._c.table("step_document_versions")
            .insert(
                {
                    "document_id": document_id,
                    "version_no": number,
                    "filename": version.filename,
                    "storage_path": version.storage_path,
                    "sha256": version.sha256,
                    "size_bytes": version.size_bytes,
                    "body_text": version.body_text,
                    "text_note": version.text_note,
                    "uploaded_by": version.uploaded_by,
                }
            )
            .execute()
            .data
        )

    # history
    def add_event(self, pipeline_id: int, step_id: int | None, actor_id: str, event_type: str, payload: dict) -> None:
        self._c.table("pipeline_events").insert(
            {"pipeline_id": pipeline_id, "step_id": step_id, "actor_id": actor_id, "event_type": event_type, "payload": payload}
        ).execute()

    def events(self, pipeline_id: int, limit: int, step_id: int | None = None) -> list[Event]:
        query = (
            self._c.table("pipeline_events").select("id, step_id, actor_id, event_type, payload, created_at")
            .eq("pipeline_id", pipeline_id)
        )
        if step_id is not None:
            query = query.eq("step_id", step_id)
        rows = query.order("id", desc=True).limit(limit).execute().data
        return [
            Event(int(r["id"]), r.get("step_id"), r.get("actor_id"), r["event_type"], r.get("payload") or {}, r["created_at"])
            for r in rows
        ]

    # purge
    def _paths(self, document_ids: list[int]) -> list[str]:
        if not document_ids:
            return []
        rows = (
            self._c.table("step_document_versions").select("storage_path").in_("document_id", document_ids)
            .execute().data
        )
        return [r["storage_path"] for r in rows]

    def _doc_ids(self, step_ids: list[int]) -> list[int]:
        if not step_ids:
            return []
        rows = self._c.table("step_documents").select("id").in_("step_id", step_ids).execute().data
        return [int(r["id"]) for r in rows]

    def _step_ids(self, pipeline_ids: list[int]) -> list[int]:
        if not pipeline_ids:
            return []
        rows = self._c.table("pipeline_steps").select("id").in_("pipeline_id", pipeline_ids).execute().data
        return [int(r["id"]) for r in rows]

    _TABLES: ClassVar[dict[str, str]] = {
        "pipelines": "pipelines",
        "steps": "pipeline_steps",
        "documents": "step_documents",
    }

    def purge_candidates(self, kind: str, cutoff: str) -> tuple[list[int], list[str]]:
        table = self._TABLES[kind]
        ids = [int(r["id"]) for r in self._c.table(table).select("id").lt("deleted_at", cutoff).execute().data]
        if kind == "pipelines":
            docs = self._doc_ids(self._step_ids(ids))
        elif kind == "steps":
            docs = self._doc_ids(ids)
        else:
            docs = ids
        return ids, self._paths(docs)

    def purge_rows(self, kind: str, ids: list[int]) -> None:
        # Children go with the row (on delete cascade).
        self._c.table(self._TABLES[kind]).delete().in_("id", ids).execute()


class SupabasePipelineFiles:
    """The private bucket of uploaded step files (service role only)."""

    def __init__(self, client: Client) -> None:
        self._bucket = client.storage.from_(BUCKET)

    def put(self, path: str, content: bytes) -> None:
        # Every path has a fresh random folder: an existing object is a real conflict.
        self._bucket.upload(path, content, {"content-type": "application/octet-stream", "upsert": "false"})

    def delete(self, paths: list[str]) -> None:
        for start in range(0, len(paths), 100):
            self._bucket.remove(paths[start : start + 100])

    def sign(self, path: str, filename: str, expires_in: int) -> str:
        result = self._bucket.create_signed_url(path, expires_in, {"download": filename})
        url = result.get("signedURL") or result.get("signedUrl")
        if not url:
            raise RuntimeError("no signed URL returned")
        return url
