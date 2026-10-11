import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from commerce import approval_repository as repo
from commerce import auth
from commerce.approval_models import (
    Application,
    ApplicationDetail,
    ApplicationPage,
    DecisionInput,
    SnapshotInput,
)
from commerce.db import connect
from commerce.execution_models import ExecutionJob
from commerce.models import Principal

router = APIRouter(prefix="/api/v1", tags=["双重确认与审批"])
Actor = Annotated[Principal, Depends(auth.current_actor)]


@router.post(
    "/previews/{preview_id}/confirm",
    response_model=Application,
    status_code=202,
    dependencies=[Depends(auth.same_origin)],
)
async def confirm(preview_id: UUID, data: SnapshotInput, actor: Actor):
    if preview_id != data.preview_id:
        raise HTTPException(409, "方案编号不匹配")
    return await repo.confirm(data, actor)


@router.get("/applications", response_model=ApplicationPage)
async def applications(
    actor: Actor,
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
):
    owner = None if actor.role == "staff" else actor.id
    args = [owner, owner, status, status]
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT count(*) AS n FROM commerce.applications WHERE (%s::text IS NULL OR user_id=%s) AND (%s::text IS NULL OR status=%s)",
            args,
        )
        count = await cursor.fetchone()
        assert count is not None
        total = count["n"]
        cursor = await conn.execute(
            "SELECT * FROM commerce.applications WHERE (%s::text IS NULL OR user_id=%s) AND (%s::text IS NULL OR status=%s) ORDER BY confirmed_at DESC,id LIMIT %s OFFSET %s",
            args + [limit, offset],
        )
        return {"items": await cursor.fetchall(), "total": total}


@router.get("/applications/{app_id}", response_model=ApplicationDetail)
async def application_detail(app_id: UUID, actor: Actor):
    return await repo.detail(app_id, actor)


@router.post(
    "/applications/{app_id}/cancel",
    response_model=Application,
    dependencies=[Depends(auth.same_origin)],
)
async def cancel(app_id: UUID, data: SnapshotInput, actor: Actor):
    return await repo.command(app_id, "cancel", data, actor)


@router.post(
    "/applications/{app_id}/decision",
    response_model=Application,
    dependencies=[Depends(auth.same_origin)],
)
async def decision(app_id: UUID, data: DecisionInput, actor: Actor):
    return await repo.command(app_id, "decision", data, actor)


@router.post(
    "/applications/{app_id}/resume",
    response_model=Application,
    status_code=202,
    dependencies=[Depends(auth.same_origin)],
)
async def resume(app_id: UUID, data: SnapshotInput, actor: Actor):
    return await repo.command(app_id, "resume", data, actor)


@router.post(
    "/applications/{app_id}/execute",
    response_model=ExecutionJob,
    status_code=202,
    dependencies=[Depends(auth.same_origin)],
)
async def execution_gate(app_id: UUID, actor: Actor, data: SnapshotInput | None = None):
    from commerce.execution_repository import enqueue

    async with connect() as conn:
        app = await repo.visible_application(conn, app_id, actor)
    if app["status"] != "approved" or app["workflow_status"] != "complete":
        raise HTTPException(409, "尚未完成用户确认与客服审批，不能执行")
    repo.require_staff(actor)
    if data is None:
        raise HTTPException(422, "执行必须绑定方案编号、版本、摘要和请求编号")
    return await enqueue(app_id, data, actor)


@router.get(
    "/applications/{app_id}/events",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "审批事件按递增 ID 持久化回放"}
    },
)
async def events(app_id: UUID, actor: Actor, request: Request, after: int = Query(0, ge=0)):
    try:
        seq = max(after, int(request.headers.get("last-event-id", "0")))
        if int(request.headers.get("last-event-id", "0")) < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Last-Event-ID 必须是非负整数") from None
    async with connect() as conn:
        await repo.visible_application(conn, app_id, actor)
    session_hash = auth.token_hash(request.cookies[auth.COOKIE_NAME])

    async def generate():
        nonlocal seq
        idle = 0
        while not await request.is_disconnected():
            try:
                fresh = await auth.actor_from_session_hash(session_hash)
                async with connect() as conn:
                    app = await repo.visible_application(conn, app_id, fresh)
                    cursor = await conn.execute(
                        "SELECT seq,kind,actor_id,payload,created_at FROM commerce.approval_events WHERE application_id=%s AND seq>%s ORDER BY seq LIMIT 200",
                        (app_id, seq),
                    )
                    for row in await cursor.fetchall():
                        seq = row["seq"]
                        yield f"id: {seq}\ndata: {json.dumps(row, default=str, ensure_ascii=False)}\n\n"
                if (
                    app["workflow_status"] in ("complete", "failed")
                    and app["execution_state"] not in ("queued", "running")
                    and seq >= app["next_event_id"]
                ):
                    return
            except HTTPException:
                yield 'data: {"kind":"error","payload":{"label":"登录失效或无权访问"}}\n\n'
                return
            except Exception:
                yield 'data: {"kind":"error","payload":{"label":"连接中断，请刷新恢复"}}\n\n'
                return
            idle += 1
            if idle % 40 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
