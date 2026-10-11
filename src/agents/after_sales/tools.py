import json
import re
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, Field, field_validator

ORDERS_FILE = Path(__file__).resolve().parents[3] / "data" / "orders.json"
WORK_ORDERS_FILE = Path(__file__).resolve().parents[3] / "data" / "work_orders.json"
ORDER_ID_PATTERN = re.compile(r"^ORD-\d{6}$")


class OrderQueryInput(BaseModel):
    order_id: str = Field(description="订单号，格式为 ORD- 后跟 6 位数字")

    @field_validator("order_id", mode="before")
    @classmethod
    def normalize_and_validate_order_id(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("订单号必须是字符串")

        normalized = value.strip().upper()
        if not ORDER_ID_PATTERN.fullmatch(normalized):
            raise ValueError("订单号格式错误，应为 ORD- 后跟 6 位数字")
        return normalized


class WorkOrderCreateInput(OrderQueryInput):
    issue_type: Literal["return", "refund", "repair", "other"] = Field(
        description="售后问题类型：return、refund、repair 或 other"
    )
    description: str = Field(min_length=5, max_length=500, description="售后问题描述")
    confirmed: bool = Field(description="用户是否已经明确确认创建工单")

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("问题描述必须是字符串")
        return value.strip()


def load_orders() -> dict[str, dict[str, Any]]:
    try:
        with ORDERS_FILE.open(encoding="utf-8") as orders_file:
            orders = json.load(orders_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolException("订单数据暂时不可用，请稍后再试") from exc

    return {order["order_id"]: order for order in orders}


def load_work_orders() -> list[dict[str, Any]]:
    if not WORK_ORDERS_FILE.exists():
        return []

    try:
        with WORK_ORDERS_FILE.open(encoding="utf-8") as work_orders_file:
            work_orders = json.load(work_orders_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolException("工单数据暂时不可用，请稍后再试") from exc

    if not isinstance(work_orders, list):
        raise ToolException("工单数据格式错误，请联系管理员")
    return work_orders


def save_work_orders(work_orders: list[dict[str, Any]]) -> None:
    try:
        WORK_ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        WORK_ORDERS_FILE.write_text(
            json.dumps(work_orders, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ToolException("工单保存失败，请稍后再试") from exc


@tool("query_order", args_schema=OrderQueryInput)
def query_order(order_id: str) -> dict[str, Any]:
    """按订单号查询售后处理所需的订单详情。仅在用户提供订单号后调用。"""
    order = load_orders().get(order_id)
    if order is None:
        raise ToolException(f"未找到订单 {order_id}，请核对订单号")
    return order


@tool("create_work_order", args_schema=WorkOrderCreateInput)
def create_work_order(
    order_id: str,
    issue_type: Literal["return", "refund", "repair", "other"],
    description: str,
    confirmed: bool,
) -> dict[str, Any]:
    """为已存在的订单创建售后工单。仅在用户明确确认后调用。"""
    raise ToolException("旧工单写入已关闭；请等待受控审批与执行流程交付")


business_tools: list[BaseTool] = [query_order]
