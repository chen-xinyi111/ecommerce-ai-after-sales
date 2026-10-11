import asyncio
import json
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from redis.exceptions import ConnectionError
from test_approvals import login
from test_execution import approved, read_job, submit
from test_supervisor import internal_key as internal_key
from test_supervisor import protocol as protocol

from commerce import approval_workflow
from commerce import async_runtime as runtime
from commerce import execution_repository as repo
from commerce.approval_models import SnapshotInput
from commerce.db import connect, connect_sync
from commerce.observability_api import router
from commerce.seed_1301 import seed_preview_policies
from commerce.telemetry import span


@pytest.fixture(scope="module", autouse=True)
def policies(demo_db):
    seed_preview_policies()


@pytest.fixture(autouse=True)
def checkpoints(monkeypatch, client):
    monkeypatch.setattr(
        approval_workflow,
        "approval_graph",
        approval_workflow.graph.compile(checkpointer=InMemorySaver()),
    )
    client.app.include_router(router)


class NoRedis:
    async def set(self, *args, **kwargs):
        raise ConnectionError("offline")


class Producer:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def send_and_wait(self, topic, value, **kwargs):
        self.messages.append((topic, json.loads(value)))
        if self.fail:
            raise ConnectionError("unavailable")


def event_for(job):
    with connect_sync() as conn:
        return conn.execute(
            "SELECT * FROM commerce.task_outbox WHERE task_type='execution' AND task_id=%s ORDER BY generation DESC LIMIT 1",
            (job["id"],),
        ).fetchone()


@pytest.mark.asyncio
async def test_task_and_outbox_rollback_together(client, monkeypatch):
    app = await approved(client)
    actor = login(client, "staff001")
    original = repo.approval.record_command

    async def broken(*args, **kwargs):
        raise RuntimeError("rollback")

    monkeypatch.setattr(repo.approval, "record_command", broken)
    from test_approvals import binding

    with pytest.raises(RuntimeError):
        await repo.enqueue(app["id"], SnapshotInput(**binding(app)), actor)
    monkeypatch.setattr(repo.approval, "record_command", original)
    with connect_sync() as conn:
        assert not conn.execute(
            "SELECT * FROM commerce.execution_jobs WHERE application_id=%s", (app["id"],)
        ).fetchone()
        assert not conn.execute(
            "SELECT * FROM commerce.task_outbox o JOIN commerce.execution_jobs j ON j.id=o.task_id WHERE j.application_id=%s",
            (app["id"],),
        ).fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "exchange", "reship", "repair"])
async def test_duplicate_delivery_redis_down_and_trace(client, protocol, kind):
    app = await approved(client, kind)
    with span("api.test"):
        job = submit(client, app)
    event = event_for(job)
    assert event["generation"] == 1 and event["traceparent"]
    assert await runtime.handle(event["id"], NoRedis())
    assert await runtime.handle(event["id"], NoRedis())
    assert read_job(job)["status"] == "completed"
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.action_results WHERE application_id=%s",
                (app["id"],),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM commerce.task_inbox WHERE event_id=%s", (event["id"],)
            ).fetchone()["n"]
            == 1
        )
    response = client.get(f"/api/v1/applications/{app['id']}/trace")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["spans"]}
    assert {"api.test", "api.enqueue.execution", "worker.execution", "mcp.client.call"} <= names
    text = response.text
    assert "session_hash" not in text and "X-Commerce-Context" not in text
    login(client, "user002")
    assert client.get(f"/api/v1/applications/{app['id']}/trace").status_code == 404
    assert client.get("/api/v1/operations").status_code == 403


@pytest.mark.asyncio
async def test_concurrent_consumers_and_lost_ack(client, protocol):
    app = await approved(client)
    job = submit(client, app)
    event = event_for(job)
    from commerce.execution_worker import process

    gate = asyncio.Event()
    started = asyncio.Event()

    async def paused(task):
        started.set()
        await gate.wait()
        await process(task)

    first = asyncio.create_task(
        runtime.handle(event["id"], NoRedis(), processors={"execution": paused})
    )
    await started.wait()
    assert not await runtime.handle(event["id"], NoRedis())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert read_job(job)["status"] == "running"
    assert await runtime.handle(event["id"], NoRedis())
    assert read_job(job)["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_publish_and_staff_redrive(client):
    app = await approved(client)
    job = submit(client, app)
    event = event_for(job)
    # Drain unrelated approval messages from this isolated module's fixture.
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.task_outbox SET status='published' WHERE id<>%s", (event["id"],)
        )
    producer = Producer(fail=True)
    for _ in range(5):
        with connect_sync() as conn:
            conn.execute(
                "UPDATE commerce.task_outbox SET available_at=now() WHERE id=%s", (event["id"],)
            )
        assert await runtime.publish_one(producer)
    with connect_sync() as conn:
        dead = conn.execute(
            "SELECT * FROM commerce.dead_letters WHERE event_id=%s", (event["id"],)
        ).fetchone()
    assert not await runtime.publish_dead_one(producer)
    with connect_sync() as conn:
        assert (
            conn.execute(
                "SELECT delivered_at FROM commerce.dead_letters WHERE id=%s", (dead["id"],)
            ).fetchone()["delivered_at"]
            is None
        )
    producer.fail = False
    assert await runtime.publish_dead_one(producer)
    producer.fail = True
    assert event_for(job)["status"] == "dead"
    assert read_job(job)["status"] == "queued"
    login(client, "user001")
    assert (
        client.post(
            f"/api/v1/operations/dead-letters/{dead['id']}/redrive", json={"note": "恢复"}
        ).status_code
        == 403
    )
    login(client, "staff001")
    assert (
        client.post(
            f"/api/v1/operations/dead-letters/{dead['id']}/redrive", json={"note": "已核对服务恢复"}
        ).status_code
        == 200
    )
    assert event_for(job)["status"] == "pending"
    producer.fail = False
    assert await runtime.publish_one(producer)
    assert event_for(job)["status"] == "published"


@pytest.mark.asyncio
async def test_unknown_event_rejected_and_uncommitted_publish_replayed(client):
    with pytest.raises(ValueError):
        await runtime.handle(uuid4(), NoRedis())
    app = await approved(client)
    job = submit(client, app)
    event = event_for(job)
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.task_outbox SET status='published' WHERE id=%s", (event["id"],)
        )
    await runtime.recover()
    assert event_for(job)["status"] == "pending"
    async with connect() as conn:
        await conn.execute("INSERT INTO commerce.task_inbox(event_id) VALUES (%s)", (event["id"],))
        await conn.execute(
            "UPDATE commerce.task_outbox SET status='published' WHERE id=%s", (event["id"],)
        )
    await runtime.recover()
    assert event_for(job)["status"] == "published"
