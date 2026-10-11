"""Evaluation-only host: actual public routers, workers, PostgreSQL and MCP HTTP.

Never imported by the user workstation; all fault controls stay in this harness.
"""

import asyncio
import importlib.util
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agents.after_sales.supervisor import after_sales_supervisor
from commerce import (
    approval_workflow,
    async_runtime,
    chat_worker,
    mcp_client,
    mcp_server,
    preview_service,
)
from commerce.api import router as business_router
from commerce.approval_api import router as approval_router
from commerce.chat_api import router as chat_router
from commerce.config import CommerceSettings
from commerce.db import pool_lifespan
from commerce.execution_api import router as execution_router
from commerce.mcp_server import mcp_app, mcp_lifespan
from commerce.observability_api import router as operations_router
from commerce.preview_api import router as preview_router
from commerce.product_api import router as product_router
from commerce.telemetry import TraceMiddleware, configure_export
from core import settings
from memory.postgres import get_postgres_connection_string

ROOT = Path(os.environ["EVAL_RUNTIME"])
VARIANT = os.environ.get("EVAL_VARIANT", "final")
assert CommerceSettings().POSTGRES_DB.startswith("agent_commerce_eval_")
mcp_client.MCP_URL = f"http://127.0.0.1:{settings.PORT}/internal/commerce-mcp/mcp"


def fault():
    path = ROOT / "fault.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def record_fault(kind, **fields):
    with (ROOT / "fault-events.jsonl").open("a", encoding="utf-8") as stream:
        from datetime import UTC, datetime

        stream.write(json.dumps(dict(at=datetime.now(UTC).isoformat(), kind=kind, **fields)) + "\n")


original_compute = preview_service.compute


async def compute(conn, order_id, *args, **kwargs):
    if order_id in fault().get("tool_failure_orders", []):
        record_fault("policy_backend_unavailable", order_id=order_id)
        raise HTTPException(503, "评测注入的政策工具异常")
    return await original_compute(conn, order_id, *args, **kwargs)


preview_service.compute = compute

mcp_server.compute = compute
baseline = None
if VARIANT == "baseline":
    spec = importlib.util.spec_from_file_location("evaluation_baseline", ROOT / "baseline.py")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    baseline.compute = compute
    selected_graph = baseline.after_sales_agent
else:
    selected_graph = after_sales_supervisor
chat_worker.after_sales_agent = selected_graph

original_call = mcp_client.Gateway.call


async def tool_call(self, session, name, arguments):
    active = fault()
    if active.get("order_id") == self.context.get("business_order_id"):
        if active.get("mode") == "write_before" and name == "execute_after_sales":
            record_fault("before_business_write", task_id=self.context["business_run_id"])
            raise mcp_client.MCPFailure("Injected before write")
        if active.get("mode") == "write_after" and name == "execute_after_sales":
            result = await original_call(self, session, name, arguments)
            record_fault("lost_response_after_commit", task_id=self.context["business_run_id"])
            raise mcp_client.MCPFailure("Injected lost committed response")
        if active.get("mode") == "hold_after_work_order" and name == "create_work_order":
            result = await original_call(self, session, name, arguments)
            record_fault("hold_after_work_order", task_id=self.context["business_run_id"])
            while fault().get("mode") == "hold_after_work_order":
                await asyncio.sleep(0.1)
            return result
    return await original_call(self, session, name, arguments)


mcp_client.Gateway.call = tool_call
original_publish = async_runtime.publish_one


class FailingProducer:
    async def send_and_wait(self, *args, **kwargs):
        record_fault("broker_send_failure")
        raise ConnectionError("Injected broker send failure")


async def publish(producer):
    return await original_publish(
        FailingProducer() if fault().get("mode") == "broker" else producer
    )


async_runtime.publish_one = publish
original_redis_set = async_runtime.Redis.set


async def redis_set(self, *args, **kwargs):
    if fault().get("mode") == "redis":
        from redis.exceptions import ConnectionError

        record_fault("redis_connection_failure")
        raise ConnectionError("Injected Redis connection failure")
    return await original_redis_set(self, *args, **kwargs)


async_runtime.Redis.set = redis_set


@asynccontextmanager
async def lifecycle(app):
    configure_export()
    async with (
        pool_lifespan(),
        AsyncPostgresSaver.from_conn_string(get_postgres_connection_string()) as saver,
        mcp_lifespan(),
    ):
        await saver.setup()
        selected_graph.checkpointer = saver
        approval_workflow.approval_graph.checkpointer = saver
        task = asyncio.create_task(async_runtime.runtime())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app = FastAPI(lifespan=lifecycle)

for router in (
    business_router,
    approval_router,
    chat_router,
    execution_router,
    operations_router,
    preview_router,
    product_router,
):
    app.include_router(router)
app.add_middleware(TraceMiddleware)
app.mount("/internal/commerce-mcp", mcp_app)

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger().setLevel(logging.WARNING)
    for name in ("mcp", "httpx", "aiokafka"):
        logging.getLogger(name).setLevel(logging.WARNING)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=settings.PORT,
        loop="core.loops:selector_loop_factory",
        log_level="warning",
    )
