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
import re
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
    GameMaster, Toolbox, ToolCallError, instructions_version, intro_note, style_note, system_prompt,
    updated_instructions,
)
from lore.settings import Settings
from lore.events import EventBus
from lore import metrics
from lore.usage import Health, Presence, Usage
from lore.voice import Voice
from lore.web import conversation, users
from lore.web.auth import COOKIE, Auth, AuthError, parse_user_map
from lore.web.drain import Drain, Draining

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
# Story memory recalled into a turn: skip what's still in the conversation, and only
# moments close enough in meaning to be worth the GM's attention.
RECALL_SKIP_RECENT = 10
RECALL_MIN_SIMILARITY = 0.55
# Marks a turn the player set to "Ask the GM" (see gm.SYSTEM on out-of-character asides).
OUT_OF_CHARACTER = "out of character, to the Game Master"


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
    users_secret: str | None
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
            users_secret=env.get("LORE_USERS_SECRET") or None,
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


SAY = re.compile(r"<say\b([^>]*)>(.*?)</say>", re.S)
SAY_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
# "the guard", "a sailor": unnamed, so not kept as a named NPC.
UNNAMED = re.compile(r"^(the|a|an|some|one|another|someone|somebody)\b", re.I)


def speakers(reply: str) -> list[dict[str, str]]:
    """The named characters who speak in a reply (<say who=...>), each once, with their first line."""
    found: dict[str, dict[str, str]] = {}
    for attrs, line in SAY.findall(reply):
        a = dict(SAY_ATTR.findall(attrs))
        who = (a.get("who") or "").strip()
        if not who or UNNAMED.match(who) or not who[0].isupper() or who.lower() in found:
            continue
        voice = a.get("voice", "").strip().lower()
        found[who.lower()] = {"name": who, "voice": voice if voice in ("feminine", "masculine") else "",
                              "line": " ".join(line.split())}
    return list(found.values())


def plain_story(reply: str) -> str:
    """A reply as story text for memory: characters' lines attributed, tags gone."""
    def line(m: re.Match) -> str:
        who = dict(SAY_ATTR.findall(m.group(1))).get("who", "someone")
        return f'{who}: "{" ".join(m.group(2).split())}"'
    return SAY.sub(line, reply).strip()


def format_world_notes(quests: list[dict[str, Any]], npcs: list[dict[str, Any]], here: str,
                       moments: list[dict[str, Any]]) -> str:
    """Quests, NPCs and remembered moments for the GM, after the table state."""
    lines = []
    if quests:
        lines.append("Active quests:")
        for q in quests:
            latest = f" Latest note: {q['notes'][-1]['note']}" if q["notes"] else ""
            lines.append(f"- {q['title']}" + (f" (from {q['giver']})" if q["giver"] else "") + f": {q['summary'][:200]}{latest}")
    here_l = here.lower()
    nearby = [n for n in npcs if here_l and here_l in (n.get("location") or "").lower()][:6]
    elsewhere = [n for n in npcs if n not in nearby and n.get("appearances")][:5]
    describe = lambda n: f"{n['title']} ({n['disposition']}{', at ' + n['location'] if n.get('location') else ''})"
    if nearby:
        lines.append(f"NPCs known at {here}: " + "; ".join(describe(n) for n in nearby))
    if elsewhere:
        lines.append("NPCs met elsewhere, who could turn up again: " + "; ".join(describe(n) for n in elsewhere))
    stubs = [n["title"] for n in npcs if n.get("stub")]
    if stubs:
        lines.append("NPCs with only a stub record (describe them with record_npc when you can): " + ", ".join(stubs[:8]))
    if moments:
        lines.append("Earlier moments that may matter now (from story memory):")
        for m in moments:
            who = f"{m['player']}: {m['said']} -> " if m["said"] else ""
            lines.append(f"- {who}{' '.join(m['narration'].split())[:400]}")
    return ("[World notes]\n" + "\n".join(lines)) if lines else ""


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
        # What they can still use: the GM must not narrate an ability that's spent.
        if sheet.get("pool_max"):
            details.append(f"{sheet['pool_name']} {sheet['pool']}/{sheet['pool_max']}")
        limited = [f"{a['name']} {a['uses_left']}/{a['max_uses']}" for a in sheet.get("abilities") or []
                   if a["max_uses"] is not None]
        if limited:
            details.append("uses left: " + ", ".join(limited))
        where = f"; at {c['location']}" if c["location"] else ""
        identity = " ".join(x for x in (sheet.get("race"), sheet.get("class")) if x)
        owner = (f"player: {c['player']}" if c["player"] else "NPC") + (f", {identity}" if identity else "")
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


