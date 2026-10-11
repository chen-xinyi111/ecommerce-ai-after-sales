from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from commerce import approval_repository as approval
from commerce import auth
from commerce.async_runtime import TABLES
from commerce.chat_repository import visible_run
from commerce.config import CommerceSettings
from commerce.db import connect
from commerce.execution_repository import row
from commerce.models import Principal

router = APIRouter(prefix="/api/v1", tags=["任务追踪与异常处理"])
Actor = Annotated[Principal, Depends(auth.current_actor)]


class ServiceVersion(BaseModel):
    version: Literal["3.3"] = "3.3"
    transport: Literal["postgres", "kafka"]


@router.get("/version", response_model=ServiceVersion)
async def service_version():
    return {"version": "3.3", "transport": CommerceSettings().ASYNC_TRANSPORT}


class TraceSpan(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    started_ns: int
    ended_ns: int
    status: str
    attributes: dict[str, Any]


class TraceDetail(BaseModel):
    spans: list[TraceSpan]
    task_id: UUID
    transport: str


class DeadLetter(BaseModel):
    id: UUID
    task_type: str | None
    task_id: UUID | None
    event_id: UUID | None
    error_code: str
    status: str
    created_at: datetime
    resolved_at: datetime | None


class Operations(BaseModel):
    transport: str
    outbox: dict[str, int]
    tasks: dict[str, dict[str, int]]
    dead_letters: list[DeadLetter]


class RedriveInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    note: str = Field(min_length=1, max_length=500)


async def spans_for(conn, tasks):
    ids = set()
    for task_type, task in tasks:
        if not task:
            continue
        table, _ = TABLES[task_type]
        record = await row(conn, f"SELECT traceparent FROM commerce.{table} WHERE id=%s", (task,))
        if record and record["traceparent"]:
            ids.add(record["traceparent"].split("-")[1])
        events = await (
            await conn.execute(
                "SELECT traceparent FROM commerce.task_outbox WHERE task_type=%s AND task_id=%s",
                (task_type, task),
            )
        ).fetchall()
        ids.update(e["traceparent"].split("-")[1] for e in events if e["traceparent"])
    if not ids:
        return []
    return await (
        await conn.execute(
            "SELECT * FROM commerce.trace_spans WHERE trace_id=ANY(%s) ORDER BY started_ns,span_id",
            (list(ids),),
        )
    ).fetchall()


@router.get("/tasks/{task_type}/{task_id}/trace", response_model=TraceDetail)
async def task_trace(
    task_type: Literal["execution", "chat", "approval"], task_id: UUID, actor: Actor
):
    async with connect() as conn:
        if task_type == "chat":
            await visible_run(conn, task_id, actor)
        else:
            app_id = task_id
            if task_type == "execution":
                job = await row(
                    conn,
                    "SELECT application_id FROM commerce.execution_jobs WHERE id=%s",
                    (task_id,),
                )
                if not job:
                    raise HTTPException(404, "执行任务不存在")
                app_id = job["application_id"]
            await approval.visible_application(conn, app_id, actor)
        return {
            "task_id": task_id,
            "transport": CommerceSettings().ASYNC_TRANSPORT,
            "spans": await spans_for(conn, [(task_type, task_id)]),
        }


@router.get("/applications/{app_id}/trace", response_model=TraceDetail)
async def application_trace(app_id: UUID, actor: Actor):
    async with connect() as conn:
        app = await approval.visible_application(conn, app_id, actor)
        preview = await row(
            conn, "SELECT run_id FROM commerce.previews WHERE id=%s", (app["preview_id"],)
        )
        job = await row(
            conn, "SELECT id FROM commerce.execution_jobs WHERE application_id=%s", (app_id,)
        )
        tasks = [
            ("approval", app_id),
            ("chat", preview["run_id"]),
            ("execution", job["id"] if job else None),
        ]
        return {
            "task_id": app_id,
            "transport": CommerceSettings().ASYNC_TRANSPORT,
            "spans": await spans_for(conn, tasks),
        }


@router.get("/operations", response_model=Operations)
async def operations(actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        outbox = await (
            await conn.execute(
                "SELECT status,count(*) AS n FROM commerce.task_outbox GROUP BY status"
            )
        ).fetchall()
        tasks = {}
        for kind, (table, column) in TABLES.items():
            groups = await (
                await conn.execute(
                    sql.SQL(
                        "SELECT {} AS status,count(*) AS n FROM commerce.{} GROUP BY {}"
                    ).format(sql.Identifier(column), sql.Identifier(table), sql.Identifier(column))
                )
            ).fetchall()
            tasks[kind] = {g["status"]: g["n"] for g in groups}
        letters = await (
            await conn.execute(
                "SELECT * FROM commerce.dead_letters ORDER BY created_at DESC LIMIT 100"
            )
        ).fetchall()
        return {
            "transport": CommerceSettings().ASYNC_TRANSPORT,
            "outbox": {g["status"]: g["n"] for g in outbox},
            "tasks": tasks,
            "dead_letters": letters,
        }


@router.post(
    "/operations/dead-letters/{letter_id}/redrive",
    response_model=DeadLetter,
    dependencies=[Depends(auth.same_origin)],
)
async def redrive(letter_id: UUID, data: RedriveInput, actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        letter = await row(
            conn, "SELECT * FROM commerce.dead_letters WHERE id=%s FOR UPDATE", (letter_id,)
        )
        if not letter:
            raise HTTPException(404, "异常记录不存在")
        if letter["status"] == "resolved":
            return letter
        if letter["error_code"] != "publish_exhausted":
            raise HTTPException(
                409,
                "此记录不能重新投递；业务执行失败请从申请页核对后重试，非法消息需由维护人员检查来源",
            )
        await conn.execute(
            "UPDATE commerce.task_outbox SET status='pending',attempts=0,available_at=now(),error_code=NULL WHERE id=%s AND status='dead'",
            (letter["event_id"],),
        )
        await conn.execute(
            "INSERT INTO commerce.async_operations(letter_id,actor_id,note) VALUES (%s,%s,%s)",
            (letter_id, actor.id, data.note.strip()),
        )
        return await row(
            conn,
            "UPDATE commerce.dead_letters SET status='resolved',resolved_at=now() WHERE id=%s RETURNING *",
            (letter_id,),
        )
