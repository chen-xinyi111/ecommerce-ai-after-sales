import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from commerce.mcp_models import INPUTS, OUTPUTS
from commerce.mcp_security import service_key, sign_context
from commerce.telemetry import carrier, instrument

MCP_URL = "http://127.0.0.1:8080/internal/commerce-mcp/mcp"
logger = logging.getLogger(__name__)


class MCPFailure(Exception):
    pass


def client_factory(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False)


class Gateway:
    def __init__(self, context, allowed_tools, *, max_calls=8):
        self.context = context
        self.allowed_tools = frozenset(allowed_tools)
        self.max_calls = max_calls
        self.calls = []

    @asynccontextmanager
    async def session(self):
        if urlsplit(MCP_URL).hostname not in {"127.0.0.1", "localhost"}:
            raise MCPFailure("MCP 地址必须为本机内部服务")
        headers = {
            "Authorization": "Bearer " + service_key(),
            "X-Commerce-Context": sign_context(self.context),
            "traceparent": carrier(),
        }
        try:
            async with (
                client_factory(headers=headers, timeout=httpx.Timeout(15)) as http_client,
                streamable_http_client(MCP_URL, http_client=http_client) as (reader, writer, _),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                yield session
        except MCPFailure:
            raise
        except Exception as exc:

            def exception_types(error):
                return [type(error).__name__] + [
                    name
                    for child in getattr(error, "exceptions", [])
                    for name in exception_types(child)
                ]

            logger.warning("Commerce MCP transport failed: %s", ",".join(exception_types(exc)))
            raise MCPFailure("MCP 服务暂时不可用，任务已停止，请联系人工客服") from None

    @instrument('mcp.client.call')
    async def call(self, session, name, arguments):
        from opentelemetry import trace
        trace.get_current_span().set_attribute('tool', name)
        if name not in self.allowed_tools or name not in INPUTS:
            raise MCPFailure("当前 Agent 或 Skill 不允许调用此工具")
        if len(self.calls) >= self.max_calls:
            raise MCPFailure("MCP 工具调用已达到上限")
        attempt = {"name": name, "arguments": arguments, "result": None, "status": "failed"}
        self.calls.append(attempt)
        try:
            request = INPUTS[name].model_validate(arguments)
            response = await asyncio.wait_for(
                session.call_tool(name, {"request": request.model_dump(mode="json")}), timeout=15
            )
            if response.isError or response.structuredContent is None:
                raise MCPFailure("MCP 工具拒绝请求，任务已停止，请联系人工客服")
            result = (
                OUTPUTS[name].model_validate(response.structuredContent).model_dump(mode="json")
            )
        except ValidationError:
            raise MCPFailure("MCP 参数或结果不符合结构定义，任务已停止") from None
        attempt.update(result=result, status="completed")
        return result
