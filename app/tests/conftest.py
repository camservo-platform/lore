"""Integration tests against real Postgres (pgvector) and Redis. Start them with:

    docker run -d --name lore-test-pg -e POSTGRES_PASSWORD=lore -p 55432:5432 pgvector/pgvector:0.8.6-pg18
    docker run -d --name lore-test-redis -p 56379:6379 redis:8.8.3

or point LORE_TEST_DATABASE_URL / LORE_TEST_REDIS_URL elsewhere. The database is wiped.
"""

import hashlib
import json
import math
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from mcp import Client
from redis.asyncio import Redis

from lore.db import create_pool
from lore.events import EventBus
from lore.mcp import game, lore
from lore.mcp.common import AppState
from lore.migrate import migrate

DATABASE_URL = os.environ.get("LORE_TEST_DATABASE_URL", "postgresql://postgres:lore@localhost:55432/postgres")
REDIS_URL = os.environ.get("LORE_TEST_REDIS_URL", "redis://localhost:56379/15")


class FakeEmbedder:
    """Bag-of-words hashed into 768 dims: texts sharing words are similar, without a model."""

    async def embed_document(self, text: str) -> list[float]:
        return self._embed(text)

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    @staticmethod
    def _embed(text: str) -> list[float]:
        vec = [0.0] * 768
        for word in re.findall(r"[a-z]+", text.lower()):
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 768] += 1
        norm = math.sqrt(sum(v * v for v in vec)) or 1
        return [v / norm for v in vec]


@pytest.fixture(scope="session")
async def app_state():
    # Plain connection first: the pool's codecs need the vector extension to exist.
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await migrate(conn)
    finally:
        await conn.close()
    pool = await create_pool(DATABASE_URL)
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    await redis.flushdb()
    yield AppState(pool=pool, bus=EventBus(redis), embedder=FakeEmbedder())
    await redis.aclose()
    await pool.close()


@pytest.fixture(scope="session")
def redis(app_state) -> Redis:
    return app_state.bus._redis


def _lifespan(app_state):
    @asynccontextmanager
    async def lifespan(_server):
        yield app_state

    return lifespan


class Tools:
    """Calls tools and returns their structured result, raising ToolFailed on tool errors.

    Each call uses its own in-process client: anyio requires a client to be opened and
    closed in the same task, which a pytest fixture spanning setup/teardown can't promise.
    """

    def __init__(self, server):
        self._server = server

    async def __call__(self, tool: str, /, **arguments: Any) -> Any:
        async with Client(self._server) as client:
            result = await client.call_tool(tool, arguments)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raise ToolFailed(text)
        if result.structured_content is not None:
            content = result.structured_content
            # Non-object return values are wrapped as {"result": ...}.
            return content["result"] if set(content) == {"result"} else content
        return json.loads(text)


class ToolFailed(Exception):
    pass


@pytest.fixture
def game_tools(app_state) -> Tools:
    return Tools(game.create_server(_lifespan(app_state)))


@pytest.fixture
def lore_tools(app_state) -> Tools:
    return Tools(lore.create_server(_lifespan(app_state)))


@pytest.fixture
async def campaign(game_tools) -> str:
    name = f"Test {uuid.uuid4().hex[:8]}"
    await game_tools("create_campaign", name=name, setting="A test realm.")
    return name
