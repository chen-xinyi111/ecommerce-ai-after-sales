import asyncio
import sys
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from test_approvals import login
from test_execution import approved, read_job, request, running, submit
from test_supervisor import internal_key as internal_key

from commerce import adapters
from commerce import approval_workflow as flow
from commerce import execution_repository as repo
from commerce import execution_worker as worker
from commerce.db import connect_sync
from commerce.mcp_security import service_key
from commerce.seed_1301 import seed_preview_policies
from commerce.sim_business import app as sim


@pytest.fixture(scope="module", autouse=True)
def policies(demo_db):
    seed_preview_policies()


@pytest.fixture(autouse=True)
def checkpoint(monkeypatch):
    monkeypatch.setattr(flow, "approval_graph", flow.graph.compile(checkpointer=InMemorySaver()))


@pytest.fixture
def http_backend(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setenv("BUSINESS_BACKEND", "http")

    def local_client(*args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=sim)
        return original(*args, **kwargs)

    monkeypatch.setattr(adapters.httpx, "AsyncClient", local_client)


def test_http_order_pagination_preserves_query_parameters(client, http_backend):
    login(client)
    first = client.get("/api/v1/orders?offset=0&limit=3").json()
    second = client.get("/api/v1/orders?offset=3&limit=3").json()
    assert first["limit"] == second["limit"] == 3
    assert second["offset"] == 3
    assert len(first["items"]) == len(second["items"]) == 3
    assert not ({row["id"] for row in first["items"]} & {row["id"] for row in second["items"]})
    assert client.get("/api/v1/inventory").status_code == 403
    login(client, "staff001")
    assert len(client.get("/api/v1/inventory").json()["items"]) == 60


@pytest.mark.parametrize("kind", ["refund", "exchange", "reship", "repair"])
@pytest.mark.asyncio
async def test_four_flows_cross_http_adapter_and_reconcile(client, http_backend, kind):
    app = await approved(client, kind)
    job = running(submit(client, app))
    await repo.create_work_order(request(job))
    first = await repo.execute_action(request(job, kind))
    second = await repo.execute_action(request(job, kind))
    assert first == second and first.status == "completed"
    await worker.settle(job["id"])
    assert read_job(job)["status"] == "completed"
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.action_results WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["n"]
            == 1
        )
    if kind in ("exchange", "reship"):
        value = dict(event_id=str(uuid4()), application_id=app["id"], status="delivered")
        assert (await adapters.remote("POST", "/callbacks/shipment", body=value))[
            "message"
        ] == "已记录"
        assert (await adapters.remote("POST", "/callbacks/shipment", body=value))[
            "message"
        ] == "已记录"
        with connect_sync() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM commerce.provider_events WHERE event_id=%s",
                    (value["event_id"],),
                ).fetchone()["n"]
                == 1
            )


@pytest.mark.asyncio
async def test_response_loss_queries_receipt_without_duplicate_refund(
    client, http_backend, monkeypatch
):
    app = await approved(client)
    job = running(submit(client, app))
    await repo.create_work_order(request(job))
    monkeypatch.setenv("SIM_FAILURE_MODE", "after")
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await repo.execute_action(request(job, "refund"))
    await worker.settle(job["id"], failure=True)
    assert read_job(job)["status"] == "completed"
    monkeypatch.setenv("SIM_FAILURE_MODE", "none")
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.refund_ledger WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["n"]
            == 1
        )


@pytest.mark.asyncio
async def test_failed_action_has_no_ledger_and_reservations_release(
    client, http_backend, monkeypatch
):
    app = await approved(client, "exchange")
    job = running(submit(client, app))
    await repo.create_work_order(request(job))
    monkeypatch.setenv("SIM_FAILURE_MODE", "before")
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await repo.execute_action(request(job, "exchange"))
    with connect_sync() as conn:
        conn.execute("UPDATE commerce.execution_jobs SET attempts=3 WHERE id=%s", (job["id"],))
    await worker.settle(job["id"], failure=True)
    assert read_job(job)["status"] == "failed"
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT status FROM commerce.inventory_reservations WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["status"]
            == "released"
        )
        assert not conn.execute(
            "SELECT 1 FROM commerce.action_results WHERE application_id=%s", (app["id"],)
        ).fetchone()


def test_simulated_api_requires_internal_auth_and_checks_ownership(client):
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(sim, backend_options=options) as simulation:
        assert (
            simulation.get("/orders/ORD-200001", params={"actor_id": "USR-001"}).status_code == 403
        )
        simulation.headers["Authorization"] = "Bearer " + service_key()
        assert (
            simulation.get("/orders/ORD-200001", params={"actor_id": "USR-002"}).status_code == 404
        )
        assert (
            simulation.get("/orders/ORD-200001", params={"actor_id": "USR-001"}).status_code == 200
        )
        assert (
            simulation.get("/orders", params={"actor_id": "USR-001", "limit": 999}).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_manual_reconciliation_only_reads_existing_receipt(client):
    app = await approved(client)
    job = running(submit(client, app))
    await repo.create_work_order(request(job))
    await repo.execute_action(request(job, "refund"))
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.execution_jobs SET status='manual_review' WHERE id=%s", (job["id"],)
        )
        conn.execute(
            "UPDATE commerce.applications SET execution_state='manual_review' WHERE id=%s",
            (app["id"],),
        )
    response = client.post(
        f"/api/v1/applications/{app['id']}/reconcile", json={"note": "核对退款账本与工单一致"}
    )
    assert response.status_code == 200
    assert read_job(job)["status"] == "completed"
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.refund_ledger WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["n"]
            == 1
        )


@pytest.mark.asyncio
async def test_manual_reconciliation_missing_receipt_cannot_resume(client):
    app = await approved(client)
    job = running(submit(client, app))
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.execution_jobs SET status='manual_review' WHERE id=%s", (job["id"],)
        )
        conn.execute(
            "UPDATE commerce.applications SET execution_state='manual_review' WHERE id=%s",
            (app["id"],),
        )
    response = client.post(
        f"/api/v1/applications/{app['id']}/reconcile",
        json={"note": "暂未找到退款账本，继续人工核对"},
    )
    assert response.status_code == 200
    assert read_job(job)["status"] == "manual_review"
