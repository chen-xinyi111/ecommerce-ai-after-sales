from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from commerce.execution_models import ActionRequest, ExecutionRequest, ExecutionStep
from commerce.models import AfterSalesHistory, Inventory, Logistics, OrderDetail
from commerce.preview_models import AfterSalesKind, PolicyReference, PreviewDraft, PreviewResult


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=r"^ORD-\d{6}$")


class InventoryRequest(OrderRequest):
    sku_id: str = Field(pattern=r"^SKU-\d{3}$")


class PolicyRequest(OrderRequest):
    kind: AfterSalesKind


class PreviewRequest(OrderRequest):
    draft: PreviewDraft


class ShipmentResult(BaseModel):
    order_id: str
    shipment: Logistics | None
    simulated: Literal[True] = True


class HistoryResult(BaseModel):
    order_id: str
    items: list[AfterSalesHistory]
    simulated: Literal[True] = True


class PolicyResult(BaseModel):
    order_id: str
    policy: PolicyReference | None
    simulated: Literal[True] = True


INPUTS = {
    "query_order": OrderRequest,
    "query_logistics": OrderRequest,
    "query_inventory": InventoryRequest,
    "query_policy": PolicyRequest,
    "query_after_sales_history": OrderRequest,
    "preview_after_sales": PreviewRequest,
    "create_work_order": ExecutionRequest,
    "execute_after_sales": ActionRequest,
}
OUTPUTS = {
    "query_order": OrderDetail,
    "query_logistics": ShipmentResult,
    "query_inventory": Inventory,
    "query_policy": PolicyResult,
    "query_after_sales_history": HistoryResult,
    "preview_after_sales": PreviewResult,
    "create_work_order": ExecutionStep,
    "execute_after_sales": ExecutionStep,
}

WRITE_TOOLS = frozenset({"create_work_order", "execute_after_sales"})
READ_TOOLS = frozenset(INPUTS) - WRITE_TOOLS
