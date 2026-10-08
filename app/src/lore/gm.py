"""The Game Master: a Claude conversation that plays the game through the MCP servers.

The conversation is append-only (thinking blocks are bound to the exact history that
produced them), so callers persist `messages` as returned and only ever add to it.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anthropic
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from lore.mcp.common import USER_HEADER

log = logging.getLogger(__name__)

# Server-side retry on another model if a request is declined, and server-side
# summarising of old turns when a long session nears the context limit.
BETAS = ["compact-2026-01-12", "server-side-fallback-2026-07-01"]
# Thinking blocks are bound to the exact system prompt, tools and history that produced
# them. We keep all three append-only, but if anything drifts (say a tool's description
# changes in a deploy), drop the stale blocks rather than failing the turn.
BINDING_BETA = "thinking-binding-controls-2026-08-01"
THINKING = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
MAX_ROUNDS = 16

SYSTEM = """\
You are the Game Master of a tabletop role-playing game, running a campaign for one
or more players at a shared table. Each player message starts with their username.

How you run the game:
- Narrate vividly but keep the players in control: describe the situation, play the
  non-player characters, and ask what they do. Never decide a player character's
  actions, words or feelings for them.
- Be brief by default: usually two to four sentences covering what the characters
  notice and what just changed, then hand the scene back. Go longer only when a player
  asks for more (looking closer, questioning someone, searching) or when something truly
  important happens: arriving somewhere new, a major revelation, a dramatic turn in a
  fight.
- Let the players discover the world's secrets themselves. Never recite lore wholesale
  or reveal hidden truths unprompted (an NPC's secret, which rumors are true, where a
  storyline is heading). Plant concrete hints instead: an odd detail, a nervous glance,
  a mark that doesn't belong, a story that doesn't add up. Reveal more as players
  investigate, ask the right people or succeed on rolls; if they seem stuck, make the
  hints stronger rather than handing over the answer.
- The game server is the truth for numbers. Change HP, items, gold, conditions, status
  and location only through its tools. Use roll_dice for every uncertain outcome (pass
  the campaign and character so the roll is logged) and narrate the result honestly,
  including failures.
