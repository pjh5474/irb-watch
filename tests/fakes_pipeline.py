"""In-memory stand-in for services.pipeline_admin.PipelineSetupStore, seeded like the migration."""

from dataclasses import replace

from irb_watch.services.pipeline_admin import (
    NameTakenError,
    PipelineTemplate,
    StepResult,
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
# Categories and numbering as the step_structure migration sets them (phase B2b), plus one
# follow-up type (id 7).
SEED_KINDS = {
    1: ("initial", False), 2: ("amendment", True), 3: ("continuing", True),
    4: ("safety", True), 5: ("deviation", True), 6: ("closing", False),
}
FOLLOW_UP_TYPE = 7
SEED_RESULTS = [
    ("approved", "승인", True, False),
    ("approved_after_revision", "수정 후 승인", True, False),
    ("corrective_approval", "시정승인", True, True),
    ("administrative_revision", "행정수정 요청", False, True),
    ("revise_and_resubmit", "보완 후 재심의", False, True),
    ("rejected", "반려", False, False),
]


class FakePipelineSetupStore:
    def __init__(self) -> None:
        self.status_rows = {
            key: StepStatus(key, label, closed, i) for i, (key, label, closed) in enumerate(SEED_STATUSES, 1)
        }
        self.types = {
            i: StepType(i, name, category=SEED_KINDS[i][0], numbered=SEED_KINDS[i][1])
            for i, name in enumerate(SEED_TYPES, 1)
        }
        self.types[FOLLOW_UP_TYPE] = StepType(FOLLOW_UP_TYPE, "보완/시정 답변", category="follow_up", numbered=True)
        self.steps_of_type: dict[int, int] = {}  # type id -> live steps using it
        self.result_rows = {
            key: StepResult(key, label, approved, needs, i)
            for i, (key, label, approved, needs) in enumerate(SEED_RESULTS, 1)
        }
        self.template_rows: dict[int | None, PipelineTemplate] = {None: PipelineTemplate(None, (1, 2, 3, 6))}
        self.guides = {10, 11}  # live guide ids
        self.ready_forms = {20}
        self.hospitals = {4, 5}
        self.updated_by: dict[int, str] = {}

    def statuses(self):
        return list(self.status_rows.values())

    def add_status(self, status):
        self.status_rows[status.key] = status
        return status

    def update_status(self, key, values):
        self.status_rows[key] = replace(self.status_rows[key], **values)
        return self.status_rows[key]

    def results(self):
        return list(self.result_rows.values())

    def add_result(self, result):
        self.result_rows[result.key] = result
        return result

    def update_result(self, key, values):
        self.result_rows[key] = replace(self.result_rows[key], **values)
        return self.result_rows[key]

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
            tuple(edit.required_documents), category=edit.category, numbered=edit.numbered,
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

    def live_steps_of_type(self, type_id):
        return self.steps_of_type.get(type_id, 0)

    def templates(self):
        return list(self.template_rows.values())

    def save_template(self, hospital_id, step_type_ids, actor_id):
        self.template_rows[hospital_id] = PipelineTemplate(hospital_id, tuple(step_type_ids))
        return self.template_rows[hospital_id]

    def delete_template(self, hospital_id):
        return self.template_rows.pop(hospital_id, None) is not None

    def live_guide_ids(self, ids):
        return {i for i in ids if i in self.guides}

    def form_template_ready(self, template_id):
        return template_id in self.ready_forms

    def hospital_exists(self, hospital_id):
        return hospital_id in self.hospitals
