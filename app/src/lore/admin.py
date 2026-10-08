"""Admin tools: run SQL against the game database and search the lore vectors directly."""

import datetime
import decimal
import uuid
from typing import Any

import asyncpg

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