- Work in this order each turn, making independent tool calls together in a single
  response rather than one at a time:
  1. Gather what you need and roll any dice.
  2. Apply the changes whose results you will describe: damage, healing, spending gold,
     using up or losing items, conditions, status. Their results can differ from what
     you expect (a character drops to 0 HP, can't afford something), so narrate from
     what the tools return.
  3. Narrate the outcome.
  4. Only then do the record-keeping that can't change what you said: log_event,
     add_lore, add_item for things found or given, move_character. The players are
     already hearing your narration while this runs, so never add narration after it.
- The lore server is the truth for the world. Search it before describing an
  established place, person, faction or past event, and record anything new you invent
  that should stay consistent (with add_lore). Record story beats with log_event.
- Each player message ends with the current table state: whether a session is open,
  every character's HP, conditions, status and location, and the latest events. Trust it
  instead of looking those up again; read a full sheet only when you need inventory or
  attributes. At the start of a conversation, search the lore relevant to where things
  stand so your recap is grounded. If the speaking player has no character in this
  campaign, help them create one (create_character with their username as player)
  before play begins.
- If the table state says no session is open, start one when play begins; when the
  players stop for the day, end it with a short recap.
- Use plain generic fantasy terminology and your own invented names; never refer to
  commercial games, publishers or their trademarked rules, places or creatures. Give
  characters attributes that suit the world and the character rather than a standard
  fixed set of stats."""

def system_prompt(campaign: str) -> str:
    return f"{SYSTEM}\n\nThis table's campaign is named {json.dumps(campaign)}."


def instructions_version(campaign: str) -> str:
    return hashlib.sha256(system_prompt(campaign).encode()).hexdigest()[:16]


def updated_instructions(campaign: str) -> str:
    """For a conversation that started under older instructions: its top-level system
    prompt stays frozen (thinking blocks are bound to it), so the new ones are appended."""
    return "Your instructions have been updated. From now on, follow these in place of the earlier ones:\n\n" + (
        system_prompt(campaign)
    )


SPEECH_STYLE = (
    "The table is now in speech mode: your replies are read aloud. Use plain spoken prose with "
    "no markdown, lists, headings or symbols, spell out numbers naturally, and keep each reply "
    "to a few sentences unless the moment calls for more."
)
TEXT_STYLE = "The table is now in text mode: your replies are read on screen. Light markdown is fine."


class Toolbox:
    """The MCP servers' tools as Claude tool definitions, called on behalf of a user."""

    def __init__(self, servers: dict[str, str]):
        self._servers = servers  # name -> streamable HTTP URL
        self._definitions: list[dict[str, Any]] | None = None
        self._owner: dict[str, str] = {}  # tool name -> server name

    @asynccontextmanager
    async def session(self, user: str) -> AsyncIterator["ToolSession"]:
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(
                httpx2.AsyncClient(headers={USER_HEADER: user}, timeout=httpx2.Timeout(120, connect=10))
            )
            clients = {
                name: await stack.enter_async_context(Client(streamable_http_client(url, http_client=http)))
                for name, url in self._servers.items()
            }
            if self._definitions is None:
                await self._load(clients)
            yield ToolSession(clients, self._owner)

    async def _load(self, clients: dict[str, Client]) -> None:
        definitions = []
        for server, client in clients.items():
            for tool in sorted((await client.list_tools()).tools, key=lambda t: t.name):
                self._owner[tool.name] = server
                definitions.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.input_schema,
                    # Stream tool inputs as generated; the MCP server validates them.
                    "eager_input_streaming": True,
                })
        # Fixed order keeps the tools prefix byte-identical, so it stays cached.
        self._definitions = definitions

    def definitions(self) -> list[dict[str, Any]]:
        assert self._definitions is not None, "open a session first"
        return self._definitions


class ToolSession:
    def __init__(self, clients: dict[str, Client], owner: dict[str, str]):
        self._clients = clients
        self._owner = owner

    async def call(self, name: str, arguments: Any) -> tuple[Any, bool]:
        """Returns (result, is_error)."""
        if name not in self._owner:
            return f"Unknown tool {name!r}.", True
        if not isinstance(arguments, dict):
            return {"INVALID_JSON": json.dumps(arguments)}, True
        result = await self._clients[self._owner[name]].call_tool(name, arguments)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.structured_content is not None and not result.is_error:
            return result.structured_content, False
        return text, bool(result.is_error)

    async def call_json(self, tool: str, /, **arguments: Any) -> Any:
        """Calls a tool and unwraps its structured result, raising on tool errors."""
        result, is_error = await self.call(tool, arguments)
        if is_error:
            raise ToolCallError(str(result))
        if isinstance(result, dict) and set(result) == {"result"}:
            return result["result"]
        return result


class ToolCallError(Exception):
    pass


def echo_content(content: list[Any]) -> list[dict[str, Any]]:
    """Assistant content to send back next time, as JSON-able dicts.

    After a mid-output fallback, blocks before the last `fallback` marker that only the
    declining model can use (thinking, tool calls) must not be echoed."""
    blocks = [b.to_dict() if hasattr(b, "to_dict") else b for b in content]
    boundary = max((i for i, b in enumerate(blocks) if b["type"] == "fallback"), default=-1)
    drop = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}
    return [b for i, b in enumerate(blocks) if i > boundary or b["type"] not in drop]


def style_note(mode: str) -> str:
    """Operator instruction for a change of table mode."""
    return SPEECH_STYLE if mode == "speech" else TEXT_STYLE


