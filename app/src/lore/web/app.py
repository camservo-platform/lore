"""The web table: a single-page UI plus the API behind it.

Who's playing comes from the X-Lore-User header (set by the ingress's basic auth), or
LORE_DEV_USER when running locally without the ingress.

Per campaign, Redis holds the GM conversation (shared by everyone at the table), a lock
so one turn runs at a time, and a `chat` stream that relays each finished turn to the
other players' browsers alongside the game's `events` stream.
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from lore import worldgen
from lore.events import stream_key
from lore.gm import GameMaster, Toolbox, ToolCallError, style_message
from lore.mcp.common import USER_HEADER
from lore.voice import Voice

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
TRANSCRIPT_TTL = 30 * 24 * 3600
LOCK_TTL = 600
KICKOFF = "(I've just sat down at the table and I'm ready to play.)"


@dataclass(frozen=True)
class WebSettings:
    redis_host: str
    redis_port: int
    redis_password: str | None
    mcp_servers: dict[str, str]
    llm_api_key: str
    llm_model: str
    llm_effort: str
    deepgram_api_key: str | None
    stt_model: str
    tts_model: str
    dev_user: str | None

    @classmethod
    def from_env(cls) -> "WebSettings":
        env = os.environ
        if env.get("LLM_PROVIDER", "anthropic") != "anthropic":
            raise SystemExit("Only LLM_PROVIDER=anthropic is supported.")
        return cls(
            redis_host=env.get("REDIS_HOST", "localhost"),
            redis_port=int(env.get("REDIS_PORT", "6379")),
            redis_password=env.get("REDIS_PASSWORD") or None,
            mcp_servers={"game": env["MCP_GAME_URL"], "lore": env["MCP_LORE_URL"]},
            llm_api_key=env["LLM_API_KEY"],
            llm_model=env.get("LLM_MODEL", "claude-opus-5-5"),
            llm_effort=env.get("LLM_EFFORT", "medium"),
            deepgram_api_key=env.get("DEEPGRAM_API_KEY") or None,
            stt_model=env.get("DEEPGRAM_STT_MODEL", "nova-3"),
            tts_model=env.get("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en"),
            dev_user=env.get("LORE_DEV_USER") or None,
        )


class Table:
    """Redis keys for one campaign's table."""

    def __init__(self, redis: Redis, campaign_id: int):
        self.redis = redis
        self.messages = f"lore:campaign:{campaign_id}:gm:messages"
        self.mode = f"lore:campaign:{campaign_id}:gm:mode"
        self.lock = f"lore:campaign:{campaign_id}:gm:lock"
        self.chat = f"lore:campaign:{campaign_id}:chat"
        self.events = stream_key(campaign_id)

    async def load(self) -> tuple[list[dict[str, Any]], str | None]:
        raw, mode = await self.redis.mget(self.messages, self.mode)
        return (json.loads(raw) if raw else []), mode

    async def save(self, messages: list[dict[str, Any]], mode: str) -> None:
        await self.redis.set(self.messages, json.dumps(messages), ex=TRANSCRIPT_TTL)
        await self.redis.set(self.mode, mode, ex=TRANSCRIPT_TTL)

    async def reset(self) -> None:
        await self.redis.delete(self.messages, self.mode)


