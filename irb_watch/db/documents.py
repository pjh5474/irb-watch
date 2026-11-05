"""Supabase-backed storage for sources, documents, versions, files and original files."""

from datetime import datetime, timedelta

from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError
from supabase import Client

from irb_watch.core.hashing import sha256_hex
from irb_watch.core.state import FileContent, StoredDocument, StoredVersion, VersionContent
from irb_watch.services.scan import LoadedStore
from irb_watch.services.sources import DuplicateSourceError, Source
from irb_watch.services.summaries import DocumentForSummary

BUCKET = "documents"
UNIQUE_VIOLATION = "23505"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _source(row: dict) -> Source:
    return Source(
        id=row["id"],
        name=row["name"],
        adapter=row["adapter"],
        url=row["url"],
        config=row.get("config") or {},
        needs_browser=row["needs_browser"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        last_scan_at=row.get("last_scan_at"),
        last_scan_status=row.get("last_scan_status"),
        last_scan_error=row.get("last_scan_error"),
        scan_started_at=row.get("scan_started_at"),
    )


class SupabaseSourceStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def create(self, name, adapter, url, config, created_by) -> Source:
        try:
            rows = (
                self._c.table("sources")
                .insert(
                    {
                        "name": name,
                        "adapter": adapter,
                        "url": url,
                        "config": config,
                        "created_by": created_by,
                    }
                )
                .execute()
                .data
            )
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise DuplicateSourceError from exc
            raise
        return _source(rows[0])

    def get(self, source_id: int) -> Source | None:
        rows = self._c.table("sources").select("*").eq("id", source_id).limit(1).execute().data
        return _source(rows[0]) if rows else None

    def list(self) -> list[Source]:
        rows = self._c.table("sources").select("*").order("id").execute().data
        return [_source(r) for r in rows]

    def update(self, source_id: int, values: dict) -> Source:
        rows = self._c.table("sources").update(values).eq("id", source_id).execute().data
        return _source(rows[0])


class SupabaseFileStorage:
    def __init__(self, client: Client) -> None:
        self._bucket = client.storage.from_(BUCKET)

    def put(self, path: str, content: bytes) -> None:
        try:
            self._bucket.upload(
                path, content, {"content-type": "application/octet-stream", "upsert": "false"}
            )
        except StorageApiError as exc:
            # An earlier run may have uploaded it before its database rows were saved.
            if "Duplicate" in str(exc) or str(getattr(exc, "status", "")) == "409":
                return
            raise


class SupabaseScanRepo:
    def __init__(self, client: Client) -> None:
        self._c = client

    # -- sources / lock ----------------------------------------------------------

    def get_source(self, source_id: int) -> Source | None:
        return SupabaseSourceStore(self._c).get(source_id)

    def claim_scan(self, source_id: int, now: datetime, stale_minutes: int) -> bool:
        cutoff = (now - timedelta(minutes=stale_minutes)).isoformat()
        rows = (
            self._c.table("sources")
            .update({"scan_started_at": now.isoformat()})
            .eq("id", source_id)
            .or_(f"scan_started_at.is.null,scan_started_at.lt.{cutoff}")
            .execute()
            .data
        )
        return bool(rows)

    def release_scan(self, source_id, status, error, scanned_at) -> None:
        values: dict = {
            "scan_started_at": None,
            "last_scan_status": status,
            "last_scan_error": error,
        }
        if scanned_at is not None:
            values["last_scan_at"] = scanned_at.isoformat()
        self._c.table("sources").update(values).eq("id", source_id).execute()

    # -- load ---------------------------------------------------------------------

    def has_stored_file(self, sha256: str) -> bool:
        rows = (
            self._c.table("document_version_files")
            .select("id")
            .eq("sha256", sha256)
            .not_.is_("storage_path", "null")
            .limit(1)
            .execute()
            .data
        )
        return bool(rows)

    def load_store(self, source_id: int) -> LoadedStore:
        loaded = LoadedStore()
        docs = self._c.table("documents").select("*").eq("source_id", source_id).execute().data
        version_ids = [d["latest_version_id"] for d in docs if d["latest_version_id"]]
        versions = {}
        files_by_version: dict[int, list[dict]] = {}
        if version_ids:
            for row in (
                self._c.table("document_versions").select("*").in_("id", version_ids).execute().data
            ):
                versions[row["id"]] = row
            for row in (
                self._c.table("document_version_files")
                .select("*")
                .in_("version_id", version_ids)
                .execute()
                .data
            ):
                files_by_version.setdefault(row["version_id"], []).append(row)

        for d in docs:
            doc = StoredDocument(
                external_id=d["external_id"],
                source_url=d["source_url"],
                posted_on=d["posted_on"],
                first_seen_at=_dt(d["first_seen_at"]),
                status=d["status"],
                miss_count=d["miss_count"],
                removed_at=_dt(d["removed_at"]),
                review_reason=d.get("review_reason"),
            )
            v = versions.get(d["latest_version_id"])
            if v is None:
                continue  # a document without a version cannot be compared; skip it
            files = tuple(
                FileContent(
                    filename=f["filename"],
                    size_label=f["size_label"] or "",
                    sha256=f["sha256"],
                    text=f["text_content"],
                    hash_only_reason=f["hash_only_reason"],
                    storage_path=f["storage_path"],
                    note=f["extract_note"],
                )
                for f in files_by_version.get(v["id"], [])
            )
            doc.versions.append(
                StoredVersion(
                    v["version_no"],
                    _dt(v["created_at"]),
                    v["change_type"],
                    VersionContent(v["title"], v["body_text"], files),
                    summary=v.get("summary"),
                    priority=v.get("priority"),
                    category=v.get("category"),
                    summary_source=v.get("summary_source"),
                    classification=v.get("classification"),
                )
            )
            loaded.store[doc.external_id] = doc
            loaded.doc_ids[doc.external_id] = d["id"]
            loaded.loaded_latest[doc.external_id] = v["version_no"]
            loaded.snapshots[doc.external_id] = _snapshot(doc)
        return loaded

    # -- save ---------------------------------------------------------------------

    def persist(
        self, source_id: int, loaded: LoadedStore, seen: set[str], now: datetime
    ) -> dict[str, int]:
        """Write what detect_changes decided. Returns external_id -> document id."""
        doc_ids = dict(loaded.doc_ids)
        for external_id, doc in loaded.store.items():
            if external_id not in doc_ids:
                doc_ids[external_id] = self._insert_document(source_id, doc, now)
                new_versions = doc.versions
            else:
                already = loaded.loaded_latest.get(external_id, 0)
                new_versions = [v for v in doc.versions if v.version_no > already]

            latest_id = None
            for version in new_versions:
                latest_id = self._insert_version(source_id, doc_ids[external_id], version)
            if latest_id is not None or _snapshot(doc) != loaded.snapshots.get(external_id):
                values = _document_values(doc)
                if latest_id is not None:
                    values["latest_version_id"] = latest_id
                self._c.table("documents").update(values).eq("id", doc_ids[external_id]).execute()

        seen_ids = [doc_ids[e] for e in seen if e in doc_ids]
        if seen_ids:
            self._c.table("documents").update({"last_seen_at": now.isoformat()}).in_(
                "id", seen_ids
            ).execute()
        return doc_ids

    def _insert_document(self, source_id: int, doc: StoredDocument, now: datetime) -> int:
        row = {
            "source_id": source_id,
            "external_id": doc.external_id,
            "first_seen_at": doc.first_seen_at.isoformat(),
            **_document_values(doc),
        }
        return self._c.table("documents").insert(row).execute().data[0]["id"]

    def _insert_version(self, source_id: int, document_id: int, version: StoredVersion) -> int:
        c = version.content
        row = self._c.table("document_versions").insert(
            {
                "document_id": document_id,
                "source_id": source_id,
                "version_no": version.version_no,
                "change_type": version.change_type,
                "title": c.title,
                "body_hash": sha256_hex(c.body_text.encode("utf-8")),
                "body_text": c.body_text,
                "created_at": version.created_at.isoformat(),
                "summary": version.summary,
                "priority": version.priority,
                "category": version.category,
                "summary_source": version.summary_source,
                "classification": version.classification,
            }
        ).execute().data[0]
        if c.files:
            self._c.table("document_version_files").insert(
                [
                    {
                        "version_id": row["id"],
                        "source_id": source_id,
                        "filename": f.filename,
                        "size_label": f.size_label,
                        "sha256": f.sha256,
                        "storage_path": f.storage_path,
                        "text_content": f.text,
                        "hash_only_reason": f.hash_only_reason,
                        "extract_note": f.note,
                    }
                    for f in c.files
                ]
            ).execute()
        return row["id"]


class SupabaseSummaryRepo:
    """Reads and writes for summaries requested by users."""

    def __init__(self, client: Client) -> None:
        self._c = client

    def get_latest(self, document_id: int) -> DocumentForSummary | None:
        docs = (
            self._c.table("documents")
            .select("id, source_id, latest_version_id")
            .eq("id", document_id)
            .limit(1)
            .execute()
            .data
        )
        if not docs or not docs[0]["latest_version_id"]:
            return None
        doc = docs[0]
        version = (
            self._c.table("document_versions")
            .select("id, version_no, title, body_text, document_summary")
            .eq("id", doc["latest_version_id"])
            .limit(1)
            .execute()
            .data[0]
        )
        rows = (
            self._c.table("document_version_files")
            .select("filename, size_label, sha256, text_content, hash_only_reason, extract_note")
            .eq("version_id", version["id"])
            .order("id")
            .execute()
            .data
        )
        files = tuple(
            FileContent(
                filename=f["filename"],
                size_label=f["size_label"] or "",
                sha256=f["sha256"],
                text=f["text_content"],
                hash_only_reason=f["hash_only_reason"],
                note=f["extract_note"],
            )
            for f in rows
        )
        return DocumentForSummary(
            document_id=doc["id"],
            source_id=doc["source_id"],
            version_id=version["id"],
            version_no=version["version_no"],
            content=VersionContent(version["title"], version["body_text"], files),
            document_summary=version["document_summary"],
        )

    def is_subscribed(self, user_id: str, source_id: int) -> bool:
        rows = (
            self._c.table("subscriptions")
            .select("user_id")
            .eq("user_id", user_id)
            .eq("source_id", source_id)
            .limit(1)
            .execute()
            .data
        )
        return bool(rows)

    def count_requests_since(self, user_id: str, since: datetime) -> int:
        result = (
            self._c.table("summary_requests")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .gte("created_at", since.isoformat())
            .limit(1)
            .execute()
        )
        return result.count or 0

    def save_summary(self, version_id: int, text: str, now: datetime) -> None:
        self._c.table("document_versions").update(
            {"document_summary": text, "document_summary_at": now.isoformat()}
        ).eq("id", version_id).execute()

    def record_request(self, user_id: str, version_id: int, now: datetime) -> None:
        self._c.table("summary_requests").insert(
            {"user_id": user_id, "version_id": version_id, "created_at": now.isoformat()}
        ).execute()


def _document_values(doc: StoredDocument) -> dict:
    return {
        "title": doc.latest.content.title,
        "source_url": doc.source_url,
        "posted_on": doc.posted_on,
        "status": doc.status,
        "miss_count": doc.miss_count,
        "removed_at": doc.removed_at.isoformat() if doc.removed_at else None,
        "review_reason": doc.review_reason,
    }


def _snapshot(doc: StoredDocument) -> tuple:
    return (
        doc.latest.content.title,
        doc.source_url,
        doc.posted_on,
        doc.status,
        doc.miss_count,
        doc.removed_at,
        doc.review_reason,
    )
