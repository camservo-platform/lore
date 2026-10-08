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
Whenever you invent something that should persist (a new NPC, a tavern's name, a
rumour, a plot thread), record it with add_lore. Game numbers such as HP and
inventory belong in the game server, not here."""

KIND_HINT = "location, npc, faction, item, history, storyline, rumor or other"


def _entry(row) -> dict[str, Any]:
    entry = {k: row[k] for k in ("kind", "title", "content", "tags")}
    if "distance" in row.keys():
        entry["similarity"] = round(1 - row["distance"], 3)
    return entry


def create_server(lifespan: Callable = default_lifespan) -> MCPServer:
    server = MCPServer("lore-lore", instructions=INSTRUCTIONS, lifespan=lifespan)

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
            row = await conn.fetchrow(
                """
                INSERT INTO lore_entries (campaign_id, kind, title, content, tags, embedding)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (campaign_id, kind, (lower(title))) DO UPDATE
                SET title = EXCLUDED.title, content = EXCLUDED.content, tags = EXCLUDED.tags,
                    embedding = EXCLUDED.embedding, updated_at = now()
                RETURNING kind, title, content, tags, (xmax <> 0) AS replaced
                """,
                cid, kind, title, content, tags, embedding,
            )
            verb = "updated" if row["replaced"] else "recorded"
            event = await events.record(
                conn, campaign_id=cid, actor=actor(ctx), type=f"lore_{verb}",
                summary=f"Lore {verb}: {kind} '{title}'", data={"kind": kind, "title": title},
            )
        await st.bus.publish(event)
        return _entry(row) | {"replaced": row["replaced"]}

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
                """
                SELECT kind, title, content, tags, embedding <=> $2 AS distance FROM lore_entries
                WHERE campaign_id = $1 AND ($3::text IS NULL OR kind = $3) AND ($4::text[] IS NULL OR tags @> $4)
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
                "SELECT kind, title, content, tags FROM lore_entries"
                " WHERE campaign_id = $1 AND lower(title) = lower($2) AND ($3::text IS NULL OR kind = $3)"
                " ORDER BY kind",
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
