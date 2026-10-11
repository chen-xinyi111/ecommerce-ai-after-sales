"""Add versioned preview policies separately; never alter the 1.1 business seed."""

from datetime import date

from psycopg.types.json import Jsonb

from commerce.db import connect_sync
from commerce.seed import BUSINESS_DATE

POLICY_RULES = {
    "refund": {
        "window_days": 7,
        "reasons": ["quality", "wrong_item", "changed_mind", "not_received"],
        "statuses": ["paid", "shipped", "delivered", "partially_refunded"],
    },
    "exchange": {
        "window_days": 15,
        "reasons": ["quality", "wrong_item"],
        "statuses": ["delivered", "partially_refunded"],
    },
    "reship": {
        "window_days": 30,
        "reasons": ["not_received"],
        "statuses": ["shipped", "delivered", "partially_refunded"],
    },
    "repair": {
        "window_days": 365,
        "reasons": ["quality"],
        "statuses": ["delivered", "partially_refunded"],
    },
}
LABELS = {"refund": "退款", "exchange": "换货", "reship": "补发", "repair": "维修"}


def seed_preview_policies():
    with connect_sync() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(1101001)")
        marker = conn.execute("SELECT * FROM commerce.demo_marker WHERE id=1").fetchone()
        if (
            not marker
            or marker["marker"] != "agent-commerce-demo-v1"
            or marker["business_date"] != BUSINESS_DATE
        ):
            raise RuntimeError("Preview seed requires the marked demonstration business database")
        existing = conn.execute("SELECT 1 FROM commerce.preview_seed_marker WHERE id=1").fetchone()
        if existing:
            return {"imported": False, "policy_version": 3}
        for kind, rules in POLICY_RULES.items():
            conditions = rules | {
                "simulated": True,
                "window_basis": "purchased_on",
                "received_required": True,
                "deduct_all_completed_after_sales": True,
                "stock_required": kind in ("exchange", "reship"),
                "requires_lost_shipment": kind == "reship",
            }
            explanation = (
                f"模拟{LABELS[kind]}政策 v3：以购买日计算期限 {rules['window_days']} 天（含边界）；"
                "数量不超过购买数量减已完成历史售后数量；必须报告是否收货和问题原因。"
            )
            if kind == "refund":
                explanation += "按所选商品单价乘申请数量估算退款，累计退款不得超过实付。未收货时原因应为未收到货。"
            if kind in ("exchange", "repair"):
                explanation += "需已收货，订单状态为已签收或部分退款；维修仅支持质量问题。"
            if kind in ("exchange", "reship"):
                explanation += "对应 SKU 可用库存必须覆盖申请数量；预览不预留库存。"
            if kind == "reship":
                explanation += "需未收货、原因为未收到货且物流记录已确认丢失。"
            conn.execute(
                "INSERT INTO commerce.policies(id,version,kind,valid_from,valid_until,conditions,explanation) "
                "VALUES (%s,3,%s,%s,%s,%s,%s)",
                (
                    f"POL-{kind.upper()}",
                    kind,
                    date(2026, 1, 1),
                    date(2026, 12, 31),
                    Jsonb(conditions),
                    explanation,
                ),
            )
        conn.execute("INSERT INTO commerce.preview_seed_marker VALUES (1,1301)")
        return {"imported": True, "policy_version": 3}


if __name__ == "__main__":
    print(seed_preview_policies())
