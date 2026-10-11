import base64
import re
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from test_approvals import advance, binding, confirm, decide, login, preview
from test_execution import clone

from commerce import approval_workflow as flow
from commerce.db import connect_sync
from commerce.seed_1301 import seed_preview_policies


@pytest.fixture(scope="module", autouse=True)
def policies(demo_db):
    seed_preview_policies()


@pytest.fixture(autouse=True)
def checkpoint(monkeypatch):
    monkeypatch.setattr(flow, "approval_graph", flow.graph.compile(checkpointer=InMemorySaver()))


def token(mail_dir):
    latest = max(mail_dir.glob("*.eml"), key=lambda path: path.stat().st_mtime_ns)
    mail = BytesParser(policy=policy.default).parsebytes(latest.read_bytes())
    return re.search(r"token=([A-Za-z0-9_-]+)", mail.get_content()).group(1)


def test_registration_without_email_and_isolation(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MAIL_DIR", str(tmp_path))
    monkeypatch.setenv("MAIL_BACKEND", "smtp")  # Registration must never invoke real mail.
    data = dict(username="newuser", password="Original-1234!")
    assert client.post("/api/v1/auth/register", json=data).status_code == 201
    assert not list(tmp_path.glob("*.eml"))
    assert client.post("/api/v1/auth/login", json=data).status_code == 200
    assert client.get("/api/v1/auth/me").json()["display_name"] == "newuser"
    assert client.get("/api/v1/orders").json()["total"] == 0
    assert client.get("/api/v1/orders/ORD-200001").status_code == 404
    assert client.get("/api/v1/staff/policies").status_code == 403
    assert client.post("/api/v1/staff/accounts", json=data).status_code == 403
    assert client.post("/api/v1/staff/accounts/reset-password", json=data).status_code == 403
    assert client.post("/api/v1/auth/register", json=data).status_code == 409
    assert client.post("/api/v1/auth/register", json=data | {"role": "staff"}).status_code == 422
    assert (
        client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "bad", "password": "Final-12345!"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/auth/change-password",
            json={"current_password": data["password"], "password": "Final-12345!"},
        ).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me").status_code == 401
    assert (
        client.post("/api/v1/auth/login", json=data | {"password": "Final-12345!"}).status_code
        == 200
    )


def test_unknown_email_expiry_and_duplicate(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MAIL_DIR", str(tmp_path))
    response = client.post("/api/v1/auth/forgot-password", json={"email": "unknown@example.test"})
    assert response.status_code == 200 and not list(tmp_path.glob("*.eml"))
    data = dict(
        username="expireduser",
        display_name="用户",
        email="expired@example.test",
        password="Original-1234!",
    )
    assert client.post("/api/v1/auth/register", json=data).status_code == 201
    assert not list(tmp_path.glob("*.eml"))
    # Legacy email endpoints remain compatible, but registration no longer uses them.
    assert (
        client.post("/api/v1/auth/forgot-password", json={"email": data["email"]}).status_code
        == 200
    )
    value = token(tmp_path)
    with connect_sync() as conn:
        conn.execute(
            "UPDATE commerce.account_tokens SET expires_at=now()-interval '1 minute' WHERE user_id=(SELECT id FROM commerce.users WHERE username='expireduser')"
        )
    assert client.post("/api/v1/auth/verify-email", json={"token": value}).status_code == 400
    assert client.post("/api/v1/auth/register", json=data).status_code == 409


def test_password_spaces_are_preserved_not_silently_changed(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MAIL_DIR", str(tmp_path))
    data = dict(
        username="spaceuser",
        display_name="用户",
        email="space@example.test",
        password="  Password-1234!  ",
    )
    assert (
        client.post("/api/v1/auth/register", json=data | {"password": "          "}).status_code
        == 422
    )
    assert client.post("/api/v1/auth/register", json=data).status_code == 201
    assert not list(tmp_path.glob("*.eml"))
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"username": data["username"], "password": data["password"].strip()},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": data["username"], "password": data["password"]}
        ).status_code
        == 200
    )


def test_staff_accounts_only_admin(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MAIL_DIR", str(tmp_path))
    login(client, "staff002")
    data = dict(
        username="staffnew",
        display_name="客服",
        password="Original-1234!",
    )
    assert client.post("/api/v1/staff/accounts", json=data).status_code == 403
    login(client, "staff001")
    assert client.get("/api/v1/auth/capabilities").json()["manage_staff"]
    assert client.post("/api/v1/staff/accounts", json=data).status_code == 201
    assert not list(tmp_path.glob("*.eml"))
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "staffnew", "password": data["password"]}
        ).json()["role"]
        == "staff"
    )


