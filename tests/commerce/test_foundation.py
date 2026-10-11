from datetime import UTC, datetime, timedelta

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation

from commerce.migrate import migrate
from commerce.seed import counts, seed


def test_seed_sizes_and_scenarios(db):
    result = counts(db)
    assert result["users"] == 202
    assert result["orders"] == 2000
    assert result["skus"] == result["inventory"] == 60
    assert result["payments"] == 2000
    assert result["policies"] == 8
    scenarios = db.execute("SELECT DISTINCT scenario FROM commerce.orders").fetchall()
    assert len(scenarios) == 6


def test_repeat_import_preserves_changes(demo_db):
    from commerce.db import connect_sync

    with connect_sync() as conn:
        before = counts(conn)
        conn.execute("UPDATE commerce.inventory SET on_hand=137 WHERE sku_id='SKU-001'")
    assert migrate() == []
    assert seed()["imported"] is False
    with connect_sync() as conn:
        assert counts(conn) == before
        assert (
            conn.execute(
                "SELECT on_hand FROM commerce.inventory WHERE sku_id='SKU-001'"
            ).fetchone()["on_hand"]
            == 137
        )


def test_migration_checksum_rejected(db):
    from commerce.db import connect_sync

    with connect_sync() as conn:
        original = conn.execute(
            "SELECT checksum FROM commerce.schema_migrations WHERE version='001_foundation.sql'"
        ).fetchone()["checksum"]
        conn.execute(
            "UPDATE commerce.schema_migrations SET checksum='tampered' WHERE version='001_foundation.sql'"
        )
    try:
        with pytest.raises(RuntimeError, match="checksum"):
            migrate()
    finally:
        with connect_sync() as conn:
            conn.execute(
                "UPDATE commerce.schema_migrations SET checksum=%s WHERE version='001_foundation.sql'",
                (original,),
            )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE commerce.orders SET refunded_cents=paid_cents+1 WHERE id='ORD-200001'",
        "UPDATE commerce.inventory SET reserved=on_hand+1 WHERE sku_id='SKU-001'",
        "UPDATE commerce.order_items SET after_sales_quantity=quantity+1 WHERE id='ORD-200001-I1'",
    ],
)
def test_numeric_constraints(db, statement):
    with pytest.raises(CheckViolation):
        db.execute(statement)


def test_cross_record_amount_constraint(db):
    db.execute("UPDATE commerce.payments SET paid_cents=paid_cents+1 WHERE order_id='ORD-200001'")
    with pytest.raises(CheckViolation):
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_item_refund_limit(db):
    row = db.execute(
        "SELECT * FROM commerce.after_sales_history WHERE kind='refund' LIMIT 1"
    ).fetchone()
    db.execute(
        "UPDATE commerce.after_sales_history SET refund_cents=refund_cents+1 WHERE id=%s",
        (row["id"],),
    )
    with pytest.raises(CheckViolation):
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_cross_order_history_rejected(db):
    with pytest.raises(ForeignKeyViolation):
        db.execute(
            "UPDATE commerce.after_sales_history SET order_item_id='ORD-200001-I1' WHERE id='HIST-000005'"
        )


def test_login_requires_auth(client):
    for path in ("/api/v1/orders", "/api/v1/auth/me", "/api/v1/inventory"):
        assert client.get(path).status_code == 401


def test_wrong_password(client):
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "user001", "password": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "missing", "password": "wrong"}
        ).status_code
        == 401
    )


def test_cookie_and_logout(login_user):
    response = login_user.post(
        "/api/v1/auth/login", json={"username": "user001", "password": "Demo-1101!"}
    )
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header
    token = login_user.cookies.get("commerce_session")
    assert login_user.get("/api/v1/auth/me").json()["role"] == "user"
    assert login_user.post("/api/v1/auth/logout").status_code == 204
    assert login_user.get("/api/v1/auth/me").status_code == 401
    assert (
        login_user.get(
            "/api/v1/auth/me", headers={"Cookie": f"commerce_session={token}"}
        ).status_code
        == 401
    )