def _complete_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drops a trailing assistant turn whose tool calls never got results (a turn that
    failed mid-way); it was never sent back to the API, so this edits nothing it has seen."""
    if messages and messages[-1]["role"] == "assistant" and any(
        b.get("type") == "tool_use" for b in messages[-1]["content"]
    ):
        return messages[:-1]
    return messages


def sse(events: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    async def body():
        async for event in events:
            if event is None:
                yield ": keepalive\n\n"
            else:
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


def create_app() -> Starlette:
    settings = WebSettings.from_env()
    redis = Redis(
        host=settings.redis_host, port=settings.redis_port, password=settings.redis_password, decode_responses=True
    )
    llm = anthropic.AsyncAnthropic(api_key=settings.llm_api_key)
    toolbox = Toolbox(settings.mcp_servers)
    gm = GameMaster(llm, toolbox, settings.llm_model, settings.llm_effort)
    voice = (
        Voice(settings.deepgram_api_key, settings.stt_model, settings.tts_model) if settings.deepgram_api_key else None
    )
    # Turns run as tasks so a dropped connection can't cut one off half-applied.
    background: set[asyncio.Task] = set()

    def user_of(request: Request) -> str:
        user = request.headers.get(USER_HEADER) or settings.dev_user
        if not user:
            raise HTTPException(401, "Not signed in.")
        return user

    def require_voice() -> Voice:
        if voice is None:
            raise HTTPException(503, "Speech is not configured (no DEEPGRAM_API_KEY).")
        return voice

    async def index(_request: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def me(request: Request) -> Response:
        return JSONResponse({"user": user_of(request), "speech": voice is not None})

    async def campaigns(request: Request) -> Response:
        async with toolbox.session(user_of(request)) as tools:
            return JSONResponse(await tools.call_json("list_campaigns"))

    async def campaign_state(request: Request) -> Response:
        name = request.query_params["name"]
        async with toolbox.session(user_of(request)) as tools:
            try:
                characters = await tools.call_json("list_characters", campaign=name)
                events = await tools.call_json("recent_events", campaign=name, limit=30)
            except ToolCallError as e:
                raise HTTPException(404, str(e)) from None
        return JSONResponse({"characters": characters, "events": events})

    async def forge_world(request: Request) -> Response:
        user = user_of(request)
        body = await request.json()
        return sse(worldgen.forge(
            llm, settings.llm_model, toolbox, user, body.get("theme", ""), (body.get("name") or "").strip() or None
        ))

    async def take_turn(request: Request) -> Response:
        user = user_of(request)
        table = Table(redis, int(request.path_params["campaign_id"]))
        body = await request.json()
        campaign = body["campaign"]
        mode = "speech" if body.get("mode") == "speech" else "text"
        text = (body.get("message") or "").strip() or KICKOFF
        turn_id = uuid.uuid4().hex

        if not await redis.set(table.lock, turn_id, nx=True, ex=LOCK_TTL):
            raise HTTPException(409, "The Game Master is answering another player; try again in a moment.")

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def run() -> None:
            messages, last_mode = await table.load()
            messages = _complete_history(messages)
            messages.append({"role": "user", "content": f"{user}: {text}"})
            if mode != last_mode:
                messages.append(style_message(mode))
            reply = ""
            try:
                async for event in gm.turn(campaign, user, messages):
                    if event["type"] == "done":
                        reply = event["text"]
                    await queue.put(event)
            except Exception as e:
                log.exception("turn failed")
                await queue.put({"type": "error", "text": f"Something went wrong: {e}"})
            finally:
                await table.save(_complete_history(messages), mode)
                await redis.delete(table.lock)
                if reply:
                    await redis.xadd(
                        table.chat, {"turn": turn_id, "user": user, "message": text, "reply": reply},
                        maxlen=200, approximate=True,
                    )
                await queue.put(None)

        task = asyncio.create_task(run())
        background.add(task)
        task.add_done_callback(background.discard)

        async def events() -> AsyncIterator[dict[str, Any]]:
            yield {"type": "turn", "id": turn_id}
            while (event := await queue.get()) is not None:
                yield event

        return sse(events())

    async def reset_table(request: Request) -> Response:
        user_of(request)
        table = Table(redis, int(request.path_params["campaign_id"]))
        if await redis.exists(table.lock):
            raise HTTPException(409, "Wait for the Game Master to finish first.")
        await table.reset()
        return JSONResponse({"reset": True})

    async def feed(request: Request) -> Response:
        """Live game events and other players' turns for one campaign."""
        user_of(request)
        table = Table(redis, int(request.path_params["campaign_id"]))

        async def events() -> AsyncIterator[dict[str, Any] | None]:
            last = {table.events: "$", table.chat: "$"}
            while not await request.is_disconnected():
                batches = await redis.xread(last, block=15000)
                if not batches:
                    yield None  # keepalive through proxies
                    continue
                for key, entries in batches:
                    for entry_id, fields in entries:
                        last[key] = entry_id
                        if key == table.events:
                            yield {"type": "event", "event": json.loads(fields["event"])}
                        else:
                            yield {"type": "chat", **fields}

        return sse(events())

    async def stt(request: Request) -> Response:
        user_of(request)
        audio = await request.body()
        if not audio:
            raise HTTPException(400, "No audio.")
        text = await require_voice().transcribe(audio, request.headers.get("content-type", "audio/webm"))
        return JSONResponse({"text": text})

    async def tts(request: Request) -> Response:
        user_of(request)
        text = (await request.json()).get("text", "").strip()
        if not text:
            raise HTTPException(400, "No text.")
        audio = require_voice().speak(text)
        # Pull the first chunk before answering so Deepgram errors become a proper status.
        first = await anext(audio)

        async def body():
            yield first
            async for chunk in audio:
                yield chunk

        return StreamingResponse(body(), media_type="audio/mpeg")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for task in background:
            task.cancel()
        if voice:
            await voice.aclose()
        await llm.close()
        await redis.aclose()

    return Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", healthz),
            Route("/api/me", me),
            Route("/api/campaigns", campaigns),
            Route("/api/worlds", forge_world, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/state", campaign_state),
            Route("/api/campaigns/{campaign_id:int}/turn", take_turn, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/reset", reset_table, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/feed", feed),
            Route("/api/stt", stt, methods=["POST"]),
            Route("/api/tts", tts, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        lifespan=lifespan,
    )
