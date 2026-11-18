"""In-memory stand-ins for services.sessions.SessionStore, services.tasks.TaskStore and the AI
session finder (CAL-1). The session store mirrors irb_sessions' unique index; the task store
mirrors the link-check trigger."""

import itertools
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from irb_watch.core.state import FileContent, VersionContent
from irb_watch.services.draft_jobs import OPEN
from irb_watch.services.sessions import (
    PostBrief,
    Session,
    SessionJob,
    SessionJobTakenError,
    SessionKind,
    SessionTakenError,
    SourceRef,
)
from irb_watch.services.tasks import Task, TaskLinkError


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeSessionStore:
    def __init__(self) -> None:
        self.kind_rows = {
            "regular": SessionKind("regular", "정기심의", 1),
            "expedited": SessionKind("expedited", "신속심의", 2),
            "other": SessionKind("other", "기타", 3),
        }
        self.rows: dict[int, Session] = {}
        self.job_rows: dict[str, SessionJob] = {}
        self.posts: dict[int, list[PostBrief]] = {}  # hospital -> posts of AI-on boards
        self.contents: dict[int, VersionContent] = {}  # version id -> content
        self.version_hospitals: dict[int, int] = {}
        self.version_sources: dict[int, SourceRef] = {}
        self.source_members: set[tuple[str, int]] = set()
        self._ids = itertools.count(100)
        self.fail_between = False

    # kinds
    def kinds(self):
        return list(self.kind_rows.values())

    def add_kind(self, key, label, position):
        if key in self.kind_rows:
            raise LookupError("taken")
        self.kind_rows[key] = SessionKind(key, label, position)
        return self.kind_rows[key]

    def update_kind(self, key, values):
        self.kind_rows[key] = replace(self.kind_rows[key], **values)
        return self.kind_rows[key]

    # sessions
    def _clash(self, s: Session) -> bool:
        if s.deleted_at is not None or s.state == "rejected":
            return False
        return any(
            o.id != s.id and o.deleted_at is None and o.state != "rejected"
            and (o.hospital_id, o.kind_key, o.meets_on) == (s.hospital_id, s.kind_key, s.meets_on)
            for o in self.rows.values()
        )

    def get(self, session_id):
        return self.rows.get(session_id)

    def sessions(self, session_ids):
        return [self.rows[i] for i in session_ids if i in self.rows]

    def between(self, hospital_ids, start, end):
        if self.fail_between:
            raise RuntimeError("database down")
        return [
            s for s in self.rows.values()
            if s.hospital_id in set(hospital_ids) and s.deleted_at is None
            and (start <= s.meets_on <= end or (s.cutoff_on is not None and start <= s.cutoff_on <= end))
        ]

    def same_day(self, hospital_id, kind_key, meets_on):
        return [s for s in self.rows.values() if (s.hospital_id, s.kind_key, s.meets_on) == (hospital_id, kind_key, meets_on)]

    def proposals(self, limit):
        return [s for s in self.rows.values() if s.state == "proposed" and s.deleted_at is None][:limit]

    def create(self, values):
        s = Session(id=next(self._ids), created_at=_now(), updated_at=_now(), **values)
        if self._clash(s):
            raise SessionTakenError
        self.rows[s.id] = s
        return s

    def update(self, session_id, values):
        s = replace(self.rows[session_id], **values, updated_at=_now())
        if self._clash(s):
            raise SessionTakenError
        self.rows[session_id] = s
        return s

    def version_hospital(self, version_id):
        return self.version_hospitals.get(version_id)

    def version_source(self, version_id):
        return self.version_sources.get(version_id)

    def is_source_member(self, user_id, source_id):
        return (user_id, source_id) in self.source_members

    # the AI job's input
    def posts_for(self, hospital_id, limit):
        return list(self.posts.get(hospital_id, []))[:limit]

    def version_content(self, version_id):
        return self.contents.get(version_id)

    def add_post(self, hospital_id: int, document_id: int, version_id: int, title: str, text: str, posted_on=None):
        self.posts.setdefault(hospital_id, []).append(PostBrief(document_id, version_id, title, posted_on))
        self.contents[version_id] = VersionContent(title, text, (FileContent("a.pdf", "1 KB", "x" * 64, text="file text"),))
        self.version_hospitals[version_id] = hospital_id

    # jobs
    def create_job(self, hospital_id, user_id):
        if any(j.hospital_id == hospital_id and j.status in OPEN for j in self.job_rows.values()):
            raise SessionJobTakenError
        job = SessionJob(str(uuid.uuid4()), hospital_id, user_id, "queued", _now())
        self.job_rows[job.id] = job
        return job

    def get_job(self, job_id):
        return self.job_rows.get(job_id)

    def jobs(self, hospital_id, limit):
        rows = [j for j in self.job_rows.values() if hospital_id is None or j.hospital_id == hospital_id]
        return sorted(rows, key=lambda j: j.created_at, reverse=True)[:limit]

    def open_jobs_of_user(self, user_id):
        return [j for j in self.job_rows.values() if j.user_id == user_id and j.status in OPEN]

    def update_job(self, job_id, values):
        job = self.job_rows.get(job_id)
        if job is not None and job.status in OPEN:
            self.job_rows[job_id] = replace(job, **values)

    def delete_job(self, job_id):
        self.job_rows.pop(job_id, None)

    def fail_open_jobs(self, error, now):
        open_ids = [j.id for j in self.job_rows.values() if j.status in OPEN]
        for i in open_ids:
            self.job_rows[i] = replace(self.job_rows[i], status="failed", error=error, finished_at=now.isoformat())
        return len(open_ids)

    def purge_jobs(self, cutoff):
        old = [j.id for j in self.job_rows.values() if j.status not in OPEN and j.created_at < cutoff.isoformat()]
        for i in old:
            del self.job_rows[i]
        return len(old)


