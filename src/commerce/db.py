from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from commerce.config import CommerceSettings

_async_pool = None
_sync_pool = None
_pool_settings = None


async def reset_async(conn):
    await conn.set_autocommit(True)
    await conn.execute("DISCARD ALL")
    await conn.set_autocommit(False)


def reset_sync(conn):
    conn.autocommit = True
    conn.execute("DISCARD ALL")
    conn.autocommit = False


@asynccontextmanager
async def pool_lifespan(*, max_size=24):
    """Process-scoped pools; callers outside the server lifecycle retain direct connections.

    DISCARD ALL removes session locks and settings before reuse. Disable preparation so
    psycopg's statement cache cannot reference a server statement cleared by DISCARD.
    Match the complete connection settings to prevent pooling across database switches.
    """
    global _async_pool, _sync_pool, _pool_settings
    if _async_pool is not None:
        raise RuntimeError("Commerce connection pools already open")
    config = CommerceSettings().connection_kwargs()
    options = config | {"row_factory": dict_row, "prepare_threshold": None}
    async_pool = AsyncConnectionPool(
        kwargs=options,
        min_size=min(4, max_size),
        max_size=max_size,
        open=False,
        timeout=10,
        reset=reset_async,
        check=AsyncConnectionPool.check_connection,
    )
    sync_pool = ConnectionPool(
        kwargs=options,
        min_size=1,
        max_size=4,
        open=False,
        timeout=5,
        reset=reset_sync,
        check=ConnectionPool.check_connection,
    )
    try:
        await async_pool.open(wait=True)
        sync_pool.open(wait=True)
        _async_pool, _sync_pool, _pool_settings = async_pool, sync_pool, config
        yield
    finally:
        _async_pool = _sync_pool = _pool_settings = None
        await async_pool.close()
        sync_pool.close()


@asynccontextmanager
async def connect() -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
    if _async_pool is not None and CommerceSettings().connection_kwargs() == _pool_settings:
        async with _async_pool.connection() as conn:
            yield conn
        return
    async with await AsyncConnection[dict[str, Any]].connect(
        **CommerceSettings().connection_kwargs(), row_factory=dict_row
    ) as conn:
        yield conn


@contextmanager
def connect_sync() -> Iterator[Connection[dict[str, Any]]]:
    if _sync_pool is not None and CommerceSettings().connection_kwargs() == _pool_settings:
        with _sync_pool.connection() as conn:
            yield conn
        return
    with Connection[dict[str, Any]].connect(
        **CommerceSettings().connection_kwargs(), row_factory=dict_row
    ) as conn:
        yield conn
