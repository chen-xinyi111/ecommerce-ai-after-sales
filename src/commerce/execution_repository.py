"""Transactional simulated actions. No model-supplied amounts or idempotency keys."""

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from commerce import approval_repository as approval
from commerce.db import connect
from commerce.execution_models import ExecutionStep
from commerce.models import Principal
from commerce.preview_models import PreviewDraft
from commerce.preview_service import compute
from commerce.telemetry import carrier, instrument


async def row(conn, query, args=()):
    return await (await conn.execute(query, args)).fetchone()


async def set_status(conn, job, status, *, result=None, error=None, delay=0):
    await conn.execute(
        "UPDATE commerce.execution_jobs SET status=%s,result=%s,error_code=%s,next_attempt_at=now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s",
        (status, Jsonb(result) if result else None, error, delay, job["id"]),
    )
    await conn.execute(
        "UPDATE commerce.applications SET execution_state=%s,updated_at=now() WHERE id=%s",
        (status, job["application_id"]),
    )


async def lock_business(conn, app):
    # All action writers lock the same order before financial/quantity rows.
    await conn.execute("SELECT id FROM commerce.orders WHERE id=%s FOR UPDATE", (app["order_id"],))
    await conn.execute(
        "SELECT id FROM commerce.order_items WHERE order_id=%s ORDER BY id FOR UPDATE",
        (app["order_id"],),
    )
    await conn.execute(
        "SELECT id FROM commerce.shipments WHERE order_id=%s ORDER BY id FOR UPDATE",
        (app["order_id"],),
    )
    preview = await row(conn, "SELECT * FROM commerce.previews WHERE id=%s", (app["preview_id"],))
    item = await row(
        conn, "SELECT * FROM commerce.order_items WHERE id=%s", (preview["input"]["order_item_id"],)
    )
    await conn.execute(
        "SELECT sku_id FROM commerce.inventory WHERE sku_id=%s FOR UPDATE", (item["sku_id"],)
    )
    await conn.execute(
        "SELECT id FROM commerce.payments WHERE order_id=%s ORDER BY id FOR UPDATE",
        (app["order_id"],),
    )
    await conn.execute("LOCK TABLE commerce.policies IN SHARE MODE")
    return preview, item


async def valid(conn, app, preview, *, credit=0):
    if (
        app["status"] != "approved"
        or app["workflow_status"] != "complete"
        or app["decision"] != "approved"
    ):
        return False
    users = await (
        await conn.execute(
        "SELECT id,role FROM commerce.users WHERE id=ANY(%s) AND active",
            ([app["user_id"], app["decision_actor"]],),
        )
    ).fetchall()
    if {r["id"] for r in users} != {app["user_id"], app["decision_actor"]}:
        return False
    if not any(r["id"] == app["decision_actor"] and r["role"] == "staff" for r in users):
        return False
    latest = await row(
        conn,
        "SELECT id FROM commerce.previews WHERE user_id=%s AND order_id=%s ORDER BY version DESC,created_at DESC,id DESC LIMIT 1",
        (app["user_id"], app["order_id"]),
    )
    if latest["id"] != preview["id"]:
        return False
    actor = Principal(id=app["user_id"], username="", display_name="", role="user")
    result = (
        await compute(
            conn, app["order_id"], actor, PreviewDraft(**preview["input"]), reserved_credit=credit
        )
    ).model_dump(mode="json")
    return result == preview["result"] and result["status"] == "eligible"