class GameMaster:
    def __init__(self, client: anthropic.AsyncAnthropic, toolbox: Toolbox, model: str, effort: str):
        self._client = client
        self._toolbox = toolbox
        self._model = model
        self._effort = effort

    async def turn(
        self, campaign: str, user: str, system: str, messages: list[dict[str, Any]], model: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Runs the GM until it hands control back to the players, appending to `messages`.
        `system` must be the prompt the conversation started with. `model` overrides the
        default for this turn (e.g. a faster one for speech); switching models within a
        conversation is fine, each model just ignores the other's thinking.

        Yields {"type": "text", "text"} deltas, {"type": "tool", "name"} as tools run, and
        finally {"type": "done", "text"} with the narration of this turn."""
        narration: list[str] = []
        async with self._toolbox.session(user) as tools:
            for _ in range(MAX_ROUNDS):
                final = None
                for attempt in range(3):
                    round_ = _Round(self._request(model or self._model, system, messages), narration)
                    try:
                        async for event in round_:
                            yield event
                        final = round_.final
                        break
                    except ValueError:
                        # Tool input JSON the SDK couldn't parse at all: there is no tool_use
                        # id to answer, so re-issue the round.
                        log.warning("unparseable tool input, retrying (%d)", attempt + 1)
                if final is None:
                    raise RuntimeError("the model kept producing unparseable tool input")
                if final.stop_reason == "refusal":
                    # Discard any partial output; the history stays as it was sent.
                    yield {"type": "error", "text": "The Game Master declined to continue that scene. Try another approach."}
                    return
                content = echo_content(final.content)
                messages.append({"role": "assistant", "content": content})
                if final.stop_reason == "pause_turn":
                    continue
                tool_uses = [b for b in content if b["type"] == "tool_use"]
                if not tool_uses:
                    break
                truncated = final.stop_reason == "max_tokens"
                results = await asyncio.gather(*(
                    self._run_tool(tools, b, truncated) for b in tool_uses
                ))
                messages.append({"role": "user", "content": list(results)})
            else:
                yield {"type": "error", "text": "The Game Master lost the thread; ask again."}
        yield {"type": "done", "text": "".join(narration).strip()}

    async def _run_tool(self, tools: ToolSession, block: dict[str, Any], truncated: bool) -> dict[str, Any]:
        if truncated:
            result, is_error = "Tool input was cut off; call the tool again.", True
        else:
            try:
                result, is_error = await tools.call(block["name"], block["input"])
            except Exception as e:  # network trouble reaching an MCP server
                log.exception("tool %s failed", block["name"])
                result, is_error = f"Tool unavailable: {e}", True
        if not isinstance(result, str):
            result = json.dumps(result)
        return {"type": "tool_result", "tool_use_id": block["id"], "content": result, "is_error": is_error}

    def _request(self, model: str, system: str, messages: list[dict[str, Any]]):
        return self._client.beta.messages.stream(
            model=model,
            max_tokens=16000,
            betas=BETAS + [BINDING_BETA],
            thinking=THINKING,
            fallbacks="default",
            context_management={"edits": [{"type": "compact_20260112"}]},
            cache_control={"type": "ephemeral"},
            output_config={"effort": self._effort},
            system=system,
            tools=self._toolbox.definitions(),
            messages=messages,
        )


class _Round:
    """One streamed model response: iterate for UI events, then read `.final`."""

    def __init__(self, stream_manager, narration: list[str]):
        self._manager = stream_manager
        self._narration = narration
        self._new_block = False
        self.final = None

    def __aiter__(self):
        return self._events()

    async def _events(self) -> AsyncIterator[dict[str, Any]]:
        async with self._manager as stream:
            async for event in stream:
                if event.type == "content_block_start" and event.content_block.type == "tool_use":
                    yield {"type": "tool", "name": event.content_block.name}
                elif event.type == "content_block_start" and event.content_block.type == "text":
                    self._new_block = True
                elif event.type == "text" and event.text:
                    if self._new_block and "".join(self._narration).strip():
                        # Separate narration from earlier text blocks (e.g. before a tool call).
                        self._narration.append("\n\n")
                        yield {"type": "text", "text": "\n\n"}
                    self._new_block = False
                    self._narration.append(event.text)
                    yield {"type": "text", "text": event.text}
            self.final = await stream.get_final_message()
