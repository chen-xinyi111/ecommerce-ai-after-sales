from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from commerce.preview_models import StoredPreview


class ThreadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=r"^ORD-\d{6}$")


class ChatThread(BaseModel):
    id: UUID
    order_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ThreadPage(BaseModel):
    items: list[ChatThread]
    total: int


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=2000)
    request_id: UUID


class ChatRun(BaseModel):
    id: UUID
    thread_id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    model: str
    error_code: str | None
    usage: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None


class ChatMessage(BaseModel):
    id: int
    run_id: UUID
    role: Literal["user", "assistant"]
    content: str
    status: Literal["complete", "partial", "failed"]
    created_at: datetime
    preview: StoredPreview | None = None


class ChatEvent(BaseModel):
    seq: int
    kind: Literal["status", "token", "tool", "preview", "route", "skill", "agent", "handoff", "done", "error"]
    payload: dict[str, Any]


class ThreadHistory(BaseModel):
    thread: ChatThread
    messages: list[ChatMessage]
    runs: list[ChatRun]


class RunEvents(BaseModel):
    run: ChatRun
    events: list[ChatEvent]
