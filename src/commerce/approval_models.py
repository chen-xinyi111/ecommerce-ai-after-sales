from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from commerce.preview_models import StoredPreview


class SnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: UUID
    preview_version: StrictInt = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: UUID


class DecisionInput(SnapshotInput):
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=1, max_length=500)


class Application(BaseModel):
    id: UUID
    user_id: str
    order_id: str
    preview_id: UUID
    preview_version: int
    content_sha256: str
    status: Literal[
        "pending_approval",
        "decision_recorded",
        "approved",
        "rejected",
        "cancelled",
        "superseded",
        "conditions_changed",
    ]
    workflow_status: Literal["queued", "running", "waiting", "complete", "failed"]
    resume_requested: bool
    decision: Literal["approved", "rejected"] | None
    decision_actor: str | None
    decision_note: str | None
    decision_at: datetime | None
    confirmed_at: datetime
    updated_at: datetime
    next_event_id: int
    error_code: str | None
    execution_state: Literal[
        "not_started", "queued", "running", "completed", "failed", "stopped", "manual_review"
    ] = "not_started"


class ApprovalEvent(BaseModel):
    seq: int
    kind: str
    actor_id: str | None
    payload: dict
    created_at: datetime


class ApplicationDetail(BaseModel):
    application: Application
    preview: StoredPreview
    events: list[ApprovalEvent]


class ApplicationPage(BaseModel):
    items: list[Application]
    total: int
