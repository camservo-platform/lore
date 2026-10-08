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
from html import escape
from urllib.parse import quote
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import httpx
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
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
from lore.settings import Settings
from lore.events import EventBus
from lore import metrics
from lore.usage import Health, Presence, Usage
from lore.voice import Voice
from lore.web import conversation
from lore.web.auth import COOKIE, Auth, AuthError, parse_user_map

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
    llm_speech_model: str
    llm_effort: str
    deepgram_api_key: str | None
    stt_model: str
    tts_model: str
    dev_user: str | None
    admins: frozenset[str]
    public_url: str
    passwords_file: str | None
    github_client_id: str | None
    github_client_secret: str | None
    github_users: str

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
            llm_speech_model=env.get("LLM_SPEECH_MODEL") or env.get("LLM_MODEL", "claude-opus-5-5"),
            llm_effort=env.get("LLM_EFFORT", "medium"),
            deepgram_api_key=env.get("DEEPGRAM_API_KEY") or None,
            stt_model=env.get("DEEPGRAM_STT_MODEL", "flux-general-en"),
            tts_model=env.get("DEEPGRAM_TTS_MODEL", "aura-2-pandora-en"),
            dev_user=env.get("LORE_DEV_USER") or None,
            public_url=env.get("LORE_PUBLIC_URL", "http://localhost:8080"),
            passwords_file=env.get("LORE_PASSWORDS_FILE") or None,
            github_client_id=env.get("GITHUB_CLIENT_ID") or None,
            github_client_secret=env.get("GITHUB_CLIENT_SECRET") or None,
            github_users=env.get("LORE_GITHUB_USERS", ""),
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


def render_login(*, github: bool, passwords: bool, error: str | None) -> str:
    options = []
    if github:
        options.append('<a class="primary" href="/auth/github">Sign in with GitHub</a>')
    if passwords:
        options.append('<a class="secondary" href="/auth/password">Sign in with a password</a>')
    if not options:
        options.append("<p>No sign-in method is configured yet.</p>")
    notice = f'<p class="error">{escape(error)}</p>' if error else ""
    return (STATIC / "login.html").read_text().replace("{{options}}", "\n".join(options)).replace("{{error}}", notice)


