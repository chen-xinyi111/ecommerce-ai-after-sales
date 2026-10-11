from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from commerce.db import connect
from commerce.models import Principal
from commerce.repository import visible_order
from commerce.telemetry import carrier, instrument


async def visible_thread(conn, thread_id: UUID, actor: Principal, *, lock=False):
    cursor = await conn.execute(
        "SELECT * FROM commerce.chat_threads t WHERE id=%s AND (user_id=%s OR (%s='staff' AND EXISTS(SELECT 1 FROM commerce.support_sessions s WHERE s.thread_id=t.id)))"
        + (" FOR UPDATE" if lock else ""),
        (thread_id, actor.id, actor.role),
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, "会话不存在")
    await visible_order(conn, row["order_id"], actor)
    return row


async def visible_run(conn, run_id: UUID, actor: Principal):
    cursor = await conn.execute("SELECT * FROM commerce.chat_runs WHERE id=%s", (run_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, "聊天任务不存在")
    await visible_thread(conn, row["thread_id"], actor)
    return row


async def append_event(conn, run_id, kind, payload):
    cursor = await conn.execute(
        "UPDATE commerce.chat_runs SET next_event_id=next_event_id+1 WHERE id=%s "
        "RETURNING next_event_id",
        (run_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    await conn.execute(
        "INSERT INTO commerce.chat_events(run_id,seq,kind,payload) VALUES (%s,%s,%s,%s)",
        (run_id, row["next_event_id"], kind, Jsonb(payload)),
    )


async def create_thread(order_id, actor):
    async with connect() as conn:
        await visible_order(conn, order_id, actor)
        cursor = await conn.execute(
            "INSERT INTO commerce.chat_threads(id,user_id,order_id,title) VALUES (%s,%s,%s,%s) "
            "RETURNING *",
            (uuid4(), actor.id, order_id, f"订单 {order_id} 的售后咨询"),
        )
        return await cursor.fetchone()


@instrument("api.enqueue.chat")
async def submit(thread_id, data, actor, session_hash, model):
    async with connect() as conn:
        await visible_thread(conn, thread_id, actor, lock=True)
        support = await (
            await conn.execute(
                "SELECT 1 FROM commerce.support_sessions WHERE thread_id=%s AND state IN ('queued','active')",
                (thread_id,),
            )
        ).fetchone()
        if support:
            raise HTTPException(409, "已转人工，请在人工咨询中发送消息")
        cursor = await conn.execute(
            "SELECT r.*,m.content FROM commerce.chat_runs r JOIN commerce.chat_messages m "
            "ON m.run_id=r.id AND m.role='user' WHERE r.thread_id=%s AND r.request_id=%s",
            (thread_id, data.request_id),
        )
        previous = await cursor.fetchone()
        if previous:
            if previous["content"] != data.message:
                raise HTTPException(409, "该请求编号已用于另一条消息")
            return previous
        cursor = await conn.execute(
            "SELECT 1 FROM commerce.chat_runs WHERE thread_id=%s AND status IN ('queued','running')",
            (thread_id,),
        )
        if await cursor.fetchone():
            raise HTTPException(409, "当前会话还有消息正在处理，请等待完成")
        cursor = await conn.execute(
            "SELECT count(*) AS n FROM commerce.chat_messages WHERE thread_id=%s AND role='user'",
            (thread_id,),
        )
        count_row = await cursor.fetchone()
        assert count_row is not None
        if count_row["n"] >= 50:
            raise HTTPException(409, "该会话已达到 50 轮，请新建咨询")
        run_id = uuid4()
        cursor = await conn.execute(
            "INSERT INTO commerce.chat_runs(id,thread_id,request_id,session_hash,status,model,traceparent) "
            "VALUES (%s,%s,%s,%s,'queued',%s,%s) RETURNING *",
            (run_id, thread_id, data.request_id, session_hash, model, carrier()),
        )
        run = await cursor.fetchone()
        await conn.execute(
            "INSERT INTO commerce.chat_messages(thread_id,run_id,role,content,status) "
            "VALUES (%s,%s,'user',%s,'complete'),(%s,%s,'assistant','','partial')",
            (thread_id, run_id, data.message, thread_id, run_id),
        )
        await conn.execute(
            "UPDATE commerce.chat_threads SET updated_at=now() WHERE id=%s", (thread_id,)
        )
        await append_event(conn, run_id, "status", {"status": "queued", "label": "等待处理"})
        return run


async def add_token(run_id, text):
    async with connect() as conn:
        await conn.execute(
            "UPDATE commerce.chat_messages SET content=content || %s WHERE run_id=%s AND role='assistant'",
            (text, run_id),
        )
        await append_event(conn, run_id, "token", {"text": text})


async def finish(run_id, *, error_code=None, usage=None):
    async with connect() as conn:
        status = "failed" if error_code else "completed"
        cursor = await conn.execute(
            "UPDATE commerce.chat_runs SET status=%s,error_code=%s,usage=coalesce(%s::jsonb,usage),finished_at=now() "
            "WHERE id=%s AND status IN ('queued','running') RETURNING thread_id,usage",
            (status, error_code, Jsonb(usage) if usage is not None else None, run_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        await conn.execute(
            "UPDATE commerce.chat_messages SET status=%s WHERE run_id=%s AND role='assistant'",
            ("failed" if error_code else "complete", run_id),
        )
        await conn.execute(
            "UPDATE commerce.chat_threads SET updated_at=now() WHERE id=%s", (row["thread_id"],)
        )
        await append_event(
            conn,
            run_id,
            "error" if error_code else "done",
            {
                "status": status,
                "error_code": error_code,
                "message": error_message(error_code) if error_code else "回复已完成",
                "usage": row["usage"],
            },
        )


def error_message(code):
    return {
        "service_restarted": "服务重启，上一条回复已中断；已接收的内容保留，请重新发送。",
        "session_expired": "登录已失效，请重新登录后发送。",
        "model_quota": "咨询服务暂时繁忙，请稍后重试或转人工客服。",
        "model_timeout": "回复超时，已接收内容保留，请稍后重新发送。",
        "model_unavailable": "咨询服务暂时不可用，已收到的内容保留，请稍后重试或转人工客服。",
        "step_limit": "本次未能完成查询，请补充明确问题或转人工客服。",
        "model_changed": "咨询服务已更新，请重新发送这条消息。",
        "conditions_changed": "业务条件已变化，任务已停止，请重新生成预览。",
    }.get(code, "本次回复失败，请稍后重新发送。")
