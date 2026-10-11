"""Authoritative confirmation/approval state. No financial or fulfillment writes."""

import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from commerce.db import connect
from commerce.preview_models import PreviewDraft
from commerce.preview_service import compute, stored
from commerce.repository import visible_order
from commerce.telemetry import carrier

ACTIVE = ("pending_approval", "decision_recorded", "approved")


def require_staff(actor):
    if actor.role != "staff":
        raise HTTPException(403, "只有客服可以审批或恢复流程")


async def lock_scope(conn, user_id, order_id):
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"application:{user_id}:{order_id}",),
    )


async def visible_application(conn, app_id, actor, *, lock=False):
    cursor = await conn.execute("SELECT * FROM commerce.applications WHERE id=%s", (app_id,))
    app = await cursor.fetchone()
    if app is None or (actor.role != "staff" and app["user_id"] != actor.id):
        raise HTTPException(404, "售后申请不存在")
    await visible_order(conn, app["order_id"], actor)
    if lock:
        await lock_scope(conn, app["user_id"], app["order_id"])
        cursor = await conn.execute(
            "SELECT * FROM commerce.applications WHERE id=%s FOR UPDATE", (app_id,)
        )
        app = await cursor.fetchone()
    return app


async def append_event(conn, app_id, key, kind, actor_id=None, **payload):
    cursor = await conn.execute(
        "SELECT 1 FROM commerce.approval_events WHERE application_id=%s AND event_key=%s",
        (app_id, key),
    )
    if await cursor.fetchone():
        return
    cursor = await conn.execute(
        "UPDATE commerce.applications SET next_event_id=next_event_id+1,updated_at=now() WHERE id=%s RETURNING next_event_id",
        (app_id,),
    )
    row = await cursor.fetchone()
    await conn.execute(
        "INSERT INTO commerce.approval_events(application_id,seq,event_key,kind,actor_id,payload) VALUES (%s,%s,%s,%s,%s,%s)",
        (app_id, row["next_event_id"], key, kind, actor_id, Jsonb(payload)),
    )


async def supersede(conn, user_id, order_id, new_preview_id):
    cursor = await conn.execute(
        "UPDATE commerce.applications SET status='superseded',workflow_status='complete',updated_at=now() WHERE user_id=%s AND order_id=%s AND status=ANY(%s) AND execution_state NOT IN ('completed','manual_review') AND NOT EXISTS (SELECT 1 FROM commerce.action_results r WHERE r.application_id=applications.id) RETURNING id",
        (user_id, order_id, list(ACTIVE)),
    )
    for app in await cursor.fetchall():
        await append_event(
            conn,
            app["id"],
            f"superseded:{new_preview_id}",
            "superseded",
            user_id,
            new_preview_id=str(new_preview_id),
            label="方案已修改，旧确认和审批失效",
        )


def check_binding(app, data):
    if (app["preview_id"], app["preview_version"], app["content_sha256"]) != (
        data.preview_id,
        data.preview_version,
        data.content_sha256,
    ):
        raise HTTPException(409, "方案编号、版本或摘要不匹配，请重新确认当前方案")


def fingerprint(action, target, data):
    return hashlib.sha256(
        json.dumps(
            {"action": action, "target": str(target), "data": data.model_dump(mode="json")},
            sort_keys=True,
        ).encode()
    ).hexdigest()


async def previous_command(conn, actor, data, digest):
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"approval-command:{actor.id}:{data.request_id}",),
    )
    cursor = await conn.execute(
        "SELECT * FROM commerce.approval_commands WHERE actor_id=%s AND request_id=%s",
        (actor.id, data.request_id),
    )
    row = await cursor.fetchone()
    if row and row["fingerprint"] != digest:
        raise HTTPException(409, "该请求编号已用于其他操作")
    if row:
        return await visible_application(conn, row["application_id"], actor)
    return None


async def record_command(conn, actor, data, digest, app_id):
    await conn.execute(
        "INSERT INTO commerce.approval_commands(actor_id,request_id,fingerprint,application_id) VALUES (%s,%s,%s,%s)",
        (actor.id, data.request_id, digest, app_id),
    )


