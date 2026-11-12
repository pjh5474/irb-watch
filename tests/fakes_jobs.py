"""In-memory stand-ins for services.draft_jobs.JobStore and a runner that runs later."""

import itertools
from dataclasses import replace
from datetime import UTC, datetime

from irb_watch.services.draft_jobs import OPEN, DraftJobService, InlineRunner, Job, JobTakenError


class FakeJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self.fail_update = False

    def create(self, user_id, kind, draft_id, step_id):
        for j in self.jobs.values():
            if j.status not in OPEN:
                continue
            # The two unique indexes of the real table.
            if step_id is not None and j.pipeline_step_id == step_id:
                raise JobTakenError
            if kind == "revision" and j.kind == "revision" and j.draft_id == draft_id:
                raise JobTakenError
        n = next(self._ids)
        job = Job(
            f"00000000-0000-0000-0000-{n:012d}", user_id, kind, "queued",
            f"2026-11-12T00:00:{n:02d}+00:00", draft_id=draft_id, pipeline_step_id=step_id,
        )
        self.jobs[job.id] = job
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def for_user(self, user_id, since):
        found = [
            j for j in self.jobs.values()
            if j.user_id == user_id and (j.status in OPEN or j.created_at >= since.isoformat())
        ]
        return sorted(found, key=lambda j: j.created_at, reverse=True)

    def update(self, job_id, values):
        if self.fail_update:
            raise RuntimeError("database down")
        if self.jobs[job_id].status in OPEN:  # like the real store: an ended job keeps its outcome
            self.jobs[job_id] = replace(self.jobs[job_id], **values)

    def delete(self, job_id):
        self.jobs.pop(job_id, None)

    def fail_open(self, error, now):
        open_ids = [j.id for j in self.jobs.values() if j.status in OPEN]
        for i in open_ids:
            self.jobs[i] = replace(self.jobs[i], status="failed", error=error, finished_at=now.isoformat())
        return len(open_ids)

    def purge_before(self, cutoff):
        old = [j.id for j in self.jobs.values() if j.status not in OPEN and j.created_at < cutoff.isoformat()]
        for i in old:
            del self.jobs[i]
        return len(old)


class LaterRunner:
    """Keeps the work until run() is called (a job that is still queued)."""

    def __init__(self) -> None:
        self.waiting: list = []

    def submit(self, work) -> None:
        self.waiting.append(work)

    def run(self) -> None:
        waiting, self.waiting = self.waiting, []
        for work in waiting:
            work()


def finished_draft(client, res, headers) -> dict:
    """For API tests with inline jobs: the answer to a draft or revise request is a job that is
    already done; returns the draft as GET /docgen/drafts/{id} shows it."""
    assert res.status_code == 202, res.text
    job = res.json()
    assert job["status"] == "done", job
    again = client.get(f"/docgen/jobs/{job['id']}", headers=headers)
    assert again.status_code == 200 and again.json()["draft_id"] == job["draft_id"]
    detail = client.get(f"/docgen/drafts/{job['draft_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    return detail.json()


def inline_jobs(store: FakeJobStore | None = None) -> DraftJobService:
    """Jobs that finish inside the request, at a fixed time."""
    return DraftJobService(
        store or FakeJobStore(), InlineRunner(), clock=lambda: datetime(2026, 11, 12, 1, tzinfo=UTC)
    )