def test_order_ownership_and_identity_spoof(login_user):
    page = login_user.get("/api/v1/orders", params={"user_id": "USR-002"}).json()
    assert page["total"] == 10
    assert all(row["user_id"] == "USR-001" for row in page["items"])
    assert login_user.get("/api/v1/orders/ORD-200001").status_code == 200
    assert login_user.get("/api/v1/orders/ORD-200002").status_code == 404
    assert login_user.get("/api/v1/orders/ORD-200002/logistics").status_code == 404
    assert (
        login_user.post(
            "/api/v1/auth/login",
            json={
                "username": "user001",
                "password": "Demo-1101!",
                "user_id": "USR-002",
                "role": "staff",
            },
        ).status_code
        == 422
    )


def test_staff_and_inventory(client):
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "staff001", "password": "Demo-1101!"}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/orders").json()["total"] == 2000
    assert client.get("/api/v1/orders/ORD-200002").status_code == 200
    assert len(client.get("/api/v1/inventory").json()["items"]) == 60
    assert client.get("/api/v1/inventory/SKU-010").json()["available"] == 0


def test_user_inventory_limits(login_user, db):
    assert login_user.get("/api/v1/inventory").status_code == 403
    sku = login_user.get("/api/v1/orders/ORD-200001").json()["items"][0]["sku_id"]
    assert login_user.get(f"/api/v1/inventory/{sku}").status_code == 200
    other = db.execute(
        "SELECT id FROM commerce.skus WHERE id NOT IN "
        "(SELECT i.sku_id FROM commerce.order_items i JOIN commerce.orders o ON o.id=i.order_id WHERE o.user_id='USR-001') LIMIT 1"
    ).fetchone()["id"]
    assert login_user.get(f"/api/v1/inventory/{other}").status_code == 404


def test_session_expiry(login_user, db):
    from commerce.auth import token_hash

    token = login_user.cookies.get("commerce_session")
    db.execute(
        "UPDATE commerce.sessions SET expires_at=%s WHERE token_hash=%s",
        (
            datetime.now(UTC) - timedelta(seconds=1),
            token_hash(token),
        ),
    )
    db.commit()
    assert login_user.get("/api/v1/orders").status_code == 401


def test_origin_protection(client):
    payload = {"username": "user001", "password": "Demo-1101!"}
    assert (
        client.post(
            "/api/v1/auth/login", json=payload, headers={"Origin": "https://other.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/auth/login", json=payload, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )


def test_database_failure(client, monkeypatch):
    monkeypatch.setenv("POSTGRES_PORT", "1")
    assert client.get("/api/v1/health").status_code == 503


def test_health(client):
    assert client.get("/api/v1/health").json()["business_date"] == "2026-10-09"


@pytest.mark.asyncio
async def test_business_tool_requires_real_session(login_user):
    from langchain_core.tools import ToolException

    from agents.after_sales.read_tools import query_business_order
    from commerce.auth import token_hash

    with pytest.raises(ToolException, match="登录"):
        await query_business_order.ainvoke(
            {"order_id": "ORD-200001"},
            config={"configurable": {"user_id": "USR-001", "role": "staff"}},
        )
    config = {
        "configurable": {
            "business_session_hash": token_hash(login_user.cookies.get("commerce_session"))
        }
    }
    assert (await query_business_order.ainvoke({"order_id": "ORD-200001"}, config=config))[
        "id"
    ] == "ORD-200001"
    with pytest.raises(ToolException, match="不存在"):
        await query_business_order.ainvoke({"order_id": "ORD-200002"}, config=config)


def test_reset_refuses_other_database(demo_db):
    from commerce.reset import reset

    with pytest.raises(RuntimeError, match="restricted"):
        reset("agent_commerce_demo")
