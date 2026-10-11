import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from commerce.db import connect_sync
from commerce.preview_models import PreviewDraft
from commerce.preview_service import evaluate
from commerce.seed import BUSINESS_DATE
from commerce.seed_1301 import seed_preview_policies


@pytest.fixture(scope="module", autouse=True)
def policies(demo_db):
    assert seed_preview_policies()["imported"]
    assert not seed_preview_policies()["imported"]


def payload(**changes):
    return {
        "request_id": str(uuid4()),
        "kind": "refund",
        "order_item_id": "ORD-200001-I1",
        "quantity": 1,
        "reason": "quality",
        "received": True,
        **changes,
    }


def fingerprint():
    with connect_sync() as conn:
        values = [
            conn.execute(
                f"SELECT row_to_json(t) AS row FROM commerce.{table} t ORDER BY id"
                if table != "inventory"
                else "SELECT row_to_json(t) AS row FROM commerce.inventory t ORDER BY sku_id"
            ).fetchall()
            for table in (
                "orders",
                "order_items",
                "payments",
                "shipments",
                "after_sales_history",
                "inventory",
            )
        ]
    return hashlib.sha256(json.dumps(values, default=str, sort_keys=True).encode()).hexdigest()


def test_preview_permissions_and_extra_fields(client):
    assert client.post("/api/v1/orders/ORD-200001/previews", json=payload()).status_code == 401
    client.post("/api/v1/auth/login", json={"username": "user001", "password": "Demo-1101!"})
    assert client.post("/api/v1/orders/ORD-200002/previews", json=payload()).status_code == 404
    for extra in (
        {"user_id": "USR-002"},
        {"refund_cents": 999999},
        {"business_date": "2026-01-01"},
        {"quantity": True},
        {"quantity": 0},
        {"received": "false"},
    ):
        assert (
            client.post("/api/v1/orders/ORD-200001/previews", json=payload(**extra)).status_code
            == 422
        )
    assert (
        client.post(
            "/api/v1/orders/ORD-200001/previews", json=payload(order_item_id="ORD-200002-I1")
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/orders/ORD-200001/previews",
            json=payload(),
            headers={"Origin": "https://bad.invalid"},
        ).status_code
        == 403
    )


def test_idempotency_private_history_and_no_business_writes(login_user):
    before = fingerprint()
    data = payload()
    path = "/api/v1/orders/ORD-200001/previews"
    first = login_user.post(path, json=data).json()
    assert first["status"] == "eligible" and first["refund_cents"] == 14900
    assert first["policy"]["version"] == 3 and first["execution_state"] == "not_executed"
    assert login_user.post(path, json=data).json() == first
    assert login_user.post(path, json=data | {"quantity": 2}).status_code == 409
    assert login_user.get(f"/api/v1/previews/{first['id']}").json() == first
    assert first in login_user.get(path).json()["items"]
    login_user.post("/api/v1/auth/login", json={"username": "staff001", "password": "Demo-1101!"})
    assert login_user.get(f"/api/v1/previews/{first['id']}").status_code == 404
    assert login_user.get(path).json()["items"] == []
    assert fingerprint() == before


def test_concurrent_duplicate_preview(login_user):
    data = payload(kind="exchange")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: login_user.post("/api/v1/orders/ORD-200001/previews", json=data), range(8)
            )
        )
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    assert len({response.json()["id"] for response in responses}) == 1


