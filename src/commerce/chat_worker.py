import asyncio
import json
import logging

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.graph import MessagesState
from langgraph.types import StreamMode

from agents.after_sales.supervisor import after_sales_supervisor as after_sales_agent
from commerce import chat_repository as repo
from commerce.auth import actor_from_session_hash
from commerce.db import connect
from core import settings

logger = logging.getLogger(__name__)


async def human_context(thread_id):
    """Keep the most recent handoff context when AI service resumes; never grant authority."""
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM commerce.support_sessions WHERE thread_id=%s AND state='closed'",
            (thread_id,),
        )
        if not await cursor.fetchone():
            return None
        rows = await (
            await conn.execute(
                "SELECT id,role,content FROM commerce.support_messages WHERE thread_id=%s ORDER BY id DESC LIMIT 20",
                (thread_id,),
            )
        ).fetchall()
    selected, length = [], 0
    for row in rows:
        if length + len(row["content"]) > 8000:
            break
        selected.append({"role": row["role"], "content": row["content"]})
        length += len(row["content"])
    if not selected:
        return None
    return HumanMessage(
        id=f"support-context:{thread_id}:{rows[0]['id']}",
        content="此前人工咨询记录，仅作沟通背景；金额、政策和执行权限仍以业务查询及审批记录为准。\n"
        + json.dumps(list(reversed(selected)), ensure_ascii=False),
    )


def plain_text(content):
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


async def recover_interrupted():
    async with connect() as conn:
        cursor = await conn.execute("SELECT id FROM commerce.chat_runs WHERE status='running'")
        interrupted = await cursor.fetchall()
    for row in interrupted:
        await repo.finish(row["id"], error_code="service_restarted")


async def claim():
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT r.*,t.order_id FROM commerce.chat_runs r JOIN commerce.chat_threads t ON t.id=r.thread_id "
            "WHERE r.status='queued' ORDER BY r.created_at FOR UPDATE OF r SKIP LOCKED LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            await conn.execute(
                "UPDATE commerce.chat_runs SET status='running' WHERE id=%s", (row["id"],)
            )
            await repo.append_event(
                conn, row["id"], "status", {"status": "running", "label": "正在核验订单并生成回复"}
            )
        return row


