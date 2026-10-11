import asyncio
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from psycopg.errors import RaiseException

from commerce import approval_repository as repo
from commerce import approval_workflow as flow
from commerce.approval_models import SnapshotInput
from commerce.db import connect_sync
from commerce.models import Principal
from commerce.seed_1301 import seed_preview_policies


@pytest.fixture(scope="module", autouse=True)
def policies(demo_db):
    seed_preview_policies()


@pytest.fixture(autouse=True)
def checkpoint(monkeypatch):
    monkeypatch.setattr(flow, "approval_graph", flow.graph.compile(checkpointer=InMemorySaver()))


def login(client, username="user001"):
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "Demo-1101!"}
    )
    response.raise_for_status()
    return Principal(**response.json())


def preview(client, kind="refund", order="ORD-200001", **changes):
    draft = (
        dict(
            kind=kind,
            order_item_id=f"{order}-I1",
            quantity=1,
            reason="not_received" if kind == "reship" else "quality",
            received=kind != "reship",
            request_id=str(uuid4()),
        )
        | changes
    )
    response = client.post(f"/api/v1/orders/{order}/previews", json=draft)
    response.raise_for_status()
    return response.json()


def binding(value):
    return dict(
        preview_id=value.get("preview_id", value["id"]),
        preview_version=value.get("preview_version", value.get("version")),
        content_sha256=value["content_sha256"],
        request_id=str(uuid4()),
    )


def confirm(client, value):
    response = client.post(f"/api/v1/previews/{value['id']}/confirm", json=binding(value))
    assert response.status_code == 202, response.text
    return response.json()


async def advance(app):
    with connect_sync() as conn:
        row = conn.execute(
            "UPDATE commerce.applications SET workflow_status='running' WHERE id=%s RETURNING *",
            (app["id"],),
        ).fetchone()
    await flow.process(row)


def details(client, app):
    response = client.get(f"/api/v1/applications/{app['id']}")
    response.raise_for_status()
    return response.json()


def decide(client, app, decision="approved"):
    return client.post(
        f"/api/v1/applications/{app['id']}/decision",
        json=binding(app) | {"decision": decision, "note": "已核对订单、政策和方案"},
    )


@pytest.mark.parametrize(
    "kind,order,user",
    [
        ("refund", "ORD-200001", "user001"),
        ("exchange", "ORD-200001", "user001"),
        ("reship", "ORD-200004", "user004"),
        ("repair", "ORD-200001", "user001"),
    ],
)
@pytest.mark.asyncio
async def test_four_kinds_double_confirmation_interrupt_and_no_execution(client, kind, order, user):
    login(client, user)
    p = preview(client, kind, order)
    app = confirm(client, p)
    assert app["status"] == "pending_approval"
    assert client.post(f"/api/v1/applications/{app['id']}/execute").status_code == 409
    await advance(app)
    state = await flow.approval_graph.aget_state(flow.config_for(app["id"]))
    assert state.next == ("wait_for_approval",) and state.tasks[0].interrupts
    assert state.tasks[0].interrupts[0].value["content_sha256"] == p["content_sha256"]
    login(client, "staff001")
    assert (
        client.post(f"/api/v1/applications/{app['id']}/resume", json=binding(app)).status_code
        == 409
    )
    response = decide(client, app)
    assert response.status_code == 200 and response.json()["status"] == "decision_recorded"
    assert details(client, app)["application"]["workflow_status"] == "waiting"
    assert client.post(f"/api/v1/applications/{app['id']}/execute").status_code == 409
    assert (
        client.post(f"/api/v1/applications/{app['id']}/resume", json=binding(app)).status_code
        == 202
    )
    await advance(app)
    d = details(client, app)
    assert (
        d["application"]["status"] == "approved"
        and d["application"]["workflow_status"] == "complete"
    )
    assert client.post(f"/api/v1/applications/{app['id']}/execute").status_code == 422
    assert [e["kind"] for e in d["events"]] == [
        "confirmed",
        "interrupt_waiting",
        "decision_recorded",
        "resume_requested",
        "approved",
    ]
    assert [e["seq"] for e in d["events"]] == list(range(1, 6))
    assert d["events"][0]["actor_id"] != d["events"][2]["actor_id"]
    response = client.get(
        f"/api/v1/applications/{app['id']}/events", headers={"Last-Event-ID": "3"}
    )
    assert "id: 4" in response.text and "id: 3" not in response.text
    login(client, user)
    assert details(client, app)["application"]["status"] == "approved"


