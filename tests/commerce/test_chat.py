import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.tools import ToolException
from langgraph.checkpoint.memory import InMemorySaver

from commerce.auth import token_hash
from commerce.chat_worker import claim, process, recover_interrupted
from commerce.db import connect_sync


def thread(client, order_id="ORD-200001"):
    response = client.post("/api/v1/threads", json={"order_id": order_id})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def submit(client, thread_id, message="查询订单", request_id=None):
    return client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={
            "message": message,
            "request_id": request_id or str(uuid4()),
        },
    )


def test_chat_requires_login(client):
    assert client.get("/api/v1/threads").status_code == 401
    assert client.post("/api/v1/threads", json={"order_id": "ORD-200001"}).status_code == 401


def test_thread_order_and_history_isolation(login_user):
    client = login_user
    assert client.post("/api/v1/threads", json={"order_id": "ORD-200002"}).status_code == 404
    own = thread(client)
    run = submit(client, own).json()["id"]
    assert client.get(f"/api/v1/threads/{own}").json()["messages"][0]["content"] == "查询订单"
    client.post("/api/v1/auth/login", json={"username": "user002", "password": "Demo-1101!"})
    for path in (f"/threads/{own}", f"/runs/{run}", f"/runs/{run}/events"):
        assert client.get("/api/v1" + path).status_code == 404
    assert client.get("/api/v1/threads").json()["items"] == []
    assert submit(client, own).status_code == 404
    client.post("/api/v1/auth/login", json={"username": "staff001", "password": "Demo-1101!"})
    assert client.get(f"/api/v1/threads/{own}").status_code == 404
    assert thread(client, "ORD-200002")


def test_identity_and_model_cannot_be_injected(login_user):
    client = login_user
    assert (
        client.post(
            "/api/v1/threads", json={"order_id": "ORD-200001", "user_id": "USR-002"}
        ).status_code
        == 422
    )
    own = thread(client)
    assert (
        client.post(
            f"/api/v1/threads/{own}/messages",
            json={
                "message": "你好",
                "request_id": str(uuid4()),
                "model": "fake",
                "role": "staff",
            },
        ).status_code
        == 422
    )
    assert submit(client, own, " ").status_code == 422
    assert submit(client, own, "a" * 2001).status_code == 422
    assert (
        client.post(
            "/api/v1/threads",
            json={"order_id": "ORD-200001"},
            headers={"Origin": "https://other.invalid"},
        ).status_code
        == 403
    )


def test_message_idempotency_and_one_active_run(login_user):
    client = login_user
    own = thread(client)
    request_id = str(uuid4())
    first = submit(client, own, request_id=request_id)
    assert first.status_code == 202
    assert submit(client, own, request_id=request_id).json()["id"] == first.json()["id"]
    assert submit(client, own, "另一个问题", request_id).status_code == 409
    assert submit(client, own).status_code == 409
    history = client.get(f"/api/v1/threads/{own}").json()
    assert len(history["messages"]) == 2 and len(history["runs"]) == 1
    assert "session_hash" not in first.json()


@pytest.mark.asyncio
async def test_restart_marks_partial_and_replays_events(login_user):
    client = login_user
    own = thread(client)
    run = submit(client, own).json()["id"]
    with connect_sync() as conn:
        conn.execute("UPDATE commerce.chat_runs SET status='running' WHERE id=%s", (run,))
        conn.execute(
            "UPDATE commerce.chat_messages SET content='已收到部分回复' WHERE run_id=%s AND role='assistant'",
            (run,),
        )
    await recover_interrupted()
    result = client.get(f"/api/v1/runs/{run}").json()
    assert (
        result["run"]["status"] == "failed" and result["run"]["error_code"] == "service_restarted"
    )
    assert [event["seq"] for event in result["events"]] == [1, 2]
    stream = client.get(f"/api/v1/runs/{run}/events", headers={"Last-Event-ID": "1"})
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "id: 2" in stream.text and "id: 1" not in stream.text
    assert "service_restarted" in stream.text
    assert (
        client.get(f"/api/v1/runs/{run}/events", headers={"Last-Event-ID": "-1"}).status_code == 400
    )
    assert (
        client.get(f"/api/v1/runs/{run}/events", headers={"Last-Event-ID": "bad"}).status_code
        == 400
    )
    history = client.get(f"/api/v1/threads/{own}").json()
    assert history["messages"][1]["content"] == "已收到部分回复"
    assert history["messages"][1]["status"] == "failed"


@pytest.mark.asyncio
async def test_order_tool_is_bound_to_selected_order(login_user):
    from agents.after_sales.read_tools import query_business_order

    config = {
        "configurable": {
            "business_session_hash": token_hash(login_user.cookies.get("commerce_session")),
            "business_order_id": "ORD-200001",
        }
    }
    with pytest.raises(ToolException, match="选定订单"):
        await query_business_order.ainvoke({"order_id": "ORD-200201"}, config=config)