def test_admin_password_reset_and_pending_registration_migration(client, tmp_path):
    from commerce import auth
    from commerce.migrate import MIGRATIONS

    data = dict(username="resetuser", password="Original-1234!")
    assert client.post("/api/v1/auth/register", json=data).status_code == 201
    assert client.post("/api/v1/auth/login", json=data).status_code == 200
    old_cookie = client.cookies.get(auth.COOKIE_NAME)
    with connect_sync() as conn:
        user_id = conn.execute(
            "SELECT id FROM commerce.users WHERE username=%s", (data["username"],)
        ).fetchone()["id"]
        conn.execute("UPDATE commerce.users SET active=false WHERE id=%s", (user_id,))
        conn.execute(
            "INSERT INTO commerce.account_tokens(token_hash,user_id,purpose,expires_at) VALUES(%s,%s,'verify',now()+interval '30 minutes')",
            (auth.token_hash("pending-" + uuid4().hex), user_id),
        )
        conn.execute("UPDATE commerce.users SET active=false WHERE id='USR-002'")
        conn.execute((MIGRATIONS / "011_local_registration.sql").read_bytes())
        assert conn.execute("SELECT active FROM commerce.users WHERE id=%s", (user_id,)).fetchone()[
            "active"
        ]
        assert not conn.execute("SELECT active FROM commerce.users WHERE id='USR-002'").fetchone()[
            "active"
        ]
        conn.execute("UPDATE commerce.users SET active=true WHERE id='USR-002'")
        assert conn.execute(
            "SELECT used_at FROM commerce.account_tokens WHERE user_id=%s", (user_id,)
        ).fetchone()["used_at"]
    login(client, "staff002")
    assert client.post("/api/v1/staff/accounts/reset-password", json=data).status_code == 403
    login(client, "staff001")
    assert (
        client.post(
            "/api/v1/staff/accounts/reset-password",
            json={"username": "missing-account", "password": "NewPassword-123!"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/staff/accounts/reset-password", json=data | {"password": "NewPassword-123!"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Cookie": f"{auth.COOKIE_NAME}={old_cookie}"}
        ).status_code
        == 401
    )
    assert client.post("/api/v1/auth/login", json=data).status_code == 401
    assert (
        client.post("/api/v1/auth/login", json=data | {"password": "NewPassword-123!"}).status_code
        == 200
    )


def test_human_takeover_blocks_auto_and_keeps_history(client):
    login(client)
    thread = client.post("/api/v1/threads", json={"order_id": "ORD-200001"}).json()["id"]
    root = f"/api/v1/threads/{thread}/support"
    assert client.post(root + "/request").status_code == 200
    assert (
        client.post(
            f"/api/v1/threads/{thread}/messages",
            json={"message": "退款", "request_id": str(uuid4())},
        ).status_code
        == 409
    )
    message = dict(content="这是用户原始消息，模拟验收内容不应替换", request_id=str(uuid4()))
    assert client.post(root + "/messages", json=message).status_code == 200
    assert client.post(root + "/messages", json=message).json()["content"] == message["content"]
    login(client, "user002")
    assert client.get(root).status_code == 404
    login(client, "staff001")
    assert client.get("/api/v1/support").json()["items"]
    assert (
        client.post(
            root + "/messages", json={"content": "回复", "request_id": str(uuid4())}
        ).status_code
        == 403
    )
    assert client.post(root + "/claim").status_code == 200
    assert client.get(f"/api/v1/threads/{thread}").status_code == 200
    login(client, "staff002")
    assert client.post(root + "/claim").status_code == 409
    assert client.post(root + "/close").status_code == 409
    login(client, "staff001")
    assert (
        client.post(
            root + "/messages", json={"content": "客服回复", "request_id": str(uuid4())}
        ).status_code
        == 200
    )
    assert client.post(root + "/close").status_code == 200
    login(client)
    history = client.get(root).json()
    assert len(history["messages"]) == 2 and history["messages"][0]["content"] == message["content"]
    assert (
        client.post(
            f"/api/v1/threads/{thread}/messages",
            json={"message": "查询订单", "request_id": str(uuid4())},
        ).status_code
        == 202
    )


def test_takeover_waits_for_running_reply(client):
    login(client)
    thread = client.post("/api/v1/threads", json={"order_id": "ORD-200001"}).json()["id"]
    assert (
        client.post(
            f"/api/v1/threads/{thread}/messages",
            json={"message": "查询", "request_id": str(uuid4())},
        ).status_code
        == 202
    )
    assert client.post(f"/api/v1/threads/{thread}/support/request").status_code == 409


