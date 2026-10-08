"""Admin tools: run SQL against the game database and search the lore vectors directly."""

import datetime
import decimal
import uuid
from typing import Any

import asyncpg

from lore import events
from lore.embeddings import Embedder

MAX_ROWS = 500
STATEMENT_TIMEOUT = "15s"
VECTOR_PREVIEW = 6


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return "\\x" + bytes(value).hex()
    if hasattr(value, "to_list") or hasattr(value, "tolist"):  # pgvector Vector / numpy array
        values = value.to_list() if hasattr(value, "to_list") else value.tolist()
        head = ", ".join(f"{v:.4f}" for v in values[:VECTOR_PREVIEW])
        return f"[{head}, …] ({len(values)} dims)"
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    return str(value)


async def run_sql(pool: asyncpg.Pool, sql: str, *, allow_writes: bool) -> dict[str, Any]:
    """Runs one statement. Read-only unless `allow_writes`; capped at MAX_ROWS rows and
    STATEMENT_TIMEOUT. Errors come back as {"error": ...} rather than raising."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return {"error": "Empty query."}
    async with pool.acquire() as conn:
        try:
            async with conn.transaction(readonly=not allow_writes):
                await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                stmt = await conn.prepare(sql)
                columns = [a.name for a in stmt.get_attributes()]
                if columns:
                    cursor = await stmt.cursor()
                    rows = await cursor.fetch(MAX_ROWS + 1)
                else:
                    rows = await stmt.fetch()
                status = stmt.get_statusmsg()
        except asyncpg.PostgresError as e:
            message = e.args[0] if e.args else str(e)
            if isinstance(e, asyncpg.ReadOnlySQLTransactionError):
                message += " (tick “Allow writes” to change data)"
            return {"error": message}
        except asyncpg.InterfaceError as e:
            return {"error": str(e)}
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return {
        "columns": columns,
        "rows": [[_json_value(v) for v in row.values()] for row in rows],
        "truncated": truncated,
        # A partially read cursor reports no status, so describe what was returned.
        "status": status or f"SELECT {len(rows)}{'+' if truncated else ''}",
        "wrote": allow_writes,
    }


async def vector_search(
    pool: asyncpg.Pool,
    embedder: Embedder,
    query: str,
    *,
    campaign_id: int | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Lore entries nearest to `query` by cosine similarity, across all campaigns by default."""
    embedding = await embedder.embed_query(query)
    rows = await pool.fetch(
        """
        SELECT l.id, c.name AS campaign, l.kind, l.title, l.content, l.tags, l.updated_at,
               1 - (l.embedding <=> $1) AS similarity
        FROM lore_entries l JOIN campaigns c ON c.id = l.campaign_id
        WHERE ($2::bigint IS NULL OR l.campaign_id = $2) AND ($3::text IS NULL OR l.kind = $3)
        ORDER BY l.embedding <=> $1 LIMIT $4
        """,
        embedding, campaign_id, kind and kind.strip().lower(), max(1, min(limit, 100)),
    )
    return [{k: _json_value(v) for k, v in dict(r).items()} | {"similarity": round(r["similarity"], 4)} for r in rows]


async def overview(pool: asyncpg.Pool) -> dict[str, Any]:
    """Tables with their columns and approximate row counts, plus lore counts per campaign and kind."""
    columns = await pool.fetch(
        """
        SELECT c.table_name, c.column_name, c.data_type, c.udt_name
        FROM information_schema.columns c
        JOIN information_schema.tables t USING (table_schema, table_name)
        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position
        """
    )
    counts = {
        r["relname"]: r["n"]
        for r in await pool.fetch("SELECT relname, n_live_tup AS n FROM pg_stat_user_tables")
    }
    tables: dict[str, dict[str, Any]] = {}
    for r in columns:
        table = tables.setdefault(r["table_name"], {"name": r["table_name"], "rows": counts.get(r["table_name"]), "columns": []})
        kind = r["udt_name"] if r["data_type"] == "USER-DEFINED" else r["data_type"]
        table["columns"].append({"name": r["column_name"], "type": kind})
    lore = await pool.fetch(
        """
        SELECT c.id AS campaign_id, c.name AS campaign, l.kind, count(*) AS entries
        FROM lore_entries l JOIN campaigns c ON c.id = l.campaign_id
        GROUP BY c.id, c.name, l.kind ORDER BY c.name, l.kind
        """
    )
    return {"tables": list(tables.values()), "lore": [dict(r) for r in lore]}


# --- world and character management --------------------------------------------------

class AdminError(Exception):
    """A refused admin change, with a message for the admin."""


