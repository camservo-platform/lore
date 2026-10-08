"""Plumbing shared by the MCP servers: process-wide state, caller identity, HTTP runner."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from redis.asyncio import Redis
from starlette.requests import Request
from starlette.responses import JSONResponse

from lore.db import create_pool
from lore.embeddings import Embedder, OllamaEmbedder
from lore.events import EventBus
from lore.settings import Settings

# Set by the ingress (Traefik basicAuth headerField) to the authenticated username.
# In-cluster callers don't go through the ingress and are recorded as "gm".
USER_HEADER = "x-lore-user"
DEFAULT_ACTOR = "gm"


@dataclass
class AppState:
    pool: asyncpg.Pool
    bus: EventBus
    embedder: Embedder


@asynccontextmanager
async def default_lifespan(_server: MCPServer) -> AsyncIterator[AppState]:
    settings = Settings.from_env()
    pool = await create_pool()
    redis = Redis(
        host=settings.redis_host, port=settings.redis_port, password=settings.redis_password, decode_responses=True
    )
    embedder = OllamaEmbedder(settings)
    try:
        yield AppState(pool=pool, bus=EventBus(redis), embedder=embedder)
    finally:
        await embedder.aclose()
        await redis.aclose()
        await pool.close()


def state(ctx: Context) -> AppState:
    return ctx.request_context.lifespan_context


def actor(ctx: Context) -> str:
    request = getattr(ctx.request_context, "request", None)
    headers = getattr(request, "headers", None)
    return (headers and headers.get(USER_HEADER)) or DEFAULT_ACTOR


async def campaign_id(conn: asyncpg.Connection, name: str) -> int:
    cid = await conn.fetchval("SELECT id FROM campaigns WHERE lower(name) = lower($1)", name)
    if cid is None:
        raise ToolError(f"No campaign named {name!r}. Use list_campaigns to see campaigns.")
    return cid


def run(server: MCPServer, path: str) -> None:
    """Serves `server` over stateless streamable HTTP, so any replica can take any request."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    server.run(
        "streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
    )
