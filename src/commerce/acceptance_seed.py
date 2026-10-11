"""Add five new marked demo orders; never reset an existing order or inventory."""

import json
from pathlib import Path

from commerce.config import CommerceSettings
from commerce.db import connect_sync
from commerce.seed import BUSINESS_DATE

CASES = Path(__file__).with_name("fixtures") / "acceptance_cases_33.json"


def seed_acceptance():
    config = CommerceSettings()
    if config.POSTGRES_DB not in {"agent_commerce_demo", "agent_commerce_container_demo"}:
        raise RuntimeError("Acceptance orders require a named demonstration database")
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    inserted = []
    with connect_sync() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(330033)")
        marker = conn.execute("SELECT * FROM commerce.demo_marker WHERE id=1").fetchone()
        if not marker or marker["marker"] != "agent-commerce-demo-v1":
            raise RuntimeError("Unmarked database; refusing to add acceptance orders")
        for case in cases:
            oid = case["order_id"]
            existing = conn.execute(
                "SELECT scenario FROM commerce.orders WHERE id=%s", (oid,)
            ).fetchone()
            if existing:
                if existing["scenario"] != "acceptance-3.3":
                    raise RuntimeError(f"Order ID already belongs to another dataset: {oid}")
                continue
            kind = case["input"]["kind"]
            conn.execute(
                "INSERT INTO commerce.orders(id,user_id,status,paid_cents,purchased_on,scenario) "
                "VALUES (%s,'USR-001','delivered',40000,%s,'acceptance-3.3')",
                (oid, BUSINESS_DATE if case["category"] == "normal" else "2026-07-07"),
            )
            conn.execute(
                "INSERT INTO commerce.order_items(id,order_id,sku_id,product_name,quantity,unit_price_cents) "
                "VALUES (%s,%s,'SKU-001','3.3 模拟验收商品',4,10000)",
                (oid + "-I1", oid),
            )
            conn.execute(
                "INSERT INTO commerce.payments(id,order_id,paid_cents,status) VALUES (%s,%s,40000,'paid')",
                ("PAY-" + oid, oid),
            )
            conn.execute(
                "INSERT INTO commerce.shipments(id,order_id,tracking_number,status) VALUES (%s,%s,%s,%s)",
                ("SHP-" + oid, oid, "SIM-33-" + oid, "lost" if kind == "reship" else "delivered"),
            )
            inserted.append(oid)
    return dict(inserted=inserted, preserved_existing=True)


if __name__ == "__main__":
    print(seed_acceptance())
