import asyncio
import os
import sys
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg import OperationalError, sql

from commerce.config import CommerceSettings
from commerce.db import connect_sync
from commerce.migrate import migrate
from commerce.seed import seed


@pytest.fixture(scope="module")
def demo_db():
    from psycopg import Connection

    config = CommerceSettings()
    dbname = "agent_commerce_test_" + uuid4().hex[:10]
    kwargs = config.connection_kwargs() | {"dbname": "postgres"}
    with Connection.connect(**kwargs, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    original = os.environ.get("POSTGRES_DB")
    os.environ["POSTGRES_DB"] = dbname
    try:
        migrate()
        seed()
        yield dbname
    finally:
        if original is None:
            os.environ.pop("POSTGRES_DB", None)
        else:
            os.environ["POSTGRES_DB"] = original
        with Connection.connect(**kwargs, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(dbname)))


@pytest.fixture
def client(demo_db):
    from commerce.api import router
    from commerce.approval_api import router as approval_router
    from commerce.chat_api import router as chat_router
    from commerce.execution_api import router as execution_router
    from commerce.preview_api import router as preview_router

    app = FastAPI()
    app.include_router(router)
    app.include_router(chat_router)
    app.include_router(preview_router)
    app.include_router(approval_router)
    app.include_router(execution_router)
    from commerce.product_api import router as product_router
    app.include_router(product_router)

    @app.exception_handler(OperationalError)
    async def unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "业务数据库暂时不可用"})

    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(app, backend_options=options) as session:
        yield session


@pytest.fixture
def login_user(client):
    response = client.post(
        "/api/v1/auth/login", json={"username": "user001", "password": "Demo-1101!"}
    )
    assert response.status_code == 200
    return client


@pytest.fixture
def db(demo_db):
    with connect_sync() as conn:
        yield conn
        conn.rollback()


def pytest_asyncio_loop_factories():
    return {"selector": asyncio.SelectorEventLoop}
