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


# --- world and character management ---------------------------------------------------

import pytest

from lore.admin import AdminError, delete_campaign, delete_character, rename_campaign, update_character, worlds


async def _ids(app_state, campaign):
    cid = await app_state.pool.fetchval("SELECT id FROM campaigns WHERE name = $1", campaign)
    char = await app_state.pool.fetchval("SELECT id FROM characters WHERE campaign_id = $1", cid)
    return cid, char


async def test_rename_world_logs_and_rejects_duplicates(app_state, game_tools, campaign):
    cid, _ = await _ids(app_state, campaign)
    event = await rename_campaign(app_state.pool, cid, f"{campaign} Reborn", "cameron")
    assert event["type"] == "campaign_renamed" and event["data"] == {"old": campaign, "new": f"{campaign} Reborn"}
    assert event["actor"] == "cameron"
    await game_tools("create_campaign", name=f"{campaign} Other", setting="")
    with pytest.raises(AdminError, match="already exists"):
        await rename_campaign(app_state.pool, cid, f"{campaign} other", "cameron")
    with pytest.raises(AdminError, match="empty"):
        await rename_campaign(app_state.pool, cid, "  ", "cameron")


async def test_edit_character_logs_exact_changes(app_state, game_tools, campaign):
    await game_tools("create_character", campaign=campaign, name="Wren", race="Human", character_class="Warden", max_hp=12, player="alice", gold=5)
    _, char = await _ids(app_state, campaign)
    event = await update_character(app_state.pool, char, {"name": "Wren Ashby", "hp": 7, "gold": 5, "player": "bob"}, "dana")
    assert event["type"] == "admin_edit" and event["actor"] == "dana"
    assert event["summary"] == "An admin corrected Wren Ashby: name Wren → Wren Ashby, hp 12 → 7, player alice → bob"
    sheet = await game_tools("get_character", campaign=campaign, character="wren ashby")
    assert (sheet["hp"], sheet["player"], sheet["gold"]) == (7, "bob", 5)
    await update_character(app_state.pool, char, {"player": ""}, "dana")
    assert (await game_tools("get_character", campaign=campaign, character="Wren Ashby"))["player"] is None


async def test_edit_character_refuses_bad_values(app_state, game_tools, campaign):
    await game_tools("create_character", campaign=campaign, name="Wren", max_hp=12)
    await game_tools("create_character", campaign=campaign, name="Quell", max_hp=12)
    char = await app_state.pool.fetchval(
        "SELECT ch.id FROM characters ch JOIN campaigns c ON c.id = ch.campaign_id WHERE c.name = $1 AND ch.name = 'Wren'",
        campaign)
    with pytest.raises(AdminError, match="HP must be between 0 and max HP"):
        await update_character(app_state.pool, char, {"hp": 99}, "dana")
    with pytest.raises(AdminError, match="Gold can't be negative"):
        await update_character(app_state.pool, char, {"gold": -1}, "dana")
    with pytest.raises(AdminError, match="already named"):
        await update_character(app_state.pool, char, {"name": "QUELL"}, "dana")   # names ignore case
    with pytest.raises(AdminError, match="Nothing changed"):
        await update_character(app_state.pool, char, {}, "dana")
    with pytest.raises(AdminError, match="Can't edit"):
        await update_character(app_state.pool, char, {"campaign_id": 1}, "dana")


async def test_delete_world_requires_exact_name(app_state, game_tools, lore_tools, campaign):
    await game_tools("create_character", campaign=campaign, name="Wren", max_hp=12)
    await lore_tools("add_lore", campaign=campaign, kind="npc", title="Marta", content="Innkeeper.")
    cid, _ = await _ids(app_state, campaign)
    with pytest.raises(AdminError, match="doesn't match"):
        await delete_campaign(app_state.pool, cid, campaign.lower())
    assert await delete_campaign(app_state.pool, cid, campaign) == campaign
    assert await app_state.pool.fetchval("SELECT count(*) FROM lore_entries WHERE campaign_id = $1", cid) == 0
    assert not [w for w in await worlds(app_state.pool) if w["id"] == cid]


