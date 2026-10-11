from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from commerce.approval_models import SnapshotInput

ExecutionStatus = Literal["queued", "running", "completed", "failed", "stopped", "manual_review"]


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    application_id: UUID


class ActionRequest(ExecutionRequest):
    kind: Literal["refund", "exchange", "reship", "repair"]


class RetryInput(SnapshotInput):
    note: str = Field(min_length=1, max_length=500)


class ExecutionStep(BaseModel):
    application_id: UUID
    work_order_id: str | None = None
    status: Literal["ready", "reserved", "completed", "stopped"]
    kind: Literal["refund", "exchange", "reship", "repair"] | None = None
    refund_cents: int = Field(default=0, ge=0)
    shipment_tracking: str | None = None
    repair_status: Literal["awaiting_inspection"] | None = None
    label: str
    simulated: Literal[True] = True


class ExecutionJob(BaseModel):
    id: UUID
    application_id: UUID
    requested_by: str
    status: ExecutionStatus
    attempts: int
    error_code: str | None
    result: ExecutionStep | None
    created_at: datetime
    updated_at: datetime


class WorkOrder(BaseModel):
    id: str
    application_id: UUID
    job_id: UUID
    order_id: str
    order_item_id: str
    kind: Literal["refund", "exchange", "reship", "repair"]
    quantity: int
    refund_cents: int
    policy_id: str
    policy_version: int
    status: Literal["ready", "reserved", "completed", "cancelled"]
    created_at: datetime
    updated_at: datetime


class Reservation(BaseModel):
    application_id: UUID
    work_order_id: str
    sku_id: str
    quantity: int
    status: Literal["held", "consumed", "released"]
    updated_at: datetime


class RefundEntry(BaseModel):
    application_id: UUID
    idempotency_key: str
    payment_id: str
    amount_cents: int
    created_at: datetime
    simulated: Literal[True]


class Dispatch(BaseModel):
    application_id: UUID
    idempotency_key: str
    work_order_id: str
    sku_id: str
    quantity: int
    tracking_number: str
    status: Literal["dispatched"]
    created_at: datetime
    simulated: Literal[True]


class RepairIntake(BaseModel):
    application_id: UUID
    work_order_id: str
    status: Literal["awaiting_inspection"]
    created_at: datetime
    simulated: Literal[True]


class ExecutionDetail(BaseModel):
    job: ExecutionJob | None
    work_order: WorkOrder | None
    reservation: Reservation | None
    refund: RefundEntry | None
    shipment: Dispatch | None
    repair: RepairIntake | None