@instrument('api.enqueue.execution')
async def enqueue(app_id, data, actor, *, retry=False):
    approval.require_staff(actor)
    digest = approval.fingerprint("retry_execution" if retry else "execute", app_id, data)
    stale = False
    result = None
    async with connect() as conn:
        previous = await approval.previous_command(conn, actor, data, digest)
        if previous:
            return await row(
                conn, "SELECT * FROM commerce.execution_jobs WHERE application_id=%s", (app_id,)
            )
        app = await approval.visible_application(conn, app_id, actor, lock=True)
        approval.check_binding(app, data)
        existing = await row(
            conn,
            "SELECT * FROM commerce.execution_jobs WHERE application_id=%s FOR UPDATE",
            (app_id,),
        )
        if existing and not retry:
            await approval.record_command(conn, actor, data, digest, app_id)
            return existing
        if retry and (not existing or existing["status"] != "failed"):
            raise HTTPException(409, "只有已中断且未提交业务结果的任务可以重试")
        if app["status"] != "approved" or app["workflow_status"] != "complete":
            raise HTTPException(409, "尚未完成用户确认与客服审批，不能执行")
        preview, _ = await lock_business(conn, app)
        stale = not await valid(conn, app, preview)
        if stale:
            await conn.execute(
                "UPDATE commerce.applications SET status='conditions_changed' WHERE id=%s",
                (app_id,),
            )
            await approval.append_event(
                conn,
                app_id,
                "execution_conditions_changed",
                "conditions_changed",
                actor.id,
                label="执行前条件变化，旧审批失效，请重新确认",
            )
        else:
            if retry:
                await conn.execute(
                    "UPDATE commerce.execution_jobs SET status='queued',attempts=0,error_code=NULL,next_attempt_at=now(),requested_by=%s,traceparent=%s,updated_at=now() WHERE id=%s",
                    (actor.id, carrier(), existing["id"]),
                )
            else:
                await conn.execute(
                    "INSERT INTO commerce.execution_jobs(id,application_id,requested_by,status,traceparent) VALUES (%s,%s,%s,'queued',%s)",
                    (uuid4(), app_id, actor.id, carrier()),
                )
            await conn.execute(
                "UPDATE commerce.applications SET execution_state='queued' WHERE id=%s", (app_id,)
            )
            await approval.record_command(conn, actor, data, digest, app_id)
            await approval.append_event(
                conn,
                app_id,
                f"execution_request:{data.request_id}",
                "execution_queued",
                actor.id,
                label="客服已提交模拟业务执行" if not retry else "客服核对后请求重试执行",
                **({"note": data.note} if retry else {}),
            )
            result = await row(
                conn, "SELECT * FROM commerce.execution_jobs WHERE application_id=%s", (app_id,)
            )
    if stale:
        raise HTTPException(409, "方案或业务条件变化，请重新生成并确认")
    assert result is not None
    return result


@asynccontextmanager
async def locked_job(request):
    async with connect() as conn:
        job = await row(
            conn,
            "SELECT * FROM commerce.execution_jobs WHERE id=%s AND application_id=%s",
            (request.job_id, request.application_id),
        )
        if not job:
            raise HTTPException(403, "执行委托不匹配")
        initial = await row(
            conn, "SELECT * FROM commerce.applications WHERE id=%s", (job["application_id"],)
        )
        await approval.lock_scope(conn, initial["user_id"], initial["order_id"])
        app = await row(
            conn, "SELECT * FROM commerce.applications WHERE id=%s FOR UPDATE", (initial["id"],)
        )
        job = await row(
            conn, "SELECT * FROM commerce.execution_jobs WHERE id=%s FOR UPDATE", (job["id"],)
        )
        if job["status"] != "running":
            raise HTTPException(409, "执行任务未运行")
        staff = await row(
            conn,
            "SELECT id FROM commerce.users WHERE id=%s AND role='staff' AND active",
            (job["requested_by"],),
        )
        if not staff:
            raise HTTPException(403, "执行客服已失效")
        preview, item = await lock_business(conn, app)
        yield conn, job, app, preview, item


async def _release(conn, app_id):
    hold = await row(
        conn,
        "SELECT * FROM commerce.inventory_reservations WHERE application_id=%s AND status='held' FOR UPDATE",
        (app_id,),
    )
    if hold:
        await conn.execute(
            "UPDATE commerce.inventory SET reserved=reserved-%s WHERE sku_id=%s",
            (hold["quantity"], hold["sku_id"]),
        )
        await conn.execute(
            "UPDATE commerce.inventory_reservations SET status='released',updated_at=now() WHERE application_id=%s",
            (app_id,),
        )
        await approval.append_event(
            conn,
            app_id,
            f"released:{uuid4()}",
            "inventory_released",
            label="释放尚未发货的库存预留",
        )
    await conn.execute(
        "UPDATE commerce.work_orders SET status='cancelled',updated_at=now() WHERE application_id=%s AND status<>'completed'",
        (app_id,),
    )


async def stop(conn, job, app):
    await release(conn, app["id"])
    result = ExecutionStep(
        application_id=app["id"],
        status="stopped",
        label="方案、审批或业务条件变化，执行停止；未提交售后动作",
    ).model_dump(mode="json")
    await set_status(conn, job, "stopped", result=result, error="conditions_changed")
    if app["status"] == "approved":
        await conn.execute(
            "UPDATE commerce.applications SET status='conditions_changed' WHERE id=%s", (app["id"],)
        )
    await approval.append_event(
        conn, app["id"], "execution_stopped", "execution_stopped", label=result["label"]
    )
    return ExecutionStep(**result)


