from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from commerce import auth, repository
from commerce.db import connect
from commerce.models import (
    Health,
    Inventory,
    InventoryPage,
    LoginInput,
    Logistics,
    OrderDetail,
    OrderPage,
    Principal,
)

router = APIRouter(prefix="/api/v1", tags=["模拟售后业务"])
Actor = Annotated[Principal, Depends(auth.current_actor)]


@router.post("/auth/login", response_model=Principal, dependencies=[Depends(auth.same_origin)])
async def login(data: LoginInput, response: Response):
    return await auth.login(data, response)


@router.post("/auth/logout", status_code=204, dependencies=[Depends(auth.same_origin)])
async def logout(request: Request, response: Response):
    await auth.logout(request, response)


@router.get("/auth/me", response_model=Principal)
async def me(actor: Actor):
    return actor


@router.get("/orders", response_model=OrderPage)
async def orders(actor: Actor, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    from commerce.adapters import ports

    return await ports()[0].page(actor, offset, limit)


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def order(order_id: str, actor: Actor):
    return await repository.order_detail(order_id, actor)


@router.get("/orders/{order_id}/logistics", response_model=Logistics)
async def logistics(order_id: str, actor: Actor):
    return await repository.logistics(order_id, actor)


@router.get("/inventory", response_model=InventoryPage)
async def all_inventory(actor: Actor):
    if actor.role != "staff":
        raise HTTPException(403, "仅客服可查询完整库存")
    from commerce.adapters import ports

    return await ports()[2].page(actor)


@router.get("/inventory/{sku_id}", response_model=Inventory)
async def inventory(sku_id: str, actor: Actor):
    return await repository.inventory(sku_id, actor)


@router.get("/health", response_model=Health)
async def health():
    async with connect() as conn:
        cursor = await conn.execute("SELECT business_date FROM commerce.demo_marker WHERE id=1")
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(503, "演示数据尚未初始化")
        return {"status": "ok", "database": "postgres", "business_date": row["business_date"]}
