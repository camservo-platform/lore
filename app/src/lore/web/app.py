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
from collections.abc import AsyncIterator, Awaitable, Callable
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
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket
from starlette.staticfiles import StaticFiles

from lore import admin, worldgen
from lore.db import create_pool
from lore.embeddings import OllamaEmbedder
from lore.events import stream_key
from lore.gm import (
    GameMaster, Toolbox, ToolCallError, instructions_version, style_note, system_prompt, updated_instructions,
)
from lore.mcp.common import USER_HEADER
from lore.settings import Settings
from lore.voice import Voice
from lore.web import conversation

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
TRANSCRIPT_TTL = 30 * 24 * 3600
# Voice sockets authenticate with a one-time ticket fetched through the normal login:
# browsers don't reliably send basic-auth credentials on WebSocket upgrades.
TICKET_TTL = 60
# Per-player display history: what each player said and what the GM answered them.
LINES_KEPT = 200
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
    admins: frozenset[str]

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
            stt_model=env.get("DEEPGRAM_STT_MODEL", "flux-general-en"),
            tts_model=env.get("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en"),
            dev_user=env.get("LORE_DEV_USER") or None,
            admins=frozenset(u.strip() for u in env.get("LORE_ADMINS", "").split(",") if u.strip()),
        )


class Table:
    """Redis keys for one campaign's table."""

    def __init__(self, redis: Redis, campaign_id: int):
        self.redis = redis
        self.messages = f"lore:campaign:{campaign_id}:gm:messages"
        self.mode = f"lore:campaign:{campaign_id}:gm:mode"
        # The top-level system prompt the conversation started with, and the version of the
        # instructions it has been given since (see gm.updated_instructions).
        self.system = f"lore:campaign:{campaign_id}:gm:system"
        self.version = f"lore:campaign:{campaign_id}:gm:version"
        self.lock = f"lore:campaign:{campaign_id}:gm:lock"
        self.chat = f"lore:campaign:{campaign_id}:chat"
        self._campaign_id = campaign_id
        self.events = stream_key(campaign_id)

    async def load(self) -> dict[str, Any]:
        raw, mode, system, version = await self.redis.mget(self.messages, self.mode, self.system, self.version)
        return {"messages": json.loads(raw) if raw else [], "mode": mode, "system": system, "version": version}

    async def save(self, messages: list[dict[str, Any]], mode: str, system: str, version: str) -> None:
        async with self.redis.pipeline() as pipe:
            for key, value in ((self.messages, json.dumps(messages)), (self.mode, mode),
                               (self.system, system), (self.version, version)):
                pipe.set(key, value, ex=TRANSCRIPT_TTL)
            await pipe.execute()

    def _lines(self, user: str) -> str:
        return f"lore:campaign:{self._campaign_id}:user:{user}:lines"

    async def add_lines(self, user: str, *lines: dict[str, Any]) -> None:
        key = self._lines(user)
        async with self.redis.pipeline() as pipe:
            pipe.rpush(key, *(json.dumps(line) for line in lines))
            pipe.ltrim(key, -LINES_KEPT, -1)
            pipe.expire(key, TRANSCRIPT_TTL)
            await pipe.execute()

    async def recent_lines(self, user: str, count: int) -> list[dict[str, Any]]:
        return [json.loads(line) for line in await self.redis.lrange(self._lines(user), -count, -1)]

    async def reset(self) -> None:
        await self.redis.delete(self.messages, self.mode, self.system, self.version)


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
    # Here rather than in __main__: with reload, the app runs in a child process.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = WebSettings.from_env()
    redis = Redis(
        host=settings.redis_host, port=settings.redis_port, password=settings.redis_password, decode_responses=True
    )
    llm = anthropic.AsyncAnthropic(api_key=settings.llm_api_key)
    toolbox = Toolbox(settings.mcp_servers)
    gm = GameMaster(llm, toolbox, settings.llm_model, settings.llm_effort)
    voice = (
        Voice(settings.deepgram_api_key, settings.tts_model) if settings.deepgram_api_key else None
    )
    # Turns run as tasks so a dropped connection can't cut one off half-applied.
    background: set[asyncio.Task] = set()
    # Admin tools talk to Postgres and the embedding server directly; connect on first use.
    admin_db: dict[str, Any] = {}
    admin_db_lock = asyncio.Lock()

    async def admin_backend() -> tuple[Any, OllamaEmbedder]:
        async with admin_db_lock:
            if not admin_db:
                admin_db["pool"] = await create_pool(min_size=0, max_size=3)
                admin_db["embedder"] = OllamaEmbedder(Settings.from_env())
        return admin_db["pool"], admin_db["embedder"]

    def user_of(request: Request) -> str:
        user = request.headers.get(USER_HEADER) or settings.dev_user
        if not user:
            raise HTTPException(401, "Not signed in.")
        return user

    def require_admin(request: Request) -> str:
        user = user_of(request)
        if user not in settings.admins:
            raise HTTPException(403, "Admins only.")
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
        user = user_of(request)
        return JSONResponse({"user": user, "speech": voice is not None, "admin": user in settings.admins})

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

    async def acquire_turn(table: Table, turn_id: str, wait: float = 0) -> bool:
        """Takes the table's turn lock, waiting up to `wait` seconds for another player's turn."""
        deadline = asyncio.get_running_loop().time() + wait
        while not await redis.set(table.lock, turn_id, nx=True, ex=LOCK_TTL):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.5)
        return True

    async def play_turn(
        table: Table, turn_id: str, campaign: str, user: str, said: str, mode: str,
        emit: Callable[[dict[str, Any]], Awaitable[None]], note: str | None = None,
    ) -> None:
        """Runs one GM turn under an already-acquired lock, streaming events to `emit`.
        `said` is what the player said (shown in their lines); `note` is extra context
        for the GM only, such as that they interrupted."""
        text = said or KICKOFF
        if note:
            text = f"({note}) {text}"
        saved = await table.load()
        messages = _complete_history(saved["messages"])
        current = instructions_version(campaign)
        if messages:
            # Conversations from before instructions were versioned get the update once.
            system, version = saved["system"] or system_prompt(campaign), saved["version"] or "unversioned"
        else:
            system, version = system_prompt(campaign), current
        messages.append({"role": "user", "content": f"{user}: {text}"})
        # Operator notes go in one appended system message: never edit what was sent.
        notes = []
        if version != current:
            notes.append(updated_instructions(campaign))
            version = current
        if mode != saved["mode"]:
            notes.append(style_note(mode))
        if notes:
            messages.append({"role": "system", "content": "\n\n".join(notes)})
        reply = ""
        try:
            async for event in gm.turn(campaign, user, system, messages):
                if event["type"] == "done":
                    reply = event["text"]
                await emit(event)
        except Exception as e:
            log.exception("turn failed")
            await emit({"type": "error", "text": f"Something went wrong: {e}"})
        finally:
            await table.save(_complete_history(messages), mode, system, version)
            await redis.delete(table.lock)
            if reply:
                lines = ([{"role": "player", "text": said}] if said else []) + [{"role": "gm", "text": reply}]
                await table.add_lines(user, *lines)
                await redis.xadd(
                    table.chat, {"turn": turn_id, "user": user, "message": said or text, "reply": reply},
                    maxlen=200, approximate=True,
                )

    def in_background(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)
        return task

    async def take_turn(request: Request) -> Response:
        user = user_of(request)
        table = Table(redis, int(request.path_params["campaign_id"]))
        body = await request.json()
        mode = "speech" if body.get("mode") == "speech" else "text"
        turn_id = uuid.uuid4().hex
        if not await acquire_turn(table, turn_id):
            raise HTTPException(409, "The Game Master is answering another player; try again in a moment.")

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def run() -> None:
            try:
                await play_turn(table, turn_id, body["campaign"], user, (body.get("message") or "").strip(), mode,
                                queue.put)
            finally:
                await queue.put(None)

        in_background(run())

        async def events() -> AsyncIterator[dict[str, Any]]:
            yield {"type": "turn", "id": turn_id}
            while (event := await queue.get()) is not None:
                yield event

        return sse(events())

    async def voice_ticket(request: Request) -> Response:
        user = user_of(request)
        require_voice()
        body = await request.json()
        ticket = uuid.uuid4().hex
        await redis.set(
            f"lore:voice:ticket:{ticket}",
            json.dumps({"user": user, "campaign_id": int(body["campaign_id"]), "campaign": body["campaign"]}),
            ex=TICKET_TTL,
        )
        return JSONResponse({"ticket": ticket})

    async def voice_socket(ws: WebSocket) -> None:
        raw = await redis.getdel(f"lore:voice:ticket:{ws.query_params.get('ticket', '')}")
        if not raw or voice is None:
            await ws.close(code=4401)
            return
        info = json.loads(raw)
        user, campaign = info["user"], info["campaign"]
        table = Table(redis, info["campaign_id"])
        await ws.accept()

        async def play(said: str, interrupted: bool, emit) -> None:
            turn_id = uuid.uuid4().hex
            await emit({"type": "turn", "id": turn_id, "said": said})
            # Spoken turns wait for another player's turn to finish instead of failing.
            if not await acquire_turn(table, turn_id, wait=120):
                await emit({"type": "error", "text": "The Game Master is still busy with another player."})
                return
            note = "I'm interrupting you" if interrupted else None
            # Shielded: if this player hangs up mid-turn, the turn still completes cleanly.
            await asyncio.shield(in_background(
                play_turn(table, turn_id, campaign, user, said, "speech", emit, note=note)
            ))

        log.info("voice session started: %s at %s", user, campaign)
        await conversation.serve(ws, settings.deepgram_api_key, settings.stt_model, play)
        log.info("voice session ended: %s at %s", user, campaign)

    async def recent_lines(request: Request) -> Response:
        """This player's own recent lines at this table (other players' turns aren't included)."""
        user = user_of(request)
        table = Table(redis, int(request.path_params["campaign_id"]))
        count = max(1, min(int(request.query_params.get("count", "40")), LINES_KEPT))
        return JSONResponse(await table.recent_lines(user, count))

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

    async def admin_overview(request: Request) -> Response:
        require_admin(request)
        pool, _ = await admin_backend()
        return JSONResponse(await admin.overview(pool))

    async def admin_sql(request: Request) -> Response:
        user = require_admin(request)
        body = await request.json()
        sql, allow_writes = body.get("sql", ""), bool(body.get("allow_writes"))
        log.info("admin %s ran SQL (writes=%s): %s", user, allow_writes, " ".join(sql.split())[:1000])
        pool, _ = await admin_backend()
        return JSONResponse(await admin.run_sql(pool, sql, allow_writes=allow_writes))

    async def admin_vector(request: Request) -> Response:
        require_admin(request)
        body = await request.json()
        query = (body.get("query") or "").strip()
        if not query:
            raise HTTPException(400, "Enter something to search for.")
        pool, embedder = await admin_backend()
        hits = await admin.vector_search(
            pool, embedder, query,
            campaign_id=int(body["campaign_id"]) if body.get("campaign_id") else None,
            kind=body.get("kind") or None,
            limit=int(body.get("limit") or 10),
        )
        return JSONResponse(hits)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for task in background:
            task.cancel()
        if voice:
            await voice.aclose()
        if admin_db:
            await admin_db["embedder"].aclose()
            await admin_db["pool"].close()
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
            Route("/api/campaigns/{campaign_id:int}/lines", recent_lines),
            Route("/api/campaigns/{campaign_id:int}/feed", feed),
            Route("/api/tts", tts, methods=["POST"]),
            Route("/api/voice/ticket", voice_ticket, methods=["POST"]),
            WebSocketRoute("/ws/voice", voice_socket),
            Route("/api/admin/overview", admin_overview),
            Route("/api/admin/sql", admin_sql, methods=["POST"]),
            Route("/api/admin/vector", admin_vector, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        lifespan=lifespan,
    )