def describe_error(e: BaseException) -> str:
    """A message a player can act on: the innermost cause, with the API's own wording."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    if isinstance(e, anthropic.APIStatusError):
        body = e.body if isinstance(e.body, dict) else {}
        return f"The Game Master couldn't reach the model: {body.get('error', {}).get('message') or e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "The Game Master couldn't reach the model (network error). Try again in a moment."
    return f"Something went wrong: {e}"


def format_table_state(characters: list[dict[str, Any]], sheets: list[dict[str, Any]],
                       events: list[dict[str, Any]]) -> str:
    """The table as the GM should see it at the start of a turn (saves it looking it up)."""
    latest = events[-1] if events else None
    session_open = bool(latest and latest.get("session_id") and latest["type"] != "session_ended")
    lines = ["[Table state]", f"Session: {'open' if session_open else 'not open'}"]
    by_name = {s["name"]: s for s in sheets}
    if characters:
        lines.append("Characters:")
    for c in characters:
        sheet = by_name.get(c["name"], {})
        details = [f"{c['hp']}/{c['max_hp']} HP"]
        if sheet.get("temp_hp"):
            details.append(f"{sheet['temp_hp']} temporary HP")
        if c["status"] != "alive":
            details.append(c["status"])
        details += sheet.get("conditions") or []
        where = f"; at {c['location']}" if c["location"] else ""
        owner = f"player: {c['player']}" if c["player"] else "NPC"
        lines.append(f"- {c['name']} ({owner}): {', '.join(details)}{where}")
    if not characters:
        lines.append("Characters: none yet")
    if events:
        lines.append("Latest events:")
        lines += [f"- {e['summary']}" for e in events]
    # Repeated here, next to the decision, because it's easy to lose in a long system prompt.
    lines.append("[Turn order: roll and apply what you'll describe first, then narrate, then record "
                 "(log_event, add_lore, add_item, move_character) after the narration.]")
    return "\n".join(lines)


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
    usage = Usage(redis)
    health = Health(redis)

    async def on_usage(model: str, used: Any) -> None:
        metrics.record_tokens(model, used)
        await usage.llm(model, used)

    async def on_model_error(error: BaseException) -> None:
        kind = metrics.classify(error)
        metrics.LLM_ERRORS.labels(kind).inc()
        await health.error(kind, describe_error(error))

    if os.environ.get("LORE_METRICS_PORT"):
        metrics.serve(int(os.environ["LORE_METRICS_PORT"]))
    presence = Presence(redis)
    bus = EventBus(redis)
    gm = GameMaster(llm, toolbox, settings.llm_model, settings.llm_effort, on_usage=on_usage)
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

    auth = Auth(
        redis, public_url=settings.public_url, passwords_file=settings.passwords_file,
        github_client_id=settings.github_client_id, github_client_secret=settings.github_client_secret,
        github_users=parse_user_map(settings.github_users), dev_user=settings.dev_user,
    )

    async def user_of(request: Request) -> str:
        user = await auth.user(request)
        if not user:
            raise HTTPException(401, "Not signed in.")
        return user

    async def require_admin(request: Request) -> str:
        user = await user_of(request)
        if user not in settings.admins:
            raise HTTPException(403, "Admins only.")
        return user

    def require_voice() -> Voice:
        if voice is None:
            raise HTTPException(503, "Speech is not configured (no DEEPGRAM_API_KEY).")
        return voice

    async def index(request: Request) -> Response:
        if not await auth.user(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    # --- signing in ----------------------------------------------------------------

    def signed_in(user: str, provider: str, session_id: str) -> Response:
        log.info("%s signed in (%s)", user, provider)
        metrics.SIGNINS.labels(provider.split(":")[0]).inc()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, session_id, **auth.cookie_args())
        return response

    async def login_page(request: Request) -> Response:
        if await auth.user(request):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(render_login(
            github=auth.github_enabled, passwords=auth.passwords.available, error=request.query_params.get("error")
        ))

    async def login_basic(request: Request) -> Response:
        """Asks the browser for a password (its native prompt), then starts a session."""
        user = auth.basic_user(request)
        if not user:
            return HTMLResponse(render_login(github=auth.github_enabled, passwords=True,
                                             error="That username and password weren't accepted."),
                                status_code=401, headers={"WWW-Authenticate": 'Basic realm="lore"'})
        return signed_in(user, "password", await auth.start_session(user, "password", request))

    async def login_github(_request: Request) -> Response:
        if not auth.github_enabled:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse(await auth.github_start(), status_code=303)

    async def github_callback(request: Request) -> Response:
        try:
            user, login = await auth.github_finish(request.query_params.get("code", ""),
                                                   request.query_params.get("state", ""))
        except (AuthError, httpx.HTTPError) as e:
            return RedirectResponse(f"/login?error={quote(str(e))}", status_code=303)
        if not user:
            log.warning("refused GitHub sign-in from %s (not in the allow list)", login)
            return RedirectResponse(
                f"/login?error={quote(f'The GitHub account {login} is not allowed to play here.')}", status_code=303)
        return signed_in(user, "github", await auth.start_session(user, f"github:{login}", request))

    async def logout(request: Request) -> Response:
        await auth.end_session(request)
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    async def admin_sessions(request: Request) -> Response:
        await require_admin(request)
        return JSONResponse(await auth.sessions())

    async def admin_revoke_session(request: Request) -> Response:
        admin_user = await require_admin(request)
        revoked = await auth.revoke(request.path_params["session"])
        log.info("admin %s revoked session %s", admin_user, request.path_params["session"])
        return JSONResponse({"revoked": revoked})

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def me(request: Request) -> Response:
        user = await user_of(request)
        await presence.seen(user, "lobby")
        return JSONResponse({"user": user, "speech": voice is not None, "admin": user in settings.admins})

    async def campaigns(request: Request) -> Response:
        user = await user_of(request)
        await presence.seen(user, "lobby")
        async with toolbox.session(user) as tools:
            return JSONResponse(await tools.call_json("list_campaigns"))

    async def campaign_state(request: Request) -> Response:
        name = request.query_params["name"]
        async with toolbox.session(await user_of(request)) as tools:
            try:
                characters, events = await asyncio.gather(
                    tools.call_json("list_characters", campaign=name),
                    tools.call_json("recent_events", campaign=name, limit=30),
                )
                # Full sheets (attributes, conditions, gold, inventory) for the sidebar.
                sheets = await asyncio.gather(*(
                    tools.call_json("get_character", campaign=name, character=c["name"]) for c in characters
                ))
            except ToolCallError as e:
                raise HTTPException(404, str(e)) from None
        return JSONResponse({"characters": list(sheets), "events": events})

    async def forge_world(request: Request) -> Response:
        user = await user_of(request)
        body = await request.json()
        async def counted(events):
            async for event in events:
                if event["type"] in ("done", "error"):
                    metrics.WORLDS.labels("ok" if event["type"] == "done" else "failed").inc()
                yield event

        return sse(counted(worldgen.forge(
            llm, settings.llm_model, toolbox, user, body.get("theme", ""), (body.get("name") or "").strip() or None,
            on_usage=on_usage, on_error=on_model_error,
        )))

    async def acquire_turn(table: Table, turn_id: str, wait: float = 0) -> bool:
        """Takes the table's turn lock, waiting up to `wait` seconds for another player's turn."""
        deadline = asyncio.get_running_loop().time() + wait
        while not await redis.set(table.lock, turn_id, nx=True, ex=LOCK_TTL):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.5)
        return True

    async def table_state(campaign: str, user: str, new_conversation: bool) -> str | None:
        try:
            async with toolbox.session(user) as tools:
                characters, events, recaps = await asyncio.gather(
                    tools.call_json("list_characters", campaign=campaign),
                    tools.call_json("recent_events", campaign=campaign, limit=6),
                    # A fresh conversation starts from the last session's recap.
                    tools.call_json("recent_events", campaign=campaign, limit=1, type="session_ended")
                    if new_conversation else asyncio.sleep(0, []),
                )
                sheets = await asyncio.gather(*(
                    tools.call_json("get_character", campaign=campaign, character=c["name"]) for c in characters
                ))
            state = format_table_state(characters, list(sheets), events)
            if recaps:
                state += f"\n[Previously: {recaps[0]['summary']}]"
            return state
        except Exception:
            log.exception("couldn't read the table state; the GM will look it up itself")
            return None

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
        state = await table_state(campaign, user, new_conversation=not messages)
        messages.append({"role": "user", "content": f"{user}: {text}" + (f"\n\n{state}" if state else "")})
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
        await usage.turn(user)
        metrics.TURNS.labels(mode).inc()
        started, narrating = asyncio.get_running_loop().time(), False
        try:
            model = settings.llm_speech_model if mode == "speech" else settings.llm_model
            async for event in gm.turn(campaign, user, system, messages, model=model):
                if event["type"] == "text" and not narrating:
                    narrating = True
                    metrics.FIRST_NARRATION.labels(mode).observe(asyncio.get_running_loop().time() - started)
                if event["type"] == "done":
                    reply = event["text"]
                await emit(event)
        except Exception as e:
            log.exception("turn failed")
            await on_model_error(e)
            await emit({"type": "error", "text": describe_error(e)})
        finally:
            metrics.TURN_SECONDS.labels(mode).observe(asyncio.get_running_loop().time() - started)
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
        user = await user_of(request)
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

    async def voices(request: Request) -> Response:
        await user_of(request)
        tts = require_voice()
        try:
            listed = await tts.voices()
        except Exception:
            log.exception("couldn't list Deepgram voices")
            listed = []
        return JSONResponse({"default": tts.default_voice, "voices": listed})

    async def voice_ticket(request: Request) -> Response:
        user = await user_of(request)
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
        started = asyncio.get_running_loop().time()
        await presence.voice(user, True)
        metrics.VOICE_SESSIONS.inc()
        try:
            await conversation.serve(ws, settings.deepgram_api_key, settings.stt_model, play)
        finally:
            await presence.voice(user, False)
            metrics.VOICE_SESSIONS.dec()
            await usage.voice(asyncio.get_running_loop().time() - started)
        log.info("voice session ended: %s at %s", user, campaign)

    async def player_roll(request: Request) -> Response:
        """A player's own roll, logged under their name so the table (and the GM) see it."""
        user = await user_of(request)
        body = await request.json()
        args = {"notation": (body.get("notation") or "").strip(), "reason": (body.get("reason") or "").strip() or "a roll",
                "campaign": body["campaign"]}
        if body.get("character"):
            args["character"] = body["character"]
        async with toolbox.session(user) as tools:
            try:
                return JSONResponse(await tools.call_json("roll_dice", **args))
            except ToolCallError as e:
                return JSONResponse({"error": str(e)}, status_code=400)

    async def recent_lines(request: Request) -> Response:
        """This player's own recent lines at this table (other players' turns aren't included)."""
        user = await user_of(request)
        table = Table(redis, int(request.path_params["campaign_id"]))
        count = max(1, min(int(request.query_params.get("count", "40")), LINES_KEPT))
        return JSONResponse(await table.recent_lines(user, count))

    async def reset_table(request: Request) -> Response:
        await user_of(request)
        table = Table(redis, int(request.path_params["campaign_id"]))
        if await redis.exists(table.lock):
            raise HTTPException(409, "Wait for the Game Master to finish first.")
        await table.reset()
        return JSONResponse({"reset": True})

    async def feed(request: Request) -> Response:
        """Live game events and other players' turns for one campaign. While it's open the
        player counts as present at this table."""
        user = await user_of(request)
        campaign_id = int(request.path_params["campaign_id"])
        table = Table(redis, campaign_id)

        async def events() -> AsyncIterator[dict[str, Any] | None]:
            last = {table.events: "$", table.chat: "$"}
            while not await request.is_disconnected():
                await presence.seen(user, "table", campaign_id)
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

    def npc_voices_key(campaign_id: int) -> str:
        return f"lore:campaign:{campaign_id}:npc-voices"

    async def npc_voice(campaign_id: int, name: str, gender: str, narrator: str) -> str:
        """The voice this NPC always speaks with in this world, assigned on first use."""
        key, field = npc_voices_key(campaign_id), name.strip().lower()
        assigned = await redis.hget(key, field)
        voices = await voice.voices()
        if not assigned:
            taken = set(await redis.hvals(key))
            candidate = voice.pick_npc_voice(voices, field, gender, taken, avoid={voice.default_voice, narrator})
            await redis.hsetnx(key, field, candidate)  # first writer wins if two players race
            assigned = await redis.hget(key, field)
        if assigned == narrator:
            # This player picked the NPC's voice for their narrator; borrow another for them.
            return voice.pick_npc_voice(voices, field, gender, {assigned}, avoid={narrator})
        return assigned

    async def tts(request: Request) -> Response:
        """MP3 for `text`, streamed. GET (text in the query) lets an <audio> element start
        playing as the first bytes arrive instead of after the whole clip is synthesised."""
        await user_of(request)
        if request.method == "GET":
            params = request.query_params
        else:
            params = await request.json()
        text, chosen = (params.get("text") or "").strip(), params.get("voice")
        if not text:
            raise HTTPException(400, "No text.")
        tts_voice = require_voice()
        if params.get("npc") and params.get("campaign_id"):
            chosen = await npc_voice(int(params["campaign_id"]), params["npc"], params.get("npc_voice") or "",
                                     narrator=await tts_voice.resolve(chosen))
        audio = tts_voice.speak(text, chosen)
        await usage.tts(len(text))
        metrics.TTS_CHARACTERS.inc(len(text))
        # Pull the first chunk before answering so Deepgram errors become a proper status.
        first = await anext(audio)

        async def body():
            yield first
            async for chunk in audio:
                yield chunk

        return StreamingResponse(body(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    async def admin_worlds(request: Request) -> Response:
        await presence.seen(await require_admin(request), "admin")
        pool, _ = await admin_backend()
        return JSONResponse(await admin.worlds(pool))

    def admin_failed(e: admin.AdminError) -> Response:
        return JSONResponse({"error": str(e)}, status_code=400)

    async def admin_rename_world(request: Request) -> Response:
        user = await require_admin(request)
        campaign_id = int(request.path_params["campaign_id"])
        pool, _ = await admin_backend()
        try:
            event = await admin.rename_campaign(pool, campaign_id, (await request.json()).get("name", ""), user)
        except admin.AdminError as e:
            return admin_failed(e)
        await bus.publish(event)  # open tables update their title and the GM's next turn
        log.info("admin %s renamed world %s: %s", user, campaign_id, event["summary"])
        return JSONResponse(event)

    async def admin_delete_world(request: Request) -> Response:
        user = await require_admin(request)
        campaign_id = int(request.path_params["campaign_id"])
        pool, _ = await admin_backend()
        try:
            name = await admin.delete_campaign(pool, campaign_id, (await request.json()).get("confirm", ""))
        except admin.AdminError as e:
            return admin_failed(e)
        # Tell anyone at the table, then clear the world's Redis state (conversation, feeds, lines).
        await redis.xadd(stream_key(campaign_id), {"event": json.dumps({
            "id": 0, "campaign_id": campaign_id, "type": "campaign_deleted", "actor": user,
            "summary": f"The world {name} was deleted", "occurred_at": "", "data": {}})})
        await asyncio.sleep(1)
        keys = [k async for k in redis.scan_iter(f"lore:campaign:{campaign_id}:*")]
        if keys:
            await redis.delete(*keys)
        log.info("admin %s deleted world %s (%s)", user, campaign_id, name)
        return JSONResponse({"deleted": name})

    async def admin_edit_character(request: Request) -> Response:
        user = await require_admin(request)
        pool, _ = await admin_backend()
        try:
            event = await admin.update_character(
                pool, int(request.path_params["character_id"]), await request.json(), user
            )
        except admin.AdminError as e:
            return admin_failed(e)
        await bus.publish(event)
        log.info("admin %s: %s", user, event["summary"])
        return JSONResponse(event)

    async def admin_npc_voices(request: Request) -> Response:
        await require_admin(request)
        campaign_id = int(request.path_params["campaign_id"])
        if request.method == "POST":
            body = await request.json()
            name, chosen = (body.get("name") or "").strip().lower(), body.get("voice") or ""
            if not name:
                return JSONResponse({"error": "Which character?"}, status_code=400)
            if chosen:
                if chosen not in {v["id"] for v in await require_voice().voices()}:
                    return JSONResponse({"error": "Unknown voice."}, status_code=400)
                await redis.hset(npc_voices_key(campaign_id), name, chosen)
            else:
                await redis.hdel(npc_voices_key(campaign_id), name)  # reassigned on next line
        assigned = await redis.hgetall(npc_voices_key(campaign_id))
        return JSONResponse([{"name": n, "voice": v} for n, v in sorted(assigned.items())])

    async def admin_lore(request: Request) -> Response:
        await require_admin(request)
        pool, _ = await admin_backend()
        return JSONResponse(await admin.list_lore(
            pool, int(request.query_params["campaign_id"]), request.query_params.get("kind") or None))

    async def admin_lore_change(request: Request) -> Response:
        """Edit (POST /lore/{id}), delete (/delete) or merge (/merge, {"into": id}) a lore entry."""
        user = await require_admin(request)
        pool, embedder = await admin_backend()
        lore_id, action = int(request.path_params["lore_id"]), request.path_params.get("action", "edit")
        body = await request.json() if action != "delete" else {}
        try:
            if action == "delete":
                event = await admin.delete_lore(pool, lore_id, user)
            elif action == "merge":
                event = await admin.merge_lore(pool, embedder, lore_id, int(body["into"]), user)
            else:
                event = await admin.update_lore(pool, embedder, lore_id, body, user)
        except admin.AdminError as e:
            return admin_failed(e)
        await bus.publish(event)
        log.info("admin %s: %s", user, event["summary"])
        return JSONResponse(event)

    async def admin_presence(request: Request) -> Response:
        await presence.seen(await require_admin(request), "admin")
        pool, _ = await admin_backend()
        names = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM campaigns")}
        people = await presence.everyone()
        for p in people:
            p["campaign"] = names.get(p["campaign_id"]) if p["campaign_id"] else None
        return JSONResponse(people)

    async def admin_tables(request: Request) -> Response:
        """Every world's GM conversation: size, mode, last activity, and any turn in progress."""
        await require_admin(request)
        pool, _ = await admin_backend()
        names = {r["id"]: r["name"] for r in await pool.fetch("SELECT id, name FROM campaigns")}
        tables = []
        async for key in redis.scan_iter("lore:campaign:*:gm:messages"):
            campaign_id = int(key.split(":")[2])
            table = Table(redis, campaign_id)
            raw, mode, lock = await redis.mget(table.messages, table.mode, table.lock)
            ttl, lock_ttl = await redis.ttl(table.messages), await redis.ttl(table.lock)
            tables.append({
                "campaign_id": campaign_id,
                "campaign": names.get(campaign_id, f"(deleted world {campaign_id})"),
                "messages": len(json.loads(raw)) if raw else 0,
                "bytes": len(raw.encode()) if raw else 0,
                "mode": mode,
                "idle_seconds": TRANSCRIPT_TTL - ttl if ttl > 0 else None,
                "turn_running": bool(lock),
                "lock_expires_in": lock_ttl if lock else None,
            })
        return JSONResponse(sorted(tables, key=lambda t: t["idle_seconds"] or 0))

    async def admin_unlock_table(request: Request) -> Response:
        user = await require_admin(request)
        table = Table(redis, int(request.path_params["campaign_id"]))
        await redis.delete(table.lock)
        log.info("admin %s cleared the turn lock for world %s", user, request.path_params["campaign_id"])
        return JSONResponse({"unlocked": True})

    async def admin_reset_table(request: Request) -> Response:
        user = await require_admin(request)
        table = Table(redis, int(request.path_params["campaign_id"]))
        await table.reset()
        log.info("admin %s reset the GM conversation for world %s", user, request.path_params["campaign_id"])
        return JSONResponse({"reset": True})

    async def admin_health(request: Request) -> Response:
        await require_admin(request)
        return JSONResponse(await health.summary())

    async def admin_usage(request: Request) -> Response:
        await require_admin(request)
        days = max(1, min(int(request.query_params.get("days", "14")), 120))
        return JSONResponse(await usage.summary(days))

    async def admin_overview(request: Request) -> Response:
        await require_admin(request)
        pool, _ = await admin_backend()
        return JSONResponse(await admin.overview(pool))

    async def admin_sql(request: Request) -> Response:
        user = await require_admin(request)
        body = await request.json()
        sql, allow_writes = body.get("sql", ""), bool(body.get("allow_writes"))
        log.info("admin %s ran SQL (writes=%s): %s", user, allow_writes, " ".join(sql.split())[:1000])
        pool, _ = await admin_backend()
        return JSONResponse(await admin.run_sql(pool, sql, allow_writes=allow_writes))

    async def admin_vector(request: Request) -> Response:
        await require_admin(request)
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
            Route("/login", login_page),
            Route("/auth/password", login_basic),
            Route("/auth/github", login_github),
            Route("/auth/github/callback", github_callback),
            Route("/auth/logout", logout, methods=["POST"]),
            Route("/api/admin/sessions", admin_sessions),
            Route("/api/admin/sessions/{session}/revoke", admin_revoke_session, methods=["POST"]),
            Route("/api/me", me),
            Route("/api/campaigns", campaigns),
            Route("/api/worlds", forge_world, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/state", campaign_state),
            Route("/api/campaigns/{campaign_id:int}/turn", take_turn, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/reset", reset_table, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/lines", recent_lines),
            Route("/api/campaigns/{campaign_id:int}/roll", player_roll, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/feed", feed),
            Route("/api/tts", tts, methods=["GET", "POST"]),
            Route("/api/voices", voices),
            Route("/api/voice/ticket", voice_ticket, methods=["POST"]),
            WebSocketRoute("/ws/voice", voice_socket),
            Route("/api/admin/overview", admin_overview),
            Route("/api/admin/worlds", admin_worlds),
            Route("/api/admin/worlds/{campaign_id:int}/rename", admin_rename_world, methods=["POST"]),
            Route("/api/admin/worlds/{campaign_id:int}/delete", admin_delete_world, methods=["POST"]),
            Route("/api/admin/characters/{character_id:int}", admin_edit_character, methods=["POST"]),
            Route("/api/admin/presence", admin_presence),
            Route("/api/admin/lore", admin_lore),
            Route("/api/admin/lore/{lore_id:int}", admin_lore_change, methods=["POST"]),
            Route("/api/admin/lore/{lore_id:int}/{action:str}", admin_lore_change, methods=["POST"]),
            Route("/api/admin/worlds/{campaign_id:int}/npc-voices", admin_npc_voices, methods=["GET", "POST"]),
            Route("/api/admin/tables", admin_tables),
            Route("/api/admin/tables/{campaign_id:int}/unlock", admin_unlock_table, methods=["POST"]),
            Route("/api/admin/tables/{campaign_id:int}/reset", admin_reset_table, methods=["POST"]),
            Route("/api/admin/usage", admin_usage),
            Route("/api/admin/health", admin_health),
            Route("/api/admin/sql", admin_sql, methods=["POST"]),
            Route("/api/admin/vector", admin_vector, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        lifespan=lifespan,
    )
