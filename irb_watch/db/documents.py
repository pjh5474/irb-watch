"""Supabase-backed storage for sources, documents, versions, files and original files."""

import json
from datetime import datetime, timedelta

from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError
from supabase import Client

from irb_watch.core.hashing import sha256_hex
from irb_watch.core.state import FileContent, StoredDocument, StoredVersion, VersionContent
from irb_watch.crawlers.errors import ScanTooLargeError
from irb_watch.services.scan import KEEP_PAUSE, LoadedStore, ScanCard
from irb_watch.services.sources import DuplicateSourceError, Source
from irb_watch.services.summaries import DocumentForSummary

BUCKET = "documents"
UNIQUE_VIOLATION = "23505"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def version_files(client: Client, version_ids: list[int]) -> dict[int, tuple[FileContent, ...]]:
    """The files of these versions, in the order they were saved: the one query every reader
    of stored files uses (scan comparison, summaries, file-warning reviews)."""
    found: dict[int, list[FileContent]] = {}
    if not version_ids:
        return {}
    rows = (
        client.table("document_version_files")
        .select("version_id, filename, size_label, sha256, text_content, hash_only_reason, storage_path, extract_note")
        .in_("version_id", version_ids)
        .order("id")
        .execute()
        .data
    )
    for f in rows:
        found.setdefault(f["version_id"], []).append(
            FileContent(
                filename=f["filename"],
                size_label=f["size_label"] or "",
                sha256=f["sha256"],
                text=f["text_content"],
                hash_only_reason=f["hash_only_reason"],
                storage_path=f["storage_path"],
                note=f["extract_note"],
            )
        )
    return {vid: tuple(files) for vid, files in found.items()}


def acknowledged_keys(client: Client, document_ids: list[int]) -> dict[int, str]:
    """File warnings an admin marked OK, per document (admin-only table document_reviews)."""
    if not document_ids:
        return {}
    rows = (
        client.table("document_reviews")
        .select("document_id, ack_key")
        .in_("document_id", document_ids)
        .execute()
        .data
    )
    return {r["document_id"]: r["ack_key"] for r in rows}


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
        requires_login=bool(row.get("requires_login", False)),
        scans_paused_reason=row.get("scans_paused_reason"),
        ai_enabled=bool(row.get("ai_enabled", True)),
        hospital_id=row.get("hospital_id"),
        board_kind=row.get("board_kind") or "other",
    )


class SupabaseSourceStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def create(
        self,
        name,
        adapter,
        url,
        config,
        created_by,
        requires_login=False,
        ai_enabled=True,
        hospital_id=None,
        board_kind="other",
    ) -> Source:
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
                        "requires_login": requires_login,
                        "ai_enabled": ai_enabled,
                        "hospital_id": hospital_id,
                        "board_kind": board_kind,
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
        try:
            rows = self._c.table("sources").update(values).eq("id", source_id).execute().data
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:  # e.g. removing the login makes it a duplicate
                raise DuplicateSourceError from exc
            raise
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
    def __init__(self, client: Client, max_payload_bytes: int = 20_000_000) -> None:
        self._c = client
        self._max_payload_bytes = max_payload_bytes

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

    def release_scan(self, source_id, status, error, scanned_at, paused_reason=None) -> None:
        values: dict = {
            "scan_started_at": None,
            "last_scan_status": status,
            "last_scan_error": error,
        }
        if paused_reason is not KEEP_PAUSE:  # text sets the pause, None clears it
            values["scans_paused_reason"] = paused_reason
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
        if version_ids:
            for row in (
                self._c.table("document_versions").select("*").in_("id", version_ids).execute().data
            ):
                versions[row["id"]] = row
        files_by_version = version_files(self._c, version_ids)
        acks = acknowledged_keys(self._c, [d["id"] for d in docs])

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
                acknowledged_key=acks.get(d["id"]),
            )
            v = versions.get(d["latest_version_id"])
            if v is None:
                continue  # a document without a version cannot be compared; skip it
            files = files_by_version.get(v["id"], ())
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

    def commit_scan(
        self,
        source_id: int,
        loaded: LoadedStore,
        seen: set[str],
        now: datetime,
        cards: list[ScanCard],
        status: str,
        error: str | None,
    ) -> dict[str, int]:
        """Save the whole scan with one call to the database function public.commit_scan.

        The function runs in a single transaction: on any error nothing is saved, so no
        partial documents, no versions without their cards, and last_scan_at never moves.
        Returns external_id -> document id for the documents written.
        """
        params = {
            "p_source_id": source_id,
            "p_now": now.isoformat(),
            "p_documents": build_commit_documents(loaded),
            "p_seen": sorted(seen),
            "p_cards": [_card_row(c) for c in cards],
            "p_status": status,
            "p_error": error,
        }
        size = len(json.dumps(params, ensure_ascii=False).encode("utf-8"))
        if size > self._max_payload_bytes:
            raise ScanTooLargeError(
                f"scan too large to save safely in one step ({size / 1e6:.1f} MB, "
                f"limit {self._max_payload_bytes / 1e6:.1f} MB); nothing was saved"
            )
        return self._c.rpc("commit_scan", params).execute().data


