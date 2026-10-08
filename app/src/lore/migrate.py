"""Applies migrations/*.sql in filename order, each once, each in its own transaction.

Run with `python -m lore.migrate`. Safe to run concurrently (every MCP pod runs it
as an init container): an advisory lock serialises runners.
"""

import asyncio
import logging
from importlib import resources

import asyncpg

log = logging.getLogger("lore.migrate")

# Arbitrary constant identifying this app's migration lock.
LOCK_ID = 7_301_955_210


def _migrations() -> list[tuple[str, str]]:
    files = resources.files("lore.migrations")
    return sorted((f.name, f.read_text()) for f in files.iterdir() if f.name.endswith(".sql"))


async def migrate(conn: asyncpg.Connection) -> list[str]:
    """Applies pending migrations; returns the names applied."""
    applied: list[str] = []
    await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for name, sql in _migrations():
            if name in done:
                continue
            log.info("applying %s", name)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", name)
            applied.append(name)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
    return applied


async def _main() -> None:
    # The database may still be starting when the init container runs.
    for attempt in range(30):
        try:
            conn = await asyncpg.connect()
            break
        except (OSError, asyncpg.CannotConnectNowError) as e:
            log.info("waiting for postgres (%s)", e)
            await asyncio.sleep(2)
    else:
        raise SystemExit("postgres not reachable")
    try:
        applied = await migrate(conn)
        log.info("applied %d migration(s)%s", len(applied), f": {', '.join(applied)}" if applied else "")
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    asyncio.run(_main())
