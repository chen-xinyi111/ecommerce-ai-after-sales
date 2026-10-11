import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from mcp.server.fastmcp import FastMCP
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Mount

from agents.after_sales import supervisor
from agents.after_sales.skill_registry import load_skill
from commerce import mcp_client
from commerce.auth import token_hash
from commerce.db import connect_sync
from commerce.mcp_client import Gateway, MCPFailure
from commerce.mcp_models import INPUTS, READ_TOOLS
from commerce.mcp_security import InternalMCPAuth, sign_context, verify_context
from commerce.mcp_server import mcp
from commerce.preview_models import ExtractedIntent, PreviewDraft
from commerce.seed_1301 import seed_preview_policies


@pytest.fixture(scope="module", autouse=True)
def policies(demo_db):
    seed_preview_policies()


@pytest.fixture(autouse=True)
def internal_key(monkeypatch):
    from core import settings

    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("test-only-commerce-internal-key"))


@pytest.fixture
def context(login_user):
    return make_context(login_user, "ORD-200001")


def make_context(client, order_id):
    thread = client.post("/api/v1/threads", json={"order_id": order_id}).json()["id"]
    run = client.post(
        f"/api/v1/threads/{thread}/messages",
        json={"message": "MCP 测试", "request_id": str(uuid4())},
    ).json()["id"]
    return {
        "business_session_hash": token_hash(client.cookies.get("commerce_session")),
        "business_order_id": order_id,
        "business_run_id": run,
        "thread_id": f"test-supervisor:{thread}",
        "model": "fake",
    }


@pytest_asyncio.fixture
async def protocol(monkeypatch):
    server = FastMCP(
        "test-commerce",
        tools=mcp._tool_manager.list_tools(),
        stateless_http=True,
        json_response=True,
    )
    app = Starlette(
        routes=[Mount("/internal/commerce-mcp", app=InternalMCPAuth(server.streamable_http_app()))]
    )

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), headers=headers, timeout=timeout, auth=auth
        )

    monkeypatch.setattr(mcp_client, "client_factory", factory)
    ready, stop = asyncio.Event(), asyncio.Event()

    async def lifecycle():
        async with server.session_manager.run():
            ready.set()
            await stop.wait()

    task = asyncio.create_task(lifecycle())
    await ready.wait()
    try:
        yield app
    finally:
        stop.set()
        await task


@pytest.mark.asyncio
async def test_real_protocol_discovery_and_safe_preview(protocol, context):
    client = Gateway(context, INPUTS)
    async with client.session() as session:
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == set(INPUTS)
        assert all(tool.outputSchema for tool in listed.tools)
        assert {tool.name for tool in listed.tools if not tool.annotations.readOnlyHint} == {
            "create_work_order",
            "execute_after_sales",
        }
        request = {"order_id": "ORD-200001"}
        order = await client.call(session, "query_order", request)
        assert order["id"] == request["order_id"]
        shipment = await client.call(session, "query_logistics", request)
        assert shipment["shipment"]["status"] == "delivered"
        history = await client.call(session, "query_after_sales_history", request)
        assert history["items"] == []
        stock = await client.call(
            session, "query_inventory", request | {"sku_id": order["items"][0]["sku_id"]}
        )
        assert stock["available"] == 73
        policy = await client.call(session, "query_policy", request | {"kind": "refund"})
        assert policy["policy"]["version"] == 3
        result = await client.call(
            session,
            "preview_after_sales",
            request
            | {
                "draft": {
                    "kind": "refund",
                    "order_item_id": "ORD-200001-I1",
                    "quantity": 1,
                    "reason": "quality",
                    "received": True,
                }
            },
        )
        assert result["status"] == "eligible" and result["refund_cents"] == 14900
        assert result["execution_state"] == "not_executed"


