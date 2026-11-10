"""The assistant's chat service: one user's conversations, limits, and the answering loop.

Rules (owner decisions 2026-11-09, see docs/decisions.md):
  * Each user has their own conversations; ownership is checked on every call and another
    user's conversation answers "not found".
  * Messages per day are limited, with one number for users and one for admins set on the admin
    page. The count is kept in the audit log (agent.message), written before the model is
    called (fail closed), so deleting a chat never resets it. 0 switches the assistant off.
  * The model sees only the tools of agent/tools.py, which are read-only and check the user's
    access. Sources with AI switched off are invisible to it.
  * Conversations are deleted 30 days after their last message (purge_old, run daily).
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.agent.data import AgentData
from irb_watch.agent.prompt import build_instructions
from irb_watch.agent.tools import ToolBox
from irb_watch.agent.types import AgentModel, Event, StepEnd, TextDelta
from irb_watch.services.errors import (
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.users import AuditSink, UserStore, active_profile

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 2_000
MAX_MESSAGES_PER_CONVERSATION = 50
HISTORY_MESSAGES = 20  # the newest messages sent to the model
MAX_STEPS = 6  # model calls per answer; the last one gets no tools
MAX_ANSWER_CHARS = 8_000
TITLE_CHARS = 60
RETENTION_DAYS = 30
MAX_LIMIT = 1_000


@dataclass(frozen=True)
class Conversation:
    id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChatMessage:
    id: int
    conversation_id: str
    role: str  # user | assistant
    content: str
    actions: list[dict] = field(default_factory=list)
    created_at: str = ""


@dataclass(frozen=True)
class Limits:
    user_daily_limit: int
    admin_daily_limit: int


class AgentStore(Protocol):
    def create_conversation(self, user_id: str, now: datetime) -> Conversation: ...
    def get_conversation(self, conversation_id: str) -> Conversation | None: ...
    def list_conversations(self, user_id: str, limit: int) -> list[Conversation]: ...
    def delete_conversation(self, conversation_id: str) -> None: ...
    def set_title(self, conversation_id: str, title: str) -> None: ...
    def list_messages(self, conversation_id: str, limit: int | None) -> list[ChatMessage]: ...
    def count_messages(self, conversation_id: str) -> int: ...
    def add_message(
        self,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        actions: list[dict],
        now: datetime,
    ) -> ChatMessage: ...
    def get_limits(self) -> Limits: ...
    def set_limits(self, limits: Limits, updated_by: str, now: datetime) -> None: ...
    def count_messages_since(self, user_id: str, since: datetime) -> int: ...
    def purge_before(self, cutoff: datetime) -> int: ...


class AnswerError(Exception):
    """A failure whose message is written for the user and is safe to show."""


def _title_from(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line[:TITLE_CHARS]


class Turn:
    """One user message that has been accepted. events() runs the answer and streams it.

    Everything that can be refused (limits, ownership, size) was checked before a Turn exists, so
    those refusals are normal HTTP errors; problems after streaming starts arrive as an error
    event with a reason, never as a silent stop.
    """

    def __init__(self, run: Callable[[], Iterator[Event]]) -> None:
        self._run = run

    def events(self) -> Iterator[Event]:
        return self._run()


class AgentService:
    def __init__(
        self,
        users: UserStore,
        store: AgentStore,
        data: AgentData,
        model: AgentModel | None,
        audit: AuditSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        extra_instructions: Callable[[], str] = lambda: "",
    ) -> None:
        self._users = users
        self._store = store
        self._data = data
        self._model = model
        self._audit = audit
        self._clock = clock
        self._extra = extra_instructions  # the admins' added instructions, read per answer

    @property
    def enabled(self) -> bool:
        return self._model is not None

    # -- limits -------------------------------------------------------------------

    def _limit_for(self, role: str) -> int:
        limits = self._store.get_limits()
        return limits.admin_daily_limit if role == "admin" else limits.user_daily_limit

    def status(self, user_id: str) -> dict:
        profile = active_profile(self._users, user_id, "this account cannot use the assistant")
        limit = self._limit_for(profile.role)
        used = self._store.count_messages_since(user_id, self._clock() - timedelta(days=1))
        return {
            "enabled": self.enabled and limit > 0,
            "used": used,
            "limit": limit,
            "reason": (
                None
                if self.enabled and limit > 0
                else "the assistant is not set up on this server"
                if not self.enabled
                else "the assistant is switched off for your account type"
            ),
        }

    def get_limits(self) -> Limits:
        return self._store.get_limits()

    def set_limits(self, admin_id: str, user_limit: int, admin_limit: int) -> Limits:
        for name, value in (("user", user_limit), ("admin", admin_limit)):
            if not 0 <= value <= MAX_LIMIT:
                raise ValidationError(f"the {name} limit must be between 0 and {MAX_LIMIT}")
        profile = active_profile(self._users, admin_id, "this account cannot change settings")
        if profile.role != "admin":
            raise ForbiddenError("admin access required")
        old = self._store.get_limits()
        new = Limits(user_limit, admin_limit)
        # Logged first: if the log cannot be written, nothing changes.
        self._audit.record_required(
            "admin",
            "agent.settings_changed",
            admin_id,
            payload={
                "old": {"user": old.user_daily_limit, "admin": old.admin_daily_limit},
                "new": {"user": new.user_daily_limit, "admin": new.admin_daily_limit},
            },
        )
        self._store.set_limits(new, admin_id, self._clock())
        return new

    # -- conversations ------------------------------------------------------------

    def _own(self, user_id: str, conversation_id: str) -> Conversation:
        conv = self._store.get_conversation(conversation_id)
        if conv is None or conv.user_id != user_id:
            raise NotFoundError("conversation not found")
        return conv

    def create_conversation(self, user_id: str) -> Conversation:
        active_profile(self._users, user_id, "this account cannot use the assistant")
        return self._store.create_conversation(user_id, self._clock())

    def list_conversations(self, user_id: str) -> list[Conversation]:
        active_profile(self._users, user_id, "this account cannot use the assistant")
        return self._store.list_conversations(user_id, 100)

    def messages(self, user_id: str, conversation_id: str) -> list[ChatMessage]:
        active_profile(self._users, user_id, "this account cannot use the assistant")
        self._own(user_id, conversation_id)
        return self._store.list_messages(conversation_id, None)

    def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        active_profile(self._users, user_id, "this account cannot use the assistant")
        self._own(user_id, conversation_id)
        self._store.delete_conversation(conversation_id)

    def purge_old(self, days: int = RETENTION_DAYS) -> int:
        """Delete conversations whose last message is older than `days`. Never raises: it runs
        from the scheduler."""
        try:
            count = self._store.purge_before(self._clock() - timedelta(days=days))
            if count:
                self._audit.record(
                    "admin", "agent.purged", None, payload={"conversations": count, "days": days}
                )
            return count
        except Exception:
            log.exception("could not delete old assistant conversations")
            return 0

    # -- one message --------------------------------------------------------------

    def start_turn(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        page_kind: str | None = None,
        page_id: int | None = None,
    ) -> Turn:
        profile = active_profile(self._users, user_id, "this account cannot use the assistant")
        text = (content or "").strip()
        if not text:
            raise ValidationError("the message is empty")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValidationError(f"the message is too long (at most {MAX_MESSAGE_CHARS} characters)")
        if self._model is None:
            raise UnavailableError("the assistant is not set up on this server")
        conv = self._own(user_id, conversation_id)
        if self._store.count_messages(conversation_id) >= MAX_MESSAGES_PER_CONVERSATION:
            raise ValidationError("this conversation is full; start a new chat")

        now = self._clock()
        limit = self._limit_for(profile.role)
        if limit <= 0:
            raise ForbiddenError("the assistant is switched off for your account type")
        used = self._store.count_messages_since(user_id, now - timedelta(days=1))
        if used >= limit:
            raise TooManyRequestsError(
                f"you have used your {limit} assistant messages for the last 24 hours"
            )

        # The log is also the counter: no entry, no model call.
        self._audit.record_required(
            "user", "agent.message", user_id, payload={"conversation_id": conversation_id}
        )
        history = self._store.list_messages(conversation_id, HISTORY_MESSAGES)
        self._store.add_message(conversation_id, user_id, "user", text, [], now)
        if not conv.title:
            self._store.set_title(conversation_id, _title_from(text))

        is_admin = profile.role == "admin"
        hint_kind = page_kind if page_kind in ("source", "document") else None
        hint_id = page_id if isinstance(page_id, int) and not isinstance(page_id, bool) else None

        def run() -> Iterator[Event]:
            return self._answer(user_id, conversation_id, is_admin, history, text, hint_kind, hint_id)

        return Turn(run)

    def _read_extra(self) -> str:
        try:
            return self._extra() or ""
        except Exception as exc:  # noqa: BLE001  the fixed rules still apply without it
            log.warning("could not read the admins' assistant instructions: %s", type(exc).__name__)
            return ""

    def _steps(
        self,
        user_id: str,
        is_admin: bool,
        instructions: str,
        items: list[dict],
        answer: list[str],
        actions: list[dict],
    ) -> Iterator[Event]:
        """The model/tool loop. Appends the answer's text and any action cards to the lists."""
        assert self._model is not None
        toolbox = ToolBox(self._data, user_id, is_admin, self._clock)
        for step in range(MAX_STEPS):
            tools = toolbox.specs() if step < MAX_STEPS - 1 else []
            end: StepEnd | None = None
            for event in self._model.step(instructions, items, tools):
                if isinstance(event, TextDelta):
                    answer.append(event.text)
                    yield Event("text", {"delta": event.text})
                elif isinstance(event, StepEnd):
                    end = event
            if end is None:
                raise AnswerError("the assistant's model stopped without finishing")
            items.extend(end.items)
            if not end.calls:
                break
            for call in end.calls:
                yield Event("tool", {"name": call.name})
                output, action = toolbox.run(call.name, call.arguments)
                if action is not None:
                    actions.append(action)
                    yield Event("action", action)
                items.append(
                    {"type": "function_call_output", "call_id": call.call_id, "output": output}
                )

    def check_preview(self, question: str) -> str:
        """Everything a test run can be refused for, checked before the run is counted."""
        if self._model is None:
            raise UnavailableError("the assistant is not set up on this server")
        text = (question or "").strip()
        if not text:
            raise ValidationError("type a question to test with")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValidationError(f"the question is too long (at most {MAX_MESSAGE_CHARS} characters)")
        return text

    def preview(
        self, admin_id: str, extra: str, document_id: int | None, question: str
    ) -> tuple[str, list[str]]:
        """Admin test run with UNSAVED instructions: one question, nothing saved or counted
        (the caller limits and logs test runs). Tools behave as for this admin, so sources with
        AI off stay invisible. Returns (answer, names of the tools used)."""
        text = self.check_preview(question)
        instructions = build_instructions(
            is_admin=True,
            now=self._clock(),
            page_kind="document" if document_id else None,
            page_id=document_id,
            extra=extra,
        )
        answer: list[str] = []
        actions: list[dict] = []
        items: list[dict] = [{"role": "user", "content": text}]
        try:
            tools = [
                e.data["name"]
                for e in self._steps(admin_id, True, instructions, items, answer, actions)
                if e.kind == "tool"
            ]
        except Exception as exc:  # shown to the admin with a reason
            reason = str(exc) if isinstance(exc, AnswerError) else type(exc).__name__
            raise UpstreamError(f"the test run failed ({reason})") from exc
        final = "".join(answer).strip()
        if not final:
            raise UpstreamError("the test run failed (the assistant gave no answer)")
        return final, tools

    def _answer(
        self,
        user_id: str,
        conversation_id: str,
        is_admin: bool,
        history: list[ChatMessage],
        text: str,
        page_kind: str | None,
        page_id: int | None,
    ) -> Iterator[Event]:
        assert self._model is not None
        instructions = build_instructions(
            is_admin=is_admin,
            now=self._clock(),
            page_kind=page_kind,
            page_id=page_id,
            extra=self._read_extra(),
        )
        items: list[dict] = [{"role": m.role, "content": m.content} for m in history]
        items.append({"role": "user", "content": text})
        answer: list[str] = []
        actions: list[dict] = []
        try:
            yield from self._steps(user_id, is_admin, instructions, items, answer, actions)
            final = "".join(answer).strip()
            if not final:
                raise AnswerError("the assistant gave no answer; please ask again")
        except Exception as exc:  # noqa: BLE001  any failure becomes an error event with a reason
            log.warning("assistant answer failed: %s", type(exc).__name__)
            # Our own messages are safe to show; for anything else only the error's type is.
            reason = str(exc) if isinstance(exc, AnswerError) else (
                f"the assistant failed ({type(exc).__name__})"
            )
            yield Event("error", {"message": reason})
            return

        try:
            saved = self._store.add_message(
                conversation_id,
                user_id,
                "assistant",
                final[:MAX_ANSWER_CHARS],
                actions,
                self._clock(),
            )
        except Exception as exc:  # noqa: BLE001  reported to the user, never silent
            log.warning("could not save the assistant answer: %s", type(exc).__name__)
            yield Event(
                "error", {"message": "the answer was shown but could not be saved to the history"}
            )
            return
        yield Event("done", {"message_id": saved.id})