def _card_row(card: ScanCard) -> dict:
    return {
        "category": card.category,
        "event_type": card.event_type,
        "actor_id": card.actor_id,
        "title": card.title,
        "summary": card.summary,
        "priority": card.priority,
        "payload": card.payload,
        "external_id": card.external_id,
    }


def _version_row(version: StoredVersion) -> dict:
    c = version.content
    return {
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
        "files": [
            {
                "filename": f.filename,
                "size_label": f.size_label,
                "sha256": f.sha256,
                "storage_path": f.storage_path,
                "text_content": f.text,
                "hash_only_reason": f.hash_only_reason,
                "extract_note": f.note,
            }
            for f in c.files
        ],
    }


def build_commit_documents(loaded: LoadedStore) -> list[dict]:
    """The documents that need writing: new ones, ones with new versions, ones whose row changed."""
    items: list[dict] = []
    for external_id, doc in loaded.store.items():
        doc_id = loaded.doc_ids.get(external_id)
        if doc_id is None:
            new_versions = list(doc.versions)
        else:
            already = loaded.loaded_latest.get(external_id, 0)
            new_versions = [v for v in doc.versions if v.version_no > already]
        changed = _snapshot(doc) != loaded.snapshots.get(external_id)
        if doc_id is not None and not new_versions and not changed:
            continue
        items.append(
            {
                "external_id": external_id,
                "doc_id": doc_id,
                "first_seen_at": doc.first_seen_at.isoformat(),
                "values": _document_values(doc),
                "versions": [_version_row(v) for v in new_versions],
            }
        )
    return items


def user_may_read(client: Client, user_id: str, source_id: int, now: datetime) -> bool:
    """Server-side twin of the database rule private.can_read_source (for non-admins).

    Any existing public source is readable; a members-only source (a study) only by users an
    admin assigned to it (source_members). `now` is unused; it stays so the callers' shape
    does not change.
    """
    source = client.table("sources").select("requires_login").eq("id", source_id).execute().data
    if not source:
        return False
    if not source[0]["requires_login"]:
        return True
    member = (
        client.table("source_members")
        .select("user_id")
        .eq("user_id", user_id)
        .eq("source_id", source_id)
        .limit(1)
        .execute()
        .data
    )
    return bool(member)


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
        source_rows = (
            self._c.table("sources")
            .select("ai_enabled")
            .eq("id", doc["source_id"])
            .limit(1)
            .execute()
            .data
        )
        # A source that cannot be read here fails closed: no AI on it.
        ai_enabled = bool(source_rows and source_rows[0]["ai_enabled"])
        version = (
            self._c.table("document_versions")
            .select("id, version_no, title, body_text, document_summary, document_summary_at")
            .eq("id", doc["latest_version_id"])
            .limit(1)
            .execute()
            .data[0]
        )
        files = version_files(self._c, [version["id"]]).get(version["id"], ())
        return DocumentForSummary(
            document_id=doc["id"],
            source_id=doc["source_id"],
            version_id=version["id"],
            version_no=version["version_no"],
            content=VersionContent(version["title"], version["body_text"], files),
            document_summary=version["document_summary"],
            summary_at=version["document_summary_at"],
            ai_enabled=ai_enabled,
        )

    def may_read(self, user_id: str, source_id: int, now: datetime) -> bool:
        return user_may_read(self._c, user_id, source_id, now)

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