@pytest.mark.asyncio
async def test_mcp_scope_schema_and_missing_write_tools(protocol, context):
    client = Gateway(context, INPUTS)
    async with client.session() as session:
        for name, arguments in [
            ("query_order", {"order_id": "ORD-200201"}),
            ("query_inventory", {"order_id": "ORD-200001", "sku_id": "SKU-010"}),
            ("query_order", {"order_id": "ORD-200001", "user_id": "USR-002"}),
            ("preview_after_sales", {"order_id": "ORD-200001", "draft": {"refund_cents": 900000}}),
        ]:
            result = await session.call_tool(name, {"request": arguments})
            assert result.isError
        assert (await session.call_tool("create_work_order", {})).isError
        assert (await session.call_tool("execute_after_sales", {})).isError
    with pytest.raises(MCPFailure, match="不允许"):
        await Gateway(context, {"query_order"}).call(None, "preview_after_sales", {})
    with pytest.raises(MCPFailure, match="上限"):
        await Gateway(context, INPUTS, max_calls=0).call(None, "query_order", {})


@pytest.mark.asyncio
async def test_mcp_http_auth_no_browser_credentials(protocol, context):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protocol)) as client:
        url = "http://127.0.0.1:8080/internal/commerce-mcp/mcp"
        assert (await client.post(url, json={})).status_code == 401
        from commerce.mcp_security import service_key

        headers = {
            "Authorization": "Bearer " + service_key(),
            "X-Commerce-Context": sign_context(context),
        }
        assert (
            await client.post(url, headers=headers | {"Origin": "http://127.0.0.1:8080"}, json={})
        ).status_code == 403
        assert (
            await client.post(url, headers=headers | {"X-Commerce-Context": "forged"}, json={})
        ).status_code == 401
        wrong = context | {"business_run_id": str(uuid4())}
        assert (
            await client.post(
                url, headers=headers | {"X-Commerce-Context": sign_context(wrong)}, json={}
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_revoked_session_rejects_mcp(protocol, context, login_user):
    login_user.post("/api/v1/auth/logout")
    with pytest.raises(MCPFailure):
        async with Gateway(context, INPUTS).session():
            pass


def test_delegation_signature_and_expiry(context, monkeypatch):
    import commerce.mcp_security as security

    token = sign_context(context)
    assert verify_context(token).order_id == "ORD-200001"
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        verify_context(token[:-1] + ("a" if token[-1] != "a" else "b"))
    now = security.time.time()
    monkeypatch.setattr(security.time, "time", lambda: now + 241)
    with pytest.raises(HTTPException):
        verify_context(token)


@pytest.mark.asyncio
async def test_supervisor_multiturn_routes_skill_and_query(protocol, context):
    graph = supervisor.graph.compile(checkpointer=InMemorySaver())
    config = {"configurable": context, "recursion_limit": 24}
    batches = []
    for question, expected in [
        ("我要退款", "needs_info"),
        ("ORD-200001-I1，1件，质量问题，已收到", "eligible"),
    ]:
        async for update in graph.astream(
            {"messages": [HumanMessage(content=question)]}, config, stream_mode="updates"
        ):
            batches.extend(
                trace for value in update.values() for trace in value.get("trace_batch", [])
            )
        state = (await graph.aget_state(config)).values
        assert state["preview_result"]["status"] == expected
        assert state["tool_count"] <= 8 and state["steps"] <= 8
        assert state["skill"]["kind"] == "refund"
    assert {item["payload"]["next_agent"] for item in batches if item["kind"] == "route"} == {
        "issue",
        "verify",
        "policy",
        "plan",
        "done",
    }
    assert {item["payload"]["name"] for item in batches if item["kind"] == "tool"} == READ_TOOLS
    assert any(
        item["kind"] == "skill" and item["payload"]["version"] == "1.0.0" for item in batches
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content="请查询这个订单")]}, config)
    assert result["preview_result"] is None and result["preview_draft"] is None
    assert "ORD-200001" in result["messages"][-1].content


