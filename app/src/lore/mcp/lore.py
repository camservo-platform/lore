"""MCP server for world knowledge: locations, NPCs, factions, history, storylines.
Entries are embedded and searched by meaning with pgvector.

Run with `python -m lore.mcp.lore`.
"""

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from lore import events
from lore.mcp.common import actor, campaign_id, default_lifespan, run, state

PATH = "/mcp/lore"

INSTRUCTIONS = """\
The campaign's world knowledge. Before describing a place, NPC, faction or past
event, search_lore for what is already established and stay consistent with it.
Whenever you invent something that should persist (a tavern's name, a rumour, a plot
thread), record it with add_lore. Every named NPC has an entry: record new ones with
record_npc, and note what happens with them (update_npc) so they can turn up again
with a history. Game numbers such as HP and inventory belong in the game server."""

KIND_HINT = "location, npc, faction, item, history, storyline, rumor or other"
STATUSES = ("alive", "dead", "missing", "unknown")
DISPOSITIONS = ("friendly", "neutral", "wary", "hostile", "unknown")
VOICES = ("", "feminine", "masculine")
# An NPC talking for several turns in a row is one appearance, not many.
APPEARANCE_GAP = "30 minutes"
NPC_COLUMNS = ("d.location, d.status, d.disposition, d.voice, d.appearances, d.first_seen, d.last_seen, d.stub")


def _entry(row) -> dict[str, Any]:
    entry = {k: row[k] for k in ("kind", "title", "content", "tags")}
    if "distance" in row.keys():
        entry["similarity"] = round(1 - row["distance"], 3)
    if row.get("status") is not None:  # an NPC: their living details too
        entry |= {k: row[k] for k in ("location", "status", "disposition", "appearances")}
        entry["last_seen"] = row["last_seen"] and row["last_seen"].isoformat()
        if row["stub"]:
            entry["stub"] = True
    return entry


def _check(value: str | None, allowed: tuple[str, ...], field: str) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value not in allowed:
        raise ToolError(f"{field} must be one of: {', '.join(a or '(empty)' for a in allowed)}.")
    return value


async def _add_note(conn, lore_id: int, cid: int, note: str, who: str) -> None:
    await conn.execute(
        "INSERT INTO lore_notes (lore_id, note, actor, session_id) VALUES ($1, $2, $3,"
        " (SELECT id FROM game_sessions WHERE campaign_id = $4 AND ended_at IS NULL))",
        lore_id, note, who, cid,
    )


