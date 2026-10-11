"""Product accounts and service collaboration; financial writes stay behind approval gates."""

import asyncio
import base64
import secrets
import smtplib
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from commerce import approval_repository as approval
from commerce import auth
from commerce import chat_repository as chat
from commerce.config import CommerceSettings
from commerce.db import connect

router = APIRouter(prefix="/api/v1", tags=["账号与服务协作"])
Actor = Annotated[auth.Principal, Depends(auth.current_actor)]
Write = [Depends(auth.same_origin)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Password = Annotated[
    str, StringConstraints(min_length=10, max_length=128, strip_whitespace=False, pattern=r"\S")
]


class Register(Input):
    username: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{2,31}$")
    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    email: str | None = Field(default=None, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=254)
    password: Password


class EmailInput(Input):
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=254)


class TokenInput(Input):
    token: str = Field(min_length=30, max_length=100)


class ResetInput(TokenInput):
    password: Password


class ChangeInput(Input):
    current_password: Annotated[
        str, StringConstraints(min_length=1, max_length=128, strip_whitespace=False)
    ]
    password: Password


async def rate_limit(request, purpose):
    ip = request.client.host if request.client else "local"
    # Separate transaction: failed auth must not roll back the attempt counter.
    async with connect() as conn:
        result = await (
            await conn.execute(
                "INSERT INTO commerce.auth_limits(bucket,attempts) VALUES(%s,1) ON CONFLICT(bucket) "
                "DO UPDATE SET attempts=CASE WHEN auth_limits.started_at<now()-interval '10 minutes' THEN 1 ELSE auth_limits.attempts+1 END, "
                "started_at=CASE WHEN auth_limits.started_at<now()-interval '10 minutes' THEN now() ELSE auth_limits.started_at END RETURNING attempts",
                (purpose + ":" + ip,),
            )
        ).fetchone()
    if result["attempts"] > 10:
        raise HTTPException(429, "请求较频繁，请十分钟后再试")


async def send_account_mail(email, purpose, token):
    config = CommerceSettings()
    mode = "verify" if purpose == "verify" else "reset"
    link = f"{config.PUBLIC_APP_URL}#/login?mode={mode}&token={token}"
    message = EmailMessage()
    message["From"] = config.SMTP_FROM
    message["To"] = email
    message["Subject"] = "青禾：验证邮箱" if mode == "verify" else "青禾：重置密码"
    message.set_content(
        f"请打开以下链接完成操作。链接有效期为 30 分钟，仅可使用一次。\n{link}\n如果不是本人操作，请忽略。"
    )
    if config.MAIL_BACKEND == "file":
        config.MAIL_DIR.mkdir(parents=True, exist_ok=True)
        (config.MAIL_DIR / f"{uuid4().hex}.eml").write_bytes(message.as_bytes())
    else:

        def deliver():
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
                smtp.starttls()
                if config.SMTP_USER:
                    smtp.login(config.SMTP_USER, config.SMTP_PASSWORD.get_secret_value())
                smtp.send_message(message)

        await asyncio.to_thread(deliver)


async def issue_token(conn, user_id, purpose):
    token = secrets.token_urlsafe(32)
    await conn.execute(
        "UPDATE commerce.account_tokens SET used_at=now() WHERE user_id=%s AND purpose=%s AND used_at IS NULL",
        (user_id, purpose),
    )
    await conn.execute(
        "INSERT INTO commerce.account_tokens(token_hash,user_id,purpose,expires_at) VALUES(%s,%s,%s,%s)",
        (auth.token_hash(token), user_id, purpose, datetime.now(UTC) + timedelta(minutes=30)),
    )
    return token


@router.post("/auth/register", status_code=201, dependencies=Write)
async def register(data: Register, request: Request):
    await rate_limit(request, "register")
    try:
        async with connect() as conn:
            user_id = "USR-" + uuid4().hex
            await conn.execute(
                "INSERT INTO commerce.users(id,username,display_name,password_hash,role,active,email) VALUES(%s,%s,%s,%s,'user',true,%s)",
                (
                    user_id,
                    data.username,
                    data.display_name or data.username,
                    auth.hasher.hash(data.password),
                    data.email.lower() if data.email else None,
                ),
            )
    except UniqueViolation:
        raise HTTPException(409, "账号已使用，请直接登录；忘记密码请联系管理员") from None
    return {"message": "注册成功，请使用账号和密码登录"}


