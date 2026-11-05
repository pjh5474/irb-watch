"""In-memory stand-ins for sources, scan storage and readers."""

import copy
from dataclasses import replace

from irb_watch.core.detect import Observation
from irb_watch.core.state import StoredDocument, VersionContent
from irb_watch.crawlers.snuh_reader import ScanReading
from irb_watch.services.scan import LoadedStore
from irb_watch.services.sources import DuplicateSourceError, Source


def make_source(source_id=1, **overrides) -> Source:
    values = {
        "id": source_id,
        "name": "SNUH rules",
        "adapter": "snuh_hrpp",
        "url": "https://hrpp.snuh.org/hrpp/bbs/list.do?key=2605277029",
        "config": {"board_key": "2605277029"},
        "needs_browser": False,
        "is_active": True,
        "created_at": "2026-11-05T00:00:00+00:00",
    }
    values.update(overrides)
    return Source(**values)


def make_obs(external_id="1", title="Title", body="body", files=(), warnings=()):
    return Observation(
        external_id,
        f"http://site/{external_id}",
        "2026-09-03",
        VersionContent(title, body, tuple(files)),
        tuple(warnings),
    )


class FakeSourceStore:
    def __init__(self) -> None:
        self.sources: dict[int, Source] = {}

    def create(self, name, adapter, url, config, created_by):
        if any(s.url == url for s in self.sources.values()):
            raise DuplicateSourceError
        source = make_source(len(self.sources) + 1, name=name, adapter=adapter, url=url, config=config)
        self.sources[source.id] = source
        return source

    def get(self, source_id):
        return self.sources.get(source_id)

    def list(self):
        return list(self.sources.values())

    def update(self, source_id, values):
        self.sources[source_id] = replace(self.sources[source_id], **values)
        return self.sources[source_id]


class FakeScanRepo:
    """Persists full history like the database does, but loads only the latest version."""

    def __init__(self) -> None:
        self.sources: dict[int, Source] = {}
        self.history: dict[int, dict[str, StoredDocument]] = {}
        self.doc_ids: dict[tuple[int, str], int] = {}
        self.locked: set[int] = set()
        self.stored_hashes: set[str] = set()
        self.fail_persist = False

    def add_source(self, source: Source) -> Source:
        self.sources[source.id] = source
        return source

    def get_source(self, source_id):
        return self.sources.get(source_id)

    def claim_scan(self, source_id, now, stale_minutes):
        if source_id in self.locked:
            return False
        self.locked.add(source_id)
        return True

    def release_scan(self, source_id, status, error, scanned_at):
        self.locked.discard(source_id)
        changes = {"last_scan_status": status, "last_scan_error": error}
        if scanned_at is not None:
            changes["last_scan_at"] = scanned_at.isoformat()
        self.sources[source_id] = replace(self.sources[source_id], **changes)

    def has_stored_file(self, sha256):
        return sha256 in self.stored_hashes

    def load_store(self, source_id):
        loaded = LoadedStore()
        for ext, full in self.history.get(source_id, {}).items():
            doc = copy.copy(full)
            doc.versions = [full.versions[-1]]
            loaded.store[ext] = doc
            loaded.doc_ids[ext] = self.doc_ids[(source_id, ext)]
            loaded.loaded_latest[ext] = full.versions[-1].version_no
        return loaded

    def persist(self, source_id, loaded, seen, now):
        if self.fail_persist:
            raise RuntimeError("database down")
        docs = self.history.setdefault(source_id, {})
        for ext, doc in loaded.store.items():
            kept = docs.get(ext)
            if kept is None:
                kept = copy.copy(doc)
                kept.versions = []
                docs[ext] = kept
                self.doc_ids[(source_id, ext)] = len(self.doc_ids) + 1
            have = kept.versions[-1].version_no if kept.versions else 0
            kept.versions.extend(v for v in doc.versions if v.version_no > have)
            kept.status, kept.miss_count, kept.removed_at = doc.status, doc.miss_count, doc.removed_at
            kept.source_url, kept.posted_on = doc.source_url, doc.posted_on
            kept.review_reason = doc.review_reason
        return {e: self.doc_ids[(source_id, e)] for e in docs}


class FakeFileStorage:
    def __init__(self) -> None:
        self.puts: list[str] = []

    def put(self, path, content):
        self.puts.append(path)


class FakeReader:
    def __init__(self, reading=None, error=None, sink_calls=()) -> None:
        self.reading = reading or ScanReading()
        self.error = error
        self.sink_calls = list(sink_calls)  # (sha256, filename, content)
        self.sink_results: list[str] = []
        self.closed = False

    def read(self, file_sink):
        for sha, name, content in self.sink_calls:
            self.sink_results.append(file_sink(sha, name, content))
        if self.error is not None:
            raise self.error
        return self.reading

    def close(self):
        self.closed = True