@pytest.mark.asyncio
async def test_offline_worker_uses_db_and_persists_stream(login_user, monkeypatch):
    from agents.after_sales.agent import graph
    from commerce import chat_worker

    monkeypatch.setattr(
        chat_worker, "settings", SimpleNamespace(USE_FAKE_MODEL=True, DEFAULT_MODEL="fake")
    )
    monkeypatch.setattr(
        chat_worker, "after_sales_agent", graph.compile(checkpointer=InMemorySaver())
    )
    client = login_user
    own = thread(client)
    run_id = submit(client, own).json()["id"]
    # Older test-created queued tasks are failed to isolate the queue consumer.
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.chat_runs SET status='failed' WHERE status='queued' AND id<>%s",
            (run_id,),
        )
    run = await claim()
    assert str(run["id"]) == run_id
    await process(run)
    history = client.get(f"/api/v1/threads/{own}").json()
    assert history["runs"][0]["status"] == "completed"
    assert "ORD-200001" in history["messages"][1]["content"]
    result = client.get(f"/api/v1/runs/{run_id}").json()
    assert result["run"]["usage"]["simulated_model"] is True
    assert any(event["kind"] == "tool" for event in result["events"])
    tokens = [event["payload"]["text"] for event in result["events"] if event["kind"] == "token"]
    assert len(tokens) > 1
    assert "".join(tokens) == history["messages"][1]["content"]
    stream = client.get(f"/api/v1/runs/{run_id}/events").text
    data = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
    assert len({item["seq"] for item in data}) == len(data)
    assert data[-1]["kind"] == "done"


@pytest.mark.asyncio
async def test_expired_session_fails_queued_run(login_user):
    client = login_user
    own = thread(client)
    run_id = submit(client, own).json()["id"]
    client.post("/api/v1/auth/logout")
    with connect_sync() as conn:
        run = conn.execute(
            "SELECT r.*,t.order_id FROM commerce.chat_runs r JOIN commerce.chat_threads t ON t.id=r.thread_id WHERE r.id=%s",
            (run_id,),
        ).fetchone()
    await process(run)
    with connect_sync() as conn:
        result = conn.execute(
            "SELECT status,error_code FROM commerce.chat_runs WHERE id=%s", (run_id,)
        ).fetchone()
    assert result == {"status": "failed", "error_code": "session_expired"}
    assert client.get(f"/api/v1/runs/{run_id}").status_code == 401


def test_concurrent_duplicate_submit_creates_one_run(login_user):
    own = thread(login_user)
    request_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(login_user, own, request_id=request_id), range(8)))
    assert all(response.status_code == 202 for response in results)
    assert len({response.json()["id"] for response in results}) == 1
    history = login_user.get(f"/api/v1/threads/{own}").json()
    assert len(history["messages"]) == 2 and len(history["runs"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code",
    [(TimeoutError(), "model_timeout"), (RuntimeError("PRIVATE_SENTINEL"), "model_unavailable")],
)
async def test_worker_failure_is_persisted_without_private_details(
    login_user, monkeypatch, error, code
):
    from commerce import chat_worker

    async def broken(run):
        raise error

    monkeypatch.setattr(chat_worker, "execute", broken)
    own = thread(login_user)
    run_id = submit(login_user, own).json()["id"]
    with connect_sync() as conn:
        run = conn.execute("SELECT * FROM commerce.chat_runs WHERE id=%s", (run_id,)).fetchone()
    await process(run)
    result = login_user.get(f"/api/v1/runs/{run_id}").json()
    assert result["run"]["status"] == "failed"
    assert result["run"]["error_code"] == code
    assert result["events"][-1]["kind"] == "error"
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


@pytest.mark.asyncio
async def test_model_mode_change_does_not_mislabel_real_usage(login_user, monkeypatch):
    from commerce import chat_worker

    monkeypatch.setattr(
        chat_worker, "settings", SimpleNamespace(USE_FAKE_MODEL=False, DEFAULT_MODEL="qwen3.8-max")
    )
    own = thread(login_user)
    run_id = submit(login_user, own).json()["id"]
    with connect_sync() as conn:
        run = conn.execute("SELECT * FROM commerce.chat_runs WHERE id=%s", (run_id,)).fetchone()
    assert run["model"] == "fake"
    await process(run)
    assert login_user.get(f"/api/v1/runs/{run_id}").json()["run"]["error_code"] == "model_changed"


@pytest.mark.asyncio
async def test_failed_run_preserves_observed_usage(login_user):
    from psycopg.types.json import Jsonb

    from commerce.chat_repository import finish

    own = thread(login_user)
    run_id = submit(login_user, own).json()["id"]
    usage = {"simulated_model": True, "calls": [{"total_tokens": 12}]}
    with connect_sync() as conn:
        conn.execute("UPDATE commerce.chat_runs SET usage=%s WHERE id=%s", (Jsonb(usage), run_id))
    await finish(run_id, error_code="model_timeout")
    result = login_user.get(f"/api/v1/runs/{run_id}").json()
    assert result["run"]["usage"] == usage
    assert result["events"][-1]["payload"]["usage"] == usage
