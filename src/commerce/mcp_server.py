from contextlib import asynccontextmanager

from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from psycopg import OperationalError

from commerce import execution_repository
from commerce.db import connect
from commerce.execution_models import ActionRequest, ExecutionRequest, ExecutionStep
from commerce.mcp_models import (
    HistoryResult,
    InventoryRequest,
    OrderRequest,
    PolicyRequest,
    PolicyResult,
    PreviewRequest,
    ShipmentResult,
)
from commerce.mcp_security import InternalMCPAuth, authorize, authorize_execution, service_key
from commerce.models import Inventory, Logistics, OrderDetail
from commerce.preview_models import PreviewResult
from commerce.preview_service import compute, policy_reference
from commerce.repository import inventory, logistics, order_detail
from commerce.seed import BUSINESS_DATE
from core import settings

mcp = FastMCP(
    "commerce-after-sales-v2.3",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    max_request_body_size=65536,
)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


async def actor_for(ctx, order_id):
    try:
        return await authorize(ctx.request_context.request.headers, order_id)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from None


@mcp.tool(annotations=READ_ONLY)
async def query_order(request: OrderRequest, ctx: Context) -> OrderDetail:
    """查询当前任务绑定的模拟订单、商品与支付信息。"""
    actor = await actor_for(ctx, request.order_id)
    return OrderDetail(**await order_detail(request.order_id, actor))


@mcp.tool(annotations=READ_ONLY)
async def query_logistics(request: OrderRequest, ctx: Context) -> ShipmentResult:
    """查询绑定订单物流；尚未发货时返回空物流，不推测丢失。"""
    actor = await actor_for(ctx, request.order_id)
    try:
        shipment = Logistics(**await logistics(request.order_id, actor))
    except HTTPException as exc:
        if exc.detail != "该订单暂无物流":
            raise ToolError(str(exc.detail)) from None
        shipment = None
    return ShipmentResult(order_id=request.order_id, shipment=shipment)


@mcp.tool(annotations=READ_ONLY)
async def query_inventory(request: InventoryRequest, ctx: Context) -> Inventory:
    """只查询绑定订单实际包含的 SKU 可用库存，不预留库存。"""
    actor = await actor_for(ctx, request.order_id)
    order = await order_detail(request.order_id, actor)
    if request.sku_id not in {item["sku_id"] for item in order["items"]}:
        raise ToolError("SKU 不属于当前订单")
    return Inventory(**await inventory(request.sku_id, actor))


@mcp.tool(annotations=READ_ONLY)
async def query_policy(request: PolicyRequest, ctx: Context) -> PolicyResult:
    """获取固定业务日期内有效的最新售后政策及版本。"""
    await actor_for(ctx, request.order_id)
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM commerce.policies WHERE kind=%s AND valid_from<=%s AND valid_until>=%s "
            "AND version>=3 ORDER BY version DESC LIMIT 1",
            (request.kind, BUSINESS_DATE, BUSINESS_DATE),
        )
        row = await cursor.fetchone()
    return PolicyResult(order_id=request.order_id, policy=policy_reference(row) if row else None)


@mcp.tool(annotations=READ_ONLY)
async def query_after_sales_history(request: OrderRequest, ctx: Context) -> HistoryResult:
    """查询当前订单已记录的历史售后数量和退款。"""
    actor = await actor_for(ctx, request.order_id)
    order = await order_detail(request.order_id, actor)
    return HistoryResult(order_id=request.order_id, items=order["after_sales_history"])


@mcp.tool(annotations=READ_ONLY)
async def preview_after_sales(request: PreviewRequest, ctx: Context) -> PreviewResult:
    """在一致的只读数据库快照中校验售后资格、金额和库存；不保存、不执行业务。"""
    actor = await actor_for(ctx, request.order_id)
    try:
        async with connect() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return await compute(conn, request.order_id, actor, request.draft)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from None
    except OperationalError:
        raise ToolError("业务数据库暂时不可用") from None


async def write_call(request, ctx, action):
    try:
        await authorize_execution(ctx.request_context.request.headers, request)
        return await action(request)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from None


@mcp.tool(annotations=WRITE)
async def create_work_order(request: ExecutionRequest, ctx: Context) -> ExecutionStep:
    """仅供受控执行 worker：重新核验批准快照，幂等创建模拟工单并预留库存。"""
    return await write_call(request, ctx, execution_repository.create_work_order)


@mcp.tool(annotations=WRITE)
async def execute_after_sales(request: ActionRequest, ctx: Context) -> ExecutionStep:
    """仅供受控执行 worker：按批准类型事务写入退款、发货或维修受理结果。"""
    return await write_call(request, ctx, execution_repository.execute_action)


mcp_app = InternalMCPAuth(mcp.streamable_http_app())


@asynccontextmanager
async def mcp_lifespan():
    if settings.COMMERCE_ENABLED is True:
        service_key()
        async with mcp.session_manager.run():
            yield
    else:
        yield