async def _create_work_order(request):
    async with locked_job(request) as (conn, job, app, preview, item):
        action = await row(
            conn, "SELECT result FROM commerce.action_results WHERE application_id=%s", (app["id"],)
        )
        if action:
            return ExecutionStep(**action["result"])
        hold = await row(
            conn,
            "SELECT * FROM commerce.inventory_reservations WHERE application_id=%s",
            (app["id"],),
        )
        credit = hold["quantity"] if hold and hold["status"] == "held" else 0
        if not await valid(conn, app, preview, credit=credit):
            return await stop(conn, job, app)
        order = await row(
            conn, "SELECT * FROM commerce.work_orders WHERE application_id=%s", (app["id"],)
        )
        kind, qty = preview["input"]["kind"], preview["input"]["quantity"]
        reserved = kind in ("exchange", "reship")
        if not order:
            await conn.execute(
                "INSERT INTO commerce.work_orders(id,application_id,job_id,order_id,order_item_id,kind,quantity,refund_cents,policy_id,policy_version,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "WO-" + str(app["id"]),
                    app["id"],
                    job["id"],
                    app["order_id"],
                    item["id"],
                    kind,
                    qty,
                    preview["result"]["refund_cents"] or 0,
                    preview["result"]["policy"]["id"],
                    preview["result"]["policy"]["version"],
                    "reserved" if reserved else "ready",
                ),
            )
        else:
            await conn.execute(
                "UPDATE commerce.work_orders SET status=%s,updated_at=now() WHERE application_id=%s",
                ("reserved" if reserved else "ready", app["id"]),
            )
        if reserved and not credit:
            await conn.execute(
                "UPDATE commerce.inventory SET reserved=reserved+%s WHERE sku_id=%s",
                (qty, item["sku_id"]),
            )
            await conn.execute(
                "INSERT INTO commerce.inventory_reservations(application_id,work_order_id,sku_id,quantity,status) VALUES (%s,%s,%s,%s,'held') ON CONFLICT(application_id) DO UPDATE SET status='held',updated_at=now()",
                (app["id"], "WO-" + str(app["id"]), item["sku_id"], qty),
            )
            await approval.append_event(
                conn,
                app["id"],
                f"reserved:{job['attempts']}",
                "inventory_reserved",
                label=f"已预留 {qty} 件库存，尚未发货",
            )
        result = ExecutionStep(
            application_id=app["id"],
            work_order_id="WO-" + str(app["id"]),
            status="reserved" if reserved else "ready",
            kind=kind,
            label="模拟工单已创建，等待执行",
        )
        await approval.append_event(
            conn,
            app["id"],
            "work_order_created",
            "mcp_write",
            job["requested_by"],
            label="MCP 创建工单已事务提交",
            tool="create_work_order",
            result=result.model_dump(mode="json"),
        )
        return result