@pytest.mark.asyncio
async def test_closed_handoff_context_preserves_user_text(client):
    from commerce.chat_worker import human_context

    login(client)
    thread = client.post("/api/v1/threads", json={"order_id": "ORD-200001"}).json()["id"]
    root = f"/api/v1/threads/{thread}/support"
    assert client.post(root + "/request").status_code == 200
    text = "用户原始描述：模拟验收商品有问题，申请数量为2"
    assert (
        client.post(
            root + "/messages", json={"content": text, "request_id": str(uuid4())}
        ).status_code
        == 200
    )
    assert await human_context(thread) is None
    login(client, "staff001")
    assert client.post(root + "/claim").status_code == 200
    assert (
        client.post(
            root + "/messages",
            json={"content": "已了解，需要您本人确认方案", "request_id": str(uuid4())},
        ).status_code
        == 200
    )
    assert client.post(root + "/close").status_code == 200
    context = await human_context(thread)
    assert text in context.content and "执行权限仍以业务查询及审批记录为准" in context.content
    from langgraph.graph.message import add_messages

    repeated = await human_context(thread)
    assert context.id == repeated.id and context.id.startswith("support-context:")
    assert len(add_messages([context], [repeated])) == 1


@pytest.mark.asyncio
async def test_material_permissions_approval_gate_notifications(client):
    login(client)
    p = preview(client, order=clone())
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"image").decode()
    path = f"/api/v1/previews/{p['id']}/materials"
    assert (
        client.post(path, json={"note": "包装损坏", "image": image, "name": "照片.png"}).status_code
        == 201
    )
    assert (
        client.post(path, json={"image": base64.b64encode(b"<script>").decode()}).status_code == 422
    )
    material_id = client.get(path).json()["items"][0]["id"]
    app = confirm(client, p)
    assert client.post(path, json={"note": "晚上传"}).status_code == 409
    await advance(app)
    notifications = client.get("/api/v1/notifications").json()["items"]
    assert any(n["application_id"] == app["id"] for n in notifications)
    notification = next(n for n in notifications if n["application_id"] == app["id"])
    assert client.post(f"/api/v1/notifications/{notification['id']}/read").status_code == 204
    login(client, "user002")
    assert client.get(f"/api/v1/materials/{material_id}/image").status_code == 404
    assert not client.get("/api/v1/notifications").json()["items"]
    login(client, "staff001")
    assert client.get(f"/api/v1/materials/{material_id}/image").status_code == 200
    request_path = f"/api/v1/applications/{app['id']}/request-materials"
    assert client.post(request_path, json={"note": "请提供清晰照片"}).status_code == 200
    assert decide(client, app).status_code == 409
    assert client.get("/api/v1/service-inbox").json()["materials"]
    login(client)
    assert (
        client.post(
            f"/api/v1/applications/{app['id']}/materials", json={"note": "追加说明"}
        ).status_code
        == 201
    )
    login(client, "staff001")
    assert decide(client, app).status_code == 200
    login(client)
    assert (
        client.post(
            f"/api/v1/applications/{app['id']}/materials", json={"note": "审批后改动"}
        ).status_code
        == 409
    )


def test_policy_publish_preserves_snapshot_and_changes_new_preview(client):
    login(client)
    p = preview(client, order=clone())
    original = client.get(f"/api/v1/orders/{p['order_id']}/previews").json()["items"][0]
    login(client, "staff001")
    rows = client.get("/api/v1/staff/policies").json()["items"]
    latest = next(row for row in rows if row["kind"] == "refund")
    data = dict(
        kind="refund",
        valid_from=latest["valid_from"],
        valid_until=latest["valid_until"],
        window_days=0,
        allowed_reasons=["quality"],
        require_received=False,
        explanation="仅购买当天支持退款",
    )
    response = client.post("/api/v1/staff/policies", json=data)
    assert response.status_code == 201 and response.json()["version"] == latest["version"] + 1
    assert len(client.get("/api/v1/staff/policies").json()["items"]) == len(rows) + 1
    login(client)
    assert client.post(f"/api/v1/previews/{p['id']}/confirm", json=binding(p)).status_code == 409
    assert client.get(f"/api/v1/orders/{p['order_id']}/previews").json()["items"][0] == original
    assert preview(client, order=p["order_id"])["status"] == "ineligible"
