import base64
import hashlib
import hmac
import json
import time
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from commerce.auth import actor_from_session_hash
from commerce.db import connect
from commerce.repository import visible_order
from core import settings


class Delegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    order_id: str = Field(pattern=r"^ORD-\d{6}$")
    run_id: UUID
    expires_at: int
    purpose: Literal["chat", "execution"] = "chat"
    application_id: UUID | None = None


def service_key():
    if not settings.AUTH_SECRET:
        raise RuntimeError("Commerce MCP requires AUTH_SECRET")
    return settings.AUTH_SECRET.get_secret_value()


def sign_context(context: dict):
    data = Delegation(
        session_hash=context.get("business_session_hash"),
        order_id=context["business_order_id"],
        run_id=context["business_run_id"],
        expires_at=int(time.time()) + 240,
        purpose=context.get("business_purpose", "chat"),
        application_id=context.get("business_application_id"),
    ).model_dump(mode="json")
    payload = base64.urlsafe_b64encode(json.dumps(data, sort_keys=True).encode()).decode()
    signature = hmac.new(service_key().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def verify_context(token: str):
    try:
        if len(token) > 2048:
            raise ValueError
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(service_key().encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        delegation = Delegation.model_validate_json(base64.urlsafe_b64decode(payload))
        now = int(time.time())
        if not now < delegation.expires_at <= now + 240:
            raise ValueError
        return delegation
    except (ValueError, TypeError):
        raise HTTPException(401, "MCP 委托凭证无效或已过期") from None


async def authorize(headers, order_id=None):
    delegation = verify_context(headers.get("x-commerce-context", ""))
    if delegation.purpose != "chat" or delegation.session_hash is None:
        raise HTTPException(403, "此委托不能调用聊天读取工具")
    if order_id and order_id != delegation.order_id:
        raise HTTPException(404, "当前任务不可访问该订单")
    actor = await actor_from_session_hash(delegation.session_hash)
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM commerce.chat_runs r JOIN commerce.chat_threads t ON t.id=r.thread_id "
            "WHERE r.id=%s AND r.session_hash=%s AND t.order_id=%s AND t.user_id=%s "
            "AND r.status IN ('queued','running')",
            (delegation.run_id, delegation.session_hash, delegation.order_id, actor.id),
        )
        if await cursor.fetchone() is None:
            raise HTTPException(403, "MCP 委托与当前任务不匹配")
        await visible_order(conn, delegation.order_id, actor)
    return actor


async def authorize_execution(headers, request=None):
    from commerce.models import Principal

    delegation = verify_context(headers.get("x-commerce-context", ""))
    if delegation.purpose != "execution" or delegation.application_id is None:
        raise HTTPException(403, "聊天 Agent 没有写入权限")
    if request and (
        request.job_id != delegation.run_id or request.application_id != delegation.application_id
    ):
        raise HTTPException(403, "写入工具参数与执行委托不匹配")
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT u.id,u.username,u.display_name,u.role FROM commerce.execution_jobs j "
            "JOIN commerce.applications a ON a.id=j.application_id JOIN commerce.users u ON u.id=j.requested_by "
            "WHERE j.id=%s AND j.application_id=%s AND a.order_id=%s AND j.status='running' AND u.active AND u.role='staff'",
            (delegation.run_id, delegation.application_id, delegation.order_id),
        )
        actor = await cursor.fetchone()
    if not actor:
        raise HTTPException(403, "写入委托无有效运行任务")
    return Principal(**actor)


class InternalMCPAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            try:
                if request.headers.get("origin") or request.headers.get("sec-fetch-site"):
                    raise HTTPException(403, "MCP 仅供内部服务调用")
                if not hmac.compare_digest(
                    request.headers.get("authorization", ""), "Bearer " + service_key()
                ):
                    raise HTTPException(401, "MCP 需要内部服务凭证")
                delegation = verify_context(request.headers.get("x-commerce-context", ""))
                if delegation.purpose == "execution":
                    await authorize_execution(request.headers)
                else:
                    await authorize(request.headers)
            except HTTPException as exc:
                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