async def test_delete_character_keeps_the_chronicle(app_state, game_tools, campaign):
    await game_tools("create_character", campaign=campaign, name="Wren", race="Human", character_class="Warden", max_hp=12, player="alice")
    await game_tools("add_item", campaign=campaign, character="Wren", item="Rope")
    cid, char = await _ids(app_state, campaign)
    history = await app_state.pool.fetchval("SELECT count(*) FROM events WHERE character_id = $1", char)
    assert history >= 1
    with pytest.raises(AdminError, match="doesn't match"):
        await delete_character(app_state.pool, char, "wren", "dana")
    event = await delete_character(app_state.pool, char, "Wren", "dana")
    assert event["type"] == "character_deleted" and event["actor"] == "dana"
    assert event["data"] == {"id": char, "name": "Wren", "player": "alice"}
    assert await app_state.pool.fetchval("SELECT count(*) FROM characters WHERE id = $1", char) == 0
    assert await app_state.pool.fetchval("SELECT count(*) FROM inventory_items WHERE character_id = $1", char) == 0
    # Past events stay, just no longer linked to the character.
    assert await app_state.pool.fetchval(
        "SELECT count(*) FROM events WHERE campaign_id = $1 AND type <> 'character_deleted'", cid) >= history + 1
    [world] = [w for w in await worlds(app_state.pool) if w["id"] == cid]
    assert world["characters"] == []
    with pytest.raises(AdminError, match="no longer exists"):
        await delete_character(app_state.pool, char, "Wren", "dana")


async def test_worlds_lists_characters_and_counts(app_state, game_tools, campaign):
    await game_tools("create_character", campaign=campaign, name="Wren", race="Human", character_class="Warden", max_hp=12, player="alice")
    [world] = [w for w in await worlds(app_state.pool) if w["name"] == campaign]
    assert world["events"] >= 2 and world["lore"] == 0
    assert [(c["name"], c["player"], c["hp"]) for c in world["characters"]] == [("Wren", "alice", 12)]


# --- lore editing ---------------------------------------------------------------------

from lore.admin import delete_lore, list_lore, merge_lore, update_lore


async def test_edit_merge_and_delete_lore(app_state, lore_tools, campaign):
    await lore_tools("add_lore", campaign=campaign, kind="npc", title="Marta", content="A dwarf innkeeper.", tags=["inn"])
    await lore_tools("add_lore", campaign=campaign, kind="rumor", title="Marta's secret", content="She hides a key.",
                     tags=["secret"])
    cid = await app_state.pool.fetchval("SELECT id FROM campaigns WHERE name = $1", campaign)
    entries = {e["title"]: e for e in await list_lore(app_state.pool, cid)}

    event = await update_lore(app_state.pool, app_state.embedder, entries["Marta"]["id"],
                              {"content": "A dwarf innkeeper who runs the Gilded Flagon.", "tags": "Inn, docks"}, "dana")
    assert event["type"] == "lore_edited"
    [marta] = await lore_tools("get_lore", campaign=campaign, title="Marta")
    assert marta["content"].endswith("Gilded Flagon.") and marta["tags"] == ["docks", "inn"]
    # Re-embedded: a search for the new words finds it.
    assert (await lore_tools("search_lore", campaign=campaign, query="gilded flagon"))[0]["title"] == "Marta"

    with pytest.raises(AdminError, match="Kind must be"):
        await update_lore(app_state.pool, app_state.embedder, entries["Marta"]["id"], {"kind": "spell"}, "dana")

    event = await merge_lore(app_state.pool, app_state.embedder, entries["Marta's secret"]["id"],
                             entries["Marta"]["id"], "dana")
    assert event["summary"] == "An admin merged lore 'Marta's secret' into 'Marta'"
    [marta] = await lore_tools("get_lore", campaign=campaign, title="Marta")
    assert marta["content"].endswith("She hides a key.") and marta["tags"] == ["docks", "inn", "secret"]
    assert [e["title"] for e in await list_lore(app_state.pool, cid)] == ["Marta"]

    await delete_lore(app_state.pool, entries["Marta"]["id"], "dana")
    assert await list_lore(app_state.pool, cid) == []
    with pytest.raises(AdminError, match="no longer exists"):
        await delete_lore(app_state.pool, entries["Marta"]["id"], "dana")
