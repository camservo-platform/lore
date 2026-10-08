"""MCP server for hard game state: campaigns, characters, HP, inventory, sessions and
the event log. Every state change records an event in the same transaction.

Run with `python -m lore.mcp.game`.
"""

from collections.abc import Callable
from typing import Any

import asyncpg
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from lore import events
from lore.mcp.common import actor, campaign_id, default_lifespan, run, state

PATH = "/mcp/game"

CHARACTER_COLUMNS = (
    "c.id, c.name, c.race, c.class, c.level, c.hp, c.max_hp, c.temp_hp, c.defense, c.attributes,"
    " c.conditions, c.gold, c.location, c.status, p.username AS player"
)

INSTRUCTIONS = """\
Authoritative game state for a tabletop role-playing campaign. Never track HP, inventory, gold,
conditions or location in your head: read them here and change them only through
these tools, which also write the campaign's event log. Use log_event for story
beats that change no numbers (an NPC met, a quest accepted, a door opened).
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

    async def sheet(conn: asyncpg.Connection, cid: int, name: str) -> dict[str, Any]:
        character = await load_character(conn, cid, name)
        character["inventory"] = [
            dict(r)
            for r in await conn.fetch(
                "SELECT name, quantity, description FROM inventory_items WHERE character_id = $1 ORDER BY name",
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
        rows = await state(ctx).pool.fetch("SELECT name, setting FROM campaigns ORDER BY created_at")
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
        return {"name": name, "setting": setting}

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
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type="session_started", summary="Session started"
            )
        await st.bus.publish(event)
        return {"session_id": row["id"], "started_at": row["started_at"].isoformat()}

    @server.tool()
    async def end_session(campaign: str, summary: str, ctx: Context) -> dict[str, Any]:
        """Ends the open session with a recap of what happened (a few sentences)."""
        st = state(ctx)
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
        await st.bus.publish(event)
        return {"session_id": session_id, "summary": summary}

    # --- characters --------------------------------------------------------------

    @server.tool()
    async def create_character(
        campaign: str,
        name: str,
        max_hp: int,
        ctx: Context,
        race: str = "",
        character_class: str = "",
        level: int = 1,
        defense: int = 10,
        attributes: dict[str, int] | None = None,
        gold: int = 0,
        location: str = "",
        player: str | None = None,
    ) -> dict[str, Any]:
        """Creates a character at full HP. `player` is the owning user's username; omit it for NPCs.
        `defense` is how hard the character is to hit; `attributes` maps stat names to scores
        (e.g. {"strength": 12, "agility": 14})."""
        if max_hp < 1:
            raise ToolError("max_hp must be at least 1.")
        st = state(ctx)
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
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
            return "character_updated", f"{c['name']}: {reason}", changes

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
        campaign: str, character: str, item: str, ctx: Context, quantity: int = 1, description: str = ""
    ) -> dict[str, Any]:
        """Adds items to a character's inventory, stacking with any of the same name."""
        if quantity < 1:
            raise ToolError("quantity must be positive.")

        async def mutate(conn, c):
            await conn.execute(
                """
                INSERT INTO inventory_items (character_id, name, quantity, description) VALUES ($1, $2, $3, $4)
                ON CONFLICT (character_id, (lower(name))) DO UPDATE
                SET quantity = inventory_items.quantity + EXCLUDED.quantity,
                    description = COALESCE(NULLIF(EXCLUDED.description, ''), inventory_items.description)
                """,
                c["id"], item, quantity, description,
            )
            return "item_added", f"{c['name']} gained {quantity} x {item}", {"item": item, "quantity": quantity}

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

    @server.tool()
    async def move_character(campaign: str, character: str, location: str, ctx: Context) -> dict[str, Any]:
        """Moves a character to a location. Call once per character when the party travels."""

        async def mutate(conn, c):
            await conn.execute("UPDATE characters SET location = $2 WHERE id = $1", c["id"], location)
            return "moved", f"{c['name']} moved to {location}", {"from": c["location"], "to": location}

        return await change(ctx, campaign, character, mutate)

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


if __name__ == "__main__":
    run(create_server(), PATH)
