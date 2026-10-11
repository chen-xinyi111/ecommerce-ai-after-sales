"""Replaceable order/refund/inventory/logistics ports. DB mode preserves old deployments.

The standalone simulation service shares the authoritative PostgreSQL ledger with the
workstation, so the existing transaction guards and timeout reconciliation remain valid.
An enterprise implementation must also synchronize authoritative facts and action receipts.
"""

from typing import Protocol

import httpx
from fastapi import HTTPException

from commerce.config import CommerceSettings
from commerce.execution_models import ExecutionStep
from commerce.models import Inventory, Logistics, OrderDetail, OrderPage


async def remote(method, path, *, actor=None, body=None):
    from commerce.mcp_security import service_key

    config = CommerceSettings()
    params = dict(httpx.URL(path).params)
    if actor:
        params["actor_id"] = actor.id
    try:
        async with httpx.AsyncClient(
            base_url=config.SIM_BUSINESS_URL, timeout=5, trust_env=False
        ) as client:
            response = await client.request(
                method,
                path,
                params=params,
                json=body,
                headers={"Authorization": "Bearer " + service_key()},
            )
        if not response.is_success:
            detail = response.json().get("detail", "业务服务暂时不可用")
            raise HTTPException(response.status_code, detail)
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "业务服务连接中断，系统将先核对办理结果再恢复") from None


class OrderPort(Protocol):
    async def detail(self, order_id, actor) -> dict: ...
    async def page(self, actor, offset, limit) -> dict: ...


class RefundPort(Protocol):
    async def execute(self, request) -> ExecutionStep: ...


class InventoryPort(Protocol):
    async def page(self, actor) -> dict: ...
    async def query(self, sku_id, actor) -> dict: ...
    async def reserve(self, request) -> ExecutionStep: ...
    async def release(self, conn, app_id): ...


class LogisticsPort(Protocol):
    async def query(self, order_id, actor) -> dict: ...
    async def dispatch(self, request) -> ExecutionStep: ...


class DatabaseOrder:
    async def detail(self, order_id, actor):
        from commerce.repository import _order_detail

        return await _order_detail(order_id, actor)

    async def page(self, actor, offset, limit):
        from commerce.repository import _order_page

        return await _order_page(actor, offset, limit)


class HTTPOrder(DatabaseOrder):
    async def detail(self, order_id, actor):
        return OrderDetail.model_validate(
            await remote("GET", "/orders/" + order_id, actor=actor)
        ).model_dump()

    async def page(self, actor, offset, limit):
        return OrderPage.model_validate(
            await remote("GET", f"/orders?offset={offset}&limit={limit}", actor=actor)
        ).model_dump()


class DatabaseRefund:
    async def execute(self, request):
        from commerce.execution_repository import _execute_action

        return await _execute_action(request)


class HTTPRefund(DatabaseRefund):
    async def execute(self, request):
        return ExecutionStep.model_validate(
            await remote("POST", "/refunds", body=request.model_dump(mode="json"))
        )


class DatabaseInventory:
    async def page(self, actor):
        from commerce.repository import _inventory_page

        return await _inventory_page(actor)

    async def query(self, sku_id, actor):
        from commerce.repository import _inventory

        return await _inventory(sku_id, actor)

    async def reserve(self, request):
        from commerce.execution_repository import _create_work_order

        return await _create_work_order(request)

    async def release(self, conn, app_id):
        # Same ledger and transaction in both simulation modes. No nested connection
        # may take locks owned by this transaction (including on failure compensation).
        from commerce.execution_repository import _release

        return await _release(conn, app_id)


class HTTPInventory(DatabaseInventory):
    async def page(self, actor):
        from commerce.models import InventoryPage

        return InventoryPage.model_validate(
            await remote("GET", "/inventory", actor=actor)
        ).model_dump()

    async def query(self, sku_id, actor):
        return Inventory.model_validate(
            await remote("GET", "/inventory/" + sku_id, actor=actor)
        ).model_dump()

    async def reserve(self, request):
        return ExecutionStep.model_validate(
            await remote("POST", "/inventory/reserve", body=request.model_dump(mode="json"))
        )


class DatabaseLogistics(DatabaseRefund):
    async def query(self, order_id, actor):
        from commerce.repository import _logistics

        return await _logistics(order_id, actor)

    async def dispatch(self, request):
        return await self.execute(request)


class HTTPLogistics(DatabaseLogistics):
    async def query(self, order_id, actor):
        return Logistics.model_validate(
            await remote("GET", "/logistics/" + order_id, actor=actor)
        ).model_dump()

    async def dispatch(self, request):
        return ExecutionStep.model_validate(
            await remote("POST", "/dispatches", body=request.model_dump(mode="json"))
        )


def ports():
    if CommerceSettings().BUSINESS_BACKEND == "http":
        return HTTPOrder(), HTTPRefund(), HTTPInventory(), HTTPLogistics()
    return DatabaseOrder(), DatabaseRefund(), DatabaseInventory(), DatabaseLogistics()
