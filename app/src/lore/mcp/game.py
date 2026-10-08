"""MCP server for hard game state: campaigns, characters, HP, inventory, sessions and
the event log. Every state change records an event in the same transaction.

Run with `python -m lore.mcp.game`.
"""

import re
import secrets
from collections.abc import Callable
from typing import Any

import asyncpg
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from lore import catalog, events
from lore.mcp.common import actor, campaign_id, default_lifespan, run, state

PATH = "/mcp/game"

CHARACTER_COLUMNS = (
    "c.id, c.campaign_id, c.name, c.race, c.class, c.level, c.hp, c.max_hp, c.temp_hp, c.defense, c.attributes,"
    " c.conditions, c.gold, c.location, c.status, c.pool_name, c.pool_max, c.pool, c.pool_refresh,"
    " p.username AS player"
)
ABILITY_COLUMNS = "name, source, summary, description, effect, level, max_uses, uses_left, cost, refresh"

INSTRUCTIONS = """\
Authoritative game state for a tabletop role-playing campaign. Never track HP, inventory, gold,
conditions or location in your head: read them here and change them only through
these tools, which also write the campaign's event log. Use log_event for story
beats that change no numbers (an NPC met, a quest accepted, a door opened).
Use roll_dice for every random outcome; never invent a roll.
Player characters pick a race and class from list_character_options; these give their
abilities. Call use_ability whenever a character uses a limited ability (it refuses if
none are left) and recover when the party properly rests.
Names are matched case-insensitively."""