@router.post("/auth/verify-email", dependencies=Write)
async def verify_email(data: TokenInput, request: Request):
    await rate_limit(request, "verify")
    async with connect() as conn:
        row = await (
            await conn.execute(
                "UPDATE commerce.account_tokens SET used_at=now() WHERE token_hash=%s AND purpose='verify' AND used_at IS NULL AND expires_at>now() RETURNING user_id",
                (auth.token_hash(data.token),),
            )
        ).fetchone()
        if not row:
            raise HTTPException(400, "验证链接已失效，请重新申请验证邮件")
        await conn.execute(
            "UPDATE commerce.users SET active=true,email_verified=true WHERE id=%s",
            (row["user_id"],),
        )
    return {"message": "邮箱验证成功，请登录"}


@router.post("/auth/forgot-password", dependencies=Write)
async def forgot(data: EmailInput, request: Request):
    await rate_limit(request, "forgot")
    try:
        async with connect() as conn:
            user = await (
                await conn.execute(
                    "SELECT id,email,email_verified FROM commerce.users WHERE lower(email)=%s",
                    (data.email.lower(),),
                )
            ).fetchone()
            if user:
                purpose = "reset" if user["email_verified"] else "verify"
                token = await issue_token(conn, user["id"], purpose)
                await send_account_mail(user["email"], purpose, token)
    except (OSError, smtplib.SMTPException):
        raise HTTPException(503, "邮件服务暂时不可用，请稍后重试") from None
    return {"message": "如果邮箱已登记，将收到操作邮件。请检查收件箱或稍后重试。"}


@router.post("/auth/reset-password", dependencies=Write)
async def reset_password(data: ResetInput, request: Request):
    await rate_limit(request, "reset")
    async with connect() as conn:
        row = await (
            await conn.execute(
                "UPDATE commerce.account_tokens SET used_at=now() WHERE token_hash=%s AND purpose='reset' AND used_at IS NULL AND expires_at>now() RETURNING user_id",
                (auth.token_hash(data.token),),
            )
        ).fetchone()
        if not row:
            raise HTTPException(400, "重置链接已失效，请重新申请邮件")
        await conn.execute(
            "UPDATE commerce.users SET password_hash=%s WHERE id=%s AND email_verified",
            (auth.hasher.hash(data.password), row["user_id"]),
        )
        await conn.execute("DELETE FROM commerce.sessions WHERE user_id=%s", (row["user_id"],))
    return {"message": "密码已重置，请重新登录"}


@router.post("/auth/change-password", dependencies=Write)
async def change_password(data: ChangeInput, actor: Actor):
    async with connect() as conn:
        user = await (
            await conn.execute(
                "SELECT password_hash FROM commerce.users WHERE id=%s FOR UPDATE", (actor.id,)
            )
        ).fetchone()
        try:
            auth.hasher.verify(user["password_hash"], data.current_password)
        except auth.VerificationError:
            raise HTTPException(400, "当前密码错误") from None
        await conn.execute(
            "UPDATE commerce.users SET password_hash=%s WHERE id=%s",
            (auth.hasher.hash(data.password), actor.id),
        )
        await conn.execute("DELETE FROM commerce.sessions WHERE user_id=%s", (actor.id,))
    return {"message": "密码已修改，请重新登录"}


@router.get("/auth/capabilities")
async def capabilities(actor: Actor):
    return {
        "manage_staff": actor.role == "staff"
        and actor.id in CommerceSettings().ADMIN_STAFF_IDS.split(",")
    }


@router.post("/staff/accounts", status_code=201, dependencies=Write)
async def create_staff(data: Register, actor: Actor):
    if actor.role != "staff" or actor.id not in CommerceSettings().ADMIN_STAFF_IDS.split(","):
        raise HTTPException(403, "仅管理员可创建客服账号")
    try:
        async with connect() as conn:
            user_id = "STAFF-" + uuid4().hex
            await conn.execute(
                "INSERT INTO commerce.users(id,username,display_name,password_hash,role,email,active) VALUES(%s,%s,%s,%s,'staff',%s,true)",
                (
                    user_id,
                    data.username,
                    data.display_name or data.username,
                    auth.hasher.hash(data.password),
                    data.email.lower() if data.email else None,
                ),
            )
    except UniqueViolation:
        raise HTTPException(409, "账号或邮箱已使用") from None
    return {"message": "客服账号已创建，可使用账号和初始密码登录"}


class AdminReset(Input):
    username: str = Field(min_length=1, max_length=32)
    password: Password


