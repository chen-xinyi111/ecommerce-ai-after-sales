import asyncio
from itertools import count
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import AsyncConnection
from psycopg.errors import RaiseException
from test_approvals import advance, binding, confirm, decide, login, preview
from test_supervisor import internal_key as internal_key
from test_supervisor import make_context
from test_supervisor import protocol as protocol

from commerce import approval_workflow as approval_flow
from commerce import execution_repository as repo
from commerce import execution_worker as worker
from commerce.approval_models import SnapshotInput
from commerce.db import connect_sync
from commerce.execution_models import ActionRequest, ExecutionRequest
from commerce.mcp_client import Gateway, MCPFailure
from commerce.seed_1301 import seed_preview_policies

numbers = count(990001)


@pytest.fixture(scope="module", autouse=True)
def policies(demo_db):
    seed_preview_policies()


@pytest.fixture(autouse=True)
def checkpoints(monkeypatch):
    monkeypatch.setattr(
        approval_flow, "approval_graph", approval_flow.graph.compile(checkpointer=InMemorySaver())
    )


def clone(kind="refund"):
    number = next(numbers)
    oid = f"ORD-{number}"
    with connect_sync() as conn:
        conn.execute(
            "INSERT INTO commerce.orders SELECT %s,user_id,status,paid_cents,refunded_cents,purchased_on,'execution-test',simulated FROM commerce.orders WHERE id='ORD-200001'",
            (oid,),
        )
        conn.execute(
            "INSERT INTO commerce.order_items SELECT %s||substring(id from 11),%s,sku_id,product_name,quantity,unit_price_cents,after_sales_quantity FROM commerce.order_items WHERE order_id='ORD-200001'",
            (oid, oid),
        )
        conn.execute(
            "INSERT INTO commerce.payments SELECT %s,%s,paid_cents,refunded_cents,status FROM commerce.payments WHERE order_id='ORD-200001'",
            (f"PAY-{number}", oid),
        )
        conn.execute(
            "INSERT INTO commerce.shipments(id,order_id,tracking_number,status) VALUES (%s,%s,%s,%s)",
            (f"SHP-{number}", oid, f"SIM-{number}", "lost" if kind == "reship" else "delivered"),
        )
    return oid


async def approved(client, kind="refund", order=None):
    order = order or clone(kind)
    login(client)
    p = preview(client, kind, order)
    assert p["status"] == "eligible", p
    app = confirm(client, p)
    await advance(app)
    login(client, "staff001")
    assert decide(client, app).status_code == 200
    assert (
        client.post(f"/api/v1/applications/{app['id']}/resume", json=binding(app)).status_code
        == 202
    )
    await advance(app)
    return client.get(f"/api/v1/applications/{app['id']}").json()["application"]


def submit(client, app):
    response = client.post(f"/api/v1/applications/{app['id']}/execute", json=binding(app))
    assert response.status_code == 202, response.text
    return response.json()


def read_job(job):
    with connect_sync() as conn:
        return conn.execute(
            "SELECT * FROM commerce.execution_jobs WHERE id=%s", (job["id"],)
        ).fetchone()


def running(job):
    with connect_sync() as conn:
        return conn.execute(
            "UPDATE commerce.execution_jobs SET status='running',attempts=attempts+1 WHERE id=%s RETURNING *",
            (job["id"],),
        ).fetchone()


def request(job, kind=None):
    model = ActionRequest if kind else ExecutionRequest
    return model(
        job_id=job["id"], application_id=job["application_id"], **({"kind": kind} if kind else {})
    )


async def tool_steps(job, kind, action=True):
    current, _, _ = await worker.inspect(job["id"])
    gateway = Gateway(
        {
            "business_purpose": "execution",
            "business_run_id": str(job["id"]),
            "business_application_id": str(job["application_id"]),
            "business_order_id": current["order_id"],
        },
        {"create_work_order", "execute_after_sales"},
    )
    async with gateway.session() as session:
        created = await gateway.call(
            session, "create_work_order", request(job).model_dump(mode="json")
        )
        if action:
            return await gateway.call(
                session, "execute_after_sales", request(job, kind).model_dump(mode="json")
            )
        return created


