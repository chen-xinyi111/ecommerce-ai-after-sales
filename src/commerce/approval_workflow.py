"""Durable approval interrupt. The gate has no side effects and no write MCP tools."""

import asyncio
import logging
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from commerce import approval_repository as repo
from commerce.db import connect
from commerce.models import Principal

logger = logging.getLogger(__name__)


class ApprovalState(TypedDict):
    application_id: str
    decision: str | None


async def wait_for_approval(state):
    # No database writes before interrupt: this node restarts on resume.
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM commerce.applications WHERE id=%s", (state["application_id"],)
        )
        app = await cursor.fetchone()
        assert app is not None
    response = interrupt(
        {
            "application_id": str(app["id"]),
            "preview_id": str(app["preview_id"]),
            "preview_version": app["preview_version"],
            "content_sha256": app["content_sha256"],
            "label": "等待客服独立审批与恢复",
        }
    )
    if not isinstance(response, dict) or response.get("application_id") != str(app["id"]):
        raise ValueError("Invalid approval resume binding")
    return {"decision": app["decision"]}


async def finalize(state):
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM commerce.applications WHERE id=%s", (state["application_id"],)
        )
        initial = await cursor.fetchone()
        assert initial is not None
        await repo.lock_scope(conn, initial["user_id"], initial["order_id"])
        cursor = await conn.execute(
            "SELECT * FROM commerce.applications WHERE id=%s FOR UPDATE", (initial["id"],)
        )
        app = await cursor.fetchone()
        assert app is not None
        if app["status"] not in ("decision_recorded", "rejected"):
            if app["status"] == "pending_approval":
                raise ValueError("No authoritative staff decision")
            return {"decision": app["status"]}
        cursor = await conn.execute(
            "SELECT id,username,display_name,role FROM commerce.users WHERE id=%s AND role='staff' AND active",
            (app["decision_actor"],),
        )
        staff = await cursor.fetchone()
        if not staff:
            raise ValueError("Staff authorization no longer valid")
        cursor = await conn.execute(
            "SELECT 1 FROM commerce.users WHERE id=%s AND active", (app["user_id"],)
        )
        if not await cursor.fetchone():
            raise ValueError("Applicant no longer active")
        if app["decision"] == "approved":
            if await repo.validate_current(conn, app, Principal(**staff)):
                return {"decision": "conditions_changed"}
            await conn.execute(
                "UPDATE commerce.applications SET status='approved',updated_at=now() WHERE id=%s",
                (app["id"],),
            )
            label = "双重确认完成；客服可发起模拟业务执行"
        else:
            label = "客服已拒绝，申请停止"
        await repo.append_event(
            conn,
            app["id"],
            "finalized",
            app["decision"],
            app["decision_actor"],
            label=label,
            execution_state="not_started",
        )
        return {"decision": app["decision"]}


graph = StateGraph(ApprovalState)
graph.add_node("wait_for_approval", wait_for_approval)
graph.add_node("finalize", finalize)
graph.add_edge(START, "wait_for_approval")
graph.add_edge("wait_for_approval", "finalize")
graph.add_edge("finalize", END)
approval_graph = graph.compile()


def config_for(app_id):
    return {"configurable": {"thread_id": f"commerce-approval:{app_id}"}, "recursion_limit": 8}


async def recover_interrupted():
    async with connect() as conn:
        cursor = await conn.execute(
            "UPDATE commerce.applications SET workflow_status='queued' WHERE workflow_status='running' RETURNING id"
        )
        for app in await cursor.fetchall():
            await repo.append_event(
                conn,
                app["id"],
                f"recovery:{uuid4()}",
                "worker_recovered",
                label="服务重启，恢复已持久化的审批任务",
            )


async def claim():
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM commerce.applications WHERE workflow_status='queued' ORDER BY confirmed_at FOR UPDATE SKIP LOCKED LIMIT 1"
        )
        app = await cursor.fetchone()
        if app:
            await conn.execute(
                "UPDATE commerce.applications SET workflow_status='running' WHERE id=%s",
                (app["id"],),
            )
        return app


async def process(app):
    try:
        config = config_for(app["id"])
        state = await approval_graph.aget_state(config)
        if app["status"] in ("cancelled", "superseded", "conditions_changed"):
            paused = False
        elif not state.values:
            await approval_graph.ainvoke(
                {"application_id": str(app["id"]), "decision": None}, config
            )
            paused = True
        elif state.next and app["resume_requested"]:
            await approval_graph.ainvoke(Command(resume={"application_id": str(app["id"])}), config)
            paused = False
        elif state.next:
            paused = True
        else:
            paused = False
        async with connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM commerce.applications WHERE id=%s FOR UPDATE", (app["id"],)
            )
            current = await cursor.fetchone()
            assert current is not None
            if current["status"] in ("cancelled", "superseded", "conditions_changed"):
                paused = False
            await conn.execute(
                "UPDATE commerce.applications SET workflow_status=%s,resume_requested=false,error_code=NULL,updated_at=now() WHERE id=%s",
                ("waiting" if paused else "complete", app["id"]),
            )
            if paused:
                await repo.append_event(
                    conn,
                    app["id"],
                    "waiting",
                    "interrupt_waiting",
                    label="LangGraph Interrupt 已持久化，等待客服审批",
                    checkpoint_thread=config["configurable"]["thread_id"],
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Approval workflow stopped (%s)", type(exc).__name__)
        async with connect() as conn:
            await conn.execute(
                "UPDATE commerce.applications SET workflow_status='failed',error_code='workflow_unavailable',updated_at=now() WHERE id=%s",
                (app["id"],),
            )
            await repo.append_event(
                conn,
                app["id"],
                f"failure:{uuid4()}",
                "workflow_failed",
                label="审批流程暂时中断，客服可通过独立恢复接口重试",
            )


async def worker():
    while True:
        try:
            async with connect() as lease:
                await lease.set_autocommit(True)
                cursor = await lease.execute("SELECT pg_try_advisory_lock(1102201) AS acquired")
                locked = await cursor.fetchone()
                assert locked is not None
                if not locked["acquired"]:
                    await asyncio.sleep(1)
                    continue
                await recover_interrupted()
                while True:
                    await lease.execute("SELECT 1")
                    app = await claim()
                    if app:
                        await asyncio.wait_for(process(app), 45)
                    else:
                        await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Approval worker unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(1)
