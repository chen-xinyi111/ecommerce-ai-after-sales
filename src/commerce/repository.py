from fastapi import HTTPException
from psycopg import sql

from commerce.db import connect
from commerce.models import Principal


async def visible_order(conn, order_id: str, actor: Principal) -> dict:
    cursor = await conn.execute(
        "SELECT * FROM commerce.orders WHERE id=%s AND (%s='staff' OR user_id=%s)",
        (order_id, actor.role, actor.id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, "订单不存在")
    return row


async def _order_detail(order_id: str, actor: Principal) -> dict:
    async with connect() as conn:
        row = await visible_order(conn, order_id, actor)
        for key, table in (
            ("items", "order_items"),
            ("payments", "payments"),
            ("after_sales_history", "after_sales_history"),
        ):
            cursor = await conn.execute(
                sql.SQL("SELECT * FROM commerce.{} WHERE order_id=%s ORDER BY id").format(
                    sql.Identifier(table)
                ),
                (order_id,),
            )
            row[key] = await cursor.fetchall()
        return row


async def _logistics(order_id: str, actor: Principal) -> dict:
    async with connect() as conn:
        await visible_order(conn, order_id, actor)
        cursor = await conn.execute(
            "SELECT id AS shipment_id, order_id, tracking_number, status "
            "FROM commerce.shipments WHERE order_id=%s",
            (order_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(404, "该订单暂无物流")
        cursor = await conn.execute(
            "SELECT occurred_at,status,description FROM commerce.logistics_events "
            "WHERE shipment_id=%s ORDER BY occurred_at,id",
            (row["shipment_id"],),
        )
        row["events"] = await cursor.fetchall()
        return row


async def _inventory(sku_id: str, actor: Principal) -> dict:
    async with connect() as conn:
        if actor.role != "staff":
            cursor = await conn.execute(
                "SELECT 1 FROM commerce.order_items i JOIN commerce.orders o ON o.id=i.order_id "
                "WHERE i.sku_id=%s AND o.user_id=%s LIMIT 1",
                (sku_id, actor.id),
            )
            if await cursor.fetchone() is None:
                raise HTTPException(404, "库存信息不存在")
        cursor = await conn.execute(
            "SELECT sku_id,on_hand,reserved,on_hand-reserved AS available "
            "FROM commerce.inventory WHERE sku_id=%s",
            (sku_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(404, "库存信息不存在")
        return row


async def _order_page(actor, offset, limit):
    async with connect() as conn:
        clause = "(%s='staff' OR user_id=%s)"
        params = (actor.role, actor.id)
        total = await (
            await conn.execute(f"SELECT count(*) AS n FROM commerce.orders WHERE {clause}", params)
        ).fetchone()
        items = await (
            await conn.execute(
                f"SELECT * FROM commerce.orders WHERE {clause} ORDER BY id LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
        ).fetchall()
        return {"items": items, "total": total["n"], "offset": offset, "limit": limit}


async def _inventory_page(actor):
    if actor.role != "staff":
        raise HTTPException(403, "仅客服可查询完整库存")
    async with connect() as conn:
        rows = await (
            await conn.execute(
                "SELECT sku_id,on_hand,reserved,on_hand-reserved AS available FROM commerce.inventory ORDER BY sku_id"
            )
        ).fetchall()
        return {"items": rows}


async def order_detail(order_id, actor):
    from commerce.adapters import ports

    return await ports()[0].detail(order_id, actor)


async def logistics(order_id, actor):
    from commerce.adapters import ports

    return await ports()[3].query(order_id, actor)


async def inventory(sku_id, actor):
    from commerce.adapters import ports

    return await ports()[2].query(sku_id, actor)