def create_server(lifespan: Callable = default_lifespan) -> MCPServer:
    server = MCPServer("lore-game", instructions=INSTRUCTIONS, lifespan=lifespan)

    async def load_character(conn: asyncpg.Connection, cid: int, name: str, *, lock: bool = False) -> dict[str, Any]:
        row = await conn.fetchrow(
            f"SELECT {CHARACTER_COLUMNS} FROM characters c LEFT JOIN players p ON p.id = c.player_id"
            f" WHERE c.campaign_id = $1 AND lower(c.name) = lower($2) {'FOR UPDATE OF c' if lock else ''}",
            cid,
            name,
        )
        if row is None:
            raise ToolError(f"No character named {name!r} in this campaign. Use list_characters.")
        return dict(row)

    async def options(conn: asyncpg.Connection, cid: int) -> list[dict[str, Any]]:
        """The core races and classes plus this world's own."""
        themes = {r["key"]: r["theme"] for r in await conn.fetch(
            "SELECT key, theme FROM world_themes WHERE campaign_id = $1", cid)}
        rows = await conn.fetch("SELECT option FROM world_options WHERE campaign_id = $1 ORDER BY id", cid)
        return ([catalog.apply_theme(o, themes.get(o["key"])) for o in catalog.core()]
                + [r["option"] | {"world": True} for r in rows])

    async def seed_abilities(conn: asyncpg.Connection, char_id: int, option: dict[str, Any], level: int) -> list[str]:
        """Gives a character the option's abilities up to `level` (ones it has are kept); returns new names."""
        added = []
        for a in catalog.abilities_for(option, level):
            name = await conn.fetchval(
                """
                INSERT INTO character_abilities (character_id, name, source, summary, description, effect, level,
                                                 max_uses, uses_left, cost, refresh)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8, $9, $10)
                ON CONFLICT (character_id, (lower(name))) DO NOTHING RETURNING name
                """,
                char_id, a["name"], a["source"], a["summary"], a["description"], a["effect"], a["level"],
                a["max_uses"], a["cost"], a["refresh"],
            )
            if name:
                added.append(name)
        return added

    async def apply_class_pool(conn: asyncpg.Connection, char_id: int, cls: dict[str, Any], level: int) -> None:
        """Sets the class's pool for `level`, keeping points already spent spent."""
        size = catalog.pool_max(cls, level)
        resource = cls["resource"]
        await conn.execute(
            """
            UPDATE characters SET pool_name = $2, pool = GREATEST(0, LEAST($3, pool + ($3 - pool_max))), pool_max = $3,
                pool_refresh = $4 WHERE id = $1
            """,
            char_id, resource.get("name", "") if size else "", size, resource["refresh"],
        )

    async def sheet(conn: asyncpg.Connection, cid: int, name: str) -> dict[str, Any]:
        character = await load_character(conn, cid, name)
        character["abilities"] = [
            dict(r) for r in await conn.fetch(
                f"SELECT {ABILITY_COLUMNS} FROM character_abilities WHERE character_id = $1 ORDER BY level, id",
                character["id"],
            )
        ]
        character["inventory"] = [
            dict(r)
            for r in await conn.fetch(
                "SELECT name, quantity, description, origin FROM inventory_items WHERE character_id = $1 ORDER BY name",
                character["id"],
            )
        ]
        return character

    async def change(
        ctx: Context,
        campaign: str,
        character: str,
        mutate: Callable[[asyncpg.Connection, dict[str, Any]], Any],
    ) -> dict[str, Any]:
        """Runs `mutate(conn, locked_character) -> (event_type, summary, data)` in a transaction,
        records the event, publishes it after commit and returns the updated sheet."""
        st = state(ctx)
        async with st.pool.acquire() as conn:
            async with conn.transaction():
                cid = await campaign_id(conn, campaign)
                before = await load_character(conn, cid, character, lock=True)
                event_type, summary, data = await mutate(conn, before)
                await conn.execute("UPDATE characters SET updated_at = now() WHERE id = $1", before["id"])
                event = await events.record(
                    conn,
                    campaign_id=cid,
                    character_id=before["id"],
                    actor=actor(ctx),
                    type=event_type,
                    summary=summary,
                    data=data,
                )
                result = await sheet(conn, cid, character)
        await st.bus.publish(event)
        return result

    # --- campaigns ---------------------------------------------------------------

    @server.tool()
    async def list_campaigns(ctx: Context) -> list[dict[str, Any]]:
        """Lists all campaigns with their setting."""
        rows = await state(ctx).pool.fetch("SELECT id, name, setting FROM campaigns ORDER BY created_at")
        return [dict(r) for r in rows]

    @server.tool()
    async def create_campaign(name: str, setting: str, ctx: Context) -> dict[str, Any]:
        """Creates a campaign. `setting` is a one-paragraph pitch; put detailed world-building in the lore server."""
        st = state(ctx)
        async with st.pool.acquire() as conn, conn.transaction():
            try:
                cid = await conn.fetchval(
                    "INSERT INTO campaigns (name, setting) VALUES ($1, $2) RETURNING id", name, setting
                )
            except asyncpg.UniqueViolationError:
                raise ToolError(f"A campaign named {name!r} already exists.") from None
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type="campaign_created", summary=f"Campaign {name} created"
            )
        await st.bus.publish(event)
        return {"id": cid, "name": name, "setting": setting}

    # --- sessions ----------------------------------------------------------------

    @server.tool()
    async def start_session(campaign: str, ctx: Context) -> dict[str, Any]:
        """Starts a play session. Events recorded until end_session are attached to it."""
        st = state(ctx)
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            try:
                row = await conn.fetchrow(
                    "INSERT INTO game_sessions (campaign_id) VALUES ($1) RETURNING id, started_at", cid
                )
            except asyncpg.UniqueViolationError:
                raise ToolError("A session is already open for this campaign; end_session first.") from None
            # Abilities and pools that come back each session do so now.
            restored = await conn.execute(
                "UPDATE character_abilities a SET uses_left = max_uses FROM characters c"
                " WHERE a.character_id = c.id AND c.campaign_id = $1 AND a.refresh = 'session'"
                " AND a.max_uses IS NOT NULL AND a.uses_left < a.max_uses",
                cid,
            )
            refilled = await conn.execute(
                "UPDATE characters SET pool = pool_max"
                " WHERE campaign_id = $1 AND pool_refresh = 'session' AND pool < pool_max",
                cid,
            )
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type="session_started", summary="Session started",
                data={"abilities_restored": int(restored.split()[-1]), "pools_refilled": int(refilled.split()[-1])},
            )
        await st.bus.publish(event)
        return {"session_id": row["id"], "started_at": row["started_at"].isoformat()}

    @server.tool()
    async def end_session(campaign: str, summary: str, ctx: Context) -> dict[str, Any]:
        """Ends the open session. `summary` is a "previously on..." recap of 3-6 sentences naming the
        characters, places and unresolved threads; it is also saved as lore (kind history) so later
        sessions can recall it."""
        st = state(ctx)
        # Embed outside the transaction: it's a network call to the embedding server.
        embedding = await st.embedder.embed_document(summary)
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            # Record before closing so the event belongs to the session it ends.
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type="session_ended", summary=summary
            )
            session_id = await conn.fetchval(
                "UPDATE game_sessions SET ended_at = now(), summary = $2"
                " WHERE campaign_id = $1 AND ended_at IS NULL RETURNING id",
                cid,
                summary,
            )
            if session_id is None:
                raise ToolError("No session is open for this campaign.")
            number = await conn.fetchval("SELECT count(*) FROM game_sessions WHERE campaign_id = $1", cid)
            title = f"Session {number} recap"
            await conn.execute(
                """
                INSERT INTO lore_entries (campaign_id, kind, title, content, tags, embedding)
                VALUES ($1, 'history', $2, $3, ARRAY['session-recap'], $4)
                ON CONFLICT (campaign_id, kind, (lower(title))) DO UPDATE
                SET content = EXCLUDED.content, embedding = EXCLUDED.embedding, updated_at = now()
                """,
                cid, title, summary, embedding,
            )
        await st.bus.publish(event)
        return {"session_id": session_id, "summary": summary, "saved_as_lore": title}

    # --- characters --------------------------------------------------------------

    @server.tool()
    async def create_character(
        campaign: str,
        name: str,
        ctx: Context,
        race: str = "",
        character_class: str = "",
        max_hp: int | None = None,
        level: int = 1,
        defense: int | None = None,
        attributes: dict[str, int] | None = None,
        gold: int = 0,
        location: str = "",
        player: str | None = None,
    ) -> dict[str, Any]:
        """Creates a character at full HP. `player` is the owning user's username; omit it for NPCs.
        A player character needs a race and class from list_character_options: they set its
        abilities and, unless given, its max HP, defense and attributes. NPCs may use any race
        and class text (catalog ones also get their abilities) but then need max_hp.
        `defense` is how hard the character is to hit; `attributes` maps stat names to scores
        (e.g. {"strength": 12, "agility": 14})."""
        st = state(ctx)
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            available = await options(conn, cid)
            race_option = catalog.find(available, "race", race) if race else None
            class_option = catalog.find(available, "class", character_class) if character_class else None
            if player and not (race_option and class_option):
                raise ToolError(
                    "A player character needs a race and a class from the list. Races: "
                    + ", ".join(o["name"] for o in available if o["kind"] == "race") + ". Classes: "
                    + ", ".join(o["name"] for o in available if o["kind"] == "class") + "."
                )
            if race_option:
                race = race_option["name"]
            if class_option:
                character_class = class_option["name"]
                if max_hp is None:
                    max_hp = class_option["hp"] + (race_option["hp"] if race_option else 0)
                if defense is None:
                    defense = class_option["defense"]
                if attributes is None:
                    attributes = dict(class_option["attributes"])
                    for stat, bonus in (race_option or {}).get("attributes", {}).items():
                        attributes[stat] = attributes.get(stat, 10) + bonus
            if max_hp is None or max_hp < 1:
                raise ToolError("max_hp must be at least 1.")
            defense = 10 if defense is None else defense
            player_id = None
            if player:
                player_id = await conn.fetchval(
                    "INSERT INTO players (username) VALUES ($1)"
                    " ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username RETURNING id",
                    player,
                )
            try:
                char_id = await conn.fetchval(
                    """
                    INSERT INTO characters (campaign_id, player_id, name, race, class, level, max_hp, hp,
                                            defense, attributes, gold, location)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $8, $9, $10, $11) RETURNING id
                    """,
                    cid, player_id, name, race, character_class, level, max_hp,
                    defense, attributes or {}, gold, location,
                )
            except asyncpg.UniqueViolationError:
                raise ToolError(f"A character named {name!r} already exists in this campaign.") from None
            except asyncpg.CheckViolationError as e:
                raise ToolError(f"Invalid character: {e.constraint_name}") from None
            for option in (race_option, class_option):
                if option:
                    await seed_abilities(conn, char_id, option, level)
            if class_option:
                await apply_class_pool(conn, char_id, class_option, level)
            kind = f"{player}'s character" if player else "NPC"
            event = await events.record(
                conn, campaign_id=cid, character_id=char_id, actor=actor(ctx), type="character_created",
                summary=f"{name} ({kind}) joined the campaign",
            )
            result = await sheet(conn, cid, name)
        await st.bus.publish(event)
        return result

    @server.tool()
    async def get_character(campaign: str, character: str, ctx: Context) -> dict[str, Any]:
        """Returns a character's full sheet including inventory."""
        async with state(ctx).pool.acquire() as conn:
            return await sheet(conn, await campaign_id(conn, campaign), character)

    # --- races, classes and abilities -----------------------------------------------

    @server.tool()
    async def list_character_options(campaign: str, ctx: Context) -> dict[str, Any]:
        """The races and classes players can pick from in this campaign, with what each means and the
        abilities it gives (`world: true` marks ones this world added). Each class decides how its
        abilities are limited: its own uses per ability, or a shared pool of points with a cost per
        ability; and whether they come back on recovery or at the start of each session."""
        async with state(ctx).pool.acquire() as conn:
            available = await options(conn, await campaign_id(conn, campaign))
        return {"races": [o for o in available if o["kind"] == "race"],
                "classes": [o for o in available if o["kind"] == "class"]}

    @server.tool()
    async def add_world_option(campaign: str, option: dict[str, Any], ctx: Context) -> dict[str, Any]:
        """Adds a race or class of this world's own, in the catalog's shape (see list_character_options)."""
        try:
            option = catalog.validate(option)
        except catalog.CatalogError as e:
            raise ToolError(f"Invalid option: {e}") from None
        st = state(ctx)
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            if catalog.find(await options(conn, cid), option["kind"], option["name"]):
                raise ToolError(f"There's already a {option['kind']} called {option['name']}.")
            await conn.execute(
                "INSERT INTO world_options (campaign_id, kind, name, option) VALUES ($1, $2, $3, $4)",
                cid, option["kind"], option["name"], option,
            )
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type="world_option_added",
                summary=f"New {option['kind']} in this world: {option['name']}", data={"name": option["name"]},
            )
        await st.bus.publish(event)
        return option

    @server.tool()
    async def theme_core_options(campaign: str, themes: list[dict[str, Any]], ctx: Context) -> dict[str, Any]:
        """Renames and re-describes the core races and classes to fit this world, keeping their
        mechanics. Each theme: {key, name, summary, description, pool_name?, abilities: [{key, name,
        summary, description, effect}]} with keys from list_character_options. An ability effect that
        changes any dice or numbers keeps its core wording."""
        st = state(ctx)
        keys = {o["key"] for o in catalog.core()}
        themes = [t for t in themes if isinstance(t, dict) and t.get("key") in keys]
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            for t in themes:
                await conn.execute(
                    "INSERT INTO world_themes (campaign_id, key, theme) VALUES ($1, $2, $3)"
                    " ON CONFLICT (campaign_id, key) DO UPDATE SET theme = EXCLUDED.theme",
                    cid, t["key"], t,
                )
            try:
                catalog.check_unique(await options(conn, cid))
            except catalog.CatalogError as e:
                raise ToolError(f"Those names clash: {e}") from None
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type="world_options_themed",
                summary="Races and classes renamed to fit the world", data={"themed": [t["key"] for t in themes]},
            )
        await st.bus.publish(event)
        return {"themed": len(themes)}

    @server.tool()
    async def choose_race_and_class(
        campaign: str, character: str, race: str, character_class: str, ctx: Context
    ) -> dict[str, Any]:
        """For a character made before races and classes gave abilities: sets them from
        list_character_options and grants their abilities (HP, defense and attributes stay)."""
        st = state(ctx)
        async with st.pool.acquire() as conn:
            available = await options(conn, await campaign_id(conn, campaign))
        race_option = catalog.find(available, "race", race)
        class_option = catalog.find(available, "class", character_class)
        if not (race_option and class_option):
            raise ToolError("Pick a race and a class from list_character_options.")

        async def mutate(conn, c):
            chosen = await conn.fetchval(
                "SELECT count(*) FROM character_abilities WHERE character_id = $1"
                " AND (source LIKE 'race: %' OR source LIKE 'class: %')", c["id"],
            )
            if chosen:
                raise ToolError(f"{c['name']} already has a race and class.")
            await conn.execute("UPDATE characters SET race = $2, class = $3 WHERE id = $1",
                               c["id"], race_option["name"], class_option["name"])
            for option in (race_option, class_option):
                await seed_abilities(conn, c["id"], option, c["level"])
            await apply_class_pool(conn, c["id"], class_option, c["level"])
            return "character_updated", f"{c['name']} is a {race_option['name']} {class_option['name']}", {
                "race": race_option["name"], "class": class_option["name"]}

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def use_ability(
        campaign: str, character: str, ability: str, ctx: Context, target: str = ""
    ) -> dict[str, Any]:
        """Spends one use of an ability (or its cost from the character's pool) before you narrate its
        effect. Fails if none are left: then the character can't use it until their uses come back.
        Unlimited abilities are logged too. `target` is who or what it's used on, if anyone."""

        async def mutate(conn, c):
            row = await conn.fetchrow(
                f"SELECT id, {ABILITY_COLUMNS} FROM character_abilities WHERE character_id = $1"
                " AND lower(name) = lower($2)", c["id"], ability,
            )
            if row is None:
                known = await conn.fetch(
                    "SELECT name FROM character_abilities WHERE character_id = $1 ORDER BY level, id", c["id"])
                raise ToolError(f"{c['name']} has no ability called {ability!r}. They have: "
                                + (", ".join(r["name"] for r in known) or "none") + ".")
            if c["status"] != "alive":
                raise ToolError(f"{c['name']} is {c['status']} and can't use abilities.")
            name, when = row["name"], "on recovery" if row["refresh"] == "recovery" else "next session"
            data = {"ability": name, "target": target}
            if row["max_uses"] is not None:
                if row["uses_left"] < 1:
                    raise ToolError(f"{c['name']} has no uses of {name} left; they come back {when}.")
                await conn.execute("UPDATE character_abilities SET uses_left = uses_left - 1 WHERE id = $1",
                                   row["id"])
                data["uses_left"] = row["uses_left"] - 1
                left = f" ({data['uses_left']}/{row['max_uses']} left)"
            elif row["cost"]:
                if c["pool"] < row["cost"]:
                    refills = "on recovery" if c["pool_refresh"] == "recovery" else "next session"
                    raise ToolError(f"{name} costs {row['cost']} {c['pool_name']} and {c['name']} has "
                                    f"{c['pool']}; it fills again {refills}.")
                await conn.execute("UPDATE characters SET pool = pool - $2 WHERE id = $1", c["id"], row["cost"])
                data["pool_left"] = c["pool"] - row["cost"]
                left = f" ({data['pool_left']}/{c['pool_max']} {c['pool_name']} left)"
            else:
                left = ""
            on = f" on {target}" if target else ""
            return "ability_used", f"{c['name']} used {name}{on}{left}", data

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def recover(
        campaign: str, reason: str, ctx: Context, characters: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """After a proper rest (a safe night's sleep, a real camp; not a pause mid-danger): restores
        abilities and pools that come back on recovery, for the named characters or, if none are given,
        every living character in the campaign. Doesn't heal HP; use heal for that."""
        if characters is None:
            async with state(ctx).pool.acquire() as conn:
                cid = await campaign_id(conn, campaign)
                characters = [r["name"] for r in await conn.fetch(
                    "SELECT name FROM characters WHERE campaign_id = $1 AND status <> 'dead' ORDER BY name", cid)]

        async def mutate(conn, c):
            restored = await conn.execute(
                "UPDATE character_abilities SET uses_left = max_uses"
                " WHERE character_id = $1 AND refresh = 'recovery' AND max_uses IS NOT NULL AND uses_left < max_uses",
                c["id"],
            )
            pool = c["pool_max"] if c["pool_refresh"] == "recovery" else c["pool"]
            await conn.execute("UPDATE characters SET pool = $2 WHERE id = $1", c["id"], pool)
            return "recovered", f"{c['name']} recovered ({reason})", {
                "reason": reason, "abilities_restored": int(restored.split()[-1]), "pool": pool}

        return [await change(ctx, campaign, name, mutate) for name in characters]

    @server.tool()
    async def grant_ability(
        campaign: str, character: str, name: str, summary: str, effect: str, source: str, ctx: Context,
        description: str = "", uses: int | None = None, cost: int | None = None, refresh: str = "recovery",
    ) -> dict[str, Any]:
        """Gives a character a new ability from the story (a relic, a teacher, a boon). Limit it with
        `uses` (comes back per `refresh`: recovery or session) or a `cost` from their pool; give
        neither for an unlimited one. `source` says where it came from."""
        if refresh not in catalog.REFRESHES:
            raise ToolError("refresh must be recovery or session.")
        if uses is not None and cost is not None:
            raise ToolError("Give uses or cost, not both.")
        if uses is not None and uses < 1:
            raise ToolError("uses must be at least 1 (or omit it for an unlimited ability).")

        async def mutate(conn, c):
            if cost is not None and not c["pool_max"]:
                raise ToolError(f"{c['name']} has no pool to pay a cost from; give uses instead.")
            try:
                await conn.execute(
                    """
                    INSERT INTO character_abilities (character_id, name, source, summary, description, effect,
                                                     level, max_uses, uses_left, cost, refresh)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8, $9, $10)
                    """,
                    c["id"], name, source, summary, description or summary, effect, c["level"], uses, cost, refresh,
                )
            except asyncpg.UniqueViolationError:
                raise ToolError(f"{c['name']} already has an ability called {name}.") from None
            except asyncpg.CheckViolationError as e:
                raise ToolError(f"Invalid ability: {e.constraint_name}") from None
            return "ability_gained", f"{c['name']} gained {name} ({source})", {"ability": name, "source": source}

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def list_characters(campaign: str, ctx: Context, location: str | None = None) -> list[dict[str, Any]]:
        """Lists characters (name, player, HP, status, location), optionally only those at `location`."""
        async with state(ctx).pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            rows = await conn.fetch(
                "SELECT c.name, p.username AS player, c.hp, c.max_hp, c.status, c.location"
                " FROM characters c LEFT JOIN players p ON p.id = c.player_id"
                " WHERE c.campaign_id = $1 AND ($2::text IS NULL OR lower(c.location) = lower($2)) ORDER BY c.name",
                cid,
                location,
            )
        return [dict(r) for r in rows]

    @server.tool()
    async def update_character(
        campaign: str,
        character: str,
        reason: str,
        ctx: Context,
        level: int | None = None,
        max_hp: int | None = None,
        defense: int | None = None,
        attributes: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Changes level, max HP, defense or attribute scores (e.g. on level-up). Given attributes are merged in. Only given fields change;
        current HP is capped at the new max."""
        changes = {k: v for k, v in {"level": level, "max_hp": max_hp, "defense": defense,
                                     "attributes": attributes}.items() if v is not None}
        if not changes:
            raise ToolError("Nothing to update.")

        async def mutate(conn, c):
            try:
                await conn.execute(
                    """
                    UPDATE characters SET level = COALESCE($2, level), max_hp = COALESCE($3, max_hp),
                        hp = LEAST(hp, COALESCE($3, max_hp)), defense = COALESCE($4, defense),
                        attributes = attributes || COALESCE($5, '{}'::jsonb)
                    WHERE id = $1
                    """,
                    c["id"], level, max_hp, defense, attributes,
                )
            except asyncpg.CheckViolationError as e:
                raise ToolError(f"Invalid value: {e.constraint_name}") from None
            data, summary = dict(changes), f"{c['name']}: {reason}"
            if level is not None and level != c["level"]:
                # A new level can unlock abilities and grow the class's pool.
                available = await options(conn, c["campaign_id"])
                race_option = catalog.find(available, "race", c["race"]) if c["race"] else None
                class_option = catalog.find(available, "class", c["class"]) if c["class"] else None
                unlocked = []
                for option in (race_option, class_option):
                    if option:
                        unlocked += await seed_abilities(conn, c["id"], option, level)
                if class_option:
                    await apply_class_pool(conn, c["id"], class_option, level)
                if unlocked:
                    data["abilities_unlocked"] = unlocked
                    summary += f" (new: {', '.join(unlocked)})"
            return "character_updated", summary, data

        return await change(ctx, campaign, character, mutate)

    # --- hit points --------------------------------------------------------------

    @server.tool()
    async def apply_damage(campaign: str, character: str, amount: int, source: str, ctx: Context) -> dict[str, Any]:
        """Applies damage after resistances. Temporary HP absorb it first. At 0 HP the character falls
        unconscious, or dies outright if the leftover damage is at least their max HP."""
        if amount < 1:
            raise ToolError("amount must be positive.")

        async def mutate(conn, c):
            if c["status"] == "dead":
                raise ToolError(f"{c['name']} is already dead.")
            absorbed = min(c["temp_hp"], amount)
            remaining = amount - absorbed
            hp = max(0, c["hp"] - remaining)
            status = c["status"]
            if hp == 0:
                status = "dead" if remaining - c["hp"] >= c["max_hp"] else "unconscious"
            await conn.execute(
                "UPDATE characters SET hp = $2, temp_hp = temp_hp - $3, status = $4 WHERE id = $1",
                c["id"], hp, absorbed, status,
            )
            summary = f"{c['name']} took {amount} damage from {source} ({c['hp']} -> {hp} HP)"
            if status != c["status"]:
                summary += f" and is {status}"
            return "damage", summary, {"amount": amount, "source": source, "absorbed_by_temp_hp": absorbed,
                                       "hp_before": c["hp"], "hp_after": hp, "status": status}

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def heal(campaign: str, character: str, amount: int, source: str, ctx: Context) -> dict[str, Any]:
        """Restores HP up to max. Healing an unconscious character brings them back to consciousness."""
        if amount < 1:
            raise ToolError("amount must be positive.")

        async def mutate(conn, c):
            if c["status"] == "dead":
                raise ToolError(f"{c['name']} is dead; healing has no effect. Use set_status to revive them.")
            hp = min(c["max_hp"], c["hp"] + amount)
            await conn.execute("UPDATE characters SET hp = $2, status = 'alive' WHERE id = $1", c["id"], hp)
            return "heal", f"{c['name']} healed {hp - c['hp']} HP from {source} ({c['hp']} -> {hp} HP)", {
                "amount": amount, "source": source, "hp_before": c["hp"], "hp_after": hp}

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def grant_temp_hp(campaign: str, character: str, amount: int, source: str, ctx: Context) -> dict[str, Any]:
        """Grants temporary HP. They don't stack: the character keeps the higher of old and new."""
        if amount < 1:
            raise ToolError("amount must be positive.")

        async def mutate(conn, c):
            temp = max(c["temp_hp"], amount)
            await conn.execute("UPDATE characters SET temp_hp = $2 WHERE id = $1", c["id"], temp)
            return "temp_hp", f"{c['name']} has {temp} temporary HP from {source}", {
                "amount": amount, "source": source, "temp_hp": temp}

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def set_status(campaign: str, character: str, status: str, reason: str, ctx: Context) -> dict[str, Any]:
        """Sets status to alive, unconscious or dead (e.g. a downed character succumbs, or is brought back).
        Reviving a character at 0 HP brings them back with 1 HP."""
        if status not in ("alive", "unconscious", "dead"):
            raise ToolError("status must be alive, unconscious or dead.")

        async def mutate(conn, c):
            hp = 1 if status == "alive" and c["hp"] == 0 else c["hp"]
            if status == "dead":
                hp = 0
            await conn.execute("UPDATE characters SET status = $2, hp = $3 WHERE id = $1", c["id"], status, hp)
            return "status", f"{c['name']} is {status}: {reason}", {"status": status, "reason": reason}

        return await change(ctx, campaign, character, mutate)

    # --- conditions --------------------------------------------------------------

    @server.tool()
    async def add_condition(campaign: str, character: str, condition: str, source: str, ctx: Context) -> dict[str, Any]:
        """Adds a condition (poisoned, prone, frightened, ...)."""
        condition = condition.strip().lower()

        async def mutate(conn, c):
            await conn.execute(
                "UPDATE characters SET conditions = array_append(array_remove(conditions, $2), $2) WHERE id = $1",
                c["id"], condition,
            )
            return "condition_added", f"{c['name']} is {condition} ({source})", {"condition": condition,
                                                                                   "source": source}

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def remove_condition(campaign: str, character: str, condition: str, ctx: Context) -> dict[str, Any]:
        """Removes a condition."""
        condition = condition.strip().lower()

        async def mutate(conn, c):
            if condition not in c["conditions"]:
                raise ToolError(f"{c['name']} is not {condition}.")
            await conn.execute(
                "UPDATE characters SET conditions = array_remove(conditions, $2) WHERE id = $1", c["id"], condition
            )
            return "condition_removed", f"{c['name']} is no longer {condition}", {"condition": condition}

        return await change(ctx, campaign, character, mutate)

    # --- inventory and gold ------------------------------------------------------

    @server.tool()
    async def add_item(
        campaign: str, character: str, item: str, ctx: Context, quantity: int = 1, description: str = "",
        origin: str = "",
    ) -> dict[str, Any]:
        """Adds items to a character's inventory, stacking with any of the same name. Give every item a
        short `description` (what it is, what it looks like). For anything that isn't ordinary gear
        (found, looted, a gift, a reward, magical or unique) give `origin`: where and from whom it came,
        e.g. "Taken from the altar of the drowned keep" or "A gift from Mira Vell for finding her lantern"."""
        if quantity < 1:
            raise ToolError("quantity must be positive.")

        async def mutate(conn, c):
            await conn.execute(
                """
                INSERT INTO inventory_items (character_id, name, quantity, description, origin)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (character_id, (lower(name))) DO UPDATE
                SET quantity = inventory_items.quantity + EXCLUDED.quantity,
                    description = COALESCE(NULLIF(EXCLUDED.description, ''), inventory_items.description),
                    origin = COALESCE(NULLIF(EXCLUDED.origin, ''), inventory_items.origin)
                """,
                c["id"], item, quantity, description, origin,
            )
            data = {"item": item, "quantity": quantity} | ({"origin": origin} if origin else {})
            return "item_added", f"{c['name']} gained {quantity} x {item}", data

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def describe_item(
        campaign: str, character: str, item: str, ctx: Context, description: str = "", origin: str = ""
    ) -> dict[str, Any]:
        """Sets an item's description and/or origin (where it came from) without changing the quantity,
        e.g. once its nature is discovered."""
        if not (description or origin):
            raise ToolError("Give a description or an origin.")

        async def mutate(conn, c):
            name = await conn.fetchval(
                """
                UPDATE inventory_items SET description = COALESCE(NULLIF($3, ''), description),
                    origin = COALESCE(NULLIF($4, ''), origin)
                WHERE character_id = $1 AND lower(name) = lower($2) RETURNING name
                """,
                c["id"], item, description, origin,
            )
            if name is None:
                raise ToolError(f"{c['name']} has no {item}.")
            return "item_described", f"{c['name']}'s {name}: {description or origin}", {
                "item": name, "description": description, "origin": origin}

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def remove_item(
        campaign: str, character: str, item: str, ctx: Context, quantity: int = 1
    ) -> dict[str, Any]:
        """Removes items (used, sold, lost, given away). Fails if the character doesn't have enough."""
        if quantity < 1:
            raise ToolError("quantity must be positive.")

        async def mutate(conn, c):
            have = await conn.fetchval(
                "SELECT quantity FROM inventory_items WHERE character_id = $1 AND lower(name) = lower($2)",
                c["id"], item,
            )
            if have is None or have < quantity:
                raise ToolError(f"{c['name']} has {have or 0} x {item}, can't remove {quantity}.")
            if have == quantity:
                await conn.execute(
                    "DELETE FROM inventory_items WHERE character_id = $1 AND lower(name) = lower($2)", c["id"], item
                )
            else:
                await conn.execute(
                    "UPDATE inventory_items SET quantity = quantity - $3"
                    " WHERE character_id = $1 AND lower(name) = lower($2)",
                    c["id"], item, quantity,
                )
            return "item_removed", f"{c['name']} lost {quantity} x {item}", {"item": item, "quantity": quantity}

        return await change(ctx, campaign, character, mutate)

    @server.tool()
    async def adjust_gold(campaign: str, character: str, amount: int, reason: str, ctx: Context) -> dict[str, Any]:
        """Adds (positive) or spends (negative) gold. Fails if the character can't afford it."""
        if amount == 0:
            raise ToolError("amount must be non-zero.")

        async def mutate(conn, c):
            if c["gold"] + amount < 0:
                raise ToolError(f"{c['name']} has only {c['gold']} gold.")
            await conn.execute("UPDATE characters SET gold = gold + $2 WHERE id = $1", c["id"], amount)
            verb = "gained" if amount > 0 else "spent"
            return "gold", f"{c['name']} {verb} {abs(amount)} gold: {reason}", {
                "amount": amount, "reason": reason, "gold_after": c["gold"] + amount}

        return await change(ctx, campaign, character, mutate)

    # --- quests ------------------------------------------------------------------

    async def quest_row(conn: asyncpg.Connection, cid: int, title: str, *, lock: bool = False) -> dict[str, Any]:
        row = await conn.fetchrow(
            "SELECT id, title, summary, giver, reward, status FROM quests WHERE campaign_id = $1"
            f" AND lower(title) = lower($2) {'FOR UPDATE' if lock else ''}", cid, title,
        )
        if row is None:
            known = await conn.fetch("SELECT title FROM quests WHERE campaign_id = $1 ORDER BY id", cid)
            raise ToolError(f"No quest called {title!r}. Quests: " + (", ".join(r["title"] for r in known) or "none") + ".")
        return dict(row)

    async def quest_log(conn: asyncpg.Connection, cid: int, status: str | None = None) -> list[dict[str, Any]]:
        quests = [dict(r) for r in await conn.fetch(
            """
            SELECT id, title, summary, giver, reward, status, created_at, finished_at FROM quests
            WHERE campaign_id = $1 AND ($2::text IS NULL OR status = $2)
            ORDER BY status <> 'active', coalesce(finished_at, created_at) DESC
            """, cid, status)]
        notes = await conn.fetch(
            "SELECT quest_id, note, actor, created_at FROM quest_notes WHERE quest_id = ANY($1::bigint[]) ORDER BY id",
            [q["id"] for q in quests],
        )
        for q in quests:
            q["notes"] = [{"note": n["note"], "by": n["actor"], "at": n["created_at"].isoformat()}
                          for n in notes if n["quest_id"] == q["id"]]
            q["created_at"] = q["created_at"].isoformat()
            q["finished_at"] = q["finished_at"] and q["finished_at"].isoformat()
            del q["id"]
        return quests

    async def quest_event(ctx: Context, campaign: str, work) -> dict[str, Any]:
        """Runs `work(conn, cid) -> (type, summary, data, title)` with its event, then returns the quest."""
        st = state(ctx)
        async with st.pool.acquire() as conn:
            async with conn.transaction():
                cid = await campaign_id(conn, campaign)
                event_type, summary, data, title = await work(conn, cid)
                event = await events.record(conn, campaign_id=cid, actor=actor(ctx), type=event_type,
                                            summary=summary, data=data)
            quest = next(q for q in await quest_log(conn, cid) if q["title"].lower() == title.lower())
        await st.bus.publish(event)
        return quest

    @server.tool()
    async def add_quest(
        campaign: str, title: str, summary: str, ctx: Context, giver: str = "", reward: str = "", note: str = ""
    ) -> dict[str, Any]:
        """Adds a quest to the party's log when they take one on. `summary` says plainly what they must
        do and why; `giver` is who asked (an NPC's name); `note` is a first lead or clue, if any."""

        async def work(conn, cid):
            try:
                quest_id = await conn.fetchval(
                    "INSERT INTO quests (campaign_id, title, summary, giver, reward) VALUES ($1, $2, $3, $4, $5)"
                    " RETURNING id", cid, title, summary, giver, reward,
                )
            except asyncpg.UniqueViolationError:
                raise ToolError(f"There's already a quest called {title!r}; use update_quest.") from None
            if note:
                await conn.execute("INSERT INTO quest_notes (quest_id, note) VALUES ($1, $2)", quest_id, note)
            return "quest_added", f"New quest: {title}" + (f" (from {giver})" if giver else ""), {
                "title": title, "giver": giver}, title

        return await quest_event(ctx, campaign, work)

    @server.tool()
    async def update_quest(
        campaign: str, title: str, ctx: Context, note: str = "", status: str | None = None, summary: str = "",
        player_note: bool = False,
    ) -> dict[str, Any]:
        """Adds a note to a quest (progress, a clue, a lead, a twist) and/or changes its status to
        active, completed, failed or abandoned. Keep notes short and concrete; players read them.
        (`player_note` is for the web table: a player's own note, shown under their name.)"""
        if status is not None and status not in ("active", "completed", "failed", "abandoned"):
            raise ToolError("status must be active, completed, failed or abandoned.")
        if not (note or status or summary):
            raise ToolError("Give a note, a status or a new summary.")

        async def work(conn, cid):
            q = await quest_row(conn, cid, title, lock=True)
            if status or summary:
                await conn.execute(
                    """
                    UPDATE quests SET status = COALESCE($2, status), summary = COALESCE(NULLIF($3, ''), summary),
                        updated_at = now(),
                        finished_at = CASE WHEN COALESCE($2, status) = 'active' THEN NULL
                                           WHEN $2 IS NOT NULL AND $2 <> status THEN now() ELSE finished_at END
                    WHERE id = $1
                    """, q["id"], status, summary,
                )
            if note:
                # The GM's tool calls run as the player whose turn it is, so only a note the player
                # wrote themselves carries their name.
                await conn.execute("INSERT INTO quest_notes (quest_id, note, actor) VALUES ($1, $2, $3)",
                                   q["id"], note, actor(ctx) if player_note else "gm")
            if status and status != q["status"]:
                summary_text = f"Quest {status}: {q['title']}"
            else:
                summary_text = f"Quest note, {q['title']}: {note}" if note else f"Quest updated: {q['title']}"
            return "quest_updated", summary_text, {"title": q["title"], "status": status, "note": note}, q["title"]

        return await quest_event(ctx, campaign, work)

    @server.tool()
    async def list_quests(campaign: str, ctx: Context, status: str | None = None) -> list[dict[str, Any]]:
        """The party's quest log with notes, active quests first; optionally only one status."""
        async with state(ctx).pool.acquire() as conn:
            return await quest_log(conn, await campaign_id(conn, campaign), status)

    @server.tool()
    async def move_character(campaign: str, character: str, location: str, ctx: Context) -> dict[str, Any]:
        """Moves a character to a location. Call once per character when the party travels."""

        async def mutate(conn, c):
            await conn.execute("UPDATE characters SET location = $2 WHERE id = $1", c["id"], location)
            return "moved", f"{c['name']} moved to {location}", {"from": c["location"], "to": location}

        return await change(ctx, campaign, character, mutate)

    # --- dice --------------------------------------------------------------------

    @server.tool()
    async def roll_dice(
        notation: str,
        reason: str,
        ctx: Context,
        campaign: str | None = None,
        character: str | None = None,
    ) -> dict[str, Any]:
        """Rolls dice such as "d20", "1d20+5", "2d6+1d4-1". Pass `campaign` (and the rolling
        `character`) to record the roll in the event log so players can see it was fair."""
        rolls, modifier = parse_and_roll(notation)
        total = sum(r for group in rolls for r in group["results"]) + modifier
        result = {"notation": notation, "rolls": rolls, "modifier": modifier, "total": total}
        if campaign:
            st = state(ctx)
            async with st.pool.acquire() as conn, conn.transaction():
                cid = await campaign_id(conn, campaign)
                char_id = (await load_character(conn, cid, character))["id"] if character else None
                who = character or "The GM"
                event = await events.record(
                    conn, campaign_id=cid, character_id=char_id, actor=actor(ctx), type="roll",
                    summary=f"{who} rolled {notation} for {reason}: {total}", data=result | {"reason": reason},
                )
            await st.bus.publish(event)
        return result

    # --- event log ---------------------------------------------------------------

    @server.tool()
    async def log_event(
        campaign: str,
        type: str,
        summary: str,
        ctx: Context,
        character: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Records a story beat that changes no numbers, e.g. type "quest_accepted", "npc_met", "combat_started".
        `summary` is one sentence a player could read in a recap."""
        st = state(ctx)
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            char_id = (await load_character(conn, cid, character))["id"] if character else None
            event = await events.record(
                conn, campaign_id=cid, character_id=char_id, actor=actor(ctx),
                type=type.strip().lower(), summary=summary, data=data,
            )
        await st.bus.publish(event)
        return event

    @server.tool()
    async def recent_events(
        campaign: str,
        ctx: Context,
        limit: int = 20,
        character: str | None = None,
        type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Returns the most recent events, oldest first, optionally for one character or of one type."""
        limit = max(1, min(limit, 200))
        async with state(ctx).pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            char_id = (await load_character(conn, cid, character))["id"] if character else None
            rows = await conn.fetch(
                f"""
                SELECT {events.EVENT_COLUMNS} FROM events
                WHERE campaign_id = $1 AND ($2::bigint IS NULL OR character_id = $2) AND ($3::text IS NULL OR type = $3)
                ORDER BY id DESC LIMIT $4
                """,
                cid, char_id, type, limit,
            )
        return [events.event_dict(r) for r in reversed(rows)]

    return server


DICE_TERM = re.compile(r"([+-])?\s*(?:(\d*)d(\d+)|(\d+))", re.IGNORECASE)


def parse_and_roll(notation: str) -> tuple[list[dict[str, Any]], int]:
    """Rolls "NdM" terms and sums constant modifiers. Raises ToolError on anything else."""
    text = notation.replace(" ", "")
    if not text:
        raise ToolError("Empty dice notation.")
    rolls: list[dict[str, Any]] = []
    modifier = 0
    pos = 0
    for m in DICE_TERM.finditer(text):
        if m.start() != pos or (pos > 0 and not m.group(1)):
            break
        pos = m.end()
        sign = -1 if m.group(1) == "-" else 1
        if m.group(3):
            count, sides = int(m.group(2) or 1), int(m.group(3))
            if not (1 <= count <= 100 and 2 <= sides <= 1000):
                raise ToolError("Dice must be 1-100 dice of 2-1000 sides.")
            results = [sign * (secrets.randbelow(sides) + 1) for _ in range(count)]
            rolls.append({"dice": f"{'-' if sign < 0 else ''}{count}d{sides}", "results": results})
        else:
            modifier += sign * int(m.group(4))
    if pos != len(text) or not rolls:
        raise ToolError(f"Can't parse dice notation {notation!r}; use forms like d20, 2d6+3, 1d8+1d6-1.")
    return rolls, modifier


if __name__ == "__main__":
    run(create_server(), PATH)
