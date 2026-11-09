"""In-memory stand-ins for the assistant's store, data and model."""

import itertools
from datetime import datetime, timedelta

from irb_watch.agent.data import (
    ChangeInfo,
    DocumentBrief,
    DocumentInfo,
    SourceInfo,
    VersionInfo,
)
from irb_watch.agent.types import StepEnd, TextDelta, ToolCall
from irb_watch.core.state import FileContent, VersionContent
from irb_watch.services.agent import ChatMessage, Conversation, Limits

NOW = datetime(2026, 11, 10, 9, 0)


class FakeAgentStore:
    def __init__(self, audit=None) -> None:
        self.conversations: dict[str, Conversation] = {}
        self.messages: dict[str, list[ChatMessage]] = {}
        self.limits = Limits(30, 100)
        self.prior_used = 0  # messages used earlier in the window, on top of the audit entries
        self.audit = audit
        self.purged_before: datetime | None = None
        self.purge_result = 0
        self.fail_add_assistant = False
        self._ids = itertools.count(1)

    def create_conversation(self, user_id, now):
        cid = f"00000000-0000-0000-0000-{next(self._ids):012d}"
        conv = Conversation(cid, user_id, "", now.isoformat(), now.isoformat())
        self.conversations[cid] = conv
        self.messages[cid] = []
        return conv

    def get_conversation(self, conversation_id):
        return self.conversations.get(conversation_id)

    def list_conversations(self, user_id, limit):
        rows = [c for c in self.conversations.values() if c.user_id == user_id]
        return sorted(rows, key=lambda c: c.updated_at, reverse=True)[:limit]

    def delete_conversation(self, conversation_id):
        self.conversations.pop(conversation_id, None)
        self.messages.pop(conversation_id, None)

    def set_title(self, conversation_id, title):
        c = self.conversations[conversation_id]
        self.conversations[conversation_id] = Conversation(
            c.id, c.user_id, title, c.created_at, c.updated_at
        )

    def list_messages(self, conversation_id, limit):
        rows = list(self.messages.get(conversation_id, []))
        return rows if limit is None else rows[-limit:]

    def count_messages(self, conversation_id):
        return len(self.messages.get(conversation_id, []))

    def add_message(self, conversation_id, user_id, role, content, actions, now):
        if role == "assistant" and self.fail_add_assistant:
            raise RuntimeError("database down")
        msg = ChatMessage(
            len(self.messages[conversation_id]) + 1,
            conversation_id,
            role,
            content,
            list(actions),
            now.isoformat(),
        )
        self.messages[conversation_id].append(msg)
        return msg

    def get_limits(self):
        return self.limits

    def set_limits(self, limits, updated_by, now):
        self.limits = limits

    def count_messages_since(self, user_id, since):
        logged = 0
        if self.audit is not None:
            logged = sum(
                1
                for e in self.audit.entries
                if e["event_type"] == "agent.message" and e["actor_id"] == user_id
            )
        return self.prior_used + logged

    def purge_before(self, cutoff):
        self.purged_before = cutoff
        return self.purge_result


class FakeAgentData:
    """sources: id -> (info, ai_enabled, members). Admins read every AI-enabled source."""

    def __init__(self) -> None:
        self.sources: dict[int, tuple[SourceInfo, bool, set[str]]] = {}
        self.documents: dict[int, DocumentInfo] = {}
        self.versions: dict[tuple[int, int], VersionContent] = {}
        self.changes_rows: list[ChangeInfo] = []
        self.calls: list[tuple] = []

    def add_source(self, source_id, name="Board", requires_login=False, ai_enabled=True, members=()):
        info = SourceInfo(source_id, name, True, "2026-11-09T06:00:00+00:00", "ok", requires_login)
        self.sources[source_id] = (info, ai_enabled, set(members))

    def add_document(self, doc_id, source_id, title="Form A", versions=2, files=("a.pdf",)):
        vinfos = tuple(
            VersionInfo(n, f"2026-11-0{n}T00:00:00+00:00", "baseline" if n == 1 else "updated",
                        None if n == 1 else f"v{n} summary", None if n == 1 else "medium")
            for n in range(1, versions + 1)
        )
        self.documents[doc_id] = DocumentInfo(
            doc_id, source_id, title, "current", "2026-11-01", f"https://site/{doc_id}", vinfos,
            tuple(files),
        )
        for n in range(1, versions + 1):
            self.versions[(doc_id, n)] = VersionContent(
                title,
                f"page text v{n}",
                (FileContent("a.pdf", "1 KB", "h" * 64, f"file text v{n}", None, None, None),),
            )

    def visible_sources(self, user_id, is_admin):
        self.calls.append(("visible_sources", user_id, is_admin))
        out = []
        for info, ai_enabled, members in self.sources.values():
            if not ai_enabled:
                continue
            if is_admin or not info.requires_login or user_id in members:
                out.append(info)
        return out

    def get_source(self, source_id):
        row = self.sources.get(source_id)
        return row[0] if row else None

    def get_document(self, document_id):
        return self.documents.get(document_id)

    def get_version(self, document_id, version_no):
        if version_no is None:
            numbers = [n for (d, n) in self.versions if d == document_id]
            if not numbers:
                return None
            version_no = max(numbers)
        return self.versions.get((document_id, version_no))

    def list_documents(self, source_id, title_contains, limit, offset):
        rows = [
            DocumentBrief(d.id, d.title, d.status, d.posted_on, "2026-11-01T00:00:00+00:00")
            for d in self.documents.values()
            if d.source_id == source_id and (not title_contains or title_contains in d.title)
        ]
        return rows[offset : offset + limit]

    def changes(self, source_ids, since, limit):
        self.calls.append(("changes", tuple(source_ids), since))
        return [c for c in self.changes_rows if c.source_id in source_ids][:limit]


class FakeAgentModel:
    """Plays scripted steps. Each step is a list of TextDelta / ToolCall pieces; the StepEnd is
    built from them. A step that is an Exception is raised."""

    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.calls: list[dict] = []

    def step(self, instructions, items, tools):
        self.calls.append(
            {"instructions": instructions, "items": [dict(i) for i in items], "tools": tools}
        )
        scripted = self.steps.pop(0) if self.steps else [TextDelta("ok")]
        if isinstance(scripted, Exception):
            raise scripted
        calls = []
        out = []
        for piece in scripted:
            if isinstance(piece, TextDelta):
                yield piece
            elif isinstance(piece, ToolCall):
                calls.append(piece)
                out.append(
                    {
                        "type": "function_call",
                        "call_id": piece.call_id,
                        "name": piece.name,
                        "arguments": piece.arguments,
                    }
                )
        text = "".join(p.text for p in scripted if isinstance(p, TextDelta))
        if text:
            out.insert(0, {"role": "assistant", "content": text})
        yield StepEnd(tuple(out), tuple(calls))


def hours_ago(n: int) -> datetime:
    return NOW - timedelta(hours=n)
