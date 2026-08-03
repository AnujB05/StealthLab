"""
Connection pool setup.

The JSONB codec registration below is not optional. Without it asyncpg
returns JSONB columns as raw `str`, so every `properties` / `io_schema` /
`change_set` field silently arrives as a string that looks fine until
something tries to subscript it. Registering the codec at pool init is
the only place this needs handling.
"""
from __future__ import annotations

import json
from typing import Optional

import asyncpg

from app.config import settings

_pool: Optional[asyncpg.Pool] = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool(dsn: Optional[str] = None, **kwargs) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn or settings.require("database_url"),
        init=_init_connection,
        min_size=kwargs.pop("min_size", 1),
        max_size=kwargs.pop("max_size", 10),
        **kwargs,
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await create_pool()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
