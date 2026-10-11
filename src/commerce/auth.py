import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request, Response

from commerce.config import CommerceSettings
from commerce.db import connect
from commerce.models import LoginInput, Principal

COOKIE_NAME = "commerce_session"
hasher = PasswordHasher()


@cache
def dummy_hash() -> str:
    return hasher.hash(secrets.token_urlsafe(32))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in CommerceSettings().BUSINESS_ORIGINS.split(","):
        raise HTTPException(403, "请求来源不允许")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站请求")


async def login(data: LoginInput, response: Response) -> Principal:
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM commerce.users WHERE username=%s AND active", (data.username,)
        )
        row = await cursor.fetchone()
        try:
            hasher.verify(row["password_hash"] if row else dummy_hash(), data.password)
        except VerificationError:
            raise HTTPException(401, "账号或密码错误") from None
        if row is None:
            raise HTTPException(401, "账号或密码错误")
        token = secrets.token_urlsafe(32)
        config = CommerceSettings()
        await conn.execute(
            "INSERT INTO commerce.sessions(token_hash,user_id,expires_at) VALUES (%s,%s,%s)",
            (
                token_hash(token),
                row["id"],
                datetime.now(UTC) + timedelta(hours=config.SESSION_HOURS),
            ),
        )
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=config.COOKIE_SECURE,
            samesite="lax",
            max_age=config.SESSION_HOURS * 3600,
            path="/api/v1",
        )
        return Principal.model_validate(row)


async def current_actor(request: Request) -> Principal:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "请先登录")
    return await actor_from_session_hash(token_hash(token))


async def actor_from_session_hash(session_hash: str) -> Principal:
    async with connect() as conn:
        cursor = await conn.execute(
            "SELECT u.id,u.username,u.display_name,u.role FROM commerce.sessions s "
            "JOIN commerce.users u ON u.id=s.user_id "
            "WHERE s.token_hash=%s AND s.expires_at>now() AND u.active",
            (session_hash,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(401, "登录已失效")
        return Principal.model_validate(row)


async def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        async with connect() as conn:
            await conn.execute(
                "DELETE FROM commerce.sessions WHERE token_hash=%s", (token_hash(token),)
            )
    response.delete_cookie(COOKIE_NAME, path="/api/v1")
