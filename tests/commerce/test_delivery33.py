import json
from types import SimpleNamespace

import pytest

from commerce import acceptance_seed
from commerce.db import connect_sync
from commerce.seed_1301 import seed_preview_policies


def test_acceptance_seed_guard_idempotence_and_collision(demo_db, monkeypatch):
    with pytest.raises(RuntimeError, match="named demonstration"):
        acceptance_seed.seed_acceptance()
    monkeypatch.setattr(
        acceptance_seed,
        "CommerceSettings",
        lambda: SimpleNamespace(POSTGRES_DB="agent_commerce_demo"),
    )
    with connect_sync() as conn:
        inventory = conn.execute("SELECT * FROM commerce.inventory ORDER BY sku_id").fetchall()
        old_orders = conn.execute("SELECT * FROM commerce.orders ORDER BY id").fetchall()
    assert len(acceptance_seed.seed_acceptance()["inserted"]) == 5
    assert acceptance_seed.seed_acceptance()["inserted"] == []
    with connect_sync() as conn:
        assert (
            conn.execute("SELECT * FROM commerce.inventory ORDER BY sku_id").fetchall() == inventory
        )
        assert (
            conn.execute(
                "SELECT * FROM commerce.orders WHERE scenario<>'acceptance-3.3' ORDER BY id"
            ).fetchall()
            == old_orders
        )
        conn.execute(
            "UPDATE commerce.orders SET scenario='unrelated-user-data' WHERE id='ORD-960001'"
        )
    with pytest.raises(RuntimeError, match="another dataset"):
        acceptance_seed.seed_acceptance()
    with connect_sync() as conn:
        conn.execute("UPDATE commerce.orders SET scenario='acceptance-3.3' WHERE id='ORD-960001'")


def test_prepared_cases_visible_and_expired_cannot_confirm(login_user, monkeypatch):
    from uuid import uuid4

    monkeypatch.setattr(
        acceptance_seed,
        "CommerceSettings",
        lambda: SimpleNamespace(POSTGRES_DB="agent_commerce_demo"),
    )
    acceptance_seed.seed_acceptance()
    seed_preview_policies()
    rows = login_user.get("/api/v1/demo-cases").json()["items"]
    assert len([r for r in rows if r["id"].startswith("acceptance33-")]) == 5
    expected = json.loads(acceptance_seed.CASES.read_text(encoding="utf-8"))[-1]
    response = login_user.post(
        "/api/v1/orders/ORD-960005/previews", json=expected["input"] | {"request_id": str(uuid4())}
    )
    assert response.status_code == 200
    preview = response.json()
    assert preview["status"] == "ineligible"
    response = login_user.post(
        f"/api/v1/previews/{preview['id']}/confirm",
        json={
            "preview_id": preview["id"],
            "preview_version": preview["version"],
            "content_sha256": preview["content_sha256"],
            "request_id": str(uuid4()),
        },
    )
    assert response.status_code == 409


def test_default_home_redirect_and_missing_build(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from service import service

    app = FastAPI()
    app.get("/")(service.commerce_home)
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307 and response.headers["location"] == "/app/"
        monkeypatch.setattr(service, "frontend_dist", service.frontend_dist / "missing-build")
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 503
        assert "尚未构建" in response.json()["detail"]
