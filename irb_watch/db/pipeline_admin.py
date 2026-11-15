"""Supabase-backed store for services.pipeline_admin."""

from datetime import UTC, datetime

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.db.paging import paged
from irb_watch.services.pipeline_admin import (
    DocumentKind,
    DocumentKindEdit,
    NameTakenError,
    PipelineTemplate,
    StepResult,
    StepStatus,
    StepType,
    StepTypeEdit,
)

UNIQUE_VIOLATION = "23505"
_TYPE_COLUMNS = (
    "id, name, description, instructions, guide_ids, form_template_id, input_earlier_steps, "
    "input_board_documents, archived_at, updated_at, category, numbered"
)
_KIND_COLUMNS = "id, step_type_id, hospital_id, name, section, origin, required, position, archived_at"


def _kind(row: dict) -> DocumentKind:
    return DocumentKind(
        id=int(row["id"]), step_type_id=int(row["step_type_id"]),
        hospital_id=int(row["hospital_id"]) if row.get("hospital_id") is not None else None,
        name=row["name"], section=row["section"], origin=row["origin"], required=bool(row["required"]),
        position=int(row["position"]), archived_at=row.get("archived_at"),
    )


def _result(row: dict) -> StepResult:
    return StepResult(
        key=row["key"],
        label=row["label"],
        approved=bool(row["approved"]),
        needs_answer=bool(row["needs_answer"]),
        position=int(row["position"]),
        archived_at=row.get("archived_at"),
    )


def _status(row: dict) -> StepStatus:
    return StepStatus(
        key=row["key"],
        label=row["label"],
        closed=bool(row["closed"]),
        position=int(row["position"]),
        archived_at=row.get("archived_at"),
    )


def _type(row: dict) -> StepType:
    return StepType(
        id=int(row["id"]),
        name=row["name"],
        description=row.get("description") or "",
        instructions=row.get("instructions") or "",
        guide_ids=tuple(int(i) for i in row.get("guide_ids") or []),
        form_template_id=row.get("form_template_id"),
        input_earlier_steps=bool(row.get("input_earlier_steps")),
        input_board_documents=bool(row.get("input_board_documents")),
        archived_at=row.get("archived_at"),
        updated_at=row.get("updated_at") or "",
        category=row.get("category") or "other",
        numbered=bool(row.get("numbered")),
    )


def _template(row: dict) -> PipelineTemplate:
    return PipelineTemplate(
        hospital_id=row.get("hospital_id"),
        step_type_ids=tuple(int(i) for i in row.get("step_type_ids") or []),
        updated_at=row.get("updated_at") or "",
    )


def _one(query) -> dict:
    try:
        rows = query.execute().data
    except APIError as exc:
        if exc.code == UNIQUE_VIOLATION:
            raise NameTakenError from exc
        raise
    if not rows:
        raise LookupError("row not found")
    return rows[0]


class SupabasePipelineSetupStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    # statuses
    def statuses(self) -> list[StepStatus]:
        return [_status(r) for r in self._c.table("step_statuses").select("*").execute().data]

    def add_status(self, status: StepStatus) -> StepStatus:
        row = _one(
            self._c.table("step_statuses").insert(
                {"key": status.key, "label": status.label, "closed": status.closed, "position": status.position}
            )
        )
        return _status(row)

    def update_status(self, key: str, values: dict) -> StepStatus:
        return _status(_one(self._c.table("step_statuses").update(values).eq("key", key)))

    # IRB results
    def results(self) -> list[StepResult]:
        return [_result(r) for r in self._c.table("step_results").select("*").execute().data]

    def add_result(self, result: StepResult) -> StepResult:
        row = _one(
            self._c.table("step_results").insert(
                {
                    "key": result.key, "label": result.label, "approved": result.approved,
                    "needs_answer": result.needs_answer, "position": result.position,
                }
            )
        )
        return _result(row)

    def update_result(self, key: str, values: dict) -> StepResult:
        return _result(_one(self._c.table("step_results").update(values).eq("key", key)))

    # step types
    def step_types(self) -> list[StepType]:
        return [_type(r) for r in self._c.table("step_types").select(_TYPE_COLUMNS).execute().data]

    def get_step_type(self, type_id: int) -> StepType | None:
        rows = self._c.table("step_types").select(_TYPE_COLUMNS).eq("id", type_id).limit(1).execute().data
        return _type(rows[0]) if rows else None

    def create_step_type(self, edit: StepTypeEdit, actor_id: str) -> StepType:
        row = _one(
            self._c.table("step_types").insert(
                {
                    "name": edit.name,
                    "description": edit.description,
                    "instructions": edit.instructions,
                    "guide_ids": edit.guide_ids,
                    "form_template_id": edit.form_template_id,
                    "input_earlier_steps": edit.input_earlier_steps,
                    "input_board_documents": edit.input_board_documents,
                    "category": edit.category,
                    "numbered": edit.numbered,
                    "updated_by": actor_id,
                }
            )
        )
        return _type(row)

    def update_step_type(self, type_id: int, values: dict, actor_id: str) -> StepType:
        return _type(
            _one(self._c.table("step_types").update({**values, "updated_by": actor_id}).eq("id", type_id))
        )

    def live_steps_of_type(self, type_id: int) -> int:
        result = (
            self._c.table("pipeline_steps").select("id", count="exact")
            .eq("step_type_id", type_id).is_("deleted_at", "null").limit(1).execute()
        )
        return result.count or 0

    # default step order
    def templates(self) -> list[PipelineTemplate]:
        rows = self._c.table("pipeline_templates").select("hospital_id, step_type_ids, updated_at").execute().data
        return [_template(r) for r in rows]

    def save_template(self, hospital_id: int | None, step_type_ids: list[int], actor_id: str) -> PipelineTemplate:
        values = {
            "step_type_ids": step_type_ids,
            "updated_by": actor_id,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        table = self._c.table("pipeline_templates")
        query = table.update(values)
        query = query.is_("hospital_id", "null") if hospital_id is None else query.eq("hospital_id", hospital_id)
        rows = query.execute().data
        if not rows:
            rows = table.insert({**values, "hospital_id": hospital_id}).execute().data
        return _template(rows[0])

    def delete_template(self, hospital_id: int) -> bool:
        rows = self._c.table("pipeline_templates").delete().eq("hospital_id", hospital_id).execute().data
        return bool(rows)

    # checks
    def live_guide_ids(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        rows = (
            self._c.table("guides").select("id").in_("id", ids).is_("archived_at", "null").execute().data
        )
        return {int(r["id"]) for r in rows}

    def form_template_ready(self, template_id: int) -> bool:
        rows = (
            self._c.table("form_templates")
            .select("id")
            .eq("id", template_id)
            .eq("status", "ready")
            .is_("archived_at", "null")
            .limit(1)
            .execute()
            .data
        )
        return bool(rows)

    def hospital_exists(self, hospital_id: int) -> bool:
        rows = self._c.table("hospitals").select("id").eq("id", hospital_id).limit(1).execute().data
        return bool(rows)

    # document lists (phase C1)
    def document_kinds(self, step_type_ids: list[int] | None = None) -> list[DocumentKind]:
        def query():
            q = self._c.table("step_type_documents").select(_KIND_COLUMNS)
            return (q.in_("step_type_id", step_type_ids) if step_type_ids is not None else q).order("id")

        if step_type_ids is not None and not step_type_ids:
            return []
        return [_kind(r) for r in paged(query)]

    def save_document_kinds(
        self, step_type_id: int, hospital_id: int | None, rows: list[DocumentKindEdit], actor_id: str
    ) -> None:
        payload = [
            {"id": r.id, "name": r.name, "section": r.section, "origin": r.origin, "required": r.required}
            for r in rows
        ]
        try:
            self._c.rpc(
                "save_step_type_documents",
                {"p_step_type_id": step_type_id, "p_hospital_id": hospital_id, "p_rows": payload, "p_actor": actor_id},
            ).execute()
        except APIError as exc:
            if exc.code == "P0001":
                raise LookupError("a row is not in this list") from exc
            if exc.code == UNIQUE_VIOLATION:
                raise NameTakenError from exc
            raise

    def delete_document_kinds(self, step_type_id: int, hospital_id: int) -> int:
        rows = (
            self._c.table("step_type_documents").delete().eq("step_type_id", step_type_id)
            .eq("hospital_id", hospital_id).execute().data
        )
        return len(rows)
