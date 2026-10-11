import random
from datetime import UTC, date, datetime, timedelta

from argon2 import PasswordHasher
from psycopg import sql
from psycopg.types.json import Jsonb

from commerce.db import connect_sync

BUSINESS_DATE = date(2026, 10, 9)
SEED = 1101
DEMO_PASSWORD = "Demo-1101!"
SCENARIOS = ("normal", "expired", "out_of_stock", "lost", "partial_refund", "history")


def insert_many(conn, table: str, columns: tuple[str, ...], rows: list[tuple]):
    if not rows:
        return
    query = sql.SQL("INSERT INTO commerce.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
    )
    with conn.cursor() as cursor:
        cursor.executemany(query, rows)


def seed() -> dict:
    rng = random.Random(SEED)
    password_hash = PasswordHasher().hash(DEMO_PASSWORD)
    with connect_sync() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(1101001)")
        marker = conn.execute("SELECT * FROM commerce.demo_marker WHERE id=1").fetchone()
        if marker:
            if marker["business_date"] != BUSINESS_DATE or marker["seed_version"] != 1:
                raise RuntimeError("Demo seed version or date differs; refusing to overwrite")
            return counts(conn) | {"imported": False}
        count_row = conn.execute("SELECT count(*) AS n FROM commerce.users").fetchone()
        assert count_row is not None
        existing = count_row["n"]
        if existing:
            raise RuntimeError("Unmarked nonempty database; refusing seed import")
        conn.execute(
            "INSERT INTO commerce.demo_marker VALUES (1,'agent-commerce-demo-v1',%s,1)",
            (BUSINESS_DATE,),
        )
        users = [
            (f"USR-{i:03}", f"user{i:03}", f"模拟用户{i:03}", password_hash, "user")
            for i in range(1, 201)
        ]
        users += [
            (f"STAFF-{i:03}", f"staff{i:03}", f"模拟客服{i:03}", password_hash, "staff")
            for i in range(1, 3)
        ]
        insert_many(
            conn, "users", ("id", "username", "display_name", "password_hash", "role"), users
        )
        insert_many(
            conn,
            "products",
            ("id", "name"),
            [(f"PRD-{i:03}", f"模拟商品{i:03}") for i in range(1, 61)],
        )
        prices = {f"SKU-{i:03}": rng.randrange(99, 1300) * 100 for i in range(1, 61)}
        insert_many(
            conn,
            "skus",
            ("id", "product_id", "price_cents"),
            [(sku, f"PRD-{i:03}", price) for i, (sku, price) in enumerate(prices.items(), 1)],
        )
        insert_many(
            conn,
            "inventory",
            ("sku_id", "on_hand", "reserved"),
            [
                (sku, 0 if i % 10 == 0 else rng.randrange(10, 100), 0)
                for i, sku in enumerate(prices, 1)
            ],
        )
        policies = []
        for kind, days in (("refund", 7), ("exchange", 15), ("reship", 30), ("repair", 365)):
            for version, start, end in (
                (1, date(2025, 1, 1), date(2025, 12, 31)),
                (2, date(2026, 1, 1), date(2026, 12, 31)),
            ):
                policies.append(
                    (
                        f"POL-{kind.upper()}",
                        version,
                        kind,
                        start,
                        end,
                        Jsonb({"window_days": days, "simulated": True}),
                        f"模拟{kind}政策：期限{days}天；具体资格由确定性规则校验",
                    )
                )
        insert_many(
            conn,
            "policies",
            ("id", "version", "kind", "valid_from", "valid_until", "conditions", "explanation"),
            policies,
        )
        orders, items, payments, shipments, events, histories = [], [], [], [], [], []
        skus = list(prices)
        for n in range(2000):
            oid = f"ORD-{200001 + n}"
            uid = f"USR-{n % 200 + 1:03}"
            scenario = SCENARIOS[n % len(SCENARIOS)]
            age = rng.randrange(40, 100) if scenario == "expired" else rng.randrange(1, 6)
            purchased = BUSINESS_DATE - timedelta(days=age)
            status = "delivered"
            if scenario == "normal":
                status = ("delivered", "shipped", "paid", "cancelled")[(n // 6) % 4]
            if scenario == "partial_refund":
                status = "partially_refunded"
            selections = rng.sample(skus, 2 if n % 4 == 0 else 1)
            if scenario == "out_of_stock":
                selections[0] = "SKU-010"
                selections = list(dict.fromkeys(selections))
            total, refunded = 0, 0
            for j, sku in enumerate(selections):
                item_id = f"{oid}-I{j + 1}"
                quantity = (
                    2
                    if scenario in ("partial_refund", "history") and j == 0
                    else rng.randrange(1, 4)
                )
                after_quantity = 1 if j == 0 and scenario in ("partial_refund", "history") else 0
                total += quantity * prices[sku]
                items.append(
                    (
                        item_id,
                        oid,
                        sku,
                        f"模拟商品{int(sku[-3:]):03}",
                        quantity,
                        prices[sku],
                        after_quantity,
                    )
                )
                if after_quantity:
                    kind = "refund" if scenario == "partial_refund" else "repair"
                    refund = prices[sku] if kind == "refund" else 0
                    refunded += refund
                    histories.append(
                        (
                            f"HIST-{n + 1:06}",
                            oid,
                            item_id,
                            kind,
                            1,
                            refund,
                            "completed",
                            f"POL-{kind.upper()}",
                            2,
                        )
                    )
            paid = 0 if status == "cancelled" else total
            orders.append((oid, uid, status, paid, refunded, purchased, scenario))
            pay_status = status if status in ("cancelled", "partially_refunded") else "paid"
            payments.append((f"PAY-{n + 1:06}", oid, paid, refunded, pay_status))
            if status not in ("paid", "cancelled"):
                sid = f"SHP-{n + 1:06}"
                ship_status = (
                    "lost"
                    if scenario == "lost"
                    else ("shipped" if status == "shipped" else "delivered")
                )
                shipments.append((sid, oid, f"SIMTRACK{n + 1:08}", ship_status))
                instant = datetime.combine(purchased, datetime.min.time(), tzinfo=UTC) + timedelta(
                    hours=8
                )
                events.append((f"{sid}-E1", sid, instant, "shipped", "模拟包裹已出库"))
                if ship_status != "shipped":
                    events.append(
                        (
                            f"{sid}-E2",
                            sid,
                            instant + timedelta(days=1),
                            ship_status,
                            "模拟物流丢失" if ship_status == "lost" else "模拟包裹已签收",
                        )
                    )
        insert_many(
            conn,
            "orders",
            ("id", "user_id", "status", "paid_cents", "refunded_cents", "purchased_on", "scenario"),
            orders,
        )
        insert_many(
            conn,
            "order_items",
            (
                "id",
                "order_id",
                "sku_id",
                "product_name",
                "quantity",
                "unit_price_cents",
                "after_sales_quantity",
            ),
            items,
        )
        insert_many(
            conn, "payments", ("id", "order_id", "paid_cents", "refunded_cents", "status"), payments
        )
        insert_many(conn, "shipments", ("id", "order_id", "tracking_number", "status"), shipments)
        insert_many(
            conn,
            "logistics_events",
            ("id", "shipment_id", "occurred_at", "status", "description"),
            events,
        )
        insert_many(
            conn,
            "after_sales_history",
            (
                "id",
                "order_id",
                "order_item_id",
                "kind",
                "quantity",
                "refund_cents",
                "status",
                "policy_id",
                "policy_version",
            ),
            histories,
        )
        return counts(conn) | {"imported": True}


def counts(conn) -> dict:
    result = {}
    for table in (
        "users",
        "skus",
        "orders",
        "order_items",
        "payments",
        "shipments",
        "logistics_events",
        "inventory",
        "policies",
        "after_sales_history",
    ):
        result[table] = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM commerce.{}").format(sql.Identifier(table))
        ).fetchone()["n"]
    return result


if __name__ == "__main__":
    print(seed())