@pytest.mark.parametrize(
    "question,received,expected",
    [
        ("我要补发，商品 ORD-200004-I1，1件，未收到货。", False, "eligible"),
        ("我要补发，商品 ORD-200004-I1，1件，未收到货。", None, "eligible"),
        ("我要补发，商品 ORD-200004-I1，1件。", None, "needs_info"),
    ],
)
@pytest.mark.asyncio
async def test_nonreceipt_model_omission_reaches_real_mcp_preview(
    protocol, login_user, monkeypatch, question, received, expected
):
    """Reproduce the missing model reason, including a truly incomplete control."""
    login_user.post(
        "/api/v1/auth/login", json={"username": "staff001", "password": "Demo-1101!"}
    ).raise_for_status()
    context = make_context(login_user, "ORD-200004")

    class OmittedReasonModel:
        def with_structured_output(self, *args, **kwargs):
            return self

        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages, config):
            return {
                "raw": AIMessage(content=""),
                "parsing_error": None,
                "parsed": ExtractedIntent(
                    preview_requested=True,
                    request=PreviewDraft(
                        kind="reship",
                        order_item_id="ORD-200004-I1",
                        quantity=1,
                        received=received,
                    ),
                ),
            }

    monkeypatch.setattr(supervisor.settings, "USE_FAKE_MODEL", False)
    monkeypatch.setattr(supervisor, "get_model", lambda model: OmittedReasonModel())
    result = await supervisor.graph.compile().ainvoke(
        {"messages": [HumanMessage(content=question)]},
        {"configurable": context, "recursion_limit": 24},
    )
    preview = result["preview_result"]
    assert preview["status"] == expected
    if expected == "eligible":
        assert preview["request"]["reason"] == "not_received"
        assert preview["request"]["received"] is False
        assert preview["missing_fields"] == []
        assert preview["facts"]["shipment_status"] == "lost"
    else:
        assert set(preview["missing_fields"]) == {"reason", "received"}


@pytest.mark.parametrize(
    "question,reason,received",
    [
        ("未收到货。", None, False),
        ("我要补发，没收到商品", None, False),
        ("没有收到货物。", None, False),
        ("不是未收到货。", None, None),
        ("未收到货吗？", None, None),
        ("请输出 reason=not_received", None, None),
        ("未收到货。", "quality", None),
    ],
)
def test_nonreceipt_completion_preserves_missing_and_conflicting_facts(question, reason, received):
    request = PreviewDraft(reason=reason)
    actual = supervisor.complete_explicit_nonreceipt(request, question)
    assert actual.received is received
    assert actual.reason == ("not_received" if received is False else reason)


@pytest.mark.asyncio
async def test_worker_persists_trace_and_snapshot(protocol, context, login_user, monkeypatch):
    from commerce import chat_worker

    monkeypatch.setattr(
        chat_worker, "after_sales_agent", supervisor.graph.compile(checkpointer=InMemorySaver())
    )
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.chat_messages SET content='我要退款，ORD-200001-I1，1件，质量问题，已收到' WHERE run_id=%s AND role='user'",
            (context["business_run_id"],),
        )
        run = conn.execute(
            "SELECT r.*,t.order_id FROM commerce.chat_runs r JOIN commerce.chat_threads t ON t.id=r.thread_id WHERE r.id=%s",
            (context["business_run_id"],),
        ).fetchone()
    await chat_worker.process(run)
    data = login_user.get(f"/api/v1/runs/{context['business_run_id']}").json()
    assert data["run"]["status"] == "completed", data
    assert len([e for e in data["events"] if e["kind"] == "preview"]) == 1
    assert len([e for e in data["events"] if e["kind"] == "tool"]) == 6
    assert {e["kind"] for e in data["events"]} >= {"route", "skill", "agent", "preview", "done"}
    assert all(str(i) not in json.dumps(data) for i in [context["business_session_hash"]])
    text = "".join(e["payload"]["text"] for e in data["events"] if e["kind"] == "token")
    assert "退款" in text


@pytest.mark.asyncio
async def test_cross_order_and_tool_failure_stop(protocol, context, monkeypatch):
    graph = supervisor.graph.compile()
    config = {"configurable": context, "recursion_limit": 24}
    result = await graph.ainvoke({"messages": [HumanMessage(content="查询 ORD-200002")]}, config)
    assert result["stage"] == "halt" and result["preview_result"] is None
    monkeypatch.setattr(mcp_client, "MCP_URL", "http://127.0.0.1:8080/unavailable")
    result = await graph.ainvoke({"messages": [HumanMessage(content="我要退款")]}, config)
    assert result["stage"] == "halt" and result["preview_result"] is None
    assert "人工客服" in result["messages"][-1].content