def create_server(lifespan: Callable = default_lifespan) -> MCPServer:
    server = MCPServer("lore-lore", instructions=INSTRUCTIONS, lifespan=lifespan)

    async def upsert(conn, cid: int, kind: str, title: str, content: str, tags: list[str], embedding,
                     *, stub: bool = False):
        row = await conn.fetchrow(
            """
            INSERT INTO lore_entries (campaign_id, kind, title, content, tags, embedding)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (campaign_id, kind, (lower(title))) DO UPDATE
            SET title = EXCLUDED.title, content = EXCLUDED.content, tags = EXCLUDED.tags,
                embedding = EXCLUDED.embedding, updated_at = now()
            RETURNING id, kind, title, content, tags, (xmax <> 0) AS replaced
            """,
            cid, kind, title, content, tags, embedding,
        )
        if kind == "npc":
            # Every NPC entry has its living details; writing one by hand means it's no longer a stub.
            await conn.execute(
                "INSERT INTO npc_details (lore_id, stub) VALUES ($1, $2)"
                " ON CONFLICT (lore_id) DO UPDATE SET stub = npc_details.stub AND EXCLUDED.stub",
                row["id"], stub,
            )
        return row

    async def npc(conn, cid: int, name: str, *, lock: bool = False):
        row = await conn.fetchrow(
            f"SELECT e.id, e.kind, e.title, e.content, e.tags, {NPC_COLUMNS} FROM lore_entries e"
            " JOIN npc_details d ON d.lore_id = e.id"
            f" WHERE e.campaign_id = $1 AND e.kind = 'npc' AND lower(e.title) = lower($2)"
            f" {'FOR UPDATE OF d' if lock else ''}", cid, name.strip(),
        )
        if row is None:
            raise ToolError(f"No NPC called {name!r}; record_npc first (search_lore kind npc to check spellings).")
        return row

    async def publish(st, event) -> None:
        await st.bus.publish(event)

    @server.tool(description=f"""Records or replaces a piece of world knowledge. `kind` is one of {KIND_HINT}.
`title` names it uniquely within its kind (e.g. "The Gilded Flagon"); writing an existing
kind+title replaces its content. `content` should be self-contained prose: it is what
search_lore will return later.""")
    async def add_lore(
        campaign: str, kind: str, title: str, content: str, ctx: Context, tags: list[str] | None = None
    ) -> dict[str, Any]:
        st = state(ctx)
        kind = kind.strip().lower()
        tags = sorted({t.strip().lower() for t in tags or [] if t.strip()})
        # Embed outside the transaction: it's a network call to the embedding server.
        embedding = await st.embedder.embed_document(f"{title}\n\n{content}")
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            row = await upsert(conn, cid, kind, title, content, tags, embedding)
            verb = "updated" if row["replaced"] else "recorded"
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type=f"lore_{verb}",
                summary=f"Lore {verb}: {kind} '{title}'", data={"kind": kind, "title": title},
            )
        await st.bus.publish(event)
        return _entry(row) | {"replaced": row["replaced"]}

    # --- NPCs -------------------------------------------------------------------

    @server.tool()
    async def record_npc(
        campaign: str, name: str, description: str, ctx: Context, location: str = "", voice: str = "",
        disposition: str = "unknown", tags: list[str] | None = None, note: str = "",
    ) -> dict[str, Any]:
        """Records a named NPC (or rewrites their entry). `description` is self-contained prose: who
        they are, what they look like, what they want and any secret. `location` is where they can
        usually be found; `voice` is feminine or masculine (it picks their speaking voice); `note`
        starts their history, e.g. "Met the party at the ferry crossing"."""
        st = state(ctx)
        voice = _check(voice, VOICES, "voice")
        disposition = _check(disposition, DISPOSITIONS, "disposition")
        tags = sorted({t.strip().lower() for t in tags or [] if t.strip()})
        embedding = await st.embedder.embed_document(f"{name}\n\n{description}")
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            row = await upsert(conn, cid, "npc", name, description, tags, embedding)
            await conn.execute(
                "UPDATE npc_details SET location = COALESCE(NULLIF($2, ''), location),"
                " voice = COALESCE(NULLIF($3, ''), voice), disposition = $4 WHERE lore_id = $1",
                row["id"], location, voice, disposition,
            )
            if note:
                await _add_note(conn, row["id"], cid, note, actor(ctx))
            verb = "updated" if row["replaced"] else "recorded"
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type=f"npc_{verb}",
                summary=f"NPC {verb}: {row['title']}", data={"name": row["title"]},
            )
            result = await npc(conn, cid, name)
        await publish(st, event)
        return _entry(result)

    @server.tool()
    async def npc_appeared(
        campaign: str, name: str, ctx: Context, location: str = "", voice: str = "", line: str = ""
    ) -> dict[str, Any]:
        """Marks that a named NPC appeared in a scene (the web table calls this for everyone who
        speaks). A known NPC's appearance is counted (once per scene) and where they were seen kept;
        an unknown one gets a stub entry for the GM to fill in with record_npc."""
        st = state(ctx)
        name = name.strip()[:80]
        if not name:
            raise ToolError("name is required.")
        async with st.pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            known = await conn.fetchval(
                "SELECT id FROM lore_entries WHERE campaign_id = $1 AND kind = 'npc' AND lower(title) = lower($2)",
                cid, name,
            )
        embedding = None
        if known is None:
            where = f" at {location}" if location else ""
            content = (f"{name}, someone the party met{where}. Not yet described." +
                       (f' First words: "{line[:200]}"' if line else ""))
            embedding = await st.embedder.embed_document(f"{name}\n\n{content}")
        async with st.pool.acquire() as conn:
            async with conn.transaction():
                event = None
                if known is None:
                    row = await upsert(conn, cid, "npc", name, content, ["stub"], embedding, stub=True)
                    await _add_note(conn, row["id"], cid, f"First met{(' at ' + location) if location else ''}.",
                                    actor(ctx))
                    event_type, summary = "npc_met", f"Met {name}"
                    lore_id = row["id"]
                else:
                    lore_id = known
                    event_type, summary = "npc_seen", f"{name} turned up again"
                changed = await conn.fetchval(
                    f"""
                    UPDATE npc_details SET appearances = appearances + 1, last_seen = now(),
                        first_seen = COALESCE(first_seen, now()), location = COALESCE(NULLIF($2, ''), location),
                        voice = CASE WHEN voice = '' THEN $3 ELSE voice END
                    WHERE lore_id = $1 AND (last_seen IS NULL OR last_seen < now() - interval '{APPEARANCE_GAP}')
                    RETURNING appearances
                    """,
                    lore_id, location, _check(voice or "", VOICES, "voice"),
                )
                if changed is not None:
                    event = await events.record(conn, campaign_id=cid, actor=actor(ctx), type=event_type,
                                                summary=summary, data={"name": name, "location": location})
            result = await npc(conn, cid, name)
        if event:
            await publish(st, event)
        return _entry(result)

    @server.tool()
    async def update_npc(
        campaign: str, name: str, ctx: Context, note: str = "", status: str | None = None,
        disposition: str | None = None, location: str = "",
    ) -> dict[str, Any]:
        """Adds to an NPC's history (`note`: what happened, a promise, a grudge, a secret revealed) and/or
        changes their status (alive, dead, missing, unknown), how they feel about the party (friendly,
        neutral, wary, hostile, unknown) or where they can be found."""
        status = _check(status, STATUSES, "status")
        disposition = _check(disposition, DISPOSITIONS, "disposition")
        if not (note or status or disposition or location):
            raise ToolError("Give a note, status, disposition or location.")
        st = state(ctx)
        async with st.pool.acquire() as conn:
            async with conn.transaction():
                cid = await campaign_id(conn, campaign)
                before = await npc(conn, cid, name, lock=True)
                await conn.execute(
                    "UPDATE npc_details SET status = COALESCE($2, status), disposition = COALESCE($3, disposition),"
                    " location = COALESCE(NULLIF($4, ''), location) WHERE lore_id = $1",
                    before["id"], status, disposition, location,
                )
                if note:
                    await _add_note(conn, before["id"], cid, note, actor(ctx))
                changes = [f"is {status}" for _ in [1] if status and status != before["status"]]
                changes += [f"now {disposition} toward the party" for _ in [1]
                            if disposition and disposition != before["disposition"]]
                summary = f"{before['title']} " + (", ".join(changes) if changes else f"- {note or 'moved to ' + location}")
                event = await events.record(conn, campaign_id=cid, actor=actor(ctx), type="npc_updated",
                                            summary=summary, data={"name": before["title"], "status": status,
                                                                   "disposition": disposition, "note": note})
            result = await npc(conn, cid, name)
        await publish(st, event)
        return _entry(result)

    @server.tool()
    async def get_npc(campaign: str, name: str, ctx: Context) -> dict[str, Any]:
        """An NPC's entry, living details and full history, oldest first."""
        async with state(ctx).pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            row = await npc(conn, cid, name)
            notes = await conn.fetch("SELECT note, created_at FROM lore_notes WHERE lore_id = $1 ORDER BY id", row["id"])
        return _entry(row) | {"history": [{"note": n["note"], "at": n["created_at"].isoformat()} for n in notes]}

    @server.tool()
    async def list_npcs(
        campaign: str, ctx: Context, location: str | None = None, include_gone: bool = False
    ) -> list[dict[str, Any]]:
        """The world's NPCs (name, a line about them, where they were last seen, status, how they feel
        about the party, appearances), most recently seen first. Filter by `location` (case-insensitive
        match on part of it); dead and missing NPCs are left out unless `include_gone`."""
        async with state(ctx).pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            rows = await conn.fetch(
                f"""
                SELECT e.kind, e.title, e.content, e.tags, {NPC_COLUMNS} FROM lore_entries e
                JOIN npc_details d ON d.lore_id = e.id
                WHERE e.campaign_id = $1 AND ($2::text IS NULL OR d.location ILIKE '%' || $2 || '%')
                  AND ($3 OR d.status IN ('alive', 'unknown'))
                ORDER BY d.last_seen DESC NULLS LAST, lower(e.title)
                """, cid, location, include_gone,
            )
        out = []
        for r in rows:
            entry = _entry(r)
            entry["summary"] = entry.pop("content").split(". ")[0][:160]
            del entry["kind"], entry["tags"]
            out.append(entry)
        return out

    # --- story memory -------------------------------------------------------------

    @server.tool()
    async def record_story(
        campaign: str, narration: str, ctx: Context, said: str = "", player: str = ""
    ) -> dict[str, Any]:
        """Keeps one moment of play (what `player` said or did, and the narration) in the world's story
        memory, searchable by meaning. The web table records every turn itself."""
        narration = narration.strip()
        if not narration:
            raise ToolError("narration is required.")
        st = state(ctx)
        # Who did what, then what happened: the text recall matches against.
        text = (f"{player}: {said}\n\n" if said else "") + narration
        embedding = await st.embedder.embed_document(text[:6000])
        async with st.pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            moment_id = await conn.fetchval(
                """
                INSERT INTO story_moments (campaign_id, session_id, player, said, narration, embedding)
                VALUES ($1, (SELECT id FROM game_sessions WHERE campaign_id = $1 AND ended_at IS NULL), $2, $3, $4, $5)
                RETURNING id
                """,
                cid, player, said, narration, embedding,
            )
        return {"id": moment_id}

    @server.tool()
    async def recall_story(
        campaign: str, query: str, ctx: Context, limit: int = 4, skip_recent: int = 0, min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Finds earlier moments of play by meaning: what the players did and what came of it, across
        every player and session in this world (e.g. "what did we promise the ferryman?"). Most relevant
        first. `skip_recent` leaves out the latest moments (already in the conversation)."""
        st = state(ctx)
        limit = max(1, min(limit, 20))
        embedding = await st.embedder.embed_query(query)
        async with st.pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            rows = await conn.fetch(
                """
                WITH older AS (
                    SELECT * FROM story_moments WHERE campaign_id = $1 ORDER BY id DESC OFFSET $3
                )
                SELECT player, said, narration, created_at, session_id, embedding <=> $2 AS distance
                FROM older WHERE 1 - (embedding <=> $2) >= $5 ORDER BY distance LIMIT $4
                """,
                cid, embedding, max(0, skip_recent), limit, min_similarity,
            )
        return [{"player": r["player"], "said": r["said"], "narration": r["narration"],
                 "at": r["created_at"].isoformat(), "session_id": r["session_id"],
                 "similarity": round(1 - r["distance"], 3)} for r in rows]

    @server.tool()
    async def search_lore(
        campaign: str,
        query: str,
        ctx: Context,
        kind: str | None = None,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Finds world knowledge by meaning (e.g. "who rules the northern marshes?"), most relevant first.
        Optionally restrict to one `kind` or to entries having all `tags`."""
        st = state(ctx)
        limit = max(1, min(limit, 20))
        embedding = await st.embedder.embed_query(query)
        async with st.pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            rows = await conn.fetch(
                f"""
                SELECT e.kind, e.title, e.content, e.tags, e.embedding <=> $2 AS distance, {NPC_COLUMNS}
                FROM lore_entries e LEFT JOIN npc_details d ON d.lore_id = e.id
                WHERE e.campaign_id = $1 AND ($3::text IS NULL OR e.kind = $3) AND ($4::text[] IS NULL OR e.tags @> $4)
                ORDER BY distance LIMIT $5
                """,
                cid, embedding, kind and kind.strip().lower(), tags and [t.strip().lower() for t in tags], limit,
            )
        return [_entry(r) for r in rows]

    @server.tool()
    async def get_lore(campaign: str, title: str, ctx: Context, kind: str | None = None) -> list[dict[str, Any]]:
        """Fetches entries by exact title (case-insensitive); several if the title exists under different kinds."""
        async with state(ctx).pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            rows = await conn.fetch(
                f"SELECT e.kind, e.title, e.content, e.tags, {NPC_COLUMNS} FROM lore_entries e"
                " LEFT JOIN npc_details d ON d.lore_id = e.id"
                " WHERE e.campaign_id = $1 AND lower(e.title) = lower($2) AND ($3::text IS NULL OR e.kind = $3)"
                " ORDER BY e.kind",
                cid, title, kind and kind.strip().lower(),
            )
        if not rows:
            raise ToolError(f"No lore titled {title!r}. Try search_lore.")
        return [_entry(r) for r in rows]

    @server.tool()
    async def list_lore(campaign: str, ctx: Context, kind: str | None = None) -> list[dict[str, Any]]:
        """Lists entry titles and tags (no content), optionally of one kind."""
        async with state(ctx).pool.acquire() as conn:
            cid = await campaign_id(conn, campaign)
            rows = await conn.fetch(
                "SELECT kind, title, tags FROM lore_entries WHERE campaign_id = $1 AND ($2::text IS NULL OR kind = $2)"
                " ORDER BY kind, lower(title)",
                cid, kind and kind.strip().lower(),
            )
        return [dict(r) for r in rows]

    @server.tool()
    async def delete_lore(campaign: str, kind: str, title: str, reason: str, ctx: Context) -> dict[str, Any]:
        """Deletes an entry that is wrong or retconned. Prefer add_lore to update facts that changed in-story."""
        st = state(ctx)
        kind = kind.strip().lower()
        async with st.pool.acquire() as conn, conn.transaction():
            cid = await campaign_id(conn, campaign)
            deleted = await conn.fetchval(
                "DELETE FROM lore_entries WHERE campaign_id = $1 AND kind = $2 AND lower(title) = lower($3)"
                " RETURNING title",
                cid, kind, title,
            )
            if deleted is None:
                raise ToolError(f"No {kind} titled {title!r}.")
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type="lore_deleted",
                summary=f"Lore deleted: {kind} '{deleted}' ({reason})", data={"kind": kind, "title": deleted},
            )
        await st.bus.publish(event)
        return {"deleted": {"kind": kind, "title": deleted}}

    return server


if __name__ == "__main__":
    run(create_server(), PATH)
