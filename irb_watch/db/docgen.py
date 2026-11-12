"""Supabase-backed storage for the docs generator."""

import logging
from datetime import datetime

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.core.state import VersionContent
from irb_watch.db.agent import SupabaseAgentData
from irb_watch.docgen.types import (
    DocVersion,
    Draft,
    DraftVersion,
    ExtraGuide,
    FormTemplate,
    Guide,
    GuideRevision,
    Inputs,
    NewDraft,
    SourceRef,
    VersionConflictError,
    draft_template_from_json,
    draft_template_to_json,
    field_from_json,
    field_to_json,
    ref_from_json,
    ref_to_json,
    step_file_from_json,
)

log = logging.getLogger(__name__)

UNIQUE_VIOLATION = "23505"
TEMPLATE_BUCKET = "form-templates"
_GUIDE_COLUMNS = "id, title, filename, body_text, created_at, archived_at, is_global, text_updated_at"
_TEMPLATE_COLUMNS = (
    "id, title, doc_type, filename, storage_path, sha256, is_global, status, fields, "
    "created_at, updated_at, archived_at"
)


class SupabaseTemplateFiles:
    """The private bucket of form-template files (service role only)."""

    def __init__(self, client: Client) -> None:
        self._bucket = client.storage.from_(TEMPLATE_BUCKET)

    def put(self, path: str, content: bytes) -> None:
        # Every path has a fresh random folder, so an existing object is a real conflict and
        # fails (never overwritten, never silently kept).
        self._bucket.upload(
            path,
            content,
            {
                "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "upsert": "false",
            },
        )

    def get(self, path: str) -> bytes:
        return self._bucket.download(path)

    def delete(self, paths: list[str]) -> None:
        if paths:
            self._bucket.remove(paths)