@router.post("/staff/accounts/reset-password", dependencies=Write)
async def admin_reset_password(data: AdminReset, actor: Actor):
    if actor.role != "staff" or actor.id not in CommerceSettings().ADMIN_STAFF_IDS.split(","):
        raise HTTPException(403, "仅管理员可重置账号密码")
    async with connect() as conn:
        user = await (
            await conn.execute(
                "SELECT id FROM commerce.users WHERE username=%s AND active FOR UPDATE",
                (data.username,),
            )
        ).fetchone()
        if not user:
            raise HTTPException(404, "未找到可用账号，请核对账号名称")
        await conn.execute(
            "UPDATE commerce.users SET password_hash=%s WHERE id=%s",
            (auth.hasher.hash(data.password), user["id"]),
        )
        await conn.execute("DELETE FROM commerce.sessions WHERE user_id=%s", (user["id"],))
        await conn.execute(
            "UPDATE commerce.account_tokens SET used_at=now() WHERE user_id=%s AND used_at IS NULL",
            (user["id"],),
        )
    return {"message": "密码已重置，请使用新密码重新登录"}


@router.get("/notifications")
async def notifications(actor: Actor):
    async with connect() as conn:
        rows = await (
            await conn.execute(
                "SELECT * FROM commerce.notifications WHERE user_id=%s ORDER BY id DESC LIMIT 100",
                (actor.id,),
            )
        ).fetchall()
    return {"items": rows, "unread": sum(row["read_at"] is None for row in rows)}


@router.post("/notifications/{notification_id}/read", status_code=204, dependencies=Write)
async def read_notification(notification_id: int, actor: Actor):
    async with connect() as conn:
        await conn.execute(
            "UPDATE commerce.notifications SET read_at=now() WHERE id=%s AND user_id=%s",
            (notification_id, actor.id),
        )


