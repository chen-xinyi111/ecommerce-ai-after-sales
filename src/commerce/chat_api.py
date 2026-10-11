import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from commerce import auth
from commerce import chat_repository as repo
from commerce.chat_models import (
    ChatRun,
    ChatThread,
    MessageInput,
    RunEvents,
    ThreadHistory,
    ThreadInput,
    ThreadPage,
)
from commerce.db import connect
from commerce.models import Principal
from core import settings

router = APIRouter(prefix="/api/v1", tags=["售后会话"])
Actor = Annotated[Principal, Depends(auth.current_actor)]


@router.post(
    "/threads", response_model=ChatThread, status_code=201, dependencies=[Depends(auth.same_origin)]
)
async def create_thread(data: ThreadInput, actor: Actor):
    return await repo.create_thread(data.order_id, actor)


@router.get("/threads", response_model=ThreadPage, operation_id="business_threads")
async def threads(actor: Actor, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT count(*) AS n FROM commerce.chat_threads WHERE user_id=%s", (actor.id,)
        )
        count_row = await cursor.fetchone()
        assert count_row is not None
        total = count_row["n"]
        cursor = await conn.execute(
            "SELECT * FROM commerce.chat_threads WHERE user_id=%s ORDER BY updated_at DESC,id "
            "LIMIT %s OFFSET %s",
            (actor.id, limit, offset),
        )
        return {"items": await cursor.fetchall(), "total": total}


@router.get("/threads/{thread_id}", response_model=ThreadHistory, operation_id="business_history")
async def history(thread_id: UUID, actor: Actor):
    async with connect() as conn:
        thread = await repo.visible_thread(conn, thread_id, actor)
        cursor = await conn.execute(
            "SELECT * FROM commerce.chat_messages WHERE thread_id=%s ORDER BY id", (thread_id,)
        )
        messages = await cursor.fetchall()
        from commerce.preview_service import stored
        for message in messages:
            if message.get("preview_id"):
                cursor = await conn.execute("SELECT * FROM commerce.previews WHERE id=%s AND user_id=%s", (message["preview_id"], thread['user_id']))
                preview = await cursor.fetchone()
                if preview:
                    message["preview"] = stored(preview)
        cursor = await conn.execute(
            "SELECT * FROM commerce.chat_runs WHERE thread_id=%s ORDER BY created_at,id",
            (thread_id,),
        )
        return {"thread": thread, "messages": messages, "runs": await cursor.fetchall()}


@router.post(
    "/threads/{thread_id}/messages",
    response_model=ChatRun,
    status_code=202,
    dependencies=[Depends(auth.same_origin)],
)
async def message(thread_id: UUID, data: MessageInput, actor: Actor, request: Request):
    assert settings.DEFAULT_MODEL is not None
    return await repo.submit(
        thread_id,
        data,
        actor,
        auth.token_hash(request.cookies[auth.COOKIE_NAME]),
        "fake" if settings.USE_FAKE_MODEL else settings.DEFAULT_MODEL.value,
    )


@router.get("/runs/{run_id}", response_model=RunEvents)
async def run_events(run_id: UUID, actor: Actor, after: int = Query(0, ge=0)):
    async with connect() as conn:
        run = await repo.visible_run(conn, run_id, actor)
        cursor = await conn.execute(
            "SELECT seq,kind,payload FROM commerce.chat_events WHERE run_id=%s AND seq>%s ORDER BY seq",
            (run_id, after),
        )
        return {"run": run, "events": await cursor.fetchall()}


@router.get(
    "/runs/{run_id}/events",
    response_class=StreamingResponse,
    operation_id="business_stream",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "按递增 ID 回放持久化事件；支持 Last-Event-ID 和 after",
        }
    },
)
async def stream(run_id: UUID, actor: Actor, request: Request, after: int = Query(0, ge=0)):
    try:
        last_event_id = int(request.headers.get("last-event-id", "0"))
        if last_event_id < 0:
            raise ValueError
        cursor_id = max(after, last_event_id)
    except ValueError:
        raise HTTPException(400, "Last-Event-ID 必须是非负整数") from None
    async with connect() as conn:
        await repo.visible_run(conn, run_id, actor)
    session_hash = auth.token_hash(request.cookies[auth.COOKIE_NAME])

    async def events():
        nonlocal cursor_id
        idle = 0
        while not await request.is_disconnected():
            try:
                fresh_actor = await auth.actor_from_session_hash(session_hash)
                async with connect() as conn:
                    run = await repo.visible_run(conn, run_id, fresh_actor)
                    cursor = await conn.execute(
                        "SELECT seq,kind,payload FROM commerce.chat_events WHERE run_id=%s AND seq>%s ORDER BY seq LIMIT 200",
                        (run_id, cursor_id),
                    )
                    rows = await cursor.fetchall()
                for row in rows:
                    cursor_id = row["seq"]
                    yield f"id: {cursor_id}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                if run["status"] in ("completed", "failed") and cursor_id >= run["next_event_id"]:
                    return
            except HTTPException:
                yield 'data: {"kind":"error","seq":0,"payload":{"message":"登录已失效或会话不可访问，请重新登录。"}}\n\n'
                return
            except Exception:
                yield 'data: {"kind":"error","seq":0,"payload":{"message":"连接暂时中断，正在恢复。","reconnect":true}}\n\n'
                return
            idle += 1
            if idle % 40 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
