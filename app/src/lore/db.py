import json

import asyncpg
from pgvector.asyncpg import register_vector


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await register_vector(conn)


async def create_pool(dsn: str | None = None, **kwargs) -> asyncpg.Pool:
    """Connection pool with jsonb and vector codecs. With no dsn, asyncpg uses the PG* env vars."""
    return await asyncpg.create_pool(dsn, init=_init_connection, min_size=1, max_size=10, **kwargs)
