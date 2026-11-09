"""The assistant's tools. All read-only, all for one user.

Rules that hold for every tool:
  * The user comes from the login token, never from the model: no tool takes a user id.
  * Access is checked here on the server for every call. A source the user may not read, a
    source with AI switched off and an id that does not exist all answer the same "not found".
  * Document text is untrusted: it is wrapped in <document> tags, size-capped, and the system
    prompt tells the model never to follow instructions inside it.
  * propose_action (admins only) executes nothing. It returns a card; the admin's click in the
    browser runs the normal admin endpoint with the admin's own login.
"""

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from irb_watch.agent.data import AgentData, SourceInfo
from irb_watch.ai.digest import change_digest, document_digest
from irb_watch.core.diff import describe_change

ACTION_KINDS = ("scan_source", "scan_all", "resummarize", "test_mail", "weekly_preview")
TEXT_CHARS = 12_000
NOT_FOUND = "error: not found (it does not exist, or you may not use it)"


class ToolArgError(ValueError):
    """The model sent arguments that do not fit; the message goes back to it so it can retry."""


def _wrap(text: str) -> str:
    safe = text.replace("</document>", "</ document>")
    return f"<document>\n{safe}\n</document>"


def _int(args: dict, key: str, lo: int, hi: int, default: int | None = None) -> int:
    value = args.get(key, default)
    if value is None:
        raise ToolArgError(f"{key} is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgError(f"{key} must be a whole number")
    if not lo <= value <= hi:
        raise ToolArgError(f"{key} must be between {lo} and {hi}")
    return value


def _optional_int(args: dict, key: str, lo: int, hi: int) -> int | None:
    return None if args.get(key) is None else _int(args, key, lo, hi)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_READ_SPECS = [
    {
        "type": "function",
        "name": "list_sources",
        "description": "List the sources (hospital IRB boards) you can use, with their ids.",
        "parameters": _schema({}),
    },
    {
        "type": "function",
        "name": "list_documents",
        "description": (
            "List documents of one source, newest first. Use title_contains to find a "
            "document by a word in its title."
        ),
        "parameters": _schema(
            {
                "source_id": {"type": "integer"},
                "title_contains": {"type": "string", "description": "optional, max 100 chars"},
                "limit": {"type": "integer", "description": "1-50, default 30"},
                "offset": {"type": "integer", "description": "default 0"},
            },
            ["source_id"],
        ),
    },
    {
        "type": "function",
        "name": "get_changes",
        "description": (
            "Recent changes (new, updated, removed documents) with their short summaries, "
            "newest first. Optionally for one source."
        ),
        "parameters": _schema(
            {
                "days": {"type": "integer", "description": "1-60, default 7"},
                "source_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "1-30, default 15"},
            }
        ),
    },
    {
        "type": "function",
        "name": "get_document",
        "description": (
            "Details of one document: title, status, link to the original, its versions with "
            "their change summaries, and the file names of the newest version."
        ),
        "parameters": _schema({"document_id": {"type": "integer"}}, ["document_id"]),
    },
    {
        "type": "function",
        "name": "get_document_text",
        "description": (
            "The text of a document (page text and attached files), shortened if long. "
            "version_no is optional; the default is the newest version."
        ),
        "parameters": _schema(
            {"document_id": {"type": "integer"}, "version_no": {"type": "integer"}},
            ["document_id"],
        ),
    },
    {
        "type": "function",
        "name": "compare_versions",
        "description": "What changed between two versions of a document, word by word.",
        "parameters": _schema(
            {
                "document_id": {"type": "integer"},
                "from_version": {"type": "integer"},
                "to_version": {"type": "integer"},
            },
            ["document_id", "from_version", "to_version"],
        ),
    },
]

_ACTION_SPEC = {
    "type": "function",
    "name": "propose_action",
    "description": (
        "Admins only. Propose an action for the admin to confirm. Nothing happens until the "
        "admin clicks Confirm; never say it is done. kinds: scan_source (needs source_id), "
        "scan_all, resummarize (needs document_id; replaces the saved summary for everyone), "
        "test_mail, weekly_preview (both mail only the admin's own address)."
    ),
    "parameters": _schema(
        {
            "kind": {"type": "string", "enum": list(ACTION_KINDS)},
            "source_id": {"type": "integer"},
            "document_id": {"type": "integer"},
        },
        ["kind"],
    ),
}


class ToolBox:
    def __init__(
        self,
        data: AgentData,
        user_id: str,
        is_admin: bool,
        clock: Callable[[], datetime],
    ) -> None:
        self._data = data
        self._user_id = user_id
        self._is_admin = is_admin
        self._clock = clock
        self._sources: list[SourceInfo] | None = None

    def specs(self) -> list[dict]:
        return [*_READ_SPECS, *([_ACTION_SPEC] if self._is_admin else [])]

    def run(self, name: str, arguments: str) -> tuple[str, dict | None]:
        """Run one tool. Returns (text for the model, an action card or None). Never raises:
        problems come back as 'error: ...' text so the model can answer or retry."""
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ToolArgError("arguments must be a JSON object")
            if name == "propose_action":
                return self._propose(args)
            handler = {
                "list_sources": self._list_sources,
                "list_documents": self._list_documents,
                "get_changes": self._get_changes,
                "get_document": self._get_document,
                "get_document_text": self._get_document_text,
                "compare_versions": self._compare_versions,
            }.get(name)
            if handler is None:
                return f"error: unknown tool {name}", None
            return handler(args), None
        except ToolArgError as exc:
            return f"error: {exc}", None
        except json.JSONDecodeError:
            return "error: the arguments were not valid JSON", None
        except Exception as exc:  # noqa: BLE001  a broken tool must not end the whole answer
            return f"error: the tool failed ({type(exc).__name__})", None

    # -- access -------------------------------------------------------------------

    def _visible(self) -> list[SourceInfo]:
        if self._sources is None:
            self._sources = self._data.visible_sources(self._user_id, self._is_admin)
        return self._sources

    def _visible_ids(self) -> set[int]:
        return {s.id for s in self._visible()}

    def _document(self, document_id: int):
        doc = self._data.get_document(document_id)
        if doc is None or doc.source_id not in self._visible_ids():
            return None
        return doc

    # -- read tools ---------------------------------------------------------------

    def _list_sources(self, args: dict) -> str:
        return _json(
            [
                {
                    "id": s.id,
                    "name": s.name,
                    "members_only": s.requires_login,
                    "is_active": s.is_active,
                    "last_scan_at": s.last_scan_at,
                    "last_scan_status": s.last_scan_status,
                }
                for s in self._visible()
            ]
        )

    def _list_documents(self, args: dict) -> str:
        source_id = _int(args, "source_id", 1, 2**62)
        if source_id not in self._visible_ids():
            return NOT_FOUND
        limit = _int(args, "limit", 1, 50, 30)
        offset = _int(args, "offset", 0, 100_000, 0)
        title = args.get("title_contains")
        if title is not None:
            if not isinstance(title, str):
                raise ToolArgError("title_contains must be text")
            title = title.strip()[:100] or None
        rows = self._data.list_documents(source_id, title, limit, offset)
        return _json(
            [
                {
                    "id": d.id,
                    "title": d.title,
                    "status": d.status,
                    "posted_on": d.posted_on,
                    "first_seen_at": d.first_seen_at,
                }
                for d in rows
            ]
        )

    def _get_changes(self, args: dict) -> str:
        days = _int(args, "days", 1, 60, 7)
        limit = _int(args, "limit", 1, 30, 15)
        source_id = _optional_int(args, "source_id", 1, 2**62)
        visible = self._visible_ids()
        if source_id is not None:
            if source_id not in visible:
                return NOT_FOUND
            ids = [source_id]
        else:
            ids = sorted(visible)
        if not ids:
            return _json([])
        rows = self._data.changes(ids, self._clock() - timedelta(days=days), limit)
        return _json(
            [
                {
                    "when": c.created_at,
                    "event": c.event_type,
                    "source_id": c.source_id,
                    "document_id": c.document_id,
                    "title": c.title,
                    "summary": c.summary,
                    "priority": c.priority,
                }
                for c in rows
            ]
        )

    def _get_document(self, args: dict) -> str:
        doc = self._document(_int(args, "document_id", 1, 2**62))
        if doc is None:
            return NOT_FOUND
        return _json(
            {
                "id": doc.id,
                "source_id": doc.source_id,
                "title": doc.title,
                "status": doc.status,
                "posted_on": doc.posted_on,
                "original_page": doc.source_url,
                "files_in_newest_version": list(doc.file_names),
                "versions": [
                    {
                        "version_no": v.version_no,
                        "created_at": v.created_at,
                        "kind": v.change_type,
                        "change_summary": v.summary,
                        "priority": v.priority,
                    }
                    for v in doc.versions
                ],
            }
        )

    def _get_document_text(self, args: dict) -> str:
        doc = self._document(_int(args, "document_id", 1, 2**62))
        if doc is None:
            return NOT_FOUND
        version_no = _optional_int(args, "version_no", 1, 100_000)
        content = self._data.get_version(doc.id, version_no)
        if content is None:
            return "error: that version does not exist"
        digest = document_digest(content)
        text = digest.text
        cut = digest.truncated or len(text) > TEXT_CHARS
        if len(text) > TEXT_CHARS:
            text = text[:TEXT_CHARS]
        note = "\n(The text was shortened; say so if it matters.)" if cut else ""
        return _wrap(text) + note

    def _compare_versions(self, args: dict) -> str:
        doc = self._document(_int(args, "document_id", 1, 2**62))
        if doc is None:
            return NOT_FOUND
        old = self._data.get_version(doc.id, _int(args, "from_version", 1, 100_000))
        new = self._data.get_version(doc.id, _int(args, "to_version", 1, 100_000))
        if old is None or new is None:
            return "error: that version does not exist"
        digest = change_digest(describe_change(old, new), new)
        text = digest.text
        if not text.strip():
            return "The two versions are identical."
        return _wrap(text[:TEXT_CHARS])

    # -- the one admin tool -------------------------------------------------------

    def _propose(self, args: dict) -> tuple[str, dict | None]:
        if not self._is_admin:
            return "error: unknown tool propose_action", None
        kind = args.get("kind")
        if kind not in ACTION_KINDS:
            raise ToolArgError(f"kind must be one of {', '.join(ACTION_KINDS)}")
        action: dict = {"id": uuid.uuid4().hex[:12], "kind": kind}
        if kind == "scan_source":
            source = self._data.get_source(_int(args, "source_id", 1, 2**62))
            if source is None:
                return NOT_FOUND, None
            action |= {"source_id": source.id, "name": source.name}
        elif kind == "resummarize":
            doc = self._document(_int(args, "document_id", 1, 2**62))
            if doc is None:
                return NOT_FOUND, None
            action |= {"document_id": doc.id, "source_id": doc.source_id, "name": doc.title}
        note = (
            "Proposed to the admin as a Confirm button. Nothing has been done yet; tell the "
            "admin to review it and click Confirm."
        )
        return note, action