async def worlds(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Every campaign with its characters and activity, newest first."""
    campaigns = await pool.fetch(
        """
        SELECT c.id, c.name, c.setting, c.created_at,
               (SELECT count(*) FROM lore_entries l WHERE l.campaign_id = c.id) AS lore,
               (SELECT count(*) FROM events e WHERE e.campaign_id = c.id) AS events,
               (SELECT max(occurred_at) FROM events e WHERE e.campaign_id = c.id) AS last_activity
        FROM campaigns c ORDER BY c.created_at DESC
        """
    )
    characters = await pool.fetch(
        """
        SELECT ch.id, ch.campaign_id, ch.name, p.username AS player, ch.race, ch.class, ch.level, ch.hp,
               ch.max_hp, ch.temp_hp, ch.defense, ch.gold, ch.status, ch.location
        FROM characters ch LEFT JOIN players p ON p.id = ch.player_id ORDER BY lower(ch.name)
        """
    )
    by_campaign: dict[int, list[dict[str, Any]]] = {}
    for ch in characters:
        by_campaign.setdefault(ch["campaign_id"], []).append({k: _json_value(v) for k, v in dict(ch).items()})
    return [
        {k: _json_value(v) for k, v in dict(c).items()} | {"characters": by_campaign.get(c["id"], [])}
        for c in campaigns
    ]


async def rename_campaign(pool: asyncpg.Pool, campaign_id: int, new_name: str, actor: str) -> dict[str, Any]:
    """Renames a campaign and logs it. Returns the event (publish it after)."""
    new_name = new_name.strip()
    if not new_name:
        raise AdminError("The new name can't be empty.")
    async with pool.acquire() as conn, conn.transaction():
        old = await conn.fetchval("SELECT name FROM campaigns WHERE id = $1 FOR UPDATE", campaign_id)
        if old is None:
            raise AdminError("That world no longer exists.")
        try:
            await conn.execute("UPDATE campaigns SET name = $2 WHERE id = $1", campaign_id, new_name)
        except asyncpg.UniqueViolationError:
            raise AdminError(f"A world named {new_name!r} already exists.") from None
        return await events.record(
            conn, campaign_id=campaign_id, actor=actor, type="campaign_renamed",
            summary=f"The world {old} is now called {new_name}", data={"old": old, "new": new_name},
        )


async def delete_campaign(pool: asyncpg.Pool, campaign_id: int, confirm_name: str) -> str:
    """Deletes a campaign and everything in it (characters, events, lore). `confirm_name`
    must match its name exactly, as a guard against deleting the wrong world."""
    async with pool.acquire() as conn, conn.transaction():
        name = await conn.fetchval("SELECT name FROM campaigns WHERE id = $1 FOR UPDATE", campaign_id)
        if name is None:
            raise AdminError("That world no longer exists.")
        if confirm_name != name:
            raise AdminError("The confirmation doesn't match the world's name.")
        await conn.execute("DELETE FROM campaigns WHERE id = $1", campaign_id)
    return name


CHARACTER_FIELDS = {"name", "player", "level", "hp", "max_hp", "temp_hp", "defense", "gold", "status"}
# Plain explanations for the characters table's CHECK constraints (see 0001_initial.sql).
CONSTRAINT_MESSAGES = {
    "characters_check": "HP must be between 0 and max HP.",
    "characters_level_check": "Level must be at least 1.",
    "characters_max_hp_check": "Max HP must be at least 1.",
    "characters_temp_hp_check": "Temporary HP can't be negative.",
    "characters_gold_check": "Gold can't be negative.",
    "characters_status_check": "Status must be alive, unconscious or dead.",
}


async def update_character(
    pool: asyncpg.Pool, character_id: int, changes: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Applies an admin's corrections to a character sheet and logs exactly what changed.
    `player` reassigns ownership (empty makes the character an NPC). Returns the event."""
    unknown = set(changes) - CHARACTER_FIELDS
    if unknown:
        raise AdminError(f"Can't edit {', '.join(sorted(unknown))}.")
    async with pool.acquire() as conn, conn.transaction():
        before = await conn.fetchrow(
            "SELECT ch.*, p.username AS player FROM characters ch LEFT JOIN players p ON p.id = ch.player_id"
            " WHERE ch.id = $1 FOR UPDATE OF ch",
            character_id,
        )
        if before is None:
            raise AdminError("That character no longer exists.")
        diff = {k: (before[k], v) for k, v in changes.items() if v != before[k] and not (k == "player" and not v and not before[k])}
        if not diff:
            raise AdminError("Nothing changed.")
        sets, args = [], [character_id]
        for field, (_, value) in diff.items():
            if field == "player":
                player_id = None
                if value:
                    player_id = await conn.fetchval(
                        "INSERT INTO players (username) VALUES ($1)"
                        " ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username RETURNING id",
                        value,
                    )
                args.append(player_id)
                sets.append(f"player_id = ${len(args)}")
            else:
                args.append(value.strip() if isinstance(value, str) else value)
                sets.append(f"{field} = ${len(args)}")
        try:
            await conn.execute(f"UPDATE characters SET {', '.join(sets)}, updated_at = now() WHERE id = $1", *args)
        except asyncpg.UniqueViolationError:
            raise AdminError(f"Another character in this world is already named {changes['name']!r}.") from None
        except asyncpg.CheckViolationError as e:
            raise AdminError(CONSTRAINT_MESSAGES.get(e.constraint_name, f"Invalid value ({e.constraint_name}).")) from None
        except (asyncpg.NotNullViolationError, asyncpg.DataError) as e:
            raise AdminError(f"Invalid value: {e.args[0] if e.args else e}") from None
        name = diff["name"][1] if "name" in diff else before["name"]
        described = ", ".join(f"{k} {_short(old)} → {_short(new)}" for k, (old, new) in diff.items())
        return await events.record(
            conn, campaign_id=before["campaign_id"], character_id=character_id, actor=actor, type="admin_edit",
            summary=f"An admin corrected {name}: {described}",
            data={k: {"from": _json_value(old), "to": _json_value(new)} for k, (old, new) in diff.items()},
        )


def _short(value: Any) -> str:
    return "none" if value in (None, "") else str(value)
