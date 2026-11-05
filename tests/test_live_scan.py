"""End-to-end scan of a REAL SNUH board into the REAL Supabase project.

Skipped unless RUN_LIVE=1 (PowerShell: $env:RUN_LIVE=1; uv run pytest -m live -k scan).
Takes a few minutes (polite 2-second delay between requests). It registers a throwaway
source, scans it three times, then deletes the source (documents, versions and file rows
cascade) and the originals it uploaded. Audit-log rows cannot be deleted and stay behind.
"""

import hashlib
import os
import uuid

import pytest

from irb_watch.api.deps import build_reader
from irb_watch.db.client import get_supabase_client
from irb_watch.db.documents import (
    BUCKET,
    SupabaseFileStorage,
    SupabaseScanRepo,
    SupabaseSourceStore,
)
from irb_watch.db.supabase_users import SupabaseAuditSink
from irb_watch.services.scan import ScanService
from irb_watch.services.sources import SourceService

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to run"),
]

URL = "https://hrpp.snuh.org/hrpp/bbs/list.do?key=2605277029"


class CountingStorage:
    def __init__(self, inner):
        self._inner = inner
        self.puts: list[str] = []

    def put(self, path, content):
        self.puts.append(path)
        self._inner.put(path, content)


def test_scan_real_board_end_to_end():
    client = get_supabase_client()
    audit = SupabaseAuditSink(client)
    storage = CountingStorage(SupabaseFileStorage(client))
    scans = ScanService(SupabaseScanRepo(client), storage, build_reader, audit)
    sources = SourceService(SupabaseSourceStore(client), audit)

    source = sources.create(None, f"live test {uuid.uuid4().hex[:8]}", URL)
    try:
        # 1. First scan: silent baseline, everything stored.
        first = scans.scan_source(source.id, None)
        assert first.status == "ok", first.error
        assert first.baseline and first.new == 0

        docs = client.table("documents").select("*").eq("source_id", source.id).execute().data
        assert docs and all(d["status"] == "current" and d["latest_version_id"] for d in docs)
        versions = (
            client.table("document_versions").select("*").eq("source_id", source.id).execute().data
        )
        assert len(versions) == len(docs) and {v["change_type"] for v in versions} == {"baseline"}
        files = (
            client.table("document_version_files").select("*").eq("source_id", source.id).execute().data
        )
        assert files and all(f["storage_path"] == f["sha256"] for f in files)

        # HWP attachments are read now: they carry text and no failure reason.
        hwp_files = [f for f in files if f["filename"].lower().endswith(".hwp")]
        assert hwp_files, "expected the board to have HWP attachments"
        for f in hwp_files:
            assert f["text_content"] and f["hash_only_reason"] is None, f["filename"]
        # Nothing on this board should be flagged unreadable.
        assert not [f for f in files if f["hash_only_reason"] == "unreadable"]
        assert all(d["review_reason"] is None for d in docs)

        # An uploaded original must come back byte for byte.
        sample = files[0]
        stored = client.storage.from_(BUCKET).download(sample["storage_path"])
        assert hashlib.sha256(stored).hexdigest() == sample["sha256"]
        uploads_after_first = len(storage.puts)
        assert uploads_after_first > 0

        # 2. Second scan with nothing changed: no events, no new versions, no uploads.
        second = scans.scan_source(source.id, None)
        assert second.status == "ok" and not second.baseline
        assert (second.new, second.updated, second.removed, second.reappeared) == (0, 0, 0, 0)
        assert len(storage.puts) == uploads_after_first

        # 3. Change one stored version behind the scanner's back: the next scan must see it.
        target = docs[0]
        client.table("document_versions").update({"body_text": "tampered"}).eq(
            "id", target["latest_version_id"]
        ).execute()
        third = scans.scan_source(source.id, None)
        assert third.updated == 1 and third.new == 0
        history = (
            client.table("document_versions")
            .select("version_no, change_type")
            .eq("document_id", target["id"])
            .order("version_no")
            .execute()
            .data
        )
        assert [(v["version_no"], v["change_type"]) for v in history] == [(1, "baseline"), (2, "updated")]
        logged = (
            client.table("audit_logs")
            .select("event_type")
            .eq("source_id", source.id)
            .eq("event_type", "document.updated")
            .execute()
            .data
        )
        assert len(logged) == 1

        status = client.table("sources").select("*").eq("id", source.id).execute().data[0]
        assert status["last_scan_status"] == "ok" and status["scan_started_at"] is None
    finally:
        client.table("sources").delete().eq("id", source.id).execute()
        uploaded = sorted(set(storage.puts))
        if uploaded:
            client.storage.from_(BUCKET).remove(uploaded)
