"""Generates a new world with the LLM and records it through the MCP servers."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from lore.gm import ToolCallError, Toolbox

log = logging.getLogger(__name__)

KINDS = ["location", "npc", "faction", "item", "history", "storyline", "rumor"]

WORLD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Evocative campaign name, 1-4 words"},
        "setting": {"type": "string", "description": "One-paragraph pitch of the world and its tone"},
        "opening": {"type": "string", "description": "Opening scene that puts the players in motion and hands them the starter quest, 2-3 paragraphs"},
        "starter_quest": {
            "type": "object",
            "description": "A short, simple first quest for brand-new players, finishable in one session",
            "properties": {
                "title": {"type": "string"},
                "giver": {"type": "string", "description": "Who asks for help (a named character from the lore)"},
                "goal": {"type": "string", "description": "What the players must do, in one plain sentence"},
                "first_step": {"type": "string", "description": "The obvious first thing to do or place to go"},
                "steps": {"type": "array", "items": {"type": "string"}, "description": "2-3 short steps, in order"},
                "reward": {"type": "string"},
            },
            "required": ["title", "giver", "goal", "first_step", "steps", "reward"],
            "additionalProperties": False,
        },
        "lore": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": KINDS},
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "Self-contained prose, 2-5 sentences"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "title", "content", "tags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "setting", "opening", "starter_quest", "lore"],
    "additionalProperties": False,
}

PROMPT = """\
Create a new world for a tabletop role-playing campaign.

{request}
{name_line}
Write between 12 and 16 lore entries that give a Game Master enough to improvise from:
the starting town or region and 2-3 nearby locations, 4-5 named non-player characters
with wants and secrets, 2 factions in tension, a little history, 2-3 storylines the
players could pursue, and a couple of rumors (some true, some not). Entries should
reference each other by title so the world hangs together. Tags are short lowercase
keywords (places, factions, themes).

Also write a starter quest for players who are new to this world and to this kind of
game: small, concrete and finishable in one session (find, deliver, rescue, investigate
something close by), given by one of your characters, with an obvious first step, two or
three clear steps and a modest reward. It can hint at a bigger storyline but must not
depend on solving it. The opening scene should end with the players being offered this
quest.

Use your own invented names and generic fantasy terminology; never draw on commercial
games, their settings, trademarked creatures or rules.
Do not reuse any of these existing campaign names, and make the world unlike theirs: {existing}"""


def starter_quest_text(quest: dict[str, Any]) -> str:
    steps = " ".join(f"({i}) {step}" for i, step in enumerate(quest["steps"], 1))
    return (f"Given by {quest['giver']}. Goal: {quest['goal']} First step: {quest['first_step']} "
            f"Steps: {steps} Reward: {quest['reward']}")


async def generate(
    client: anthropic.AsyncAnthropic, model: str, theme: str, name: str | None, existing: list[str],
    on_usage=None,
) -> dict[str, Any]:
    async with client.beta.messages.stream(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": WORLD_SCHEMA}},
        messages=[{
            "role": "user",
            "content": PROMPT.format(
                request=(f"Player's request: {theme.strip()}" if theme.strip() else
                         "The players left the theme to you: invent an original premise with a distinctive "
                         "hook, tone and central tension, rather than a generic fantasy kingdom."),
                name_line=f"Name the campaign exactly: {name}\n" if name else "",
                existing=", ".join(existing) or "(none)",
            ),
        }],
    ) as stream:
        response = await stream.get_final_message()
    if on_usage:
        await on_usage(response.model, response.usage)
    if response.stop_reason == "refusal":
        raise WorldGenError("The model declined to create that world; try a different theme.")
    if response.stop_reason == "max_tokens":
        raise WorldGenError("The world description ran too long; try a narrower theme.")
    text = next(b.text for b in response.content if b.type == "text")
    world = json.loads(text)
    if name:
        world["name"] = name
    return world


async def forge(
    client: anthropic.AsyncAnthropic, model: str, toolbox: Toolbox, user: str, theme: str, name: str | None,
    on_usage=None, on_error=None,
) -> AsyncIterator[dict[str, Any]]:
    """Yields progress events; the last is {"type": "done", "campaign": {...}} or {"type": "error"}."""
    try:
        async with toolbox.session(user) as tools:
            existing = [c["name"] for c in await tools.call_json("list_campaigns")]
            if name and name.lower() in {e.lower() for e in existing}:
                yield {"type": "error", "text": f"A world named {name!r} already exists."}
                return
            yield {"type": "status", "text": "Dreaming up the world…"}
            world = await generate(client, model, theme, name, existing, on_usage)
            yield {"type": "status", "text": f"Founding {world['name']}…"}
            campaign = await tools.call_json("create_campaign", name=world["name"], setting=world["setting"])
            quest = world["starter_quest"]
            entries = world["lore"] + [
                {"kind": "storyline", "title": "Opening scene", "content": world["opening"], "tags": ["opening"]},
                {"kind": "storyline", "title": f"Starter quest: {quest['title']}", "tags": ["starter-quest", "quest"],
                 "content": starter_quest_text(quest)},
            ]
            for i, entry in enumerate(entries, 1):
                await tools.call_json("add_lore", campaign=world["name"], **entry)
                yield {"type": "lore", "kind": entry["kind"], "title": entry["title"], "done": i, "total": len(entries)}
        yield {"type": "done", "campaign": campaign}
    except (WorldGenError, ToolCallError) as e:
        yield {"type": "error", "text": str(e)}
    except anthropic.APIError as e:
        log.exception("world generation failed")
        if on_error:
            await on_error(e)
        yield {"type": "error", "text": f"The LLM request failed: {e.message}"}
    except Exception as e:
        log.exception("world generation failed")
        while isinstance(e, BaseExceptionGroup) and e.exceptions:
            e = e.exceptions[0]
        if isinstance(e, anthropic.APIStatusError):
            if on_error:
                await on_error(e)
            yield {"type": "error", "text": f"The LLM request failed: {e.message}"}
        else:
            yield {"type": "error", "text": f"World generation failed: {e}"}


class WorldGenError(Exception):
    pass
