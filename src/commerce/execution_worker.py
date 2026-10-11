"""PostgreSQL queue with durable MCP step results and bounded retry/reconciliation."""

import asyncio
import logging
from uuid import uuid4

from commerce import approval_repository as approval
from commerce import execution_repository as repo
from commerce.db import connect
from commerce.execution_models import ExecutionStep
from commerce.mcp_client import Gateway

logger = logging.getLogger(__name__)


async def inspect(job_id):
    async with connect() as conn:
        job = await repo.row(
            conn,
            "SELECT j.*,a.order_id,p.input FROM commerce.execution_jobs j JOIN commerce.applications a ON a.id=j.application_id JOIN commerce.previews p ON p.id=a.preview_id WHERE j.id=%s",
            (job_id,),
        )
        action = await repo.row(
            conn,
            "SELECT result FROM commerce.action_results WHERE application_id=%s",
            (job["application_id"],),
        )
        work_order = await repo.row(
            conn,
            "SELECT * FROM commerce.work_orders WHERE application_id=%s",
            (job["application_id"],),
        )
        return job, action, work_order


async def settle(job_id, *, failure=False):
    async with connect() as conn:
        job = await repo.row(conn, "SELECT * FROM commerce.execution_jobs WHERE id=%s", (job_id,))
        initial = await repo.row(
            conn, "SELECT * FROM commerce.applications WHERE id=%s", (job["application_id"],)
        )
        await approval.lock_scope(conn, initial["user_id"], initial["order_id"])
        app = await repo.row(
            conn, "SELECT * FROM commerce.applications WHERE id=%s FOR UPDATE", (initial["id"],)
        )
        job = await repo.row(
            conn, "SELECT * FROM commerce.execution_jobs WHERE id=%s FOR UPDATE", (job_id,)
        )
        if job["status"] in ("completed", "stopped", "manual_review"):
            return
        action = await repo.row(
            conn, "SELECT * FROM commerce.action_results WHERE application_id=%s", (app["id"],)
        )
        if action:
            # A committed action always wins over a lost MCP response or stale approval.
            result = ExecutionStep(**action["result"]).model_dump(mode="json")
            order = await repo.row(
                conn, "SELECT * FROM commerce.work_orders WHERE application_id=%s", (app["id"],)
            )
            history = await repo.row(
                conn,
                "SELECT * FROM commerce.after_sales_history WHERE id=%s",
                ("AS-" + str(app["id"]),),
            )
            consistent = bool(
                order
                and history
                and order["status"] == "completed"
                and history["quantity"] == order["quantity"]
                and history["refund_cents"] == order["refund_cents"]
            )
            if order and order["kind"] == "refund":
                ledger = await repo.row(
                    conn,
                    "SELECT * FROM commerce.refund_ledger WHERE application_id=%s",
                    (app["id"],),
                )
                consistent = consistent and bool(
                    ledger and ledger["amount_cents"] == order["refund_cents"]
                )
            elif order and order["kind"] in ("exchange", "reship"):
                shipment = await repo.row(
                    conn,
                    "SELECT * FROM commerce.after_sales_shipments WHERE application_id=%s",
                    (app["id"],),
                )
                consistent = consistent and bool(
                    shipment and shipment["quantity"] == order["quantity"]
                )
            elif order:
                intake = await repo.row(
                    conn,
                    "SELECT * FROM commerce.repair_intakes WHERE application_id=%s",
                    (app["id"],),
                )
                consistent = consistent and bool(intake)
            if not consistent:
                await repo.set_status(conn, job, "manual_review", error="reconciliation_required")
                await approval.append_event(
                    conn,
                    app["id"],
                    "execution_review",
                    "manual_review",
                    label="已存在提交结果但关联记录需对账；不重复执行，不回滚已退款或发货",
                )
                return
            await repo.set_status(conn, job, "completed", result=result)
            await approval.append_event(
                conn,
                app["id"],
                "execution_completed",
                "execution_completed",
                job["requested_by"],
                label="执行完成，已核对持久业务结果",
                result=result,
            )
            return
        if not failure:
            await repo.set_status(conn, job, "manual_review", error="missing_action_result")
            await approval.append_event(
                conn,
                app["id"],
                "execution_review",
                "manual_review",
                label="工具响应缺少持久动作结果，需要人工核对",
            )
            return
        await repo.lock_business(conn, app)
        staff = await repo.row(
            conn,
            "SELECT id FROM commerce.users WHERE id=%s AND active AND role='staff'",
            (job["requested_by"],),
        )
        if app["status"] != "approved" or not staff:
            await repo.stop(conn, job, app)
        elif job["attempts"] >= 3:
            await repo.release(conn, app["id"])
            await repo.set_status(conn, job, "failed", error="execution_unavailable")
            await approval.append_event(
                conn,
                app["id"],
                f"failed:{uuid4()}",
                "execution_failed",
                label="三次尝试后中断；已确认无提交结果并释放库存，客服核对后可重试",
            )
        else:
            await repo.set_status(conn, job, "queued", error="retry_pending", delay=2 ** job["attempts"])
            await approval.append_event(
                conn,
                app["id"],
                f"retry:{uuid4()}",
                "execution_retry",
                label="连接或步骤中断；已查询持久结果，稍后恢复未完成步骤",
            )


