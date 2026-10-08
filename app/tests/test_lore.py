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
