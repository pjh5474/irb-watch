"""Supabase-backed pieces of the file download links."""

from datetime import datetime

from supabase import Client

from irb_watch.db.documents import BUCKET, user_may_read
from irb_watch.services.downloads import StoredFile


class SupabaseDownloadRepo:
    def __init__(self, client: Client) -> None:
        self._c = client

    def get_file(self, file_id: int) -> StoredFile | None:
        rows = (
            self._c.table("document_version_files")
            .select("id, source_id, version_id, filename, sha256, storage_path")
            .eq("id", file_id)
            .limit(1)
            .execute()
            .data
        )
        if not rows:
            return None
        f = rows[0]
        versions = (
            self._c.table("document_versions")
            .select("document_id, version_no")
            .eq("id", f["version_id"])
            .limit(1)
            .execute()
            .data
        )
        if not versions:
            return None
        return StoredFile(
            file_id=f["id"],
            source_id=f["source_id"],
            document_id=versions[0]["document_id"],
            version_no=versions[0]["version_no"],
            filename=f["filename"],
            sha256=f["sha256"],
            storage_path=f["storage_path"],
        )

    def may_read(self, user_id: str, source_id: int, now: datetime) -> bool:
        return user_may_read(self._c, user_id, source_id, now)

    def count_links_since(self, user_id: str, since: datetime) -> int:
        result = (
            self._c.table("audit_logs")
            .select("id", count="exact")
            .eq("actor_id", user_id)
            .eq("event_type", "file.download_link")
            .gte("created_at", since.isoformat())
            .execute()
        )
        return result.count or 0


class SupabaseUrlSigner:
    def __init__(self, client: Client) -> None:
        self._bucket = client.storage.from_(BUCKET)

    def sign(self, storage_path: str, filename: str, expires_in: int) -> str:
        result = self._bucket.create_signed_url(
            storage_path, expires_in, {"download": filename}
        )
        url = result.get("signedURL")
        if not url:
            raise RuntimeError("storage returned no signed URL")
        return url
