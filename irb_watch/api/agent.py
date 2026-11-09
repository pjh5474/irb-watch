"""The assistant's API: a user's own conversations and a streamed answer.

POST /agent/conversations/{id}/messages answers with server-sent events:
  event: text    data: {"delta": "..."}            a piece of the answer
  event: tool    data: {"name": "get_document"}    the assistant is looking something up
  event: action  data: {...}                       admins: a proposal that waits for Confirm
  event: done    data: {"message_id": 12}          finished and saved
  event: error   data: {"message": "..."}          failed, with the reason
Refusals that happen before streaming starts (limit, size, ownership) are normal HTTP errors.
"""

import json
from collections.abc import Iterator
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from irb_watch.agent.types import Event
from irb_watch.api.deps import current_user, get_agent_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.agent import AgentService, ChatMessage, Conversation

router = APIRouter(prefix="/agent", tags=["agent"])


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, c: Conversation) -> "ConversationOut":
        return cls(id=c.id, title=c.title, created_at=c.created_at, updated_at=c.updated_at)


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    actions: list[dict]
    created_at: str

    @classmethod
    def of(cls, m: ChatMessage) -> "MessageOut":
        return cls(
            id=m.id, role=m.role, content=m.content, actions=m.actions, created_at=m.created_at
        )


class StatusOut(BaseModel):
    enabled: bool
    used: int
    limit: int
    reason: str | None = None


class PageIn(BaseModel):
    kind: Literal["source", "document", "other"]
    id: int | None = None


class MessageIn(BaseModel):
    content: str = Field(max_length=20_000)  # the real limit is checked by the service
    page: PageIn | None = None


@router.get("/status", response_model=StatusOut)
def status(
    user: AuthUser = Depends(current_user), service: AgentService = Depends(get_agent_service)
) -> StatusOut:
    return StatusOut(**service.status(user.id))


@router.post("/conversations", status_code=201, response_model=ConversationOut)
def create_conversation(
    user: AuthUser = Depends(current_user), service: AgentService = Depends(get_agent_service)
) -> ConversationOut:
    return ConversationOut.of(service.create_conversation(user.id))


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    user: AuthUser = Depends(current_user), service: AgentService = Depends(get_agent_service)
) -> list[ConversationOut]:
    return [ConversationOut.of(c) for c in service.list_conversations(user.id)]


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(
    conversation_id: UUID,
    user: AuthUser = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> list[MessageOut]:
    return [MessageOut.of(m) for m in service.messages(user.id, str(conversation_id))]


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: UUID,
    user: AuthUser = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> None:
    service.delete_conversation(user.id, str(conversation_id))


def _sse(events: Iterator[Event]) -> Iterator[str]:
    for event in events:
        yield f"event: {event.kind}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


@router.post("/conversations/{conversation_id}/messages")
def send_message(
    conversation_id: UUID,
    body: MessageIn,
    user: AuthUser = Depends(current_user),
    service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    page = body.page
    # Everything that can be refused is refused here, before the stream starts.
    turn = service.start_turn(
        user.id,
        str(conversation_id),
        body.content,
        page.kind if page else None,
        page.id if page else None,
    )
    return StreamingResponse(
        _sse(turn.events()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
