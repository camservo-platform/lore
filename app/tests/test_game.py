import json

import pytest

from conftest import ToolFailed
from lore.events import stream_key


async def make_fighter(game_tools, campaign, **overrides):
    args = dict(campaign=campaign, name="Brakka", max_hp=20, race="Human", character_class="Warrior",
                defense=16, player="alice", gold=10)
    return await game_tools("create_character", **(args | overrides))


async def test_create_and_get_character(game_tools, campaign):
    created = await make_fighter(game_tools, campaign)
    assert created["hp"] == created["max_hp"] == 20
    assert created["player"] == "alice"
    assert created["inventory"] == []

    fetched = await game_tools("get_character", campaign=campaign, character="brakka")
    assert fetched["name"] == "Brakka"


async def test_duplicate_names_rejected_case_insensitively(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    with pytest.raises(ToolFailed, match="already exists"):
        await make_fighter(game_tools, campaign, name="BRAKKA")


async def test_unknown_campaign_and_character(game_tools, campaign):
    with pytest.raises(ToolFailed, match="No campaign"):
        await game_tools("get_character", campaign="Nope", character="x")
    with pytest.raises(ToolFailed, match="No character"):
        await game_tools("get_character", campaign=campaign, character="Nobody")


async def test_damage_uses_temp_hp_first_then_knocks_unconscious(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    await game_tools("grant_temp_hp", campaign=campaign, character="Brakka", amount=5, source="protective ward")

    c = await game_tools("apply_damage", campaign=campaign, character="Brakka", amount=8, source="goblin")
    assert (c["temp_hp"], c["hp"], c["status"]) == (0, 17, "alive")

    c = await game_tools("apply_damage", campaign=campaign, character="Brakka", amount=17, source="ogre")
    assert (c["hp"], c["status"]) == (0, "unconscious")

    c = await game_tools("heal", campaign=campaign, character="Brakka", amount=50, source="potion")
    assert (c["hp"], c["status"]) == (20, "alive")


async def test_overflow_damage_kills_outright(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    # 20 HP, max 20: 40 damage leaves 20 over, which equals max HP.
    c = await game_tools("apply_damage", campaign=campaign, character="Brakka", amount=40, source="dragon breath")
    assert c["status"] == "dead"
    with pytest.raises(ToolFailed, match="dead"):
        await game_tools("heal", campaign=campaign, character="Brakka", amount=5, source="potion")
    c = await game_tools("set_status", campaign=campaign, character="Brakka", status="alive", reason="resurrection ritual")
    assert (c["hp"], c["status"]) == (1, "alive")


async def test_inventory_stacks_and_refuses_overdraw(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    await game_tools("add_item", campaign=campaign, character="Brakka", item="Torch", quantity=2)
    c = await game_tools("add_item", campaign=campaign, character="Brakka", item="torch", quantity=3)
    assert c["inventory"] == [{"name": "Torch", "quantity": 5, "description": ""}]

    with pytest.raises(ToolFailed, match="can't remove 6"):
        await game_tools("remove_item", campaign=campaign, character="Brakka", item="Torch", quantity=6)
    c = await game_tools("remove_item", campaign=campaign, character="Brakka", item="Torch", quantity=5)
    assert c["inventory"] == []


async def test_gold_cannot_go_negative(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    c = await game_tools("adjust_gold", campaign=campaign, character="Brakka", amount=-4, reason="ale")
    assert c["gold"] == 6
    with pytest.raises(ToolFailed, match="only 6 gold"):
        await game_tools("adjust_gold", campaign=campaign, character="Brakka", amount=-7, reason="sword")


async def test_conditions(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    await game_tools("add_condition", campaign=campaign, character="Brakka", condition="Poisoned", source="dart")
    c = await game_tools("add_condition", campaign=campaign, character="Brakka", condition="poisoned", source="x")
    assert c["conditions"] == ["poisoned"]
    c = await game_tools("remove_condition", campaign=campaign, character="Brakka", condition="poisoned")
    assert c["conditions"] == []


async def test_update_character_caps_hp(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    c = await game_tools("update_character", campaign=campaign, character="Brakka", reason="curse",
                         max_hp=12, attributes={"strength": 18})
    assert (c["max_hp"], c["hp"], c["attributes"]) == (12, 12, {"strength": 18})


async def test_failed_change_writes_no_event(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    before = await game_tools("recent_events", campaign=campaign)
    with pytest.raises(ToolFailed):
        await game_tools("remove_item", campaign=campaign, character="Brakka", item="Rope")
    assert await game_tools("recent_events", campaign=campaign) == before


async def test_events_are_logged_attached_to_session_and_streamed(game_tools, campaign, app_state, redis):
    await make_fighter(game_tools, campaign)
    session = await game_tools("start_session", campaign=campaign)
    with pytest.raises(ToolFailed, match="already open"):
        await game_tools("start_session", campaign=campaign)

    await game_tools("apply_damage", campaign=campaign, character="Brakka", amount=3, source="rat")
    await game_tools("log_event", campaign=campaign, type="NPC_met", summary="Met the innkeeper", character="Brakka")
    await game_tools("end_session", campaign=campaign, summary="Rats in the cellar.")

    log = await game_tools("recent_events", campaign=campaign)
    assert [e["type"] for e in log] == [
        "campaign_created", "character_created", "session_started", "damage", "npc_met", "session_ended"]
    assert all(e["actor"] == "gm" for e in log)
    assert [e["session_id"] for e in log[2:]] == [session["session_id"]] * 4
    assert log[3]["data"] == {"amount": 3, "source": "rat", "absorbed_by_temp_hp": 0,
                              "hp_before": 20, "hp_after": 17, "status": "alive"}

    only_damage = await game_tools("recent_events", campaign=campaign, type="damage")
    assert [e["id"] for e in only_damage] == [log[3]["id"]]

    streamed = await redis.xrange(stream_key(log[0]["campaign_id"]))
    assert [json.loads(fields["event"])["id"] for _, fields in streamed] == [e["id"] for e in log]


async def test_roll_dice_totals_and_logs(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    r = await game_tools("roll_dice", notation="2d6+1d4-1", reason="sword", campaign=campaign, character="Brakka")
    assert [g["dice"] for g in r["rolls"]] == ["2d6", "1d4"]
    assert all(1 <= v <= 6 for v in r["rolls"][0]["results"]) and 1 <= r["rolls"][1]["results"][0] <= 4
    assert r["modifier"] == -1
    assert r["total"] == sum(r["rolls"][0]["results"]) + r["rolls"][1]["results"][0] - 1
    [event] = await game_tools("recent_events", campaign=campaign, type="roll")
    assert event["summary"] == f"Brakka rolled 2d6+1d4-1 for sword: {r['total']}"


@pytest.mark.parametrize("notation", ["", "2x6", "d20 + banana", "1d1", "101d6", "5"])
async def test_roll_dice_rejects_bad_notation(game_tools, notation):
    with pytest.raises(ToolFailed):
        await game_tools("roll_dice", notation=notation, reason="test")


async def test_roll_dice_without_campaign_logs_nothing(game_tools):
    r = await game_tools("roll_dice", notation="D20", reason="test")
    assert 1 <= r["total"] <= 20