@pytest.mark.asyncio
async def test_role_ownership_forged_binding_and_schema(client):
    user = login(client)
    p = preview(client)
    data = binding(p)
    for patch, code in [
        ({"user_id": user.id}, 422),
        ({"preview_version": p["version"] + 1}, 409),
        ({"content_sha256": "0" * 64}, 409),
        ({"preview_id": str(uuid4())}, 409),
    ]:
        assert (
            client.post(f"/api/v1/previews/{p['id']}/confirm", json=data | patch).status_code
            == code
        )
    app = confirm(client, p)
    await advance(app)
    assert decide(client, app).status_code == 403
    assert (
        client.post(f"/api/v1/applications/{app['id']}/resume", json=binding(app)).status_code
        == 403
    )
    login(client, "user002")
    assert client.get(f"/api/v1/applications/{app['id']}").status_code == 404
    assert client.post(f"/api/v1/previews/{p['id']}/confirm", json=data).status_code == 404
    assert app["id"] not in [v["id"] for v in client.get("/api/v1/applications").json()["items"]]
    login(client, "staff001")
    assert client.post(f"/api/v1/previews/{p['id']}/confirm", json=data).status_code == 403
    assert decide(client, app).status_code == 200
    assert (
        client.post(f"/api/v1/applications/{app['id']}/cancel", json=binding(app)).status_code
        == 403
    )


@pytest.mark.asyncio
async def test_rejection_cancellation_and_explicit_resume(client):
    login(client)
    app = confirm(client, preview(client))
    await advance(app)
    login(client, "staff001")
    assert decide(client, app, "rejected").json()["status"] == "rejected"
    assert client.post(f"/api/v1/applications/{app['id']}/execute").status_code == 409
    client.post(f"/api/v1/applications/{app['id']}/resume", json=binding(app)).raise_for_status()
    await advance(app)
    assert details(client, app)["application"]["workflow_status"] == "complete"
    login(client)
    another = confirm(client, preview(client))
    await advance(another)
    assert (
        client.post(f"/api/v1/applications/{another['id']}/cancel", json=binding(another)).json()[
            "status"
        ]
        == "cancelled"
    )
    login(client, "staff001")
    assert decide(client, another).status_code == 409
    assert (
        client.post(
            f"/api/v1/applications/{another['id']}/resume", json=binding(another)
        ).status_code
        == 409
    )


@pytest.mark.asyncio
async def test_new_preview_invalidates_approved_snapshot_and_requires_new_confirmation(client):
    login(client)
    p = preview(client)
    app = confirm(client, p)
    await advance(app)
    login(client, "staff001")
    decide(client, app).raise_for_status()
    client.post(f"/api/v1/applications/{app['id']}/resume", json=binding(app)).raise_for_status()
    await advance(app)
    login(client)
    newer = preview(client, "exchange")
    assert newer["version"] == p["version"] + 1
    assert details(client, app)["application"]["status"] == "superseded"
    assert client.post(f"/api/v1/applications/{app['id']}/execute").status_code == 409
    login(client, "staff001")
    assert decide(client, app).status_code == 409
    login(client)
    new_app = confirm(client, newer)
    assert new_app["status"] == "pending_approval" and new_app["decision"] is None
    assert len(details(client, new_app)["events"]) == 1