def sse(events: AsyncIterator[Any], retry_ms: int | None = None) -> StreamingResponse:
    """Server-Sent Events from `events`: dicts, (id, dict) pairs for resumable streams, or
    None for a keepalive. `retry_ms` tells the browser how soon to reconnect."""
    async def body():
        if retry_ms is not None:
            yield f"retry: {retry_ms}\n\n"
        async for event in events:
            if event is None:
                yield ": keepalive\n\n"
            elif isinstance(event, tuple):
                yield f"id: {event[0]}\ndata: {json.dumps(event[1])}\n\n"
            else:
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# A feed's SSE id is "<events id>,<chat id>": where the browser got to in both streams, so
# a reconnect (e.g. to the new pod after a deploy) resumes without missing anything.
STREAM_ID = re.compile(r"\d+-\d+")


def parse_feed_id(raw: str | None) -> tuple[str, str] | None:
    parts = (raw or "").split(",")
    if len(parts) != 2 or not all(STREAM_ID.fullmatch(p) for p in parts):
        return None
    return parts[0], parts[1]


def stream_id_le(a: str, b: str) -> bool:
    return tuple(map(int, a.split("-"))) <= tuple(map(int, b.split("-")))


# Shown when a deploy cancels a turn that ran past the drain deadline (or was hurried).
TURN_CUT_SHORT = ("The server restarted before the Game Master finished. Anything already recorded "
                  "stands; say that again to carry on.")
DRAIN_RETRY_MS = 1000


