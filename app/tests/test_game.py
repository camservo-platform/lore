import json

import pytest

from conftest import ToolFailed
from lore.events import stream_key


async def make_fighter(game_tools, campaign, **overrides):
    args = dict(campaign=campaign, name="Brakka", max_hp=20, race="Human", character_class="Vanguard",
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
    assert c["inventory"] == [{"name": "Torch", "quantity": 5, "description": "", "origin": ""}]

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
    assert (c["max_hp"], c["hp"], c["attributes"]["strength"]) == (12, 12, 18)


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


async def test_end_session_saves_recap_as_lore(game_tools, lore_tools, campaign):
    await game_tools("start_session", campaign=campaign)
    result = await game_tools("end_session", campaign=campaign, summary="Wren found the drowned bell under the ice.")
    assert result["saved_as_lore"] == "Session 1 recap"
    [entry] = await lore_tools("get_lore", campaign=campaign, title="Session 1 recap")
    assert entry["kind"] == "history" and entry["tags"] == ["session-recap"]
    hits = await lore_tools("search_lore", campaign=campaign, query="drowned bell ice", kind="history")
    assert hits[0]["title"] == "Session 1 recap"
    await game_tools("start_session", campaign=campaign)
    assert (await game_tools("end_session", campaign=campaign, summary="Next."))["saved_as_lore"] == "Session 2 recap"


# --- races, classes and abilities ------------------------------------------------------

async def make_hero(game_tools, campaign, **overrides):
    args = dict(campaign=campaign, name="Sela", race="dwarf", character_class="arcanist", player="alice")
    return await game_tools("create_character", **(args | overrides))


def ability(sheet, name):
    return next(a for a in sheet["abilities"] if a["name"] == name)


async def test_player_characters_pick_from_the_catalog(game_tools, campaign):
    with pytest.raises(ToolFailed, match="Races: Human"):
        await game_tools("create_character", campaign=campaign, name="Sela", max_hp=10, race="Space Pirate",
                         character_class="Arcanist", player="alice")
    sela = await make_hero(game_tools, campaign)
    # Names are canonicalised; HP, defense, attributes and pool come from race and class.
    assert (sela["race"], sela["class"], sela["max_hp"], sela["defense"]) == ("Dwarf", "Arcanist", 9, 11)
    assert sela["attributes"]["strength"] == 9                       # 8 + the dwarf's +1
    assert (sela["pool_name"], sela["pool"], sela["pool_max"]) == ("Aether", 4, 4)
    names = [a["name"] for a in sela["abilities"]]
    assert "Stoneblood" in names and "Spark Lance" in names
    assert "Binding Glyph" not in names                              # unlocks at level 2
    # NPCs keep free-form race and class.
    npc = await game_tools("create_character", campaign=campaign, name="Ogre", max_hp=30, race="Ogre")
    assert npc["abilities"] == []


async def test_uses_and_pools_run_out_and_recover(game_tools, campaign):
    await make_hero(game_tools, campaign)
    for left in (3, 2, 1, 0):  # Spark Lance costs 1 Aether
        sela = await game_tools("use_ability", campaign=campaign, character="Sela", ability="spark lance",
                                target="a goblin")
        assert sela["pool"] == left
    with pytest.raises(ToolFailed, match="costs 1 Aether and Sela has 0"):
        await game_tools("use_ability", campaign=campaign, character="Sela", ability="Spark Lance")
    sela = await game_tools("use_ability", campaign=campaign, character="Sela", ability="Read the Weave")  # free
    assert sela["pool"] == 0

    sela = await game_tools("use_ability", campaign=campaign, character="Sela", ability="Stoneblood")
    assert ability(sela, "Stoneblood")["uses_left"] == 0
    with pytest.raises(ToolFailed, match="no uses of Stoneblood left; they come back on recovery"):
        await game_tools("use_ability", campaign=campaign, character="Sela", ability="Stoneblood")
    with pytest.raises(ToolFailed, match="They have: Stoneblood"):
        await game_tools("use_ability", campaign=campaign, character="Sela", ability="Fly")

    [sela] = await game_tools("recover", campaign=campaign, reason="a night at the inn")
    assert sela["pool"] == 4 and ability(sela, "Stoneblood")["uses_left"] == 1
    events = await game_tools("recent_events", campaign=campaign, type="ability_used")
    assert events[0]["summary"] == "Sela used Spark Lance on a goblin (3/4 Aether left)"


async def test_session_classes_come_back_each_session_not_on_recovery(game_tools, campaign):
    await make_hero(game_tools, campaign, name="Ivo", race="Human", character_class="Shade")
    await game_tools("use_ability", campaign=campaign, character="Ivo", ability="Vanish")
    [ivo] = await game_tools("recover", campaign=campaign, reason="camp", characters=["Ivo"])
    assert ability(ivo, "Vanish")["uses_left"] == 1                  # a Shade's tricks wait for the session
    await game_tools("start_session", campaign=campaign)
    ivo = await game_tools("get_character", campaign=campaign, character="Ivo")
    assert ability(ivo, "Vanish")["uses_left"] == 2


async def test_level_up_unlocks_abilities_and_grows_the_pool(game_tools, campaign):
    await make_hero(game_tools, campaign)
    await game_tools("use_ability", campaign=campaign, character="Sela", ability="Veil of Mist")  # 4 -> 2
    sela = await game_tools("update_character", campaign=campaign, character="Sela", reason="level up", level=2)
    assert (sela["pool"], sela["pool_max"]) == (4, 6)                # spent points stay spent
    assert "Binding Glyph" in [a["name"] for a in sela["abilities"]]
    events = await game_tools("recent_events", campaign=campaign, type="character_updated")
    assert events[-1]["summary"] == "Sela: level up (new: Binding Glyph)"


async def test_world_themes_rename_core_options_but_keep_the_numbers(game_tools, campaign):
    await game_tools("theme_core_options", campaign=campaign, themes=[{
        "key": "arcanist", "name": "Techno-Witch", "summary": "Hacks reality with salvaged code.",
        "pool_name": "Charge",
        "abilities": [{"key": "spark-lance", "name": "Arc Bolt",
                       "effect": "One target in sight takes 1d10 energy damage on a hit."},
                      {"key": "veil-of-mist", "name": "Smoke Screen", "effect": "Smoke fills 99 paces."}],
    }])
    options = await game_tools("list_character_options", campaign=campaign)
    witch = next(o for o in options["classes"] if o["key"] == "arcanist")
    assert (witch["name"], witch["resource"]["name"]) == ("Techno-Witch", "Charge")
    smoke = next(a for a in witch["abilities"] if a["key"] == "veil-of-mist")
    assert smoke["effect"].startswith("Fog fills about ten paces")   # changed a number: core wording kept

    hero = await make_hero(game_tools, campaign, character_class="techno-witch")
    assert hero["class"] == "Techno-Witch" and hero["pool_name"] == "Charge"
    assert ability(hero, "Arc Bolt")["effect"] == "One target in sight takes 1d10 energy damage on a hit."
    with pytest.raises(ToolFailed, match="clash"):
        await game_tools("theme_core_options", campaign=campaign, themes=[{"key": "warden", "name": "Techno-Witch"}])


async def test_worlds_can_add_their_own_options(game_tools, campaign):
    option = {"kind": "race", "name": "Tidekin", "summary": "Sea folk.", "description": "Born of the tide.",
              "hp": 9, "attributes": {"agility": 1},
              "abilities": [{"name": "Gills", "summary": "Breathe water.", "description": "You breathe water.",
                             "effect": "Breathe underwater.", "level": 1, "uses": 0}]}
    added = await game_tools("add_world_option", campaign=campaign, option=option)
    assert added["hp"] == 3 and added["abilities"][0]["uses"] is None  # clamped; 0 uses means unlimited
    options = await game_tools("list_character_options", campaign=campaign)
    assert [o["name"] for o in options["races"]][-1] == "Tidekin" and options["races"][-1]["world"]
    with pytest.raises(ToolFailed, match="already"):
        await game_tools("add_world_option", campaign=campaign, option=option)


async def test_existing_characters_choose_once_and_story_grants_abilities(game_tools, campaign):
    await game_tools("create_character", campaign=campaign, name="Old", max_hp=10, race="goblin")  # made before races gave abilities
    old = await game_tools("choose_race_and_class", campaign=campaign, character="Old", race="Elf",
                           character_class="Mender")
    assert (old["race"], old["class"], old["max_hp"]) == ("Elf", "Mender", 10)  # HP kept
    assert "Mend Wounds" in [a["name"] for a in old["abilities"]]
    with pytest.raises(ToolFailed, match="already has a race and class"):
        await game_tools("choose_race_and_class", campaign=campaign, character="Old", race="Elf",
                         character_class="Shade")
    old = await game_tools("grant_ability", campaign=campaign, character="Old", name="Lantern of Ages",
                           summary="An old lantern's light.", effect="Reveals invisible things.",
                           source="relic", uses=1)
    assert ability(old, "Lantern of Ages")["uses_left"] == 1


# --- quests and item origins ----------------------------------------------------------

async def test_quest_log_with_notes_and_status(game_tools, campaign):
    q = await game_tools("add_quest", campaign=campaign, title="The Lost Lantern", summary="Find Mira's lantern.",
                         giver="Mira Vell", reward="20 gold", note="First step: ask at the mill.")
    assert (q["status"], q["giver"], [n["note"] for n in q["notes"]]) == ("active", "Mira Vell", ["First step: ask at the mill."])
    with pytest.raises(ToolFailed, match="already a quest"):
        await game_tools("add_quest", campaign=campaign, title="the lost lantern", summary="x")
    await game_tools("add_quest", campaign=campaign, title="Rats", summary="Clear the cellar.")
    q = await game_tools("update_quest", campaign=campaign, title="the lost lantern", note="The miller saw a heron take it.")
    # GM tool calls run under a player's name, so notes are the GM's unless marked as the player's
    # own (in-process calls have no player, so both read "gm" here; the web table sends the name).
    q = await game_tools("update_quest", campaign=campaign, title="the lost lantern", note="Check the reeds?",
                         player_note=True)
    assert [n["by"] for n in q["notes"]] == ["gm", "gm", "gm"]
    q = await game_tools("update_quest", campaign=campaign, title="The Lost Lantern", status="completed",
                         note="Returned the lantern.")
    assert q["status"] == "completed" and q["finished_at"]
    log = await game_tools("list_quests", campaign=campaign)
    assert [x["title"] for x in log] == ["Rats", "The Lost Lantern"]  # active first
    with pytest.raises(ToolFailed, match="Quests: The Lost Lantern, Rats"):
        await game_tools("update_quest", campaign=campaign, title="Dragons", note="x")
    events = await game_tools("recent_events", campaign=campaign, type="quest_updated")
    assert events[-1]["summary"] == "Quest completed: The Lost Lantern"


async def test_items_keep_description_and_origin(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    c = await game_tools("add_item", campaign=campaign, character="Brakka", item="Heron Feather",
                         description="A silver-veined feather.", origin="Taken from the heron's nest at the mill.")
    c = await game_tools("add_item", campaign=campaign, character="Brakka", item="heron feather")  # stacks, keeps text
    item = c["inventory"][0]
    assert (item["quantity"], item["description"], item["origin"]) == (
        2, "A silver-veined feather.", "Taken from the heron's nest at the mill.")
    c = await game_tools("describe_item", campaign=campaign, character="Brakka", item="Heron Feather",
                         description="It hums near water.")
    assert c["inventory"][0]["description"] == "It hums near water."
    with pytest.raises(ToolFailed, match="has no Rope"):
        await game_tools("describe_item", campaign=campaign, character="Brakka", item="Rope", origin="x")


# --- snapshots and rollback ---------------------------------------------------------------

async def test_rollback_restores_the_world_before_a_turn(game_tools, lore_tools, campaign):
    await make_hero(game_tools, campaign)
    await game_tools("add_quest", campaign=campaign, title="Lantern", summary="Find it.")
    await lore_tools("record_npc", campaign=campaign, name="Mira", description="The miller.", note="Met at the mill.")
    await game_tools("start_session", campaign=campaign)
    await lore_tools("record_story", campaign=campaign, narration="The party arrives at the mill.")
    await game_tools("save_snapshot", campaign=campaign, turn_id="turn-1", extra={"messages": 4})
    before = await game_tools("get_character", campaign=campaign, character="Sela")

    # The turn that went wrong: damage, loot, a spent ability, a quest change, an NPC change,
    # a new character, new lore, a story moment, and the session ended.
    hit = await game_tools("apply_damage", campaign=campaign, character="Sela", amount=5, source="misheard attack")
    await game_tools("add_item", campaign=campaign, character="Sela", item="Cursed Ring", origin="misheard")
    await game_tools("use_ability", campaign=campaign, character="Sela", ability="Spark Lance")
    await game_tools("update_quest", campaign=campaign, title="Lantern", status="failed", note="Gave up.")
    await lore_tools("update_npc", campaign=campaign, name="Mira", status="dead", note="Killed by mistake.")
    await lore_tools("record_npc", campaign=campaign, name="Mira", description="A ghost now.")
    await game_tools("create_character", campaign=campaign, name="Ghost", max_hp=5)
    await lore_tools("add_lore", campaign=campaign, kind="rumor", title="Wrong rumor", content="Never happened.")
    await lore_tools("record_story", campaign=campaign, narration="Sela was struck down by a misheard attack.")
    await game_tools("end_session", campaign=campaign, summary="A session that never happened.")
    await game_tools("save_snapshot", campaign=campaign, turn_id="turn-2", extra={"messages": 9})

    damage_event = (await game_tools("recent_events", campaign=campaign, type="damage"))[-1]
    result = await game_tools("rollback", campaign=campaign, event_id=damage_event["id"])
    assert result["turns"] == ["turn-1", "turn-2"] and result["extra"] == {"messages": 4}

    after = await game_tools("get_character", campaign=campaign, character="Sela")
    assert after == before and hit["hp"] != before["hp"]
    assert [c["name"] for c in await game_tools("list_characters", campaign=campaign)] == ["Sela"]
    [quest] = await game_tools("list_quests", campaign=campaign)
    assert quest["status"] == "active" and len(quest["notes"]) == 0
    mira = await lore_tools("get_npc", campaign=campaign, name="Mira")
    assert (mira["status"], mira["content"], [h["note"] for h in mira["history"]]) == (
        "alive", "The miller.", ["Met at the mill."])
    assert await lore_tools("search_lore", campaign=campaign, query="miller", kind="npc")  # re-embedded, searchable
    assert not [e for e in await lore_tools("list_lore", campaign=campaign) if e["title"] == "Wrong rumor"]
    moments = await lore_tools("recall_story", campaign=campaign, query="mill party struck", limit=5)
    assert [m["narration"] for m in moments] == ["The party arrives at the mill."]
    # The session is open again, and the log no longer shows what was undone.
    log = await game_tools("recent_events", campaign=campaign, limit=50)
    types = [e["type"] for e in log]
    assert "damage" not in types and "session_ended" not in types and types[-1] == "rolled_back"
    assert "session_started" in types
    assert log[-1]["summary"] == "gm rolled the world back to before: " + damage_event["summary"]

    # The undone turn's snapshot is gone; the one rolled back to stays for another rollback.
    with pytest.raises(ToolFailed, match="already undone"):
        await game_tools("rollback", campaign=campaign, event_id=damage_event["id"])
    window = await game_tools("rollback_window", campaign=campaign)
    assert window["after_event_id"] < damage_event["id"]


async def test_too_old_events_cannot_be_rolled_back(game_tools, campaign):
    await make_fighter(game_tools, campaign)
    first = (await game_tools("recent_events", campaign=campaign))[-1]
    assert (await game_tools("rollback_window", campaign=campaign))["after_event_id"] is None
    await game_tools("save_snapshot", campaign=campaign, turn_id="t")
    with pytest.raises(ToolFailed, match="too far back"):
        await game_tools("rollback", campaign=campaign, event_id=first["id"])
