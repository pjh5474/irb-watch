"""Supabase-backed store and file bucket for services.pipelines."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.services.pipelines import (
    DocVersion,
    Event,
    NameTakenError,
    NewVersion,
    Pipeline,
    Step,
    StepDocument,
    StepListError,
)

UNIQUE_VIOLATION = "23505"
BUCKET = "pipeline-files"
_STEP_COLUMNS = (
    "id, pipeline_id, step_type_id, name, position, status_key, due_on, submitted_on, decided_on, "
    "note, deleted_at, updated_at"
)
_VERSION_COLUMNS = (
    "id, document_id, version_no, filename, storage_path, sha256, size_bytes, has_text, text_note, created_at"
)


def _pipeline(r: dict) -> Pipeline:
    return Pipeline(
        id=int(r["id"]), user_id=str(r["user_id"]), project_id=int(r["project_id"]),
        hospital_id=int(r["hospital_id"]), created_at=r["created_at"], updated_at=r["updated_at"],
        deleted_at=r.get("deleted_at"),
    )


def _step(r: dict) -> Step:
    return Step(
        id=int(r["id"]), pipeline_id=int(r["pipeline_id"]), step_type_id=r.get("step_type_id"),
        name=r["name"], position=int(r["position"]), status_key=r["status_key"],
        due_on=r.get("due_on"), submitted_on=r.get("submitted_on"), decided_on=r.get("decided_on"),
        note=r.get("note") or "", deleted_at=r.get("deleted_at"), updated_at=r.get("updated_at") or "",
    )


def _version(r: dict) -> DocVersion:
    return DocVersion(
        id=int(r["id"]), document_id=int(r["document_id"]), version_no=int(r["version_no"]),
        filename=r["filename"], storage_path=r["storage_path"], sha256=r["sha256"],
        size_bytes=int(r["size_bytes"]), has_text=bool(r.get("has_text")), text_note=r.get("text_note"),
        created_at=r["created_at"],
    )


def _unique(exc: APIError) -> bool:
    return exc.code == UNIQUE_VIOLATION


_PAGE = 1000
_IN_CHUNK = 200  # ids per `in` filter, so the request address stays short


def _chunks(ids: list[int]) -> list[list[int]]:
    return [ids[i : i + _IN_CHUNK] for i in range(0, len(ids), _IN_CHUNK)]


def _paged(make_query: Callable[[], Any]) -> list[dict]:
    """Every row of an ordered query: the API answers at most 1,000 rows per request, and a cut
    answer would look like real data."""
    out: list[dict] = []
    start = 0
    while True:
        rows = make_query().range(start, start + _PAGE - 1).execute().data
        out += rows
        if len(rows) < _PAGE:
            return out
        start += _PAGE


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

    def project_names(self, project_ids: list[int]) -> dict[int, str]:
        """Read by the server for the work page and reminders (the browser reads projects under
        RLS itself); the callers only ask for projects of the user's own pipelines."""
        out: dict[int, str] = {}
        for chunk in _chunks(sorted(set(project_ids))):
            rows = self._c.table("projects").select("id, name").in_("id", chunk).execute().data
            out.update({int(r["id"]): r["name"] for r in rows})
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

    def get_pipeline(self, pipeline_id: int) -> Pipeline | None:
        rows = self._c.table("pipelines").select("*").eq("id", pipeline_id).limit(1).execute().data
        return _pipeline(rows[0]) if rows else None

    def create_pipeline(self, user_id: str, project_id: int, hospital_id: int, steps: list[dict]) -> Pipeline:
        org = (
            self._c.table("organization_members").select("organization_id").eq("user_id", user_id)
            .limit(1).execute().data
        )
        values = {"user_id": user_id, "project_id": project_id, "hospital_id": hospital_id}
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
                self._c.table("pipeline_steps").insert([{**s, "pipeline_id": row["id"]} for s in steps]).execute()
        except Exception:
            # Do not leave a pipeline without its steps.
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

    def add_step(self, pipeline_id: int, values: dict) -> Step:
        rows = self._c.table("pipeline_steps").insert({**values, "pipeline_id": pipeline_id}).execute().data
        return _step(rows[0])

    def update_step(self, step_id: int, values: dict) -> Step:
        rows = self._c.table("pipeline_steps").update(values).eq("id", step_id).execute().data
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

    # documents
    def documents(self, step_ids: list[int]) -> list[StepDocument]:
        # Chunked and paged: the work page reads every step of all the user's pipelines, and a
        # silently cut answer would show files as missing.
        docs: list[dict] = []
        for chunk in _chunks(sorted(set(step_ids))):
            docs += _paged(
                lambda chunk=chunk: self._c.table("step_documents")
                .select("id, step_id, name, created_at, deleted_at").in_("step_id", chunk).order("id")
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
        return [
            StepDocument(
                id=int(d["id"]), step_id=int(d["step_id"]), name=d["name"], created_at=d["created_at"],
                deleted_at=d.get("deleted_at"), versions=tuple(by_doc.get(int(d["id"]), [])),
            )
            for d in docs
        ]

    def create_document(self, step_id: int, name: str) -> StepDocument:
        try:
            row = self._c.table("step_documents").insert({"step_id": step_id, "name": name}).execute().data[0]
        except APIError as exc:
            if _unique(exc):
                raise NameTakenError from exc
            raise
        return StepDocument(int(row["id"]), int(row["step_id"]), row["name"], row["created_at"])

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
        r = rows[0]
        return StepDocument(int(r["id"]), int(r["step_id"]), r["name"], r["created_at"], r.get("deleted_at"))

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