@router.get("/support")
async def support_list(actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        rows = await (
            await conn.execute(
                "SELECT s.*,t.order_id,t.title FROM commerce.support_sessions s JOIN commerce.chat_threads t ON t.id=s.thread_id WHERE s.state<>'closed' ORDER BY s.updated_at",
                (),
            )
        ).fetchall()
    return {"items": rows}


@router.get("/threads/{thread_id}/support")
async def support_history(thread_id: UUID, actor: Actor):
    async with connect() as conn:
        await chat.visible_thread(conn, thread_id, actor)
        session = await (
            await conn.execute(
                "SELECT * FROM commerce.support_sessions WHERE thread_id=%s", (thread_id,)
            )
        ).fetchone()
        rows = await (
            await conn.execute(
                "SELECT * FROM commerce.support_messages WHERE thread_id=%s ORDER BY id",
                (thread_id,),
            )
        ).fetchall()
    return {"session": session, "messages": rows}


@router.post("/threads/{thread_id}/support/request", dependencies=Write)
async def request_support(thread_id: UUID, actor: Actor):
    if actor.role != "user":
        raise HTTPException(403, "请由用户申请人工服务")
    async with connect() as conn:
        await chat.visible_thread(conn, thread_id, actor, lock=True)
        active = await (
            await conn.execute(
                "SELECT 1 FROM commerce.chat_runs WHERE thread_id=%s AND status IN ('queued','running')",
                (thread_id,),
            )
        ).fetchone()
        if active:
            raise HTTPException(409, "请等待当前回复结束后转人工")
        await conn.execute(
            "INSERT INTO commerce.support_sessions(thread_id,state) VALUES(%s,'queued') ON CONFLICT(thread_id) DO UPDATE SET state=CASE WHEN support_sessions.state='closed' THEN 'queued' ELSE support_sessions.state END,staff_id=CASE WHEN support_sessions.state='closed' THEN NULL ELSE support_sessions.staff_id END,updated_at=now()",
            (thread_id,),
        )
    return {"message": "已转人工，请等待客服接单"}


@router.post("/threads/{thread_id}/support/claim", dependencies=Write)
async def claim_support(thread_id: UUID, actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        await chat.visible_thread(conn, thread_id, actor, lock=True)
        row = await (
            await conn.execute(
                "UPDATE commerce.support_sessions SET state='active',staff_id=%s,updated_at=now() WHERE thread_id=%s AND (state='queued' OR (state='active' AND staff_id=%s)) RETURNING *",
                (actor.id, thread_id, actor.id),
            )
        ).fetchone()
        if not row:
            raise HTTPException(409, "咨询已被其他客服接单或已结束，请刷新")
    return row


@router.post("/threads/{thread_id}/support/close", dependencies=Write)
async def close_support(thread_id: UUID, actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        await chat.visible_thread(conn, thread_id, actor, lock=True)
        row = await (
            await conn.execute(
                "UPDATE commerce.support_sessions SET state='closed',updated_at=now() WHERE thread_id=%s AND state='active' AND staff_id=%s RETURNING *",
                (thread_id, actor.id),
            )
        ).fetchone()
        if not row:
            raise HTTPException(409, "请由接单客服结束咨询")
    return {"message": "人工服务已结束，可以继续使用售后助手"}


class SupportMessage(Input):
    content: str = Field(min_length=1, max_length=2000)
    request_id: UUID


@router.post("/threads/{thread_id}/support/messages", dependencies=Write)
async def support_message(thread_id: UUID, data: SupportMessage, actor: Actor):
    async with connect() as conn:
        await chat.visible_thread(conn, thread_id, actor, lock=True)
        session = await (
            await conn.execute(
                "SELECT * FROM commerce.support_sessions WHERE thread_id=%s FOR UPDATE",
                (thread_id,),
            )
        ).fetchone()
        if not session or session["state"] == "closed":
            raise HTTPException(409, "人工服务已结束，请刷新后继续咨询")
        if actor.role == "staff" and (
            session["state"] != "active" or session["staff_id"] != actor.id
        ):
            raise HTTPException(403, "请先接单，再回复用户")
        old = await (
            await conn.execute(
                "SELECT * FROM commerce.support_messages WHERE thread_id=%s AND actor_id=%s AND request_id=%s",
                (thread_id, actor.id, data.request_id),
            )
        ).fetchone()
        if old:
            if old["content"] != data.content:
                raise HTTPException(409, "请求编号已用于另一条消息")
            return old
        row = await (
            await conn.execute(
                "INSERT INTO commerce.support_messages(thread_id,actor_id,role,content,request_id) VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (thread_id, actor.id, actor.role, data.content, data.request_id),
            )
        ).fetchone()
        await conn.execute(
            "UPDATE commerce.support_sessions SET updated_at=now() WHERE thread_id=%s", (thread_id,)
        )
        await conn.execute(
            "UPDATE commerce.chat_threads SET updated_at=now() WHERE id=%s", (thread_id,)
        )
    return row


class MaterialInput(Input):
    note: str = Field(default="", max_length=2000)
    image: str | None = Field(default=None, max_length=2800000)
    name: str = Field(default="图片", max_length=100)


async def material_target(conn, target, actor, application):
    if application:
        app = await approval.visible_application(conn, target, actor, lock=True)
        return app["preview_id"], app
    row = await (
        await conn.execute("SELECT id,user_id FROM commerce.previews WHERE id=%s", (target,))
    ).fetchone()
    if not row or (actor.role != "staff" and row["user_id"] != actor.id):
        raise HTTPException(404, "方案不存在")
    return row["id"], None


async def material_list(target, actor, application):
    async with connect() as conn:
        preview_id, app = await material_target(conn, target, actor, application)
        rows = await (
            await conn.execute(
                "SELECT id,note,name,mime,created_at FROM commerce.materials WHERE preview_id=%s ORDER BY created_at",
                (preview_id,),
            )
        ).fetchall()
        requests = (
            await (
                await conn.execute(
                    "SELECT * FROM commerce.material_requests WHERE application_id=%s ORDER BY created_at",
                    (target,),
                )
            ).fetchall()
            if app
            else []
        )
    return {"items": rows, "requests": requests}


async def add_material(target, data, actor, application):
    if actor.role != "user":
        raise HTTPException(403, "仅申请用户可上传材料")
    image, mime = None, None
    if data.image:
        try:
            image = base64.b64decode(data.image, validate=True)
        except ValueError:
            raise HTTPException(422, "图片编码无效") from None
        if len(image) > 2097152:
            raise HTTPException(422, "图片不能超过 2 MB")
        mime = (
            "image/png"
            if image.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if image.startswith(b"\xff\xd8\xff")
            else None
        )
        if not mime:
            raise HTTPException(422, "仅支持 PNG、JPEG 图片")
    if not data.note and not image:
        raise HTTPException(422, "请填写说明或上传图片")
    async with connect() as conn:
        preview_id, app = await material_target(conn, target, actor, application)
        if app:
            if app["status"] != "pending_approval" or app["decision"]:
                raise HTTPException(409, "当前申请不能追加材料；业务信息变化请重新申请")
        else:
            await conn.execute(
                "SELECT id FROM commerce.previews WHERE id=%s FOR UPDATE", (preview_id,)
            )
            existing = await (
                await conn.execute(
                    "SELECT 1 FROM commerce.applications WHERE preview_id=%s", (preview_id,)
                )
            ).fetchone()
            if existing:
                raise HTTPException(409, "方案已提交，请从我的售后补充材料")
        count = await (
            await conn.execute(
                "SELECT count(*) AS n FROM commerce.materials WHERE preview_id=%s", (preview_id,)
            )
        ).fetchone()
        if count["n"] >= 10:
            raise HTTPException(409, "每份申请最多十条材料")
        await conn.execute(
            "INSERT INTO commerce.materials(id,preview_id,application_id,actor_id,note,image,mime,name) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                preview_id,
                target if app else None,
                actor.id,
                data.note,
                image,
                mime,
                data.name,
            ),
        )
        if app:
            await conn.execute(
                "UPDATE commerce.material_requests SET state='answered' WHERE application_id=%s AND state='pending'",
                (target,),
            )
            await approval.append_event(
                conn,
                target,
                f"material:{uuid4()}",
                "material_added",
                actor.id,
                label="用户已补充材料",
            )
    return {"message": "材料已保存"}


@router.get("/previews/{target}/materials")
async def preview_materials(target: UUID, actor: Actor):
    return await material_list(target, actor, False)


@router.post("/previews/{target}/materials", status_code=201, dependencies=Write)
async def preview_upload(target: UUID, data: MaterialInput, actor: Actor):
    return await add_material(target, data, actor, False)


@router.get("/applications/{target}/materials")
async def application_materials(target: UUID, actor: Actor):
    return await material_list(target, actor, True)


@router.post("/applications/{target}/materials", status_code=201, dependencies=Write)
async def application_upload(target: UUID, data: MaterialInput, actor: Actor):
    return await add_material(target, data, actor, True)


@router.get("/materials/{material_id}/image")
async def material_image(material_id: UUID, actor: Actor):
    async with connect() as conn:
        row = await (
            await conn.execute("SELECT * FROM commerce.materials WHERE id=%s", (material_id,))
        ).fetchone()
        if not row:
            raise HTTPException(404, "材料不存在")
        await material_target(
            conn, row["application_id"] or row["preview_id"], actor, bool(row["application_id"])
        )
        if not row["image"]:
            raise HTTPException(404, "没有图片")
    return Response(
        bytes(row["image"]),
        media_type=row["mime"],
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


class NoteInput(Input):
    note: str = Field(min_length=1, max_length=500)


@router.post("/applications/{app_id}/request-materials", dependencies=Write)
async def request_materials(app_id: UUID, data: NoteInput, actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        app = await approval.visible_application(conn, app_id, actor, lock=True)
        if (
            app["status"] != "pending_approval"
            or app["workflow_status"] != "waiting"
            or app["decision"]
        ):
            raise HTTPException(409, "仅待审批申请可以请求补充材料")
        pending = await (
            await conn.execute(
                "SELECT 1 FROM commerce.material_requests WHERE application_id=%s AND state='pending'",
                (app_id,),
            )
        ).fetchone()
        if pending:
            raise HTTPException(409, "已有补充请求，请等待用户提交")
        request_id = uuid4()
        await conn.execute(
            "INSERT INTO commerce.material_requests(id,application_id,staff_id,note) VALUES(%s,%s,%s,%s)",
            (request_id, app_id, actor.id, data.note),
        )
        await conn.execute(
            "INSERT INTO commerce.notifications(user_id,application_id,event_key,title) VALUES(%s,%s,%s,%s)",
            (app["user_id"], app_id, "material:" + str(request_id), "请补充售后材料：" + data.note),
        )
        await approval.append_event(
            conn,
            app_id,
            f"material_request:{request_id}",
            "material_requested",
            actor.id,
            label="客服请求补充材料",
            note=data.note,
        )
    return {"message": "已通知用户补充材料，补齐前暂停审批"}


@router.get("/service-inbox")
async def service_inbox(actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        materials = await (
            await conn.execute(
                "SELECT DISTINCT a.id,a.order_id FROM commerce.applications a JOIN commerce.material_requests m ON m.application_id=a.id WHERE m.state='pending' AND a.status='pending_approval'",
                (),
            )
        ).fetchall()
        approval_count = await (
            await conn.execute(
                "SELECT count(*) AS n FROM commerce.applications WHERE status='pending_approval'",
                (),
            )
        ).fetchone()
        exceptions = await (
            await conn.execute(
                "SELECT count(*) AS n FROM commerce.execution_jobs WHERE status IN ('failed','manual_review','stopped')",
                (),
            )
        ).fetchone()
        support_count = await (
            await conn.execute(
                "SELECT count(*) AS n FROM commerce.support_sessions WHERE state<>'closed'", ()
            )
        ).fetchone()
    return {
        "materials": materials,
        "approval": approval_count["n"],
        "exceptions": exceptions["n"],
        "support": support_count["n"],
    }


class PolicyInput(Input):
    kind: Literal["refund", "exchange", "reship", "repair"]
    valid_from: date
    valid_until: date
    window_days: int = Field(ge=0, le=3650)
    allowed_reasons: list[Literal["quality", "wrong_item", "not_received", "changed_mind"]] = Field(
        min_length=1, max_length=4
    )
    require_received: bool
    explanation: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def dates(self):
        if self.valid_until < self.valid_from:
            raise ValueError("结束日期不能早于开始日期")
        return self


@router.get("/staff/policies")
async def policy_versions(actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        rows = await (
            await conn.execute("SELECT * FROM commerce.policies ORDER BY kind,version DESC", ())
        ).fetchall()
        today = await (
            await conn.execute("SELECT business_date FROM commerce.demo_marker WHERE id=1", ())
        ).fetchone()
    return {"items": rows, "business_date": today["business_date"]}


@router.post("/applications/{app_id}/reconcile", dependencies=Write)
async def reconcile(app_id: UUID, data: NoteInput, actor: Actor):
    from commerce import execution_repository as execution
    from commerce import execution_worker as worker

    approval.require_staff(actor)
    async with connect() as conn:
        await approval.visible_application(conn, app_id, actor, lock=True)
        job = await execution.row(
            conn,
            "SELECT * FROM commerce.execution_jobs WHERE application_id=%s FOR UPDATE",
            (app_id,),
        )
        if not job or job["status"] not in ("manual_review", "failed"):
            raise HTTPException(409, "当前申请无需人工对账，请刷新进度")
        receipt = await execution.row(
            conn, "SELECT 1 FROM commerce.action_results WHERE application_id=%s", (app_id,)
        )
        await approval.append_event(
            conn,
            app_id,
            f"reconcile:{uuid4()}",
            "reconcile_requested",
            actor.id,
            label="客服已核对办理记录",
            note=data.note,
        )
        if receipt:
            await execution.set_status(conn, job, "running")
    if receipt:
        # This only verifies durable ledger consistency. It never calls a write tool.
        await worker.settle(job["id"])
    return {
        "message": "已重新核对办理结果"
        if receipt
        else "已保存核对记录，暂无持久办理结果。仍需人工核对，请勿重复办理。"
    }


@router.post("/staff/policies", status_code=201, dependencies=Write)
async def publish_policy(data: PolicyInput, actor: Actor):
    approval.require_staff(actor)
    async with connect() as conn:
        await conn.execute("LOCK TABLE commerce.policies IN EXCLUSIVE MODE")
        last = await (
            await conn.execute(
                "SELECT * FROM commerce.policies WHERE kind=%s ORDER BY version DESC LIMIT 1",
                (data.kind,),
            )
        ).fetchone()
        # Keep established rule keys; a new version never rewrites old snapshots.
        conditions = dict(last["conditions"]) if last else {}
        if data.require_received != (data.kind in ("exchange", "repair")):
            raise HTTPException(422, "换货和维修需要收货；补发要求未收货，不能修改这些基本约束")
        conditions.update(
            window_days=data.window_days,
            reasons=list(dict.fromkeys(data.allowed_reasons)),
            received_required=data.require_received,
        )
        row = await (
            await conn.execute(
                "INSERT INTO commerce.policies(id,version,kind,valid_from,valid_until,conditions,explanation) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    last["id"] if last else "POL-" + data.kind.upper(),
                    (last["version"] + 1) if last else 3,
                    data.kind,
                    data.valid_from,
                    data.valid_until,
                    Jsonb(conditions),
                    data.explanation,
                ),
            )
        ).fetchone()
    return row
