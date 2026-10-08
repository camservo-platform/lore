from lore.admin import MAX_ROWS, overview, run_sql, vector_search


async def test_select_returns_columns_and_json_values(app_state, game_tools, lore_tools, campaign):
    await lore_tools("add_lore", campaign=campaign, kind="npc", title="Marta", content="A dwarf innkeeper.")
    result = await run_sql(
        app_state.pool,
        f"SELECT l.title, l.created_at, l.embedding, l.tags FROM lore_entries l JOIN campaigns c ON c.id = l.campaign_id"
        f" WHERE c.name = '{campaign}';",
        allow_writes=False,
    )
    assert result["columns"] == ["title", "created_at", "embedding", "tags"]
    [[title, created, embedding, tags]] = result["rows"]
    assert title == "Marta" and "T" in created and embedding.endswith("(768 dims)") and tags == []
    assert result["status"] == "SELECT 1"


async def test_read_only_by_default(app_state, campaign):
    result = await run_sql(app_state.pool, f"UPDATE campaigns SET setting = 'x' WHERE name = '{campaign}'",
                           allow_writes=False)
    assert "read-only" in result["error"] and "Allow writes" in result["error"]


async def test_writes_when_allowed(app_state, game_tools, campaign):
    result = await run_sql(app_state.pool, f"UPDATE campaigns SET setting = 'Changed.' WHERE name = '{campaign}'",
                           allow_writes=True)
    assert result["status"] == "UPDATE 1" and result["columns"] == []
    [c] = [c for c in await game_tools("list_campaigns") if c["name"] == campaign]
    assert c["setting"] == "Changed."


async def test_rows_are_capped(app_state):
    result = await run_sql(app_state.pool, "SELECT generate_series(1, 2000) AS n", allow_writes=False)
    assert len(result["rows"]) == MAX_ROWS and result["truncated"] is True


async def test_errors_are_reported(app_state):
    assert "syntax error" in (await run_sql(app_state.pool, "SELEC 1", allow_writes=False))["error"]
    assert (await run_sql(app_state.pool, "  ;", allow_writes=False))["error"] == "Empty query."


async def test_vector_search_across_campaigns(app_state, game_tools, lore_tools, campaign):
    await lore_tools("add_lore", campaign=campaign, kind="location", title="Ember Market",
                     content="A night bazaar selling fireproof cloaks.")
    hits = await vector_search(app_state.pool, app_state.embedder, "fireproof cloaks bazaar", limit=3)
    assert hits[0]["title"] == "Ember Market" and hits[0]["campaign"] == campaign
    assert hits[0]["similarity"] >= hits[-1]["similarity"]
    assert await vector_search(app_state.pool, app_state.embedder, "cloaks", kind="faction",
                               campaign_id=-1) == []


async def test_overview_lists_tables_and_lore_counts(app_state, lore_tools, campaign):
    await lore_tools("add_lore", campaign=campaign, kind="rumor", title="Whispers", content="Something stirs.")
    data = await overview(app_state.pool)
    tables = {t["name"]: t for t in data["tables"]}
    assert {"campaigns", "characters", "events", "lore_entries"} <= set(tables)
    assert {"name": "embedding", "type": "vector"} in tables["lore_entries"]["columns"]
    assert any(r["campaign"] == campaign and r["kind"] == "rumor" for r in data["lore"])