async def _execute_action(request):
    async with locked_job(request) as (conn, job, app, preview, item):
        if request.kind != preview["input"]["kind"]:
            raise HTTPException(409, "动作类型与批准方案不匹配")
        previous = await row(
            conn, "SELECT result FROM commerce.action_results WHERE application_id=%s", (app["id"],)
        )
        if previous:
            return ExecutionStep(**previous["result"])
        order = await row(
            conn, "SELECT * FROM commerce.work_orders WHERE application_id=%s", (app["id"],)
        )
        if not order or order["status"] not in ("ready", "reserved"):
            raise HTTPException(409, "尚未创建可执行工单")
        hold = await row(
            conn,
            "SELECT * FROM commerce.inventory_reservations WHERE application_id=%s FOR UPDATE",
            (app["id"],),
        )
        credit = hold["quantity"] if hold and hold["status"] == "held" else 0
        if not await valid(conn, app, preview, credit=credit):
            return await stop(conn, job, app)
        kind, qty, cents = order["kind"], order["quantity"], order["refund_cents"]
        key = f"{app['id']}:execute:{kind}"
        tracking = None
        repair_status = None
        if kind == "refund":
            payment = await row(
                conn, "SELECT * FROM commerce.payments WHERE order_id=%s", (app["order_id"],)
            )
            if cents <= 0 or payment["paid_cents"] - payment["refunded_cents"] < cents:
                return await stop(conn, job, app)
            await conn.execute(
                "INSERT INTO commerce.refund_ledger(application_id,idempotency_key,payment_id,amount_cents) VALUES (%s,%s,%s,%s)",
                (app["id"], key, payment["id"], cents),
            )
            await conn.execute(
                "UPDATE commerce.payments SET refunded_cents=refunded_cents+%s,status='partially_refunded' WHERE id=%s",
                (cents, payment["id"]),
            )
            await conn.execute(
                "UPDATE commerce.orders SET refunded_cents=refunded_cents+%s,status='partially_refunded' WHERE id=%s",
                (cents, app["order_id"]),
            )
            label = f"模拟退款已入账：{cents // 100}.{cents % 100:02d} 元"
        elif kind in ("exchange", "reship"):
            if not hold or hold["status"] != "held" or hold["quantity"] != qty:
                raise RuntimeError("Missing inventory reservation")
            await conn.execute(
                "UPDATE commerce.inventory SET on_hand=on_hand-%s,reserved=reserved-%s WHERE sku_id=%s",
                (qty, qty, item["sku_id"]),
            )
            await conn.execute(
                "UPDATE commerce.inventory_reservations SET status='consumed',updated_at=now() WHERE application_id=%s",
                (app["id"],),
            )
            tracking = "SIM-AS-" + app["id"].hex
            await conn.execute(
                "INSERT INTO commerce.after_sales_shipments(application_id,idempotency_key,work_order_id,sku_id,quantity,tracking_number,status) VALUES (%s,%s,%s,%s,%s,%s,'dispatched')",
                (app["id"], key, order["id"], item["sku_id"], qty, tracking),
            )
            label = "模拟换货已发货" if kind == "exchange" else "模拟补发已发货"
        else:
            repair_status = "awaiting_inspection"
            await conn.execute(
                "INSERT INTO commerce.repair_intakes(application_id,work_order_id,status) VALUES (%s,%s,'awaiting_inspection')",
                (app["id"], order["id"]),
            )
            label = "模拟维修已受理，等待检查；未声称修复完成"
        await conn.execute(
            "UPDATE commerce.order_items SET after_sales_quantity=after_sales_quantity+%s WHERE id=%s",
            (qty, item["id"]),
        )
        await conn.execute(
            "INSERT INTO commerce.after_sales_history(id,order_id,order_item_id,kind,quantity,refund_cents,status,policy_id,policy_version) VALUES (%s,%s,%s,%s,%s,%s,'completed',%s,%s)",
            (
                "AS-" + str(app["id"]),
                app["order_id"],
                item["id"],
                kind,
                qty,
                cents,
                order["policy_id"],
                order["policy_version"],
            ),
        )
        result = ExecutionStep(
            application_id=app["id"],
            work_order_id=order["id"],
            status="completed",
            kind=kind,
            refund_cents=cents,
            shipment_tracking=tracking,
            repair_status=repair_status,
            label=label,
        )
        await conn.execute(
            "INSERT INTO commerce.action_results(application_id,idempotency_key,work_order_id,result) VALUES (%s,%s,%s,%s)",
            (app["id"], key, order["id"], Jsonb(result.model_dump(mode="json"))),
        )
        await conn.execute(
            "UPDATE commerce.work_orders SET status='completed',updated_at=now() WHERE id=%s",
            (order["id"],),
        )
        await approval.append_event(
            conn,
            app["id"],
            "action_committed",
            "mcp_write",
            job["requested_by"],
            label=label + "；数据库事务已提交",
            tool="execute_after_sales",
            result=result.model_dump(mode="json"),
        )
        return result


async def release(conn, app_id):
    from commerce.adapters import ports
    return await ports()[2].release(conn, app_id)


async def create_work_order(request):
    from commerce.adapters import ports
    return await ports()[2].reserve(request)


async def execute_action(request):
    from commerce.adapters import ports, DatabaseRefund
    if request.kind == 'refund':
        return await ports()[1].execute(request)
    if request.kind in ('exchange', 'reship'):
        return await ports()[3].dispatch(request)
    # Repair intake is a local service record; completion still needs inspection.
    return await DatabaseRefund().execute(request)


async def detail(app_id, actor):
    async with connect() as conn:
        await approval.visible_application(conn, app_id, actor)
        result = {}
        for name, table in [
            ("job", "execution_jobs"),
            ("work_order", "work_orders"),
            ("reservation", "inventory_reservations"),
            ("refund", "refund_ledger"),
            ("shipment", "after_sales_shipments"),
            ("repair", "repair_intakes"),
        ]:
            result[name] = await row(
                conn, f"SELECT * FROM commerce.{table} WHERE application_id=%s", (app_id,)
            )
        return result