@pytest.mark.parametrize("kind", ["refund", "exchange", "reship", "repair"])
@pytest.mark.asyncio
async def test_four_actions_real_mcp_and_duplicate_execution(client, protocol, kind):
    app = await approved(client, kind)
    with connect_sync() as conn:
        before = conn.execute(
            "SELECT * FROM commerce.orders WHERE id=%s", (app["order_id"],)
        ).fetchone()
        item = conn.execute(
            "SELECT * FROM commerce.order_items WHERE id=%s", (app["order_id"] + "-I1",)
        ).fetchone()
        stock = conn.execute(
            "SELECT * FROM commerce.inventory WHERE sku_id=%s", (item["sku_id"],)
        ).fetchone()
    actor = login(client, "staff001")
    data = SnapshotInput(**binding(app))
    jobs = await asyncio.gather(*[repo.enqueue(app["id"], data, actor) for _ in range(5)])
    assert len({j["id"] for j in jobs}) == 1
    job = running(jobs[0])
    await worker.process(job)
    assert read_job(job)["status"] == "completed"
    assert submit(client, app)["id"] == str(job["id"])
    # Redelivery after a committed action performs only reconciliation.
    await worker.process(job)
    d = client.get(f"/api/v1/applications/{app['id']}/execution").json()
    assert d["work_order"]["status"] == "completed" and d["job"]["result"]["kind"] == kind
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.action_results WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["n"]
            == 1
        )
        after = conn.execute(
            "SELECT * FROM commerce.orders WHERE id=%s", (app["order_id"],)
        ).fetchone()
        new_item = conn.execute(
            "SELECT * FROM commerce.order_items WHERE id=%s", (item["id"],)
        ).fetchone()
        new_stock = conn.execute(
            "SELECT * FROM commerce.inventory WHERE sku_id=%s", (item["sku_id"],)
        ).fetchone()
    assert new_item["after_sales_quantity"] == item["after_sales_quantity"] + 1
    assert after["refunded_cents"] == before["refunded_cents"] + (
        item["unit_price_cents"] if kind == "refund" else 0
    )
    assert new_stock["on_hand"] == stock["on_hand"] - (1 if kind in ("exchange", "reship") else 0)
    assert new_stock["reserved"] == stock["reserved"]
    assert (
        client.post(f"/api/v1/applications/{app['id']}/resume", json=binding(app)).status_code
        == 409
    )
    assert (
        client.get(f"/api/v1/applications/{app['id']}").json()["application"]["status"]
        == "approved"
    )
    if kind == "refund":
        assert d["refund"]["amount_cents"] == item["unit_price_cents"]
    if kind in ("exchange", "reship"):
        assert d["reservation"]["status"] == "consumed" and d["shipment"]["status"] == "dispatched"
    if kind == "repair":
        assert d["repair"]["status"] == "awaiting_inspection"
    login(client)
    assert (
        client.post(f"/api/v1/applications/{app['id']}/cancel", json=binding(app)).status_code
        == 409
    )
    # New previews must not relabel an already committed action as cancelled/superseded.
    preview(client, "repair", app["order_id"])
    assert (
        client.get(f"/api/v1/applications/{app['id']}").json()["application"]["status"]
        == "approved"
    )