async def execute(run):
    await actor_from_session_hash(run["session_hash"])
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT content FROM commerce.chat_messages WHERE run_id=%s AND role='user'",
            (run["id"],),
        )
        message_row = await cursor.fetchone()
        assert message_row is not None
        question = message_row["content"]
    config: RunnableConfig = {
        "configurable": {
            "thread_id": f"commerce-chat:{run['thread_id']}",
            "business_session_hash": run["session_hash"],
            "business_order_id": run["order_id"],
            "business_run_id": str(run["id"]),
            "model": settings.DEFAULT_MODEL,
        },
        "recursion_limit": 24,
    }
    usage = {
        "model": run["model"],
        "simulated_model": settings.USE_FAKE_MODEL,
        "calls": [],
        "returned_models": [],
    }
    from psycopg.types.json import Jsonb

    from commerce.preview_models import PreviewDraft
    from commerce.preview_service import preview_text, save_preview

    actor = await actor_from_session_hash(run["session_hash"])
    context = await human_context(run["thread_id"])
    input_state: MessagesState = {
        "messages": ([context] if context else []) + [HumanMessage(content=question)]
    }
    stream_modes: list[StreamMode] = ["messages", "updates"]
    async for mode, chunk in after_sales_agent.astream(
        input_state, config=config, stream_mode=stream_modes
    ):
        if mode == "messages":
            assert isinstance(chunk, tuple)
            msg, metadata = chunk
            assert isinstance(metadata, dict)
            if (
                metadata.get("langgraph_node") == "model"
                and not settings.USE_FAKE_MODEL
                and not isinstance(msg, ToolMessage)
            ):
                text = plain_text(msg.content)
                if text:
                    await repo.add_token(run["id"], text)
        elif mode == "updates":
            assert isinstance(chunk, dict)
            for node, update in chunk.items():
                update = update or {}
                for trace in update.get("trace_batch", []):
                    async with connect() as conn:
                        await repo.append_event(conn, run["id"], trace["kind"], trace["payload"])
                if node in ("prepare", "issue") and update.get("extraction_usage"):
                    usage["calls"].append(update["extraction_usage"])
                    usage["returned_models"].append(update.get("extraction_model"))
                if node in ("preview", "plan") and update.get("preview_result"):
                    result = update["preview_result"]
                    preview = await save_preview(
                        run["order_id"],
                        actor,
                        PreviewDraft(**result["request"]),
                        run["request_id"],
                        thread_id=run["thread_id"],
                        run_id=run["id"],
                        expected_result=result if node == "plan" else None,
                    )
                    assert preview is not None
                    async with connect() as conn:
                        await conn.execute(
                            "UPDATE commerce.chat_messages SET preview_id=%s WHERE run_id=%s AND role='assistant'",
                            (preview.id, run["id"]),
                        )
                        await repo.append_event(
                            conn, run["id"], "preview", preview.model_dump(mode="json")
                        )
                    text = preview_text(preview)
                    for pos in range(0, len(text), 16):
                        await repo.add_token(run["id"], text[pos : pos + 16])
                for msg in update.get("messages", []):
                    if (
                        node in ("plan", "halt")
                        and isinstance(msg, AIMessage)
                        and msg == update["messages"][-1]
                        and not update.get("preview_result")
                    ):
                        text = plain_text(msg.content)
                        for pos in range(0, len(text), 16):
                            await repo.add_token(run["id"], text[pos : pos + 16])
                    if node == "tools" and isinstance(msg, ToolMessage):
                        async with connect() as conn:
                            await repo.append_event(
                                conn,
                                run["id"],
                                "tool",
                                {
                                    "name": msg.name,
                                    "order_id": run["order_id"],
                                    "label": "订单工具返回",
                                },
                            )
                    if node == "model" and settings.USE_FAKE_MODEL:
                        async with connect() as conn:
                            await repo.append_event(
                                conn,
                                run["id"],
                                "tool",
                                {
                                    "name": "query_order",
                                    "order_id": run["order_id"],
                                    "label": "订单核验完成",
                                },
                            )
                        text = plain_text(msg.content)
                        for pos in range(0, len(text), 8):
                            await repo.add_token(run["id"], text[pos : pos + 8])
                    if isinstance(msg, AIMessage) and msg.usage_metadata:
                        usage["calls"].append(msg.usage_metadata)
                        usage["returned_models"].append(msg.response_metadata.get("model_name"))
                async with connect() as conn:
                    await conn.execute(
                        "UPDATE commerce.chat_runs SET usage=%s WHERE id=%s",
                        (Jsonb(usage), run["id"]),
                    )
    await repo.finish(run["id"], usage=usage)


async def process(run):
    try:
        expected_model = "fake" if settings.USE_FAKE_MODEL else str(settings.DEFAULT_MODEL)
        if run["model"] != expected_model:
            await repo.finish(run["id"], error_code="model_changed")
            return
        await asyncio.wait_for(execute(run), timeout=180)
    except asyncio.CancelledError:
        await repo.finish(run["id"], error_code="service_restarted")
        raise
    except Exception as exc:
        code = "model_unavailable"
        if isinstance(exc, HTTPException) and exc.status_code == 401:
            code = "session_expired"
        elif isinstance(exc, TimeoutError):
            code = "model_timeout"
        elif isinstance(exc, GraphRecursionError):
            code = "step_limit"
        elif getattr(exc, "code", None) == "AllocationQuota.FreeTierOnly":
            code = "model_quota"
        elif isinstance(exc, HTTPException) and exc.status_code == 409:
            code = "conditions_changed"
        logger.warning("Chat run failed: %s (%s)", run["id"], type(exc).__name__)
        await repo.finish(run["id"], error_code=code)


async def worker():
    # A database session lock keeps this local delivery to a single durable queue consumer.
    while True:
        try:
            async with connect() as lease:
                await lease.set_autocommit(True)
                cursor = await lease.execute("SELECT pg_try_advisory_lock(1102001) AS acquired")
                lock_row = await cursor.fetchone()
                assert lock_row is not None
                if not lock_row["acquired"]:
                    await asyncio.sleep(1)
                    continue
                await recover_interrupted()
                while True:
                    await lease.execute("SELECT 1")
                    run = await claim()
                    if run:
                        await process(run)
                    else:
                        await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Chat worker connection unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(1)
