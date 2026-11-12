"""In-memory stand-ins for services.pipelines.PipelineStore and PipelineFiles."""

import itertools
from dataclasses import replace
from datetime import UTC, datetime

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakePipelineStore:
    def __init__(self) -> None:
        self.projects: dict[int, str] = {}  # project id -> owner
        self.project_hospitals: dict[int, set[int]] = {}
        self.names = {4: "SNUH", 5: "Ewha", 6: "Kimpo"}  # hospital id -> name
        self.pipes: dict[int, Pipeline] = {}
        self.step_rows: dict[int, Step] = {}
        self.docs: dict[int, StepDocument] = {}
        self.versions: dict[int, list[DocVersion]] = {}
        self.texts: dict[int, str | None] = {}
        self.event_rows: list[Event] = []
        self._ids = itertools.count(1)
        self.fail_version = False
        self.version_race = False
        self.steps_changed_meanwhile = False
        self.fail_purge_rows: set[str] = set()

    def add_project(self, project_id: int, owner: str, hospitals: set[int]) -> None:
        self.projects[project_id] = owner
        self.project_hospitals[project_id] = set(hospitals)

    # projects and hospitals
    def project_owner(self, project_id):
        return self.projects.get(project_id)

    def project_hospital_ids(self, project_id):
        return set(self.project_hospitals.get(project_id, set()))

    def hospital_names(self, hospital_ids):
        return {h: self.names[h] for h in hospital_ids if h in self.names}

    # pipelines
    def pipelines_of_project(self, project_id):
        return [p for p in self.pipes.values() if p.project_id == project_id]

    def get_pipeline(self, pipeline_id):
        return self.pipes.get(pipeline_id)

    def create_pipeline(self, user_id, project_id, hospital_id, steps):
        if any(
            p.project_id == project_id and p.hospital_id == hospital_id and p.deleted_at is None
            for p in self.pipes.values()
        ):
            raise NameTakenError
        pid = next(self._ids)
        self.pipes[pid] = Pipeline(pid, user_id, project_id, hospital_id, _now(), _now())
        for s in steps:
            self.add_step(pid, s)
        return self.pipes[pid]

    def set_pipeline_deleted(self, pipeline_id, deleted_at):
        p = self.pipes[pipeline_id]
        if deleted_at is None and any(
            o.id != p.id and o.project_id == p.project_id and o.hospital_id == p.hospital_id and o.deleted_at is None
            for o in self.pipes.values()
        ):
            raise NameTakenError
        self.pipes[pipeline_id] = replace(p, deleted_at=deleted_at)
        return self.pipes[pipeline_id]

    def touch_pipeline(self, pipeline_id):
        self.pipes[pipeline_id] = replace(self.pipes[pipeline_id], updated_at=_now())

    # steps
    def steps(self, pipeline_ids):
        return [s for s in self.step_rows.values() if s.pipeline_id in pipeline_ids]

    def get_step(self, step_id):
        return self.step_rows.get(step_id)

    def version_text(self, version_id):
        return self.texts.get(version_id)

    def add_step(self, pipeline_id, values):
        sid = next(self._ids)
        self.step_rows[sid] = Step(
            sid, pipeline_id, values.get("step_type_id"), values["name"], values["position"], values["status_key"]
        )
        return self.step_rows[sid]

    def update_step(self, step_id, values):
        if step_id not in self.step_rows:
            raise LookupError("step not found")
        values = {k: v for k, v in values.items() if k != "updated_at"}
        self.step_rows[step_id] = replace(self.step_rows[step_id], **values)
        return self.step_rows[step_id]

    def reorder_steps(self, pipeline_id, step_ids):
        live = sorted(s.id for s in self.step_rows.values() if s.pipeline_id == pipeline_id and s.deleted_at is None)
        if self.steps_changed_meanwhile or sorted(step_ids) != live:
            raise StepListError
        for n, sid in enumerate(step_ids, start=1):
            self.step_rows[sid] = replace(self.step_rows[sid], position=n)

    # documents
    def documents(self, step_ids):
        return [
            replace(d, versions=tuple(sorted(self.versions.get(d.id, []), key=lambda v: -v.version_no)))
            for d in self.docs.values()
            if d.step_id in step_ids
        ]

    def _live_name_taken(self, step_id, name, except_id=None):
        return any(
            d.step_id == step_id and d.deleted_at is None and d.id != except_id and d.name.lower() == name.lower()
            for d in self.docs.values()
        )

    def create_document(self, step_id, name):
        if self._live_name_taken(step_id, name):
            raise NameTakenError
        did = next(self._ids)
        self.docs[did] = StepDocument(did, step_id, name, _now())
        return self.docs[did]

    def delete_empty_document(self, document_id):
        if not self.versions.get(document_id):
            self.docs.pop(document_id, None)

    def set_document_deleted(self, document_id, deleted_at):
        d = self.docs[document_id]
        if deleted_at is None and self._live_name_taken(d.step_id, d.name, d.id):
            raise NameTakenError
        self.docs[document_id] = replace(d, deleted_at=deleted_at)
        return self.docs[document_id]

    def add_version(self, document_id, version: NewVersion):
        if self.fail_version:
            raise RuntimeError("database down")
        if self.version_race:
            raise NameTakenError
        number = len(self.versions.get(document_id, [])) + 1
        vid = next(self._ids)
        v = DocVersion(
            vid, document_id, number, version.filename, version.storage_path, version.sha256,
            version.size_bytes, version.body_text is not None, version.text_note, _now(),
        )
        self.versions.setdefault(document_id, []).append(v)
        self.texts[vid] = version.body_text
        return v

    # history
    def add_event(self, pipeline_id, step_id, actor_id, event_type, payload):
        self.event_rows.append(Event(next(self._ids), step_id, actor_id, event_type, payload, _now()))
        self.event_rows[-1] = replace(self.event_rows[-1])
        self.event_pipeline = pipeline_id

    def events(self, pipeline_id, limit):
        return list(reversed(self.event_rows))[:limit]

    # purge
    def _rows(self, kind):
        return {"pipelines": self.pipes, "steps": self.step_rows, "documents": self.docs}[kind]

    def _docs_under(self, kind, ids):
        if kind == "pipelines":
            steps = [s.id for s in self.step_rows.values() if s.pipeline_id in ids]
            return [d.id for d in self.docs.values() if d.step_id in steps]
        if kind == "steps":
            return [d.id for d in self.docs.values() if d.step_id in ids]
        return list(ids)

    def purge_candidates(self, kind, cutoff):
        ids = [r.id for r in self._rows(kind).values() if r.deleted_at and r.deleted_at < cutoff]
        paths = [v.storage_path for d in self._docs_under(kind, ids) for v in self.versions.get(d, [])]
        return ids, paths

    def purge_rows(self, kind, ids):
        if kind in self.fail_purge_rows:
            raise RuntimeError("database down")
        docs = self._docs_under(kind, ids)
        steps = [s.id for s in self.step_rows.values() if s.pipeline_id in ids] if kind == "pipelines" else []
        for d in docs:
            self.docs.pop(d, None)
            self.versions.pop(d, None)
        for s in steps + (ids if kind == "steps" else []):
            self.step_rows.pop(s, None)
        for i in ids:
            self._rows(kind).pop(i, None)


class FakePipelineFiles:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_put = False
        self.fail_delete = False

    def put(self, path, content):
        if self.fail_put:
            raise RuntimeError("storage down")
        if path in self.stored:
            raise RuntimeError("exists")
        self.stored[path] = content

    def delete(self, paths):
        if self.fail_delete:
            raise RuntimeError("storage down")
        self.deleted += paths
        for p in paths:
            self.stored.pop(p, None)

    def sign(self, path, filename, expires_in):
        return f"https://files.example/{path}?download={filename}&ttl={expires_in}"
