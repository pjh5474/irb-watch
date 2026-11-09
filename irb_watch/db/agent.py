"""Supabase-backed storage and reads for the assistant."""

from datetime import datetime

from supabase import Client

from irb_watch.agent.data import (
    ChangeInfo,
    DocumentBrief,
    DocumentInfo,
    SourceInfo,
    VersionInfo,
)
from irb_watch.core.state import FileContent, VersionContent
from irb_watch.services.agent import ChatMessage, Conversation, Limits

DEFAULT_LIMITS = Limits(user_daily_limit=30, admin_daily_limit=100)


def _conversation(row: dict) -> Conversation:
    return Conversation(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _message(row: dict) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        actions=row.get("actions") or [],
        created_at=row["created_at"],
    )


class SupabaseAgentStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def create_conversation(self, user_id: str, now: datetime) -> Conversation:
        rows = (
            self._c.table("chat_conversations")
            .insert(
                {"user_id": user_id, "created_at": now.isoformat(), "updated_at": now.isoformat()}
            )
            .execute()
            .data
        )
        return _conversation(rows[0])

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        rows = (
            self._c.table("chat_conversations")
            .select("*")
            .eq("id", conversation_id)
            .limit(1)
            .execute()
            .data
        )
        return _conversation(rows[0]) if rows else None

    def list_conversations(self, user_id: str, limit: int) -> list[Conversation]:
        rows = (
            self._c.table("chat_conversations")
            .select("*")
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        return [_conversation(r) for r in rows]

    def delete_conversation(self, conversation_id: str) -> None:
        # Its messages go with it (on delete cascade).
        self._c.table("chat_conversations").delete().eq("id", conversation_id).execute()

    def set_title(self, conversation_id: str, title: str) -> None:
        self._c.table("chat_conversations").update({"title": title}).eq(
            "id", conversation_id
        ).execute()

    def list_messages(self, conversation_id: str, limit: int | None) -> list[ChatMessage]:
        query = self._c.table("chat_messages").select("*").eq("conversation_id", conversation_id)
        if limit is None:
            rows = query.order("id").execute().data
        else:
            rows = list(reversed(query.order("id", desc=True).limit(limit).execute().data))
        return [_message(r) for r in rows]

    def count_messages(self, conversation_id: str) -> int:
        result = (
            self._c.table("chat_messages")
            .select("id", count="exact")
            .eq("conversation_id", conversation_id)
            .limit(1)
            .execute()
        )
        return result.count or 0

    def add_message(
        self,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        actions: list[dict],
        now: datetime,
    ) -> ChatMessage:
        rows = (
            self._c.table("chat_messages")
            .insert(
                {
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "role": role,
                    "content": content,
                    "actions": actions,
                    "created_at": now.isoformat(),
                }
            )
            .execute()
            .data
        )
        self._c.table("chat_conversations").update({"updated_at": now.isoformat()}).eq(
            "id", conversation_id
        ).execute()
        return _message(rows[0])

    def get_limits(self) -> Limits:
        rows = self._c.table("agent_settings").select("*").eq("id", 1).limit(1).execute().data
        if not rows:
            return DEFAULT_LIMITS
        return Limits(rows[0]["user_daily_limit"], rows[0]["admin_daily_limit"])

    def set_limits(self, limits: Limits, updated_by: str, now: datetime) -> None:
        self._c.table("agent_settings").update(
            {
                "user_daily_limit": limits.user_daily_limit,
                "admin_daily_limit": limits.admin_daily_limit,
                "updated_by": updated_by,
                "updated_at": now.isoformat(),
            }
        ).eq("id", 1).execute()

    def count_messages_since(self, user_id: str, since: datetime) -> int:
        result = (
            self._c.table("audit_logs")
            .select("id", count="exact")
            .eq("actor_id", user_id)
            .eq("event_type", "agent.message")
            .gte("created_at", since.isoformat())
            .limit(1)
            .execute()
        )
        return result.count or 0

    def purge_before(self, cutoff: datetime) -> int:
        rows = (
            self._c.table("chat_conversations")
            .delete()
            .lt("updated_at", cutoff.isoformat())
            .execute()
            .data
        )
        return len(rows or [])


def _source_info(row: dict) -> SourceInfo:
    return SourceInfo(
        id=row["id"],
        name=row["name"],
        is_active=row["is_active"],
        last_scan_at=row.get("last_scan_at"),
        last_scan_status=row.get("last_scan_status"),
        requires_login=bool(row.get("requires_login", False)),
    )


class SupabaseAgentData:
    """Reads for the assistant's tools. The access rule is the same as private.can_read_source:
    admins read every source, anyone reads a public one, a study only its assigned members. On
    top of that a source with ai_enabled = false is hidden: its text must not reach the model."""

    def __init__(self, client: Client) -> None:
        self._c = client

    def visible_sources(self, user_id: str, is_admin: bool) -> list[SourceInfo]:
        rows = (
            self._c.table("sources")
            .select("id, name, is_active, last_scan_at, last_scan_status, requires_login")
            .eq("ai_enabled", True)
            .order("id")
            .execute()
            .data
        )
        if is_admin:
            return [_source_info(r) for r in rows]
        member_of: set[int] = set()
        if any(r["requires_login"] for r in rows):
            member_rows = (
                self._c.table("source_members")
                .select("source_id")
                .eq("user_id", user_id)
                .execute()
                .data
            )
            member_of = {m["source_id"] for m in member_rows}
        return [_source_info(r) for r in rows if not r["requires_login"] or r["id"] in member_of]

    def get_source(self, source_id: int) -> SourceInfo | None:
        rows = (
            self._c.table("sources")
            .select("id, name, is_active, last_scan_at, last_scan_status, requires_login")
            .eq("id", source_id)
            .limit(1)
            .execute()
            .data
        )
        return _source_info(rows[0]) if rows else None

    def get_document(self, document_id: int) -> DocumentInfo | None:
        docs = (
            self._c.table("documents")
            .select("id, source_id, title, status, posted_on, source_url, latest_version_id")
            .eq("id", document_id)
            .limit(1)
            .execute()
            .data
        )
        if not docs:
            return None
        doc = docs[0]
        versions = (
            self._c.table("document_versions")
            .select("id, version_no, created_at, change_type, summary, priority")
            .eq("document_id", document_id)
            .order("version_no")
            .execute()
            .data
        )
        file_names: tuple[str, ...] = ()
        if versions:
            newest = versions[-1]["id"]
            files = (
                self._c.table("document_version_files")
                .select("filename")
                .eq("version_id", newest)
                .order("id")
                .execute()
                .data
            )
            file_names = tuple(f["filename"] for f in files)
        return DocumentInfo(
            id=doc["id"],
            source_id=doc["source_id"],
            title=doc["title"],
            status=doc["status"],
            posted_on=doc["posted_on"],
            source_url=doc["source_url"],
            versions=tuple(
                VersionInfo(
                    v["version_no"], v["created_at"], v["change_type"], v["summary"], v["priority"]
                )
                for v in versions
            ),
            file_names=file_names,
        )

    def get_version(self, document_id: int, version_no: int | None) -> VersionContent | None:
        query = (
            self._c.table("document_versions")
            .select("id, title, body_text")
            .eq("document_id", document_id)
        )
        if version_no is None:
            query = query.order("version_no", desc=True)
        else:
            query = query.eq("version_no", version_no)
        rows = query.limit(1).execute().data
        if not rows:
            return None
        version = rows[0]
        files = (
            self._c.table("document_version_files")
            .select("filename, size_label, sha256, text_content, hash_only_reason, extract_note")
            .eq("version_id", version["id"])
            .order("id")
            .execute()
            .data
        )
        return VersionContent(
            version["title"],
            version["body_text"],
            tuple(
                FileContent(
                    filename=f["filename"],
                    size_label=f["size_label"] or "",
                    sha256=f["sha256"],
                    text=f["text_content"],
                    hash_only_reason=f["hash_only_reason"],
                    note=f["extract_note"],
                )
                for f in files
            ),
        )

    def list_documents(
        self, source_id: int, title_contains: str | None, limit: int, offset: int
    ) -> list[DocumentBrief]:
        query = (
            self._c.table("documents")
            .select("id, title, status, posted_on, first_seen_at")
            .eq("source_id", source_id)
        )
        if title_contains:
            # '%' and '\' are the pattern's own characters; they are not searchable text.
            clean = title_contains.replace("%", " ").replace("\\", " ").strip()
            if clean:
                query = query.ilike("title", f"%{clean}%")
        rows = query.order("id", desc=True).range(offset, offset + limit - 1).execute().data
        return [
            DocumentBrief(r["id"], r["title"], r["status"], r["posted_on"], r["first_seen_at"])
            for r in rows
        ]

    def changes(self, source_ids: list[int], since: datetime, limit: int) -> list[ChangeInfo]:
        rows = (
            self._c.table("audit_logs")
            .select("created_at, event_type, source_id, document_id, title, summary, priority")
            .eq("category", "document")
            .in_("source_id", source_ids)
            .gte("created_at", since.isoformat())
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        return [
            ChangeInfo(
                r["created_at"],
                r["event_type"],
                r["source_id"],
                r["document_id"],
                r["title"],
                r["summary"],
                r["priority"],
            )
            for r in rows
        ]