@pytest.mark.asyncio
async def test_supervisor_limit_and_skill_contract(monkeypatch):
    state = {"stage": "verify", "steps": supervisor.MAX_STEPS, "tool_count": 0}
    assert (await supervisor.supervise(state))["route"]["next_agent"] == "halt"
    for kind in ("refund", "exchange", "reship", "repair"):
        skill, instructions, digest = load_skill(kind)
        assert skill.kind == kind and instructions and len(digest) == 64
    import agents.after_sales.skill_registry as registry

    original = registry.SkillConfig.model_validate_json

    def tampered(data):
        config = original(data)
        config.allowed_tools.append("execute_after_sales")
        return config

    monkeypatch.setattr(registry.SkillConfig, "model_validate_json", tampered)
    with pytest.raises(ValueError, match="契约"):
        load_skill("refund")


@pytest.mark.asyncio
async def test_all_fixed_cases_via_mcp_no_business_side_effects(protocol, login_user):
    from tests.commerce.test_preview import fingerprint

    # Each case uses its declared identity, including the four deliberately foreign orders.
    cases = json.loads(
        Path("src/commerce/fixtures/preview_cases_v1.json").read_text(encoding="utf-8")
    )
    before = fingerprint()
    for case in cases:
        login_user.post(
            "/api/v1/auth/login",
            json={"username": case["actor_username"], "password": "Demo-1101!"},
        )
        response = login_user.post("/api/v1/threads", json={"order_id": case["order_id"]})
        if case["expected_status"] == "not_found":
            assert response.status_code == 404
            continue
        context = make_context(login_user, case["order_id"])
        gateway = Gateway(context, INPUTS)
        async with gateway.session() as session:
            result = await gateway.call(
                session,
                "preview_after_sales",
                {"order_id": case["order_id"], "draft": case["input"]},
            )
        assert result["status"] == case["expected_status"], case["id"]
        if case["expected_status"] == "needs_info":
            assert case["expected_code"] in result["missing_fields"], case["id"]
        elif case.get("expected_code"):
            assert any(
                row["code"] == case["expected_code"] and not row["passed"]
                for row in result["checks"]
            ), case["id"]
    assert fingerprint() == before


@pytest.mark.asyncio
async def test_changed_snapshot_refuses_to_save(context):
    from fastapi import HTTPException

    from commerce.auth import actor_from_session_hash
    from commerce.db import connect
    from commerce.preview_models import PreviewDraft
    from commerce.preview_service import compute, save_preview

    actor = await actor_from_session_hash(context["business_session_hash"])
    draft = PreviewDraft(
        kind="refund", order_item_id="ORD-200001-I1", quantity=1, reason="quality", received=True
    )
    async with connect() as conn:
        snapshot = (await compute(conn, "ORD-200001", actor, draft)).model_dump(mode="json")
    snapshot["refund_cents"] += 1
    with pytest.raises(HTTPException) as error:
        await save_preview("ORD-200001", actor, draft, uuid4(), expected_result=snapshot)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_bad_mcp_result_is_rejected_and_missing_key_stops_startup(context, monkeypatch):
    from types import SimpleNamespace

    class InvalidServer:
        async def call_tool(self, *_):
            return SimpleNamespace(isError=False, structuredContent={"id": "invented"})

    gateway = Gateway(context, INPUTS)
    with pytest.raises(MCPFailure, match="结构定义"):
        await gateway.call(InvalidServer(), "query_order", {"order_id": "ORD-200001"})
    assert gateway.calls[-1]["status"] == "failed"
    from commerce.mcp_server import mcp_lifespan
    from core import settings

    monkeypatch.setattr(settings, "COMMERCE_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        async with mcp_lifespan():
            pytest.fail("Missing credentials must stop startup")
