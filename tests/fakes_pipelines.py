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
    StepCopyError,
    StepDocument,
    StepListError,
    StepParentError,
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
        self.copy_refusal: str | None = None  # copy_step refuses with this reason (a race)
        self.copy_calls = 0
        self.closed_statuses = {"approved", "rejected", "not_needed", "done"}  # step_statuses.closed, for copy_step

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

    def projects_info(self, project_ids):
        return {p: (f"Project {p}", self.projects[p]) for p in project_ids if p in self.projects}

    def live_pipelines_of_projects(self, project_ids):
        return [p for p in self.pipes.values() if p.project_id in set(project_ids) and p.deleted_at is None]

    # pipelines
    def pipelines_of_project(self, project_id):
        return [p for p in self.pipes.values() if p.project_id == project_id]

    def live_pipelines_of_user(self, user_id):
        return [p for p in self.pipes.values() if p.user_id == user_id and p.deleted_at is None]

    def live_pipelines(self, pipeline_ids):
        return [p for p in self.pipes.values() if p.id in set(pipeline_ids) and p.deleted_at is None]

    def due_steps(self, until, skip_statuses):
        return [
            s for s in self.step_rows.values()
            if s.deleted_at is None and s.due_on is not None and s.due_on <= until
            and s.status_key not in skip_statuses
        ]

    def targeted_steps(self, skip_statuses):
        return [
            s for s in self.step_rows.values()
            if s.deleted_at is None and s.target_session_id is not None and s.status_key not in skip_statuses
        ]

    def hospitals_of_projects(self, project_ids):
        return {p: set(self.project_hospitals.get(p, set())) for p in project_ids if p in self.project_hospitals}

    def get_pipeline(self, pipeline_id):
        return self.pipes.get(pipeline_id)

    def create_pipeline(self, user_id, project_id, hospital_id, steps, site_id=None):
        # The live unique index: per site (phase B2a), per project and hospital without sites.
        if any(
            p.deleted_at is None
            and (p.site_id == site_id if site_id is not None else (p.project_id, p.hospital_id) == (project_id, hospital_id))
            for p in self.pipes.values()
        ):
            raise NameTakenError
        pid = next(self._ids)
        self.pipes[pid] = Pipeline(pid, user_id, project_id, hospital_id, _now(), _now(), site_id=site_id)
        for s in steps:
            self.add_step(pid, {k: v for k, v in s.items() if k != "documents"}, s.get("documents"))
        return self.pipes[pid]

    def set_pipeline_deleted(self, pipeline_id, deleted_at):
        p = self.pipes[pipeline_id]
        if deleted_at is None and any(
            o.id != p.id and o.deleted_at is None
            and (o.site_id == p.site_id if p.site_id is not None else (o.project_id, o.hospital_id) == (p.project_id, p.hospital_id))
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

    def add_step(self, pipeline_id, values, documents=None):
        step = self._add_step(pipeline_id, values)
        for d in documents or []:
            self.create_document(step.id, d["name"], {k: v for k, v in d.items() if k != "name"})
        return step

    def _add_step(self, pipeline_id, values):
        parent = values.get("parent_step_id")
        if parent is not None:  # the composite key and the one-level trigger
            row = self.step_rows.get(parent)
            if row is None or row.pipeline_id != pipeline_id:
                raise StepParentError("the step to answer is gone or in another pipeline")
            if row.parent_step_id is not None:
                raise StepParentError("a follow-up step answers a main step, not another follow-up")
            if row.deleted_at is not None:
                raise StepParentError("the step to answer was just removed")
        sid = next(self._ids)
        self.step_rows[sid] = Step(
            sid, pipeline_id, values.get("step_type_id"), values["name"], values["position"], values["status_key"],
            parent_step_id=parent, sequence_no=values.get("sequence_no"), subject=values.get("subject", ""),
            due_on=values.get("due_on"), target_session_id=values.get("target_session_id"),
            copied_from_step_id=values.get("copied_from_step_id"),
        )
        return self.step_rows[sid]

    def _copy_taken(self, step):
        """pipeline_steps_live_copy_key: one live copy of a step per pipeline (phase C2)."""
        return step.copied_from_step_id is not None and any(
            s.id != step.id and s.pipeline_id == step.pipeline_id and s.deleted_at is None
            and s.copied_from_step_id == step.copied_from_step_id
            for s in self.step_rows.values()
        )

    def update_step(self, step_id, values):
        if step_id not in self.step_rows:
            raise LookupError("step not found")
        if "deleted_at" in values and values["deleted_at"] is None and self._copy_taken(self.step_rows[step_id]):
            raise NameTakenError
        if values.get("deleted_at") and any(  # trigger check_step_removal
            s.parent_step_id == step_id and s.deleted_at is None for s in self.step_rows.values()
        ):
            raise StepParentError("this step has follow-up steps; remove them first")
        values = {k: v for k, v in values.items() if k != "updated_at"}
        self.step_rows[step_id] = replace(self.step_rows[step_id], **values)
        return self.step_rows[step_id]

    def reorder_steps(self, pipeline_id, step_ids):
        live = sorted(s.id for s in self.step_rows.values() if s.pipeline_id == pipeline_id and s.deleted_at is None)
        if self.steps_changed_meanwhile or sorted(step_ids) != live:
            raise StepListError
        for n, sid in enumerate(step_ids, start=1):
            self.step_rows[sid] = replace(self.step_rows[sid], position=n)

    def copy_step(self, step_id, actor_id, targets, skipped):
        """copy_pipeline_step: checks again, then writes everything or nothing."""
        if self.copy_refusal:
            raise StepCopyError(self.copy_refusal)
        src = self.step_rows.get(step_id)
        if src is None or src.deleted_at is not None or src.parent_step_id is not None:
            raise StepCopyError("the step to copy was removed meanwhile")
        home = self.pipes[src.pipeline_id]
        before = (dict(self.step_rows), dict(self.docs), list(self.event_rows))
        done = []
        try:
            for t in targets:
                p = self.pipes.get(t["pipeline_id"])
                if p is None or p.deleted_at is not None or p.project_id != home.project_id or p.id == home.id:
                    raise StepCopyError("the pipeline of a site was removed meanwhile")
                s = t.get("step") or {}
                if t["mode"] == "add":
                    if sum(1 for x in self.step_rows.values() if x.pipeline_id == p.id and x.deleted_at is None) >= 60:
                        raise StepCopyError("the pipeline of a site reached 60 steps meanwhile")
                    position = max((x.position for x in self.step_rows.values() if x.pipeline_id == p.id), default=0) + 1
                    row = self._add_step(p.id, {**s, "position": position, "copied_from_step_id": src.id})
                    if self._copy_taken(row):
                        raise StepCopyError("the step was just copied to one of the sites")
                    target = row.id
                else:
                    target = t["step_id"]
                    row = self.step_rows.get(target)
                    has_file = any(self.versions.get(d.id) for d in self.docs.values() if d.step_id == target)
                    answered = any(
                        x.parent_step_id == target and x.deleted_at is None for x in self.step_rows.values()
                    )
                    # The same checks as the SQL function (the closed statuses of the seed).
                    if row is None or row.pipeline_id != p.id or row.deleted_at is not None or has_file \
                            or row.step_type_id != src.step_type_id \
                            or answered or row.parent_step_id is not None \
                            or row.copied_from_step_id is not None or row.submitted_on or row.decided_on \
                            or row.result_key or row.status_key in self.closed_statuses | {"submitted"}:
                        raise StepCopyError("a step at one of the sites changed meanwhile")
                    self.step_rows[target] = replace(
                        row,
                        subject=row.subject if row.subject.strip() else (s.get("subject") or row.subject),
                        due_on=row.due_on or s.get("due_on"),
                        target_session_id=row.target_session_id or s.get("target_session_id"),
                        copied_from_step_id=src.id,
                    )
                for u in t.get("slot_updates") or []:
                    d = self.docs.get(u["id"])
                    if d is None or d.step_id != target or d.deleted_at is not None:
                        raise StepCopyError("a document of a step at one of the sites changed meanwhile")
                    self.docs[d.id] = replace(
                        d, position=u["position"], status=u.get("status") or d.status,
                        copied_from_document_id=u.get("copied_from_document_id") or d.copied_from_document_id,
                    )
                for x in t.get("slot_inserts") or []:
                    try:
                        self.create_document(target, x["name"], {k: v for k, v in x.items() if k != "name"})
                    except NameTakenError as exc:
                        raise StepCopyError("a document name is taken") from exc
                if sum(1 for d in self.docs.values() if d.step_id == target and d.deleted_at is None) > 60:
                    raise StepCopyError("a step at one of the sites would have more than 60 documents")
                self.add_event(
                    p.id, target, actor_id, "step.copied_in",
                    {"from_pipeline_id": home.id, "from_step_id": src.id, "mode": t["mode"], "name": src.name},
                )
                done.append({"pipeline_id": p.id, "step_id": target, "mode": t["mode"]})
        except StepCopyError:
            self.step_rows, self.docs, self.event_rows = before
            raise
        self.add_event(home.id, src.id, actor_id, "step.copied", {"to": done, "skipped": skipped})
        self.copy_calls += 1
        return done

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

    def create_document(self, step_id, name, values=None):
        if self._live_name_taken(step_id, name):
            raise NameTakenError
        did = next(self._ids)
        self.docs[did] = StepDocument(did, step_id, name, _now(), **(values or {}))
        return self.docs[did]

    def update_document(self, document_id, values):
        d = self.docs.get(document_id)
        if d is None:
            raise LookupError("document not found")
        if "name" in values and self._live_name_taken(d.step_id, values["name"], d.id):
            raise NameTakenError
        self.docs[document_id] = replace(d, **values)
        return self.docs[document_id]

    def reorder_documents(self, step_id, document_ids):
        live = sorted(d.id for d in self.docs.values() if d.step_id == step_id and d.deleted_at is None)
        if sorted(document_ids) != live or len(set(document_ids)) != len(document_ids):
            raise StepListError
        for n, did in enumerate(document_ids, start=1):
            self.docs[did] = replace(self.docs[did], position=n)

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

    def events(self, pipeline_id, limit, step_id=None):
        rows = [e for e in reversed(self.event_rows) if step_id is None or e.step_id == step_id]
        return rows[:limit]

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
        for sid, row in list(self.step_rows.items()):  # on delete set null (parent_step_id)
            if row.parent_step_id is not None and row.parent_step_id not in self.step_rows:
                self.step_rows[sid] = replace(row, parent_step_id=None)
        for sid, row in list(self.step_rows.items()):  # on delete set null (copied_from_step_id)
            if row.copied_from_step_id is not None and row.copied_from_step_id not in self.step_rows:
                self.step_rows[sid] = replace(row, copied_from_step_id=None)
        for did, doc in list(self.docs.items()):  # on delete set null (copied_from_document_id)
            if doc.copied_from_document_id is not None and doc.copied_from_document_id not in self.docs:
                self.docs[did] = replace(doc, copied_from_document_id=None)
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
