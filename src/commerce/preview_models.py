from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class AfterSalesKind(StrEnum):
    REFUND = "refund"
    EXCHANGE = "exchange"
    RESHIP = "reship"
    REPAIR = "repair"


class ProblemReason(StrEnum):
    QUALITY = "quality"
    WRONG_ITEM = "wrong_item"
    NOT_RECEIVED = "not_received"
    CHANGED_MIND = "changed_mind"


class PreviewDraft(BaseModel):
    """User-reported application facts only; monetary and policy values are server-derived."""

    model_config = ConfigDict(extra="forbid")
    kind: AfterSalesKind | None = None
    order_item_id: str | None = Field(default=None, max_length=64)
    quantity: StrictInt | None = Field(default=None, ge=1, le=100)
    reason: ProblemReason | None = None
    received: StrictBool | None = None


class ExtractedIntent(BaseModel):
    """Classify a request and extract explicitly reported facts without guessing."""

    preview_requested: bool
    request: PreviewDraft


class PreviewInput(PreviewDraft):
    request_id: UUID


class PolicyReference(BaseModel):
    id: str
    version: int
    kind: AfterSalesKind
    valid_from: date
    valid_until: date
    window_days: int
    window_basis: Literal["purchased_on"] = "purchased_on"
    explanation: str


class RuleCheck(BaseModel):
    code: str
    passed: bool
    explanation: str


class PreviewFacts(BaseModel):
    purchased_on: date
    age_days: int
    order_status: str
    shipment_status: str | None
    paid_cents: int
    historical_refund_cents: int
    remaining_refund_cents: int
    purchased_quantity: int | None = None
    historical_after_sales_quantity: int | None = None
    remaining_quantity: int | None = None
    available_stock: int | None = None
    unit_price_cents: int | None = None


class PreviewResult(BaseModel):
    schema_version: Literal["1.3"] = "1.3"
    order_id: str
    status: Literal["eligible", "needs_info", "ineligible"]
    business_date: date
    request: PreviewDraft
    policy: PolicyReference | None
    facts: PreviewFacts
    checks: list[RuleCheck]
    missing_fields: list[str]
    questions: list[str]
    refund_cents: int | None = Field(default=None, ge=0)
    summary: str
    warnings: list[str]
    execution_state: Literal["not_executed"] = "not_executed"
    simulated: Literal[True] = True


class StoredPreview(PreviewResult):
    id: UUID
    version: int
    content_sha256: str
    created_at: datetime


class PreviewPage(BaseModel):
    items: list[StoredPreview]


class PolicyPage(BaseModel):
    items: list[PolicyReference]
    business_date: date


class DemoCase(BaseModel):
    id: str
    title: str
    category: str
    actor_username: str
    order_id: str
    input: PreviewDraft
    expected_status: str
    expected_code: str | None = None


class DemoCasePage(BaseModel):
    items: list[DemoCase]
    total: int