@pytest.mark.parametrize("phase", ["confirmation", "decision", "finalize"])
@pytest.mark.asyncio
async def test_condition_change_stops_old_snapshot(client, phase):
    login(client)
    p = preview(client)
    app = None if phase == "confirmation" else confirm(client, p)
    if app:
        await advance(app)
        login(client, "staff001")
    if phase == "finalize":
        decide(client, app).raise_for_status()
        client.post(
            f"/api/v1/applications/{app['id']}/resume", json=binding(app)
        ).raise_for_status()
    with connect_sync() as conn:
        stock = conn.execute(
            "SELECT i.sku_id,i.on_hand FROM commerce.inventory i JOIN commerce.order_items oi ON oi.sku_id=i.sku_id WHERE oi.id=%s",
            (p["request"]["order_item_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE commerce.inventory SET on_hand=on_hand+1 WHERE sku_id=%s", (stock["sku_id"],)
        )
    try:
        if phase == "confirmation":
            assert (
                client.post(f"/api/v1/previews/{p['id']}/confirm", json=binding(p)).status_code
                == 409
            )
        elif phase == "decision":
            assert decide(client, app).status_code == 409
            assert details(client, app)["application"]["status"] == "conditions_changed"
        else:
            await advance(app)
            assert details(client, app)["application"]["status"] == "conditions_changed"
            assert client.post(f"/api/v1/applications/{app['id']}/execute").status_code == 409
    finally:
        with connect_sync() as conn:
            conn.execute(
                "UPDATE commerce.inventory SET on_hand=%s WHERE sku_id=%s",
                (stock["on_hand"], stock["sku_id"]),
            )


@pytest.mark.asyncio
async def test_concurrent_and_repeated_commands_do_not_duplicate_audit(client):
    actor = login(client)
    p = preview(client)
    data = SnapshotInput(**binding(p))
    rows = await asyncio.gather(*[repo.confirm(data, actor) for _ in range(6)])
    assert len({r["id"] for r in rows}) == 1
    app = client.get(f"/api/v1/applications/{rows[0]['id']}").json()["application"]
    assert len(details(client, app)["events"]) == 1
    await advance(app)
    login(client, "staff001")
    data = binding(app) | {"decision": "approved", "note": "同一审批重试"}
    for _ in range(3):
        assert (
            client.post(f"/api/v1/applications/{app['id']}/decision", json=data).status_code == 200
        )
    assert (
        client.post(
            f"/api/v1/applications/{app['id']}/decision", json=data | {"decision": "rejected"}
        ).status_code
        == 409
    )
    data = binding(app)
    for _ in range(3):
        assert client.post(f"/api/v1/applications/{app['id']}/resume", json=data).status_code == 202
    await advance(app)
    await advance(app)  # Simulate loss of acknowledgement after a completed checkpoint.
    assert [e["kind"] for e in details(client, app)["events"]].count("approved") == 1


@pytest.mark.asyncio
async def test_forged_graph_resume_cannot_replace_database_decision(client):
    login(client)
    app = confirm(client, preview(client))
    await advance(app)
    with pytest.raises(ValueError, match="No authoritative"):
        await flow.approval_graph.ainvoke(
            Command(resume={"application_id": app["id"], "decision": "approved"}),
            flow.config_for(app["id"]),
        )
    assert details(client, app)["application"]["status"] == "pending_approval"


@pytest.mark.asyncio
async def test_worker_restart_recovers_running_checkpoint(client):
    login(client)
    app = confirm(client, preview(client))
    await advance(app)
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.applications SET workflow_status='running' WHERE id=%s", (app["id"],)
        )
    await flow.recover_interrupted()
    assert details(client, app)["application"]["workflow_status"] == "queued"
    await advance(app)
    assert details(client, app)["application"]["workflow_status"] == "waiting"
    assert [e["kind"] for e in details(client, app)["events"]].count("interrupt_waiting") == 1


def test_csrf_missing_auth_and_immutable_audit(client):
    assert client.get("/api/v1/applications").status_code == 401
    login(client)
    p = preview(client)
    assert (
        client.post(
            f"/api/v1/previews/{p['id']}/confirm",
            json=binding(p),
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    app = confirm(client, p)
    with connect_sync() as conn, pytest.raises(RaiseException, match="append-only"):
        conn.execute("DELETE FROM commerce.approval_events WHERE application_id=%s", (app["id"],))