def _template(row: dict, links: tuple[int, ...]) -> FormTemplate:
    return FormTemplate(
        id=row["id"],
        title=row["title"],
        doc_type=row.get("doc_type") or "",
        filename=row["filename"],
        storage_path=row["storage_path"],
        sha256=row["sha256"],
        is_global=bool(row.get("is_global")),
        status=row["status"],
        fields=tuple(field_from_json(f) for f in row.get("fields") or []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row.get("archived_at"),
        hospital_ids=links,
    )


def _draft(row: dict) -> Draft:
    return Draft(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"] or "",
        instruction=row["instruction"] or "",
        document_refs=tuple(ref_from_json(r) for r in row.get("document_refs") or []),
        guide_ids=tuple(int(g) for g in row.get("guide_ids") or []),
        extra_guides=tuple(
            ExtraGuide(e.get("filename", ""), e.get("text", "")) for e in row.get("extra_guides") or []
        ),
        ai_override_source_ids=tuple(int(s) for s in row.get("ai_override_source_ids") or []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        template=draft_template_from_json(row["template"]) if row.get("template") else None,
        pipeline_step_id=row.get("pipeline_step_id"),
        step_files=tuple(step_file_from_json(f) for f in row.get("step_files") or []),
    )


def _version(row: dict) -> DraftVersion:
    return DraftVersion(
        draft_id=row["draft_id"],
        version_no=row["version_no"],
        markdown=row["markdown"],
        origin=row["origin"],
        instruction=row.get("instruction") or "",
        created_at=row["created_at"],
        inputs=row.get("inputs"),
        fields=row.get("fields"),
    )


def _selection(inputs: Inputs, now: datetime) -> dict:
    """The draft row's current selection (guide files with their text)."""
    return {
        "document_refs": [ref_to_json(r) for r in inputs.document_refs],
        "guide_ids": list(inputs.guide_ids),
        "extra_guides": [{"filename": e.filename, "text": e.text} for e in inputs.extra_guides],
        "ai_override_source_ids": list(inputs.ai_override_source_ids),
        "updated_at": now.isoformat(),
    }


class SupabaseDocgenStore:
    def __init__(self, client: Client) -> None:
        self._c = client
        self._reads = SupabaseAgentData(client)

    # -- what a user may read ------------------------------------------------------

    def readable_sources(self, user_id: str, is_admin: bool) -> dict[int, SourceRef]:
        """Same rule as private.can_read_source; AI-off sources are included."""
        rows = (
            self._c.table("sources")
            .select("id, name, requires_login, ai_enabled, hospital_id")
            .order("id")
            .execute()
            .data
        )
        member_of: set[int] = set()
        if not is_admin and any(r["requires_login"] for r in rows):
            member_rows = (
                self._c.table("source_members")
                .select("source_id")
                .eq("user_id", user_id)
                .execute()
                .data
            )
            member_of = {m["source_id"] for m in member_rows}
        return {
            r["id"]: SourceRef(r["id"], r["name"], bool(r["ai_enabled"]), r.get("hospital_id"))
            for r in rows
            if is_admin or not r["requires_login"] or r["id"] in member_of
        }

    def get_source(self, source_id: int) -> SourceRef | None:
        rows = (
            self._c.table("sources")
            .select("id, name, ai_enabled, hospital_id")
            .eq("id", source_id)
            .limit(1)
            .execute()
            .data
        )
        if not rows:
            return None
        r = rows[0]
        return SourceRef(r["id"], r["name"], bool(r["ai_enabled"]), r.get("hospital_id"))

    def get_doc_version(
        self, document_id: int, version_no: int | None, previous: bool = True
    ) -> DocVersion | None:
        docs = (
            self._c.table("documents")
            .select("id, source_id, title")
            .eq("id", document_id)
            .limit(1)
            .execute()
            .data
        )
        if not docs:
            return None
        query = self._c.table("document_versions").select("version_no").eq("document_id", document_id)
        if version_no is None:
            query = query.order("version_no", desc=True)
        else:
            query = query.eq("version_no", version_no)
        rows = query.limit(1).execute().data
        if not rows:
            return None
        number = rows[0]["version_no"]
        content = self._reads.get_version(document_id, number)
        if content is None:
            return None
        before = self._reads.get_version(document_id, number - 1) if previous and number > 1 else None
        return DocVersion(
            document_id=document_id,
            source_id=docs[0]["source_id"],
            title=docs[0]["title"],
            version_no=number,
            content=content,
            previous=before,
        )

    def get_version_content(self, document_id: int, version_no: int) -> VersionContent | None:
        return self._reads.get_version(document_id, version_no)

    # -- guides ----------------------------------------------------------------------

    def _links(self, guide_ids: list[int]) -> dict[int, tuple[int, ...]]:
        if not guide_ids:
            return {}
        rows = (
            self._c.table("hospital_guides")
            .select("hospital_id, guide_id")
            .in_("guide_id", guide_ids)
            .execute()
            .data
        )
        out: dict[int, list[int]] = {}
        for r in rows:
            out.setdefault(r["guide_id"], []).append(r["hospital_id"])
        return {g: tuple(sorted(s)) for g, s in out.items()}

    def _guides(self, rows: list[dict]) -> list[Guide]:
        links = self._links([r["id"] for r in rows])
        return [
            Guide(
                id=r["id"],
                title=r["title"],
                filename=r["filename"] or "",
                body_text=r["body_text"],
                created_at=r["created_at"],
                archived_at=r.get("archived_at"),
                hospital_ids=links.get(r["id"], ()),
                is_global=bool(r.get("is_global")),
                text_updated_at=r.get("text_updated_at"),
            )
            for r in rows
        ]

    def create_guide(self, title, filename, body_text, sha256, created_by, now) -> Guide:
        rows = (
            self._c.table("guides")
            .insert(
                {
                    "title": title,
                    "filename": filename,
                    "body_text": body_text,
                    "sha256": sha256,
                    "created_by": created_by,
                    "created_at": now.isoformat(),
                }
            )
            .execute()
            .data
        )
        return self._guides(rows)[0]

    def list_guides(self, include_archived: bool) -> list[Guide]:
        query = self._c.table("guides").select(_GUIDE_COLUMNS)
        if not include_archived:
            query = query.is_("archived_at", "null")
        return self._guides(query.order("id", desc=True).execute().data)

    def get_guides(self, guide_ids: list[int]) -> list[Guide]:
        if not guide_ids:
            return []
        rows = self._c.table("guides").select(_GUIDE_COLUMNS).in_("id", guide_ids).execute().data
        by_id = {g.id: g for g in self._guides(rows)}
        return [by_id[g] for g in guide_ids if g in by_id]

    def guides_for_hospitals(self, hospital_ids: list[int]) -> list[Guide]:
        if not hospital_ids:
            return []
        links = (
            self._c.table("hospital_guides")
            .select("guide_id")
            .in_("hospital_id", hospital_ids)
            .execute()
            .data
        )
        ids = sorted({r["guide_id"] for r in links})
        global_rows = (
            self._c.table("guides")
            .select(_GUIDE_COLUMNS)
            .eq("is_global", True)
            .is_("archived_at", "null")
            .order("id")
            .execute()
            .data
        )
        global_ids = {r["id"] for r in global_rows}
        linked = [i for i in ids if i not in global_ids]
        rows = (
            self._c.table("guides")
            .select(_GUIDE_COLUMNS)
            .in_("id", linked)
            .is_("archived_at", "null")
            .order("id")
            .execute()
            .data
            if linked
            else []
        )
        return self._guides(global_rows + rows)

    def hospital_ids(self) -> set[int]:
        rows = self._c.table("hospitals").select("id").execute().data
        return {int(r["id"]) for r in rows}

    def hospital_exists(self, hospital_id: int) -> bool:
        rows = self._c.table("hospitals").select("id").eq("id", hospital_id).limit(1).execute().data
        return bool(rows)

    def update_guide(self, guide_id: int, title: str | None, is_global: bool | None) -> None:
        changes: dict = {}
        if title is not None:
            changes["title"] = title
        if is_global is not None:
            changes["is_global"] = is_global
        if changes:
            self._c.table("guides").update(changes).eq("id", guide_id).execute()

    def replace_guide_text(self, guide, filename, body_text, sha256, by, now) -> None:
        current = (
            self._c.table("guides")
            .select("filename, body_text, sha256, created_at, text_updated_at")
            .eq("id", guide.id)
            .limit(1)
            .execute()
            .data
        )
        if not current:
            raise VersionConflictError
        old = current[0]
        # The update only applies if the guide still has the text read above (same hash and the
        # same replacement time), so of two replacements at the same moment one gets a conflict
        # and writes nothing. Two requests, not one transaction: the old text is kept in memory
        # and written to the history right after.
        query = (
            self._c.table("guides")
            .update(
                {
                    "filename": filename,
                    "body_text": body_text,
                    "sha256": sha256,
                    "text_updated_at": now.isoformat(),
                }
            )
            .eq("id", guide.id)
            .eq("sha256", old["sha256"])
        )
        if old["text_updated_at"] is None:
            query = query.is_("text_updated_at", "null")
        else:
            query = query.eq("text_updated_at", old["text_updated_at"])
        if not query.execute().data:
            raise VersionConflictError
        row = {
            "guide_id": guide.id,
            "filename": old["filename"] or "",
            "body_text": old["body_text"],
            "sha256": old["sha256"],
            "valid_from": old["text_updated_at"] or old["created_at"],
            "replaced_by": by,
            "replaced_at": now.isoformat(),
        }
        try:
            self._c.table("guide_revisions").insert(row).execute()
        except Exception:  # noqa: BLE001  any failure: try once more, then log and raise
            try:  # one more try: the old text exists nowhere else now
                self._c.table("guide_revisions").insert(row).execute()
            except Exception:
                log.error(
                    "guide %s: new text saved, but its old text (sha256 %s) could not be kept "
                    "in guide_revisions",
                    guide.id,
                    old["sha256"],
                )
                raise

    def list_guide_revisions(self, guide_id: int) -> list[GuideRevision]:
        rows = (
            self._c.table("guide_revisions")
            .select("id, guide_id, filename, body_text, valid_from, replaced_by, replaced_at")
            .eq("guide_id", guide_id)
            .order("replaced_at", desc=True)
            .execute()
            .data
        )
        return [
            GuideRevision(
                r["id"],
                r["guide_id"],
                r["filename"] or "",
                r["body_text"],
                r["valid_from"],
                r.get("replaced_by"),
                r["replaced_at"],
            )
            for r in rows
        ]

    def archive_guide(self, guide_id: int, now: datetime) -> None:
        self._c.table("guides").update({"archived_at": now.isoformat()}).eq("id", guide_id).execute()
        self._c.table("hospital_guides").delete().eq("guide_id", guide_id).execute()

    def link_guide(self, hospital_id: int, guide_id: int, now: datetime) -> None:
        self._c.table("hospital_guides").upsert(
            {"hospital_id": hospital_id, "guide_id": guide_id, "created_at": now.isoformat()},
            on_conflict="hospital_id,guide_id",
            ignore_duplicates=True,
        ).execute()

    def unlink_guide(self, hospital_id: int, guide_id: int) -> None:
        self._c.table("hospital_guides").delete().eq("hospital_id", hospital_id).eq(
            "guide_id", guide_id
        ).execute()

    # -- form templates ----------------------------------------------------------------

    def _template_links(self, ids: list[int]) -> dict[int, tuple[int, ...]]:
        if not ids:
            return {}
        rows = (
            self._c.table("hospital_form_templates")
            .select("hospital_id, template_id")
            .in_("template_id", ids)
            .execute()
            .data
        )
        out: dict[int, list[int]] = {}
        for r in rows:
            out.setdefault(r["template_id"], []).append(r["hospital_id"])
        return {t: tuple(sorted(s)) for t, s in out.items()}

    def _templates(self, rows: list[dict]) -> list[FormTemplate]:
        links = self._template_links([r["id"] for r in rows])
        return [_template(r, links.get(r["id"], ())) for r in rows]

    def create_template(
        self, title, doc_type, filename, storage_path, sha256, fields, created_by, now
    ) -> FormTemplate:
        rows = (
            self._c.table("form_templates")
            .insert(
                {
                    "title": title,
                    "doc_type": doc_type,
                    "filename": filename,
                    "storage_path": storage_path,
                    "sha256": sha256,
                    "fields": [field_to_json(f) for f in fields],
                    "status": "review",
                    "created_by": created_by,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            )
            .execute()
            .data
        )
        return self._templates(rows)[0]

    def list_templates(self, include_archived: bool) -> list[FormTemplate]:
        query = self._c.table("form_templates").select(_TEMPLATE_COLUMNS)
        if not include_archived:
            query = query.is_("archived_at", "null")
        return self._templates(query.order("id", desc=True).execute().data)

    def get_template(self, template_id: int) -> FormTemplate | None:
        rows = (
            self._c.table("form_templates")
            .select(_TEMPLATE_COLUMNS)
            .eq("id", template_id)
            .limit(1)
            .execute()
            .data
        )
        return self._templates(rows)[0] if rows else None

    def templates_for_hospitals(self, hospital_ids: list[int]) -> list[FormTemplate]:
        if not hospital_ids:
            return []
        global_rows = (
            self._c.table("form_templates")
            .select(_TEMPLATE_COLUMNS)
            .eq("is_global", True)
            .eq("status", "ready")
            .is_("archived_at", "null")
            .order("id")
            .execute()
            .data
        )
        links = (
            self._c.table("hospital_form_templates")
            .select("template_id")
            .in_("hospital_id", hospital_ids)
            .execute()
            .data
        )
        global_ids = {r["id"] for r in global_rows}
        linked = sorted({r["template_id"] for r in links} - global_ids)
        rows = (
            self._c.table("form_templates")
            .select(_TEMPLATE_COLUMNS)
            .in_("id", linked)
            .eq("status", "ready")
            .is_("archived_at", "null")
            .order("id")
            .execute()
            .data
            if linked
            else []
        )
        return self._templates(global_rows + rows)

    def update_template(self, template_id: int, changes: dict, now: datetime) -> None:
        row = dict(changes)
        if "fields" in row:
            row["fields"] = [field_to_json(f) for f in row["fields"]]
        row["updated_at"] = now.isoformat()
        self._c.table("form_templates").update(row).eq("id", template_id).execute()

    def archive_template(self, template_id: int, now: datetime) -> None:
        self._c.table("form_templates").update(
            {"archived_at": now.isoformat(), "updated_at": now.isoformat()}
        ).eq("id", template_id).execute()
        self._c.table("hospital_form_templates").delete().eq("template_id", template_id).execute()

    def link_template(self, hospital_id: int, template_id: int, now: datetime) -> None:
        self._c.table("hospital_form_templates").upsert(
            {"hospital_id": hospital_id, "template_id": template_id, "created_at": now.isoformat()},
            on_conflict="hospital_id,template_id",
            ignore_duplicates=True,
        ).execute()

    def unlink_template(self, hospital_id: int, template_id: int) -> None:
        self._c.table("hospital_form_templates").delete().eq("hospital_id", hospital_id).eq(
            "template_id", template_id
        ).execute()

    # -- drafts ----------------------------------------------------------------------

    def create_draft(self, new: NewDraft, first: DraftVersion, now: datetime) -> Draft:
        inputs = Inputs(
            new.document_refs, new.guide_ids, new.extra_guides, new.ai_override_source_ids,
            new.step_files,
        )
        rows = (
            self._c.table("drafts")
            .insert(
                {
                    "user_id": new.user_id,
                    "title": new.title,
                    "instruction": new.instruction,
                    **_selection(inputs, now),
                    "pipeline_step_id": new.pipeline_step_id,
                    "step_files": [f.to_json() for f in new.step_files],
                    "template": draft_template_to_json(new.template) if new.template else None,
                    "created_at": now.isoformat(),
                }
            )
            .execute()
            .data
        )
        draft = _draft(rows[0])
        try:
            self._c.table("draft_versions").insert(
                {
                    "draft_id": draft.id,
                    "user_id": new.user_id,
                    "version_no": 1,
                    "markdown": first.markdown,
                    "origin": first.origin,
                    "instruction": first.instruction,
                    "inputs": inputs.to_json(),
                    "fields": first.fields,
                    "created_at": now.isoformat(),
                }
            ).execute()
        except Exception:
            # Two requests, not one transaction: do not leave a draft without its text.
            self.delete_draft(draft.id)
            raise
        return draft

    def get_draft(self, draft_id: str) -> Draft | None:
        rows = self._c.table("drafts").select("*").eq("id", draft_id).limit(1).execute().data
        return _draft(rows[0]) if rows else None

    def list_drafts(self, user_id: str, limit: int) -> list[Draft]:
        rows = (
            self._c.table("drafts")
            .select("*")
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        return [_draft(r) for r in rows]

    def drafts_of_steps(self, user_id: str, step_ids: list[int]) -> list[Draft]:
        if not step_ids:
            return []
        rows = (
            self._c.table("drafts")
            .select("*")
            .eq("user_id", user_id)
            .in_("pipeline_step_id", step_ids)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
            .data
        )
        return [_draft(r) for r in rows]

    def list_versions(self, draft_id: str) -> list[DraftVersion]:
        rows = (
            self._c.table("draft_versions")
            .select("*")
            .eq("draft_id", draft_id)
            .order("version_no")
            .execute()
            .data
        )
        return [_version(r) for r in rows]

    def add_version(
        self,
        draft,
        markdown,
        origin,
        instruction,
        now,
        inputs=None,
        update_selection=False,
        fields=None,
    ) -> DraftVersion:
        last = (
            self._c.table("draft_versions")
            .select("version_no")
            .eq("draft_id", draft.id)
            .order("version_no", desc=True)
            .limit(1)
            .execute()
            .data
        )
        number = (last[0]["version_no"] if last else 0) + 1
        # unique (draft_id, version_no): of two saves at the same moment one fails, with a reason.
        try:
            rows = (
                self._c.table("draft_versions")
                .insert(
                    {
                        "draft_id": draft.id,
                        "user_id": draft.user_id,
                        "version_no": number,
                        "markdown": markdown,
                        "origin": origin,
                        "instruction": instruction,
                        "inputs": inputs.to_json() if inputs is not None else None,
                        "fields": fields,
                        "created_at": now.isoformat(),
                    }
                )
                .execute()
                .data
            )
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise VersionConflictError from exc
            raise
        # Two requests, not one transaction. The version is saved (and for an AI revision paid
        # for), so a failure here is logged, not raised: the user must not retry and pay again.
        # The draft then keeps its previous selection (the side panel shows the older one).
        if update_selection and inputs is not None:
            update = _selection(inputs, now)
        else:
            update = {"updated_at": now.isoformat()}
        try:
            self._c.table("drafts").update(update).eq("id", draft.id).execute()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "draft %s: version %s saved, but the draft row was not updated: %s",
                draft.id,
                rows[0]["version_no"],
                type(exc).__name__,
            )
        return _version(rows[0])

    def delete_draft(self, draft_id: str) -> None:
        self._c.table("drafts").delete().eq("id", draft_id).execute()

    def count_requests_since(self, user_id: str, since: datetime) -> int:
        result = (
            self._c.table("audit_logs")
            .select("id", count="exact")
            .eq("actor_id", user_id)
            .in_("event_type", ["docgen.generate", "docgen.revise"])
            .gte("created_at", since.isoformat())
            .limit(1)
            .execute()
        )
        return result.count or 0

    def purge_before(self, cutoff: datetime) -> tuple[int, list[str]]:
        # The delete returns the deleted rows, so the files are those of exactly these drafts.
        # A pipeline step's drafts are kept: they go with their step (owner, 2026-11-11).
        rows = (
            self._c.table("drafts").delete().lt("updated_at", cutoff.isoformat())
            .is_("pipeline_step_id", "null").execute().data
            or []
        )
        paths = []
        for r in rows:
            t = r.get("template") or {}
            # Library files are shared by every draft of the template: never deleted here.
            if t.get("template_id") is None and t.get("storage_path"):
                paths.append(t["storage_path"])
        return len(rows), paths