def create_app(drain: Drain | None = None) -> Starlette:
    # Here rather than in __main__: with reload, the app runs in a child process.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = WebSettings.from_env()
    # Without one (tests, live reload) nothing ever drains.
    drain = drain or Drain()
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

    user_store = users.user_store(settings.users_secret, settings.passwords_file)

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

    # --- user admin: password logins (GitHub accounts and admins are set in values) ---

    async def admin_users(request: Request) -> Response:
        await require_admin(request)
        try:
            names = await user_store.names()
            error = None
        except Exception as e:
            log.exception("couldn't read the user list")
            names, error = [], str(e) if isinstance(e, users.UserError) else "Couldn't read the user list."
        sessions: dict[str, int] = {}
        for s in await auth.sessions():
            sessions[s["user"]] = sessions.get(s["user"], 0) + 1
        github = parse_user_map(settings.github_users)
        everyone = sorted(set(names) | set(github.values()) | set(settings.admins) | set(sessions))
        return JSONResponse({
            "writable": user_store.writable and error is None, "error": error,
            "users": [{
                "name": name, "password": name in names, "admin": name in settings.admins,
                "github": sorted(login for login, n in github.items() if n == name), "sessions": sessions.get(name, 0),
            } for name in everyone],
        })

    def user_failed(e: users.UserError) -> Response:
        return JSONResponse({"error": str(e)}, status_code=400)

    async def admin_add_user(request: Request) -> Response:
        admin_user = await require_admin(request)
        body = await request.json()
        password = body.get("password") or users.new_password()
        try:
            name = users.check_name(body.get("name", ""))
            auth.passwords.use(await user_store.set_password(name, password, create=True))
        except users.UserError as e:
            return user_failed(e)
        log.info("admin %s added user %s", admin_user, name)
        return JSONResponse({"name": name, "password": None if body.get("password") else password})

    async def admin_set_password(request: Request) -> Response:
        admin_user = await require_admin(request)
        name, body = request.path_params["name"], await request.json()
        password = body.get("password") or users.new_password()
        try:
            auth.passwords.use(await user_store.set_password(name, password, create=False))
        except users.UserError as e:
            return user_failed(e)
        # The old password may be why it's being changed: sign its sessions out.
        signed_out = await auth.revoke_user(name, provider="password")
        log.info("admin %s set a new password for %s (%d sessions signed out)", admin_user, name, signed_out)
        return JSONResponse({"name": name, "password": None if body.get("password") else password,
                             "signed_out": signed_out})

    async def admin_delete_user(request: Request) -> Response:
        admin_user = await require_admin(request)
        name = request.path_params["name"]
        if name == admin_user:
            return user_failed(users.UserError("You can't delete your own login while signed in with it."))
        try:
            auth.passwords.use(await user_store.delete(name))
        except users.UserError as e:
            return user_failed(e)
        signed_out = await auth.revoke_user(name, provider="password")
        log.info("admin %s deleted user %s (%d sessions signed out)", admin_user, name, signed_out)
        return JSONResponse({"deleted": name, "signed_out": signed_out})

    async def admin_sign_out_user(request: Request) -> Response:
        admin_user = await require_admin(request)
        name = request.path_params["name"]
        signed_out = await auth.revoke_user(name)
        log.info("admin %s signed %s out everywhere (%d sessions)", admin_user, name, signed_out)
        return JSONResponse({"signed_out": signed_out})

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
                characters, events, quests = await asyncio.gather(
                    tools.call_json("list_characters", campaign=name),
                    tools.call_json("recent_events", campaign=name, limit=30),
                    tools.call_json("list_quests", campaign=name),
                )
                # Full sheets (attributes, conditions, gold, inventory) for the sidebar.
                sheets = await asyncio.gather(*(
                    tools.call_json("get_character", campaign=name, character=c["name"]) for c in characters
                ))
            except ToolCallError as e:
                raise HTTPException(404, str(e)) from None
        return JSONResponse({"characters": list(sheets), "events": events, "quests": quests})

    async def quest_note(request: Request) -> Response:
        """A player's own note on a quest (a theory, a reminder); recorded under their name."""
        body = await request.json()
        note = (body.get("note") or "").strip()
        if not note or len(note) > 500:
            return JSONResponse({"error": "Write a note (up to 500 characters)."}, status_code=400)
        async with toolbox.session(await user_of(request)) as tools:
            try:
                return JSONResponse(await tools.call_json("update_quest", campaign=body["campaign"],
                                                          title=body["title"], note=note, player_note=True))
            except ToolCallError as e:
                return JSONResponse({"error": str(e)}, status_code=400)

    async def character_options(request: Request) -> Response:
        """The races and classes to pick from at this table (core, as this world names them, plus its own)."""
        async with toolbox.session(await user_of(request)) as tools:
            try:
                return JSONResponse(await tools.call_json("list_character_options",
                                                          campaign=request.query_params["name"]))
            except ToolCallError as e:
                raise HTTPException(404, str(e)) from None

    async def create_character(request: Request) -> Response:
        """A player makes their character from the picker (name, race, class). For a character
        made before races and classes gave abilities, `existing` names it and only race and
        class are set."""
        user = await user_of(request)
        body = await request.json()
        name = (body.get("name") or "").strip()
        async with toolbox.session(user) as tools:
            try:
                if body.get("existing"):
                    sheet = await tools.call_json("get_character", campaign=body["campaign"],
                                                  character=body["existing"])
                    if sheet["player"] != user:
                        raise HTTPException(403, "That's not your character.")
                    sheet = await tools.call_json(
                        "choose_race_and_class", campaign=body["campaign"], character=sheet["name"],
                        race=body.get("race", ""), character_class=body.get("class", ""))
                else:
                    if not name or len(name) > 40:
                        return JSONResponse({"error": "Give your character a name (up to 40 characters)."},
                                            status_code=400)
                    sheet = await tools.call_json(
                        "create_character", campaign=body["campaign"], name=name, race=body.get("race", ""),
                        character_class=body.get("class", ""), player=user)
            except ToolCallError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(sheet)

    async def forge_world(request: Request) -> Response:
        user = await user_of(request)
        body = await request.json()
        if drain.draining.is_set():
            raise HTTPException(503, "Lore is restarting; try again in a moment.")

        async def forging() -> AsyncIterator[dict[str, Any]]:
            # Held so a deploy lets the world finish forging.
            with drain.hold():
                async for event in worldgen.forge(
                    llm, settings.llm_model, toolbox, user, body.get("theme", ""),
                    (body.get("name") or "").strip() or None, on_usage=on_usage, on_error=on_model_error,
                ):
                    if event["type"] in ("done", "error"):
                        metrics.WORLDS.labels("ok" if event["type"] == "done" else "failed").inc()
                    yield event

        return sse(forging())

    async def acquire_turn(table: Table, turn_id: str, wait: float = 0) -> bool:
        """Takes the table's turn lock, waiting up to `wait` seconds for another player's turn."""
        deadline = asyncio.get_running_loop().time() + wait
        while not await redis.set(table.lock, turn_id, nx=True, ex=LOCK_TTL):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.5)
        return True

    async def optional(coro, default: Any, what: str) -> Any:
        """Extra context that's nice to have: a failure (say, the embedding server) mustn't stop the turn."""
        try:
            return await coro
        except Exception:
            log.warning("couldn't load %s for the table state", what, exc_info=True)
            return default

    async def table_state(campaign: str, user: str, new_conversation: bool, said: str = "") -> str | None:
        try:
            async with toolbox.session(user) as tools:
                characters, events, recaps = await asyncio.gather(
                    tools.call_json("list_characters", campaign=campaign),
                    tools.call_json("recent_events", campaign=campaign, limit=6),
                    # A fresh conversation starts from the last session's recap.
                    tools.call_json("recent_events", campaign=campaign, limit=1, type="session_ended")
                    if new_conversation else asyncio.sleep(0, []),
                )
                here = next((c["location"] for c in sorted(characters, key=lambda c: c["player"] != user)
                             if c["player"] and c["location"]), "")
                sheets, quests, npcs, moments = await asyncio.gather(
                    asyncio.gather(*(tools.call_json("get_character", campaign=campaign, character=c["name"])
                                     for c in characters)),
                    optional(tools.call_json("list_quests", campaign=campaign, status="active"), [], "quests"),
                    optional(tools.call_json("list_npcs", campaign=campaign), [], "NPCs"),
                    # What the players did before that bears on this: their choices keep consequences.
                    optional(tools.call_json("recall_story", campaign=campaign, query=said, limit=3,
                                             skip_recent=0 if new_conversation else RECALL_SKIP_RECENT,
                                             min_similarity=RECALL_MIN_SIMILARITY), [], "story memory")
                    if said else asyncio.sleep(0, []),
                )
            state = format_table_state(characters, list(sheets), events)
            notes = format_world_notes(quests, npcs, here, moments)
            if notes:
                state += "\n" + notes
            if recaps:
                state += f"\n[Previously: {recaps[0]['summary']}]"
            return state
        except Exception:
            log.exception("couldn't read the table state; the GM will look it up itself")
            return None

    async def play_turn(
        table: Table, turn_id: str, campaign: str, user: str, said: str, mode: str,
        emit: Callable[[dict[str, Any]], Awaitable[None]], note: str | None = None, intro: bool = False,
        aside: bool = False,
    ) -> None:
        """Runs one GM turn under an already-acquired lock, streaming events to `emit`.
        `said` is what the player said (shown in their lines); `note` is extra context
        for the GM only, such as that they interrupted; `aside` marks an out-of-character
        question to the GM. A deploy waits for it to finish."""
        with drain.hold():
            await _play_turn(table, turn_id, campaign, user, said, mode, emit, note, intro, aside)

    async def _play_turn(
        table: Table, turn_id: str, campaign: str, user: str, said: str, mode: str,
        emit: Callable[[dict[str, Any]], Awaitable[None]], note: str | None, intro: bool, aside: bool,
    ) -> None:
        text = said or KICKOFF
        notes = [n for n in (note, OUT_OF_CHARACTER if aside and said else None) if n]
        if notes:
            text = f"({'; '.join(notes)}) {text}"
        saved = await table.load()
        messages = _complete_history(saved["messages"])
        current = instructions_version(campaign)
        if messages:
            # Conversations from before instructions were versioned get the update once.
            system, version = saved["system"] or system_prompt(campaign), saved["version"] or "unversioned"
        else:
            system, version = system_prompt(campaign), current
        state = await table_state(campaign, user, new_conversation=not messages, said=said)
        messages.append({"role": "user", "content": f"{user}: {text}" + (f"\n\n{state}" if state else "")})
        # Operator notes go in one appended system message: never edit what was sent.
        notes = []
        if version != current:
            notes.append(updated_instructions(campaign))
            version = current
        if mode != saved["mode"]:
            notes.append(style_note(mode))
        if intro and len(messages) == 1:
            # The first turn in a world just forged: a short how-to-play, then its starter quest.
            notes.append(intro_note(mode))
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
        except asyncio.CancelledError:
            # Cancelled by a drain that ran out of time: still save and unlock (below).
            log.warning("turn cancelled at %s", campaign)
            await emit({"type": "error", "text": TURN_CUT_SHORT})
            raise
        finally:
            metrics.TURN_SECONDS.labels(mode).observe(asyncio.get_running_loop().time() - started)
            await table.save(_complete_history(messages), mode, system, version)
            await redis.delete(table.lock)
            if reply:
                aside = aside and bool(said)
                lines = ([{"role": "player", "text": said}] if said else []) + [{"role": "gm", "text": reply}]
                await table.add_lines(user, *({**line, "aside": True} if aside else line for line in lines))
                await redis.xadd(
                    table.chat, {"turn": turn_id, "user": user, "message": said or text, "reply": reply,
                                 "aside": "1" if aside else ""},
                    maxlen=200, approximate=True,
                )
                if not aside:  # out-of-character answers aren't part of the story
                    in_background(remember_turn(campaign, user, said, reply))

    async def remember_turn(campaign: str, user: str, said: str, reply: str) -> None:
        """After a turn: keep it in story memory and note every named NPC who spoke (a stub for new
        ones), so the world remembers what happened and who was there. Best effort."""
        with drain.hold():
            try:
                async with toolbox.session(user) as tools:
                    characters = await tools.call_json("list_characters", campaign=campaign)
                    here = next((c["location"] for c in characters if c["player"] == user and c["location"]), "")
                    await tools.call_json("record_story", campaign=campaign, narration=plain_story(reply),
                                          said=said, player=user)
                    for s in speakers(reply):
                        await tools.call_json("npc_appeared", campaign=campaign, name=s["name"], location=here,
                                              voice=s["voice"], line=s["line"][:200])
            except Exception:
                log.exception("couldn't remember a turn at %s", campaign)

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
        if drain.draining.is_set():
            # Rare: the ingress stops sending requests here before the drain starts.
            raise HTTPException(503, "Lore is restarting; try again in a moment.")
        if not await acquire_turn(table, turn_id):
            raise HTTPException(409, "The Game Master is answering another player; try again in a moment.")

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def run() -> None:
            try:
                await play_turn(table, turn_id, body["campaign"], user, (body.get("message") or "").strip(), mode,
                                queue.put, intro=bool(body.get("intro")), aside=bool(body.get("aside")))
            except Exception as e:
                # Failures inside the GM loop are reported there; this catches the rest so the
                # player sees an error instead of a reply that silently never comes.
                log.exception("turn crashed")
                await queue.put({"type": "error", "text": describe_error(e)})
                await redis.delete(table.lock)
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
        if drain.draining.is_set():
            await ws.close(code=1012)  # "service restart": the browser reconnects to the new pod
            return
        raw = await redis.getdel(f"lore:voice:ticket:{ws.query_params.get('ticket', '')}")
        if not raw or voice is None:
            await ws.close(code=4401)
            return
        info = json.loads(raw)
        user, campaign = info["user"], info["campaign"]
        table = Table(redis, info["campaign_id"])
        await ws.accept()

        async def play(utterance: conversation.Utterance, emit) -> None:
            turn_id = uuid.uuid4().hex
            await emit({"type": "turn", "id": turn_id, "said": utterance.said, "aside": utterance.aside})
            # Spoken turns wait for another player's turn to finish instead of failing.
            if not await acquire_turn(table, turn_id, wait=120):
                await emit({"type": "error", "text": "The Game Master is still busy with another player."})
                return
            note = "I'm interrupting you" if utterance.interrupted else None
            # Shielded: if this player hangs up mid-turn, the turn still completes cleanly.
            await asyncio.shield(in_background(play_turn(
                table, turn_id, campaign, user, utterance.said, "speech", emit, note=note,
                intro=utterance.intro, aside=utterance.aside,
            )))

        log.info("voice session started: %s at %s", user, campaign)
        started = asyncio.get_running_loop().time()
        await presence.voice(user, True)
        metrics.VOICE_SESSIONS.inc()
        try:
            # Held so a deploy waits for this player to finish speaking and hear the reply;
            # then the session asks the browser to reconnect (to the new pod).
            with drain.hold():
                await conversation.serve(ws, settings.deepgram_api_key, settings.stt_model, play, drain.draining)
        finally:
            await presence.voice(user, False)
            metrics.VOICE_SESSIONS.dec()
            await usage.voice(asyncio.get_running_loop().time() - started)
        if drain.draining.is_set():
            try:
                await ws.close(code=1012)
            except RuntimeError:
                pass  # already closed
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

        keys = (table.events, table.chat)
        resume = parse_feed_id(request.headers.get("last-event-id") or request.query_params.get("last"))

        async def tip(key: str) -> str:
            newest = await redis.xrevrange(key, count=1)
            return newest[0][0] if newest else "0-0"

        async def events() -> AsyncIterator[Any]:
            # While draining, end at once: the browser reconnects to the new pod and resumes.
            if drain.draining.is_set():
                return
            tips = dict(zip(keys, await asyncio.gather(*(tip(k) for k in keys))))
            last = dict(zip(keys, resume)) if resume else dict(tips)

            def feed_id() -> str:
                return ",".join(last[k] for k in keys)

            # Gives the browser a resume point straight away, before anything happens.
            yield feed_id(), {"type": "hello", "resumed": resume is not None}
            while not await request.is_disconnected():
                await presence.seen(user, "table", campaign_id)
                try:
                    batches = await drain.interruptible(redis.xread(last, count=200, block=15000))
                except Draining:
                    return
                if not batches:
                    yield None  # keepalive through proxies
                    continue
                for key, entries in batches:
                    for entry_id, fields in entries:
                        last[key] = entry_id
                        # Missed while reconnecting: shown, but not read aloud again.
                        replay = stream_id_le(entry_id, tips[key])
                        if key == table.events:
                            yield feed_id(), {"type": "event", "event": json.loads(fields["event"]), "replay": replay}
                        else:
                            yield feed_id(), {"type": "chat", **fields, "replay": replay}

        return sse(events(), retry_ms=DRAIN_RETRY_MS)

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

    async def admin_delete_character(request: Request) -> Response:
        user = await require_admin(request)
        pool, _ = await admin_backend()
        try:
            event = await admin.delete_character(
                pool, int(request.path_params["character_id"]), (await request.json()).get("confirm", ""), user
            )
        except admin.AdminError as e:
            return admin_failed(e)
        await bus.publish(event)  # open tables refresh their party; the GM sees it in the chronicle
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
            Route("/api/admin/users", admin_users),
            Route("/api/admin/users/add", admin_add_user, methods=["POST"]),
            Route("/api/admin/users/{name}/password", admin_set_password, methods=["POST"]),
            Route("/api/admin/users/{name}/delete", admin_delete_user, methods=["POST"]),
            Route("/api/admin/users/{name}/sign-out", admin_sign_out_user, methods=["POST"]),
            Route("/api/me", me),
            Route("/api/campaigns", campaigns),
            Route("/api/worlds", forge_world, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/state", campaign_state),
            Route("/api/campaigns/{campaign_id:int}/options", character_options),
            Route("/api/campaigns/{campaign_id:int}/quests/note", quest_note, methods=["POST"]),
            Route("/api/campaigns/{campaign_id:int}/characters", create_character, methods=["POST"]),
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
            Route("/api/admin/characters/{character_id:int}/delete", admin_delete_character, methods=["POST"]),
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
