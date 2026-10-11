import pytest
from langchain_core.tools import ToolException
from pydantic import ValidationError

from agents.after_sales.tools import create_work_order, query_order


def test_query_order_returns_matching_order():
    result = query_order.invoke({"order_id": "ORD-100001"})

    assert result["order_id"] == "ORD-100001"
    assert result["product_name"] == "智能手环 S1"
    assert result["status"] == "delivered"


def test_query_order_normalizes_whitespace_and_case():
    result = query_order.invoke({"order_id": "  ord-100002  "})

    assert result["order_id"] == "ORD-100002"


@pytest.mark.parametrize("order_id", ["100001", "ORD-1", "ORD-ABCDEF", ""])
def test_query_order_rejects_invalid_order_id(order_id):
    with pytest.raises(ValidationError, match="订单号格式错误"):
        query_order.invoke({"order_id": order_id})


def test_query_order_reports_missing_order():
    with pytest.raises(ToolException, match="未找到订单 ORD-999999"):
        query_order.invoke({"order_id": "ORD-999999"})


@pytest.mark.parametrize("confirmed", [False, True])
def test_legacy_create_work_order_disabled(tmp_path, monkeypatch, confirmed):
    work_orders_file = tmp_path / "work_orders.json"
    monkeypatch.setattr("agents.after_sales.tools.WORK_ORDERS_FILE", work_orders_file)
    with pytest.raises(ToolException, match="写入已关闭"):
        create_work_order.invoke(
            {
                "order_id": "ORD-100001",
                "issue_type": "refund",
                "description": "商品存在质量问题",
                "confirmed": confirmed,
            }
        )
    assert not work_orders_file.exists()