@pytest.mark.asyncio
async def test_access_binding_and_chat_cannot_write(client, protocol):
    app = await approved(client)
    login(client)
    assert (
        client.post(f"/api/v1/applications/{app['id']}/execute", json=binding(app)).status_code
        == 403
    )
    chat = Gateway(
        make_context(client, app["order_id"]), {"create_work_order", "execute_after_sales"}
    )
    async with chat.session() as session:
        for name, args in [
            ("create_work_order", {"job_id": str(uuid4()), "application_id": app["id"]}),
            (
                "execute_after_sales",
                {"job_id": str(uuid4()), "application_id": app["id"], "kind": "refund"},
            ),
        ]:
            assert (await session.call_tool(name, {"request": args})).isError
    login(client, "user002")
    assert client.get(f"/api/v1/applications/{app['id']}/execution").status_code == 404
    login(client, "staff001")
    assert (
        client.post(
            f"/api/v1/applications/{app['id']}/execute",
            json=binding(app) | {"preview_version": 999},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/applications/{app['id']}/execute", json=binding(app) | {"amount_cents": 1}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/applications/{app['id']}/execute",
            json=binding(app),
            headers={"Origin": "https://evil.invalid"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("phase", ["submit", "reserved", "cancelled", "modified"])
@pytest.mark.asyncio
async def test_changed_conditions_or_cancel_stop_and_release(client, protocol, phase):
    app = await approved(client, "exchange")
    with connect_sync() as conn:
        sku = conn.execute(
            "SELECT sku_id FROM commerce.order_items WHERE id=%s", (app["order_id"] + "-I1",)
        ).fetchone()["sku_id"]
        baseline = conn.execute(
            "SELECT * FROM commerce.inventory WHERE sku_id=%s", (sku,)
        ).fetchone()
    if phase == "submit":
        with connect_sync() as conn:
            conn.execute("UPDATE commerce.inventory SET on_hand=on_hand+1 WHERE sku_id=%s", (sku,))
        assert (
            client.post(f"/api/v1/applications/{app['id']}/execute", json=binding(app)).status_code
            == 409
        )
        return
    job = running(submit(client, app))
    assert (await tool_steps(job, "exchange", action=False))["status"] == "reserved"
    if phase == "reserved":
        with connect_sync() as conn:
            conn.execute("UPDATE commerce.inventory SET on_hand=on_hand+1 WHERE sku_id=%s", (sku,))
    else:
        login(client)
        if phase == "cancelled":
            assert (
                client.post(
                    f"/api/v1/applications/{app['id']}/cancel", json=binding(app)
                ).status_code
                == 200
            )
        else:
            preview(client, "repair", app["order_id"])
    await worker.process(job)
    assert read_job(job)["status"] == "stopped"
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT reserved FROM commerce.inventory WHERE sku_id=%s", (sku,)
            ).fetchone()["reserved"]
            == baseline["reserved"]
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.action_results WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("lost_tool", ["create_work_order", "execute_after_sales"])
@pytest.mark.asyncio
async def test_lost_mcp_response_queries_results_before_retry(client, protocol, lost_tool):
    app = await approved(client, "exchange")
    job = running(submit(client, app))

    class LostResponse(Gateway):
        async def call(self, session, name, args):
            result = await super().call(session, name, args)
            if name == lost_tool:
                raise MCPFailure("response lost after actual commit")
            return result

    await worker.process(job, LostResponse)
    if lost_tool == "create_work_order":
        assert read_job(job)["status"] == "queued"
        await worker.process(running(job))
    assert read_job(job)["status"] == "completed"
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.after_sales_shipments WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["n"]
            == 1
        )


@pytest.mark.parametrize("crash_step", ["reservation", "action"])
@pytest.mark.asyncio
async def test_restart_from_persisted_steps(client, protocol, crash_step):
    app = await approved(client, "reship")
    job = running(submit(client, app))
    await tool_steps(job, "reship", action=crash_step == "action")
    assert read_job(job)["status"] == "running"
    await worker.recover_interrupted()
    assert read_job(job)["status"] == "queued"

    class NeverCall:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Committed action must be read without another MCP call")

    await worker.process(running(job), NeverCall if crash_step == "action" else Gateway)
    assert read_job(job)["status"] == "completed"


@pytest.mark.asyncio
async def test_financial_transaction_rollback_and_bounded_failure_retry(
    client, protocol, monkeypatch
):
    app = await approved(client)
    job = running(submit(client, app))
    await repo.create_work_order(request(job))
    original = AsyncConnection.execute

    async def fail_commit_record(self, query, *args, **kwargs):
        if isinstance(query, str) and query.startswith("INSERT INTO commerce.action_results"):
            raise RuntimeError("Injected transaction failure after financial writes")
        return await original(self, query, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", fail_commit_record)
    with pytest.raises(RuntimeError):
        await repo.execute_action(request(job, "refund"))
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT refunded_cents FROM commerce.orders WHERE id=%s", (app["order_id"],)
            ).fetchone()["refunded_cents"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.refund_ledger WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["n"]
            == 0
        )
    await worker.process(job)
    await worker.process(running(job))
    await worker.process(running(job))
    assert read_job(job)["status"] == "failed"
    monkeypatch.setattr(AsyncConnection, "execute", original)
    retry = binding(app) | {"note": "已核对无退款记录，重试"}
    for _ in range(2):
        assert (
            client.post(f"/api/v1/applications/{app['id']}/retry-execution", json=retry).status_code
            == 202
        )
    await worker.process(running(job))
    assert read_job(job)["status"] == "completed"


@pytest.mark.asyncio
async def test_two_orders_compete_for_last_stock_database_prevents_oversell(client, protocol):
    with connect_sync() as conn:
        sku = conn.execute(
            "SELECT sku_id FROM commerce.order_items WHERE id='ORD-200001-I1'"
        ).fetchone()["sku_id"]
        before = conn.execute("SELECT * FROM commerce.inventory WHERE sku_id=%s", (sku,)).fetchone()
        conn.execute("UPDATE commerce.inventory SET on_hand=1,reserved=0 WHERE sku_id=%s", (sku,))
    try:
        first = await approved(client, "exchange")
        second = await approved(client, "exchange")
        jobs = [running(submit(client, a)) for a in (first, second)]
        outcomes = await asyncio.gather(*[repo.create_work_order(request(j)) for j in jobs])
        assert sorted(o.status for o in outcomes) == ["reserved", "stopped"]
        winner = jobs[next(i for i, o in enumerate(outcomes) if o.status == "reserved")]
        await worker.process(winner)
        with connect_sync() as conn:
            stock = conn.execute(
                "SELECT * FROM commerce.inventory WHERE sku_id=%s", (sku,)
            ).fetchone()
            assert stock["on_hand"] == 0 and stock["reserved"] == 0
    finally:
        with connect_sync() as conn:
            conn.execute(
                "UPDATE commerce.inventory SET on_hand=%s,reserved=%s WHERE sku_id=%s",
                (before["on_hand"], before["reserved"], sku),
            )


@pytest.mark.asyncio
async def test_terminal_failure_releases_reservation_and_explicit_retry(client, protocol):
    app = await approved(client, "exchange")
    job = running(submit(client, app))
    await tool_steps(job, "exchange", action=False)

    class Unavailable(Gateway):
        async def call(self, session, name, args):
            raise MCPFailure("Injected unavailable action")

    for attempt in range(3):
        await worker.process(job if attempt == 0 else running(job), Unavailable)
    assert read_job(job)["status"] == "failed"
    detail = client.get(f"/api/v1/applications/{app['id']}/execution").json()
    assert detail["reservation"]["status"] == "released"
    assert detail["work_order"]["status"] == "cancelled"
    assert detail["shipment"] is None
    body = binding(app) | {"note": "核对库存预留已释放，未发货"}
    assert (
        client.post(f"/api/v1/applications/{app['id']}/retry-execution", json=body).status_code
        == 202
    )
    await worker.process(running(job))
    assert read_job(job)["status"] == "completed"


@pytest.mark.asyncio
async def test_execution_capability_bound_to_job_and_kind(client, protocol):
    first = await approved(client)
    second = await approved(client)
    job = running(submit(client, first))
    other = running(submit(client, second))
    current, _, _ = await worker.inspect(job["id"])
    gateway = Gateway(
        {
            "business_purpose": "execution",
            "business_run_id": str(job["id"]),
            "business_application_id": str(job["application_id"]),
            "business_order_id": current["order_id"],
        },
        {"create_work_order", "execute_after_sales"},
        max_calls=6,
    )
    async with gateway.session() as session:
        with pytest.raises(MCPFailure):
            await gateway.call(session, "create_work_order", request(other).model_dump(mode="json"))
        await gateway.call(session, "create_work_order", request(job).model_dump(mode="json"))
        with pytest.raises(MCPFailure):
            await gateway.call(
                session, "execute_after_sales", request(job, "repair").model_dump(mode="json")
            )
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.action_results WHERE application_id=%s",
                (first["id"],),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.asyncio
async def test_concurrent_refund_commits_once_and_ledger_is_immutable(client, protocol):
    app = await approved(client)
    job = running(submit(client, app))
    await repo.create_work_order(request(job))
    results = await asyncio.gather(*[repo.execute_action(request(job, "refund")) for _ in range(5)])
    assert len({r.model_dump_json() for r in results}) == 1
    await worker.settle(job["id"])
    with connect_sync() as conn:
        payment = conn.execute(
            "SELECT * FROM commerce.payments WHERE order_id=%s", (app["order_id"],)
        ).fetchone()
        assert (
            payment["refunded_cents"] == 14900
            and payment["refunded_cents"] <= payment["paid_cents"]
        )
        with pytest.raises(RaiseException):
            conn.execute(
                "UPDATE commerce.refund_ledger SET amount_cents=1 WHERE application_id=%s",
                (app["id"],),
            )