async def process(job, gateway_factory=Gateway):
    try:
        current, action, order = await inspect(job["id"])
        if action:
            await settle(job["id"])
            return
        gateway = gateway_factory(
            {
                "business_purpose": "execution",
                "business_run_id": str(job["id"]),
                "business_application_id": str(current["application_id"]),
                "business_order_id": current["order_id"],
            },
            {"create_work_order", "execute_after_sales"},
            max_calls=2,
        )
        arguments = {"job_id": str(job["id"]), "application_id": str(current["application_id"])}
        async with gateway.session() as session:
            if not order or order["status"] not in ("ready", "reserved"):
                created = await gateway.call(session, "create_work_order", arguments)
                if created["status"] == "stopped":
                    return
            result = await gateway.call(
                session, "execute_after_sales", arguments | {"kind": current["input"]["kind"]}
            )
            if result["status"] == "stopped":
                return
        await settle(job["id"])
    except asyncio.CancelledError:
        # Leave durable running task for recovery. No speculative compensation.
        raise
    except Exception as exc:
        logger.warning("Execution interrupted (%s)", type(exc).__name__)
        await settle(job["id"], failure=True)


async def recover_interrupted():
    async with connect() as conn:
        jobs = await (
            await conn.execute(
                "UPDATE commerce.execution_jobs SET status='queued',next_attempt_at=now(),updated_at=now() WHERE status='running' RETURNING *"
            )
        ).fetchall()
        for job in jobs:
            await conn.execute(
                "UPDATE commerce.applications SET execution_state='queued' WHERE id=%s",
                (job["application_id"],),
            )
            await approval.append_event(
                conn,
                job["application_id"],
                f"execution_recovery:{uuid4()}",
                "execution_recovered",
                label="服务重启，先查询已提交步骤再恢复执行",
            )


async def claim():
    async with connect() as conn:
        job = await repo.row(
            conn,
            "SELECT * FROM commerce.execution_jobs WHERE status='queued' AND next_attempt_at<=now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
        )
        if job:
            job = await repo.row(
                conn,
                "UPDATE commerce.execution_jobs SET status='running',attempts=attempts+1,updated_at=now() WHERE id=%s RETURNING *",
                (job["id"],),
            )
            await conn.execute(
                "UPDATE commerce.applications SET execution_state='running' WHERE id=%s",
                (job["application_id"],),
            )
        return job


async def worker():
    while True:
        try:
            async with connect() as lease:
                await lease.set_autocommit(True)
                lock = await repo.row(lease, "SELECT pg_try_advisory_lock(1102301) AS acquired")
                if not lock["acquired"]:
                    await asyncio.sleep(1)
                    continue
                await recover_interrupted()
                while True:
                    await lease.execute("SELECT 1")
                    job = await claim()
                    if job:
                        try:
                            await asyncio.wait_for(process(job), 45)
                        except TimeoutError:
                            await settle(job["id"], failure=True)
                    else:
                        await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Execution worker unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(1)
