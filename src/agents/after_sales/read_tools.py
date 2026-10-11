from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import ToolException, tool

from agents.after_sales.tools import OrderQueryInput
from commerce.auth import actor_from_session_hash
from commerce.repository import order_detail


@tool("query_order", args_schema=OrderQueryInput)
async def query_business_order(order_id: str, config: RunnableConfig) -> dict:
    """查询当前登录用户可访问的模拟订单；客服可以查询全部模拟订单。"""
    session_hash = config.get("configurable", {}).get("business_session_hash")
    if not isinstance(session_hash, str):
        raise ToolException("订单查询需要有效业务登录会话")
    selected_order = config.get("configurable", {}).get("business_order_id")
    if selected_order and order_id != selected_order:
        raise ToolException("本会话仅可查询当前选定订单，请为其他订单新建咨询")
    try:
        actor = await actor_from_session_hash(session_hash)
        result = await order_detail(order_id, actor)
        return result | {"simulated": True}
    except HTTPException as exc:
        raise ToolException(str(exc.detail)) from None


business_tools = [query_business_order]