CASES = json.loads(Path("src/commerce/fixtures/preview_cases_v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_frozen_cases(client, case):
    client.post(
        "/api/v1/auth/login", json={"username": case["actor_username"], "password": "Demo-1101!"}
    )
    before = fingerprint()
    response = client.post(
        f"/api/v1/orders/{case['order_id']}/previews",
        json=case["input"] | {"request_id": str(uuid4())},
    )
    if case["expected_status"] == "not_found":
        assert response.status_code == 404
    else:
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == case["expected_status"], result
        code = case["expected_code"]
        if result["status"] == "needs_info":
            assert code in result["missing_fields"] and result["questions"]
        elif code:
            assert any(row["code"] == code and not row["passed"] for row in result["checks"])
        if result["status"] != "eligible":
            assert result["refund_cents"] is None
    assert fingerprint() == before


@pytest.mark.parametrize(
    "kind,days", [("refund", 7), ("exchange", 15), ("reship", 30), ("repair", 365)]
)
@pytest.mark.parametrize("delta,eligible", [(0, True), (1, False), (-1, True)])
def test_policy_window_boundaries(db, kind, days, delta, eligible):
    order = db.execute("SELECT * FROM commerce.orders WHERE id='ORD-200001'").fetchone()
    order["purchased_on"] = BUSINESS_DATE - timedelta(days=days + delta)
    items = db.execute(
        "SELECT * FROM commerce.order_items WHERE order_id=%s ORDER BY id", (order["id"],)
    ).fetchall()
    policy = db.execute(
        "SELECT * FROM commerce.policies WHERE kind=%s AND version=3", (kind,)
    ).fetchone()
    draft = PreviewDraft(
        kind=kind,
        order_item_id=items[0]["id"],
        quantity=1,
        reason="not_received" if kind == "reship" else "quality",
        received=kind != "reship",
    )
    result = evaluate(
        order,
        items,
        [],
        {"status": "lost" if kind == "reship" else "delivered"},
        {"on_hand": 10, "reserved": 0},
        policy,
        draft,
    )
    assert (result.status == "eligible") == eligible


def test_future_date_missing_policy_and_money_limit(db):
    order = db.execute("SELECT * FROM commerce.orders WHERE id='ORD-200001'").fetchone()
    items = db.execute(
        "SELECT * FROM commerce.order_items WHERE order_id=%s ORDER BY id", (order["id"],)
    ).fetchall()
    policy = db.execute(
        "SELECT * FROM commerce.policies WHERE kind='refund' AND version=3"
    ).fetchone()
    draft = PreviewDraft(**{k: v for k, v in payload().items() if k != "request_id"})
    order["purchased_on"] = BUSINESS_DATE + timedelta(days=1)
    result = evaluate(order, items, [], None, None, policy, draft)
    assert result.status == "ineligible"
    order["purchased_on"] = BUSINESS_DATE
    order["paid_cents"] = 1
    result = evaluate(order, items, [], None, None, policy, draft)
    assert any(row.code == "refund_limit" and not row.passed for row in result.checks)
    assert evaluate(order, items, [], None, None, None, draft).status == "ineligible"


def test_case_visibility_and_policies(login_user):
    cases = login_user.get("/api/v1/demo-cases").json()
    assert cases["total"] > 0 and all(row["order_id"] == "ORD-200001" for row in cases["items"])
    login_user.post("/api/v1/auth/login", json={"username": "staff001", "password": "Demo-1101!"})
    assert login_user.get("/api/v1/demo-cases").json()["total"] == 60
    assert len(login_user.get("/api/v1/policies").json()["items"]) == 4


@pytest.mark.asyncio
async def test_multiturn_preview_card_events_and_checkpoint(login_user, monkeypatch):
    from agents.after_sales.agent import graph
    from commerce import chat_worker
    from commerce.chat_worker import process

    saver = InMemorySaver()
    monkeypatch.setattr(chat_worker, "after_sales_agent", graph.compile(checkpointer=saver))
    thread = login_user.post("/api/v1/threads", json={"order_id": "ORD-200001"}).json()["id"]
    before = fingerprint()
    for question, expected in [
        ("我要退款", "needs_info"),
        ("ORD-200001-I1，1件，质量问题，已收到", "eligible"),
    ]:
        run_id = login_user.post(
            f"/api/v1/threads/{thread}/messages",
            json={"message": question, "request_id": str(uuid4())},
        ).json()["id"]
        with connect_sync() as conn:
            run = conn.execute(
                "SELECT r.*,t.order_id FROM commerce.chat_runs r JOIN commerce.chat_threads t ON t.id=r.thread_id WHERE r.id=%s",
                (run_id,),
            ).fetchone()
        await process(run)
        history = login_user.get(f"/api/v1/threads/{thread}").json()
        assert history["runs"][-1]["status"] == "completed", history
        assert history["messages"][-1]["preview"]["status"] == expected
        events = login_user.get(f"/api/v1/runs/{run_id}").json()["events"]
        assert len([row for row in events if row["kind"] == "preview"]) == 1
        assert events[-1]["kind"] == "done"
    # Recompile against the same persisted state, then verify the pending information survived.
    state = await graph.compile(checkpointer=saver).aget_state(
        {"configurable": {"thread_id": f"commerce-chat:{thread}"}}
    )
    assert state.values["preview_draft"]["quantity"] == 1
    assert fingerprint() == before
