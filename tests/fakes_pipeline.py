"""In-memory stand-in for services.pipeline_admin.PipelineSetupStore, seeded like the migration."""

from dataclasses import replace

from irb_watch.services.pipeline_admin import (
    NameTakenError,
    PipelineTemplate,
    StepStatus,
    StepType,
    StepTypeEdit,
)

SEED_STATUSES = [
    ("not_started", "시작 전", False),
    ("preparing", "준비 중", False),
    ("submitted", "제출됨", False),
    ("revisions_requested", "보완 요청", False),
    ("approved", "승인", True),
    ("rejected", "반려", True),
    ("not_needed", "해당 없음", True),
]
SEED_TYPES = ["초기 심의", "변경 심의", "지속 심의", "중대한 이상사례 보고", "위반/이탈 보고", "종료 보고"]


class FakePipelineSetupStore:
    def __init__(self) -> None:
        self.status_rows = {
            key: StepStatus(key, label, closed, i) for i, (key, label, closed) in enumerate(SEED_STATUSES, 1)
        }
        self.types = {i: StepType(i, name) for i, name in enumerate(SEED_TYPES, 1)}
        self.template_rows: dict[int | None, PipelineTemplate] = {None: PipelineTemplate(None, (1, 2, 3, 6))}
        self.guides = {10, 11}  # live guide ids
        self.ready_forms = {20}
        self.sources = {4, 5}
        self.updated_by: dict[int, str] = {}

    def statuses(self):
        return list(self.status_rows.values())

    def add_status(self, status):
        self.status_rows[status.key] = status
        return status

    def update_status(self, key, values):
        self.status_rows[key] = replace(self.status_rows[key], **values)
        return self.status_rows[key]

    def step_types(self):
        return list(self.types.values())

    def get_step_type(self, type_id):
        return self.types.get(type_id)

    def _name_taken(self, name, except_id=None):
        return any(
            t.archived_at is None and t.id != except_id and t.name.lower() == name.lower()
            for t in self.types.values()
        )

    def create_step_type(self, edit: StepTypeEdit, actor_id):
        if self._name_taken(edit.name):
            raise NameTakenError
        new_id = max(self.types) + 1
        self.types[new_id] = StepType(
            new_id, edit.name, edit.description, edit.instructions, tuple(edit.guide_ids),
            edit.form_template_id, edit.input_earlier_steps, edit.input_board_documents,
            tuple(edit.required_documents),
        )
        self.updated_by[new_id] = actor_id
        return self.types[new_id]

    def update_step_type(self, type_id, values, actor_id):
        current = self.types[type_id]
        name = values.get("name", current.name)
        unarchiving = "archived_at" in values and values["archived_at"] is None
        if ("name" in values or unarchiving) and self._name_taken(name, type_id):
            raise NameTakenError
        values = {k: v for k, v in values.items() if k != "updated_at"}
        for key in ("guide_ids", "required_documents"):
            if key in values:
                values[key] = tuple(values[key])
        self.types[type_id] = replace(current, **values)
        self.updated_by[type_id] = actor_id
        return self.types[type_id]

    def templates(self):
        return list(self.template_rows.values())

    def save_template(self, source_id, step_type_ids, actor_id):
        self.template_rows[source_id] = PipelineTemplate(source_id, tuple(step_type_ids))
        return self.template_rows[source_id]

    def delete_template(self, source_id):
        return self.template_rows.pop(source_id, None) is not None

    def live_guide_ids(self, ids):
        return {i for i in ids if i in self.guides}

    def form_template_ready(self, template_id):
        return template_id in self.ready_forms

    def source_exists(self, source_id):
        return source_id in self.sources