async def preview_current(conn, preview, actor):
    cursor = await conn.execute(
        "SELECT id FROM commerce.previews WHERE user_id=%s AND order_id=%s ORDER BY version DESC,created_at DESC,id DESC LIMIT 1",
        (preview["user_id"], preview["order_id"]),
    )
    latest = await cursor.fetchone()
    if latest["id"] != preview["id"]:
        return "superseded"
    result = (
        await compute(conn, preview["order_id"], actor, PreviewDraft(**preview["input"]))
    ).model_dump(mode="json")
    if result != preview["result"] or result["status"] != "eligible":
        return "conditions_changed"
    return None


async def validate_current(conn, app, actor):
    pending = await (await conn.execute("SELECT 1 FROM commerce.material_requests WHERE application_id=%s AND state='pending'", (app['id'],))).fetchone()
    if pending:
        raise HTTPException(409, '请等待用户补充材料后再审批')
    cursor = await conn.execute("SELECT * FROM commerce.previews WHERE id=%s", (app["preview_id"],))
    preview = await cursor.fetchone()
    reason = await preview_current(conn, preview, actor)
    if reason:
        await conn.execute(
            "UPDATE commerce.applications SET status=%s,workflow_status='complete',updated_at=now() WHERE id=%s",
            (reason, app["id"]),
        )
        await append_event(
            conn, app["id"], reason, reason, label="方案或业务条件已变化，请重新生成并确认方案"
        )
    return reason


async def confirm(data, actor):
    if actor.role != "user":
        raise HTTPException(403, "方案必须由订单所属用户本人确认")
    digest = fingerprint("confirm", data.preview_id, data)
    async with connect() as conn:
        previous = await previous_command(conn, actor, data, digest)
        if previous:
            return previous
        cursor = await conn.execute(
            "SELECT * FROM commerce.previews WHERE id=%s AND user_id=%s",
            (data.preview_id, actor.id),
        )
        preview = await cursor.fetchone()
        if preview is None:
            raise HTTPException(404, "预览不存在")
        await visible_order(conn, preview["order_id"], actor)
        check_binding(
            {
                "preview_id": preview["id"],
                "preview_version": preview["version"],
                "content_sha256": preview["content_sha256"],
            },
            data,
        )
        await lock_scope(conn, actor.id, preview["order_id"])
        await conn.execute('SELECT id FROM commerce.previews WHERE id=%s FOR UPDATE', (preview['id'],))
        cursor = await conn.execute(
            "SELECT * FROM commerce.applications WHERE preview_id=%s", (preview["id"],)
        )
        existing = await cursor.fetchone()
        if existing:
            await record_command(conn, actor, data, digest, existing["id"])
            return existing
        if await preview_current(conn, preview, actor):
            raise HTTPException(409, "方案已过时或条件变化，请重新生成合格方案后确认")
        app_id = uuid4()
        await conn.execute(
            "INSERT INTO commerce.applications(id,user_id,order_id,preview_id,preview_version,content_sha256,status,workflow_status,traceparent) VALUES (%s,%s,%s,%s,%s,%s,'pending_approval','queued',%s)",
            (
                app_id,
                actor.id,
                preview["order_id"],
                preview["id"],
                preview["version"],
                preview["content_sha256"],
                carrier(),
            ),
        )
        await append_event(
            conn,
            app_id,
            "confirmed",
            "confirmed",
            actor.id,
            preview_id=str(preview["id"]),
            version=preview["version"],
            content_sha256=preview["content_sha256"],
            label="用户已确认，等待客服审批",
        )
        await record_command(conn, actor, data, digest, app_id)
        cursor = await conn.execute("SELECT * FROM commerce.applications WHERE id=%s", (app_id,))
        return await cursor.fetchone()


