import pytest

from conftest import ToolFailed


async def seed(lore_tools, campaign):
    await lore_tools("add_lore", campaign=campaign, kind="Location", title="The Gilded Flagon",
                     content="A smoky tavern by the docks run by the dwarf Marta. Sailors trade rumours here.",
                     tags=["Docks", "tavern"])
    await lore_tools("add_lore", campaign=campaign, kind="npc", title="Baron Voss",
                     content="The baron rules the northern marshes from a drowned keep and taxes every barge.",
                     tags=["marshes"])
    await lore_tools("add_lore", campaign=campaign, kind="faction", title="The Reed Wardens",
                     content="Rangers sworn to protect the marshes from the baron's tax collectors.",
                     tags=["marshes"])


async def test_search_ranks_by_meaning(lore_tools, campaign):
    await seed(lore_tools, campaign)
    results = await lore_tools("search_lore", campaign=campaign, query="who rules the northern marshes")
    assert results[0]["title"] == "Baron Voss"
    assert results[0]["similarity"] > results[-1]["similarity"]


async def test_search_filters_by_kind_and_tags(lore_tools, campaign):
    await seed(lore_tools, campaign)
    factions = await lore_tools("search_lore", campaign=campaign, query="baron marshes", kind="Faction")
    assert [r["title"] for r in factions] == ["The Reed Wardens"]

    docks = await lore_tools("search_lore", campaign=campaign, query="anything", tags=["docks"])
    assert [r["title"] for r in docks] == ["The Gilded Flagon"]


async def test_lore_is_scoped_to_campaign(lore_tools, game_tools, campaign):
    await seed(lore_tools, campaign)
    await game_tools("create_campaign", name=f"{campaign} other", setting="")
    assert await lore_tools("search_lore", campaign=f"{campaign} other", query="baron") == []


async def test_add_existing_title_replaces(lore_tools, game_tools, campaign):
    await seed(lore_tools, campaign)
    result = await lore_tools("add_lore", campaign=campaign, kind="npc", title="baron voss",
                              content="The baron is dead; his keep lies empty.")
    assert result["replaced"] is True
    [entry] = await lore_tools("get_lore", campaign=campaign, title="Baron Voss")
    assert entry["content"].startswith("The baron is dead")
    assert entry["tags"] == []

    log = await game_tools("recent_events", campaign=campaign, type="lore_updated")
    assert log[-1]["summary"] == "Lore updated: npc 'baron voss'"


async def test_list_and_delete(lore_tools, campaign):
    await seed(lore_tools, campaign)
    listed = await lore_tools("list_lore", campaign=campaign)
    assert [(e["kind"], e["title"]) for e in listed] == [
        ("faction", "The Reed Wardens"), ("location", "The Gilded Flagon"), ("npc", "Baron Voss")]
    assert listed[1]["tags"] == ["docks", "tavern"]

    await lore_tools("delete_lore", campaign=campaign, kind="npc", title="BARON VOSS", reason="retcon")
    with pytest.raises(ToolFailed, match="No lore"):
        await lore_tools("get_lore", campaign=campaign, title="Baron Voss")


# --- NPCs and story memory -------------------------------------------------------------

async def test_npcs_have_details_history_and_stubs(lore_tools, game_tools, campaign):
    mira = await lore_tools("record_npc", campaign=campaign, name="Mira Vell",
                            description="The miller. Wants her lantern back; secretly owes the smugglers.",
                            location="the mill", voice="feminine", disposition="friendly",
                            note="Asked the party to find her lantern.")
    assert (mira["status"], mira["disposition"], mira["location"]) == ("alive", "friendly", "the mill")
    # A new name in a scene gets a stub; repeated appearances in one scene count once.
    stub = await lore_tools("npc_appeared", campaign=campaign, name="Oskar", location="the ferry", line="Fare's two coins.")
    assert stub["stub"] and stub["appearances"] == 1 and "Fare's two coins" in stub["content"]
    again = await lore_tools("npc_appeared", campaign=campaign, name="oskar", location="the ferry")
    assert again["appearances"] == 1
    # Writing the entry properly clears the stub flag.
    oskar = await lore_tools("record_npc", campaign=campaign, name="Oskar", description="A gruff ferryman.")
    assert "stub" not in oskar and oskar["appearances"] == 1
    await lore_tools("update_npc", campaign=campaign, name="mira vell", status="missing", disposition="wary",
                     note="Vanished after the party found the smugglers' ledger.")
    full = await lore_tools("get_npc", campaign=campaign, name="Mira Vell")
    assert [h["note"] for h in full["history"]] == [
        "Asked the party to find her lantern.", "Vanished after the party found the smugglers' ledger."]
    assert full["status"] == "missing"
    # Gone NPCs drop out of the list unless asked for; location filters.
    assert [n["title"] for n in await lore_tools("list_npcs", campaign=campaign)] == ["Oskar"]
    assert [n["title"] for n in await lore_tools("list_npcs", campaign=campaign, location="mill", include_gone=True)] == ["Mira Vell"]
    # NPCs made with add_lore get details too, and search shows them.
    await lore_tools("add_lore", campaign=campaign, kind="npc", title="Brother Hale", content="A monk of the hill shrine.")
    hit = (await lore_tools("search_lore", campaign=campaign, query="monk shrine", kind="npc", limit=1))[0]
    assert (hit["title"], hit["status"]) == ("Brother Hale", "alive")
    with pytest.raises(ToolFailed, match="record_npc first"):
        await lore_tools("update_npc", campaign=campaign, name="Nobody", note="x")


async def test_story_memory_recalls_choices_across_players(lore_tools, campaign):
    await lore_tools("record_story", campaign=campaign, player="alice", said="I promise the ferryman a silver ring",
                     narration="Oskar pockets your promise of a silver ring and poles you across.")
    for i in range(3):
        await lore_tools("record_story", campaign=campaign, player="bob", said=f"I look around {i}",
                         narration=f"Rain on the docks, gulls and nets {i}.")
    found = await lore_tools("recall_story", campaign=campaign, query="the ferryman wants his silver ring", limit=1)
    assert found[0]["player"] == "alice" and "silver ring" in found[0]["narration"]
    # The latest moments are skipped (they're still in the conversation).
    recent = await lore_tools("recall_story", campaign=campaign, query="rain docks gulls nets", skip_recent=3, limit=5)
    assert [m["player"] for m in recent] == ["alice"]
