"""Standalone protected simulation HTTP API. Run on localhost:8081 only."""

import asyncio
import secrets
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from commerce import approval_repository as approval
from commerce import execution_repository as execution
from commerce import repository
from commerce.config import CommerceSettings
from commerce.db import connect
from commerce.execution_models import ActionRequest, ExecutionRequest, ExecutionStep
from commerce.mcp_security import service_key
from commerce.models import Inventory, Logistics, OrderDetail, OrderPage, Principal


async def internal_auth(request: Request):
    if not secrets.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + service_key()
    ):
        raise HTTPException(403, "业务接口认证失败")


app = FastAPI(title="青禾独立模拟业务接口", dependencies=[Depends(internal_auth)])


async def actor(actor_id: str):
    async with connect() as conn:
        row = await (
            await conn.execute("SELECT * FROM commerce.users WHERE id=%s AND active", (actor_id,))
        ).fetchone()
    if not row:
        raise HTTPException(403, "账号无效")
    return Principal.model_validate(row)


@app.get("/health")
async def health():
    return {"status": "ok", "simulated": True}


@app.get("/orders", response_model=OrderPage)
async def orders(actor_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return await repository._order_page(await actor(actor_id), offset, limit)


@app.get("/orders/{order_id}", response_model=OrderDetail)
async def order(order_id: str, actor_id: str):
    return await repository._order_detail(order_id, await actor(actor_id))


@app.get("/inventory/{sku_id}", response_model=Inventory)
async def inventory(sku_id: str, actor_id: str):
    return await repository._inventory(sku_id, await actor(actor_id))


@app.get("/inventory")
async def inventory_page(actor_id: str):
    return await repository._inventory_page(await actor(actor_id))


@app.get("/logistics/{order_id}", response_model=Logistics)
async def logistics(order_id: str, actor_id: str):
    return await repository._logistics(order_id, await actor(actor_id))


async def action(request, handler):
    mode = CommerceSettings().SIM_FAILURE_MODE
    if mode == "before":
        raise HTTPException(503, "业务服务暂时无法办理，请稍后恢复")
    result = await handler(request)
    if mode == "after":
        raise HTTPException(503, "响应中断，请核对办理结果")
    if mode == "timeout":
        await asyncio.sleep(6)
    return result


@app.post("/inventory/reserve", response_model=ExecutionStep)
async def reserve(data: ExecutionRequest):
    return await action(data, execution._create_work_order)


@app.post("/inventory/release")
async def release(data: ExecutionRequest):
    async with execution.locked_job(data) as (conn, job, application, preview, item):
        if await execution.row(
            conn,
            "SELECT 1 FROM commerce.action_results WHERE application_id=%s",
            (application["id"],),
        ):
            raise HTTPException(409, "已经办理，不能释放已消耗的库存")
        await execution._release(conn, application["id"])
    return {"message": "尚未发货的预留已释放"}


@app.post("/refunds", response_model=ExecutionStep)
async def refund(data: ActionRequest):
    if data.kind != "refund":
        raise HTTPException(422, "此接口仅办理退款")
    return await action(data, execution._execute_action)


@app.post("/dispatches", response_model=ExecutionStep)
async def dispatch(data: ActionRequest):
    if data.kind not in ("exchange", "reship"):
        raise HTTPException(422, "此接口仅办理发货")
    return await action(data, execution._execute_action)


@app.get("/results/{application_id}")
async def result(application_id: UUID):
    async with connect() as conn:
        row = await execution.row(
            conn,
            "SELECT result FROM commerce.action_results WHERE application_id=%s",
            (application_id,),
        )
    return {"result": row["result"] if row else None}


class Callback(BaseModel):
    event_id: UUID
    application_id: UUID
    status: str


@app.post("/callbacks/shipment")
async def callback(data: Callback):
    if data.status not in ("dispatched", "delivered"):
        raise HTTPException(422, "不支持此物流状态")
    async with connect() as conn:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("provider-event:" + str(data.event_id),),
        )
        existing = await execution.row(
            conn, "SELECT * FROM commerce.provider_events WHERE event_id=%s", (data.event_id,)
        )
        if existing:
            if (
                existing["application_id"] != data.application_id
                or existing["status"] != data.status
            ):
                raise HTTPException(409, "回调编号已用于其他事件")
            return {"message": "已记录"}
        application = await execution.row(
            conn,
            "SELECT * FROM commerce.applications WHERE id=%s FOR UPDATE",
            (data.application_id,),
        )
        shipment = await execution.row(
            conn,
            "SELECT 1 FROM commerce.after_sales_shipments WHERE application_id=%s",
            (data.application_id,),
        )
        if not application or not shipment:
            raise HTTPException(404, "发货记录不存在")
        await conn.execute(
            "INSERT INTO commerce.provider_events(event_id,application_id,status) VALUES(%s,%s,%s)",
            (data.event_id, data.application_id, data.status),
        )
        await approval.append_event(
            conn,
            data.application_id,
            "provider:" + str(data.event_id),
            "shipment_updated",
            label="售后商品已签收" if data.status == "delivered" else "售后商品已发货",
        )
        await conn.execute(
            "INSERT INTO commerce.notifications(user_id,application_id,event_key,title) VALUES(%s,%s,%s,%s)",
            (
                application["user_id"],
                data.application_id,
                "provider:" + str(data.event_id),
                "售后物流进度已更新",
            ),
        )
    return {"message": "已记录"}