async def command(app_id, action, data, actor):
    if action in ("decision", "resume"):
        require_staff(actor)
    digest = fingerprint(action, app_id, data)
    stale = None
    async with connect() as conn:
        previous = await previous_command(conn, actor, data, digest)
        if previous:
            return previous
        app = await visible_application(conn, app_id, actor, lock=True)
        check_binding(app, data)
        if action == "cancel":
            if actor.role != "user" or app["user_id"] != actor.id:
                raise HTTPException(403, "只有申请用户本人可以取消")
            if app["status"] not in ACTIVE:
                raise HTTPException(409, "申请已停止，不能再次取消")
            cursor = await conn.execute(
                "SELECT 1 FROM commerce.action_results WHERE application_id=%s", (app_id,)
            )
            if await cursor.fetchone() or app["execution_state"] in ("completed", "manual_review"):
                raise HTTPException(409, "业务结果已提交或需对账，不能取消或伪装回滚")
            await conn.execute(
                "UPDATE commerce.applications SET status='cancelled',workflow_status='complete',updated_at=now() WHERE id=%s",
                (app_id,),
            )
            await append_event(
                conn, app_id, "cancelled", "cancelled", actor.id, label="用户已取消，流程停止"
            )
        else:
            cursor = await conn.execute(
                "SELECT 1 FROM commerce.action_results WHERE application_id=%s", (app_id,)
            )
            if await cursor.fetchone() or app["execution_state"] in ("completed", "manual_review"):
                raise HTTPException(409, "业务结果已提交，审批历史不能重新恢复或改写")
            if app["status"] in ("superseded", "cancelled", "conditions_changed"):
                raise HTTPException(409, "申请已失效或取消，请重新确认新方案")
            stale = await validate_current(conn, app, actor)
            if not stale and action == "decision":
                if app["status"] != "pending_approval" or app["workflow_status"] != "waiting":
                    raise HTTPException(409, "流程尚未等待审批或已有决定，请刷新后重试")
                await conn.execute(
                    "UPDATE commerce.applications SET decision=%s,decision_actor=%s,decision_note=%s,decision_at=now(),status=%s,updated_at=now() WHERE id=%s",
                    (
                        data.decision,
                        actor.id,
                        data.note,
                        "decision_recorded" if data.decision == "approved" else "rejected",
                        app_id,
                    ),
                )
                await append_event(
                    conn,
                    app_id,
                    "decision",
                    "decision_recorded",
                    actor.id,
                    decision=data.decision,
                    note=data.note,
                    preview_id=str(data.preview_id),
                    version=data.preview_version,
                    content_sha256=data.content_sha256,
                    label="客服决定已保存，等待独立恢复",
                )
            elif not stale and action == "resume":
                if app["workflow_status"] == "complete":
                    raise HTTPException(409, "流程已经结束")
                if app["workflow_status"] not in ("waiting", "failed"):
                    raise HTTPException(409, "流程正在处理，请等待完成")
                if not app["decision"] and app["workflow_status"] != "failed":
                    raise HTTPException(409, "尚无客服决定，不能恢复审批")
                await conn.execute(
                    "UPDATE commerce.applications SET workflow_status='queued',resume_requested=%s,error_code=NULL,traceparent=%s,updated_at=now() WHERE id=%s",
                    (bool(app["decision"]), carrier(), app_id),
                )
                await append_event(
                    conn,
                    app_id,
                    f"resume:{data.request_id}",
                    "resume_requested",
                    actor.id,
                    label="已通过独立接口请求恢复审批流程",
                )
        if not stale:
            await record_command(conn, actor, data, digest, app_id)
        cursor = await conn.execute("SELECT * FROM commerce.applications WHERE id=%s", (app_id,))
        result = await cursor.fetchone()
    if stale:
        raise HTTPException(409, "方案或业务条件已变化，旧审批失效，请重新确认")
    return result


async def detail(app_id, actor):
    async with connect() as conn:
        app = await visible_application(conn, app_id, actor)
        cursor = await conn.execute(
            "SELECT * FROM commerce.previews WHERE id=%s", (app["preview_id"],)
        )
        preview = stored(await cursor.fetchone())
        cursor = await conn.execute(
            "SELECT seq,kind,actor_id,payload,created_at FROM commerce.approval_events WHERE application_id=%s ORDER BY seq",
            (app_id,),
        )
        return {"application": app, "preview": preview, "events": await cursor.fetchall()}
