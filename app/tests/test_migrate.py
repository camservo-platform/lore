from lore.migrate import migrate


async def test_migrate_is_idempotent(app_state):
    async with app_state.pool.acquire() as conn:
        assert await migrate(conn) == []
        assert await conn.fetchval("SELECT count(*) FROM schema_migrations") >= 1
        assert await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
