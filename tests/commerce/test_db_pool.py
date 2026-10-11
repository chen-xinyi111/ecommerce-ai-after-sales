"""Pooling must not leak transactions, advisory locks, settings or database identity."""

import pytest

from commerce.db import connect, connect_sync, pool_lifespan


@pytest.mark.asyncio
async def test_async_pool_rollback_session_reset_and_reuse(demo_db):
    async with pool_lifespan(max_size=1):
        with pytest.raises(RuntimeError, match="rollback"):
            async with connect() as conn:
                pid = (await (await conn.execute("SELECT pg_backend_pid() AS pid")).fetchone())[
                    "pid"
                ]
                await conn.execute(
                    "UPDATE commerce.inventory SET on_hand=on_hand+123 WHERE sku_id='SKU-001'"
                )
                await conn.execute("SELECT pg_advisory_lock(3201001)")
                await conn.execute("SET application_name='leaked-pool-setting'")
                raise RuntimeError("rollback")
        async with connect() as conn:
            record = await (
                await conn.execute(
                    "SELECT pg_backend_pid() AS pid,current_setting('application_name') AS name"
                )
            ).fetchone()
            assert record["pid"] == pid
            assert record["name"] != "leaked-pool-setting"
            locks = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'"
                )
            ).fetchone()
            assert locks["n"] == 0
        # Validate rollback from a separately authenticated connection, not the pool cache.
        with connect_sync() as conn:
            assert (
                conn.execute(
                    "SELECT on_hand FROM commerce.inventory WHERE sku_id='SKU-001'"
                ).fetchone()["on_hand"]
                < 123
            )


@pytest.mark.asyncio
async def test_sync_pool_resets_autocommit_and_locks(demo_db):
    async with pool_lifespan(max_size=1):
        with connect_sync() as conn:
            conn.autocommit = True
            conn.execute("SELECT pg_advisory_lock(3201002)")
            conn.execute("SET application_name='sync-leaked-setting'")
        with connect_sync() as conn:
            assert not conn.autocommit
            record = conn.execute("SELECT current_setting('application_name') AS name").fetchone()
            assert record["name"] != "sync-leaked-setting"
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'"
                ).fetchone()["n"]
                == 0
            )


@pytest.mark.asyncio
async def test_database_switch_never_reuses_another_database_pool(demo_db, monkeypatch):
    async with pool_lifespan(max_size=1):
        monkeypatch.setenv("POSTGRES_DB", "postgres")
        async with connect() as conn:
            assert (await (await conn.execute("SELECT current_database() AS db")).fetchone())[
                "db"
            ] == "postgres"
        with connect_sync() as conn:
            assert conn.execute("SELECT current_database() AS db").fetchone()["db"] == "postgres"
        monkeypatch.setenv("POSTGRES_DB", demo_db)
        async with connect() as conn:
            assert (await (await conn.execute("SELECT current_database() AS db")).fetchone())[
                "db"
            ] == demo_db