@dataclass
class Found:
    post: int
    meets_on: str
    meets_at: str | None = None
    cutoff_on: str | None = None
    cutoff_at: str | None = None
    kind: str = "regular"
    quote: str = "quote"


class FakeFinder:
    model = "fake-model"

    def __init__(self, found=None, error: Exception | None = None) -> None:
        self.found = list(found or [])
        self.error = error
        self.calls: list[tuple[list[str], dict, str]] = []

    def find(self, posts, kinds, today):
        self.calls.append((posts, kinds, today))
        if self.error is not None:
            raise self.error
        return list(self.found)


class FakeDailyStore:
    """services.session_daily.DailySearchStore in memory."""

    def __init__(self) -> None:
        self.notice: dict[int, int] = {}  # source id -> hospital id (active notice boards, AI on)
        self.posts: list[tuple[int, PostBrief, str]] = []  # (source id, post, saved at)
        self.states: dict[int, object] = {}

    def notice_sources(self, source_ids):
        out: dict[int, list[int]] = {}
        for sid in source_ids:
            if sid in self.notice:
                out.setdefault(self.notice[sid], []).append(sid)
        return out

    def changed_posts(self, source_ids, since, limit):
        rows = sorted((r for r in self.posts if r[0] in source_ids and r[2] > since), key=lambda r: r[2], reverse=True)
        return [p for _, p, _ in rows][:limit]

    def state(self, hospital_id):
        return self.states.get(hospital_id)

    def save_state(self, state):
        self.states[state.hospital_id] = state


class HeldRunner:
    """Keeps the work until run() is called (a job stays queued in between)."""

    def __init__(self) -> None:
        self.work = []

    def submit(self, work):
        self.work.append(work)

    def run(self):
        while self.work:
            self.work.pop(0)()


class FakeTaskStore:
    def __init__(self, pipelines=None, sites=None) -> None:
        self.rows: dict[int, Task] = {}
        self._ids = itertools.count(500)
        self.pipelines = pipelines  # FakePipelineStore: the trigger's step check
        self.sites = sites  # FakeSiteStore: the trigger's site check

    def _check(self, t: Task) -> None:
        if t.site_id is not None and self.sites is not None:
            site = self.sites.rows.get(t.site_id)
            if site is None or site.project_id != t.project_id:
                raise TaskLinkError("the site is not in this project")
        if t.step_id is not None and self.pipelines is not None:
            step = self.pipelines.step_rows.get(t.step_id)
            pipe = self.pipelines.pipes.get(step.pipeline_id) if step else None
            if pipe is None or pipe.project_id != t.project_id:
                raise TaskLinkError("the step is not in this project")

    def tasks_of_project(self, project_id):
        return [t for t in self.rows.values() if t.project_id == project_id]

    def tasks_between(self, project_ids, start, end):
        return [
            t for t in self.rows.values()
            if t.project_id in set(project_ids) and t.deleted_at is None and start <= t.due_on <= end
        ]

    def due_tasks(self, until):
        return [t for t in self.rows.values() if t.deleted_at is None and t.done_at is None and t.due_on <= until]

    def get(self, task_id):
        return self.rows.get(task_id)

    def create(self, values):
        t = Task(id=next(self._ids), created_at=_now(), updated_at=_now(), **values)
        self._check(t)
        self.rows[t.id] = t
        return t

    def update(self, task_id, values):
        if task_id not in self.rows:
            raise LookupError("gone")
        t = replace(self.rows[task_id], **values, updated_at=_now())
        self._check(t)
        self.rows[task_id] = t
        return t

    def purge_removed(self, cutoff):
        old = [t.id for t in self.rows.values() if t.deleted_at is not None and t.deleted_at < cutoff]
        for i in old:
            del self.rows[i]
        return len(old)
