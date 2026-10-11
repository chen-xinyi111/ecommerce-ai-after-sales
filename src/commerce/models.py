from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class Principal(BaseModel):
    id: str
    username: str
    display_name: str
    role: Literal["user", "staff"]


class OrderSummary(BaseModel):
    id: str
    user_id: str
    status: str
    paid_cents: int
    refunded_cents: int
    purchased_on: date
    simulated: bool = True


class OrderItem(BaseModel):
    id: str
    sku_id: str
    product_name: str
    quantity: int
    unit_price_cents: int
    after_sales_quantity: int


class Payment(BaseModel):
    id: str
    paid_cents: int
    refunded_cents: int
    status: str


class AfterSalesHistory(BaseModel):
    id: str
    order_item_id: str
    kind: str
    quantity: int
    refund_cents: int
    status: str


class OrderDetail(OrderSummary):
    items: list[OrderItem]
    payments: list[Payment]
    after_sales_history: list[AfterSalesHistory]


class OrderPage(BaseModel):
    items: list[OrderSummary]
    total: int
    offset: int
    limit: int


class LogisticsEvent(BaseModel):
    occurred_at: datetime
    status: str
    description: str


class Logistics(BaseModel):
    shipment_id: str
    order_id: str
    tracking_number: str
    status: str
    events: list[LogisticsEvent]
    simulated: bool = True


class Inventory(BaseModel):
    sku_id: str
    on_hand: int
    reserved: int
    available: int
    simulated: bool = True


class InventoryPage(BaseModel):
    items: list[Inventory]


class Health(BaseModel):
    status: str
    database: str
    business_date: date
    simulated: bool = True
