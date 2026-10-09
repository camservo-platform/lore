import pytest

from lore.web.app import LINES_KEPT, Table


async def test_lines_are_per_player_per_campaign_and_capped(redis):
    alice, other_campaign = Table(redis, 9001), Table(redis, 9002)
    await alice.add_lines("alice", {"role": "player", "text": "I open the door."}, {"role": "gm", "text": "It creaks."})
    await alice.add_lines("bob", {"role": "gm", "text": "Bob's scene."})
    await other_campaign.add_lines("alice", {"role": "gm", "text": "Elsewhere."})

    assert [line["text"] for line in await alice.recent_lines("alice", 10)] == ["I open the door.", "It creaks."]
    assert [line["text"] for line in await alice.recent_lines("bob", 10)] == ["Bob's scene."]
    assert [line["text"] for line in await alice.recent_lines("alice", 1)] == ["It creaks."]

    await alice.add_lines("alice", *({"role": "gm", "text": str(i)} for i in range(LINES_KEPT + 5)))
    lines = await alice.recent_lines("alice", LINES_KEPT * 2)
    assert len(lines) == LINES_KEPT and lines[-1]["text"] == str(LINES_KEPT + 4)
    assert 0 < await redis.ttl("lore:campaign:9001:user:alice:lines")


def test_table_state_summarises_party_session_and_events():
    from lore.web.app import format_table_state
    characters = [
        {"name": "Wren", "player": "alice", "hp": 4, "max_hp": 12, "status": "alive", "location": "the docks"},
        {"name": "Quell", "player": None, "hp": 0, "max_hp": 20, "status": "unconscious", "location": ""},
    ]
    sheets = [{"name": "Wren", "temp_hp": 3, "conditions": ["poisoned"]}, {"name": "Quell", "temp_hp": 0, "conditions": []}]
    events = [{"summary": "Session started", "type": "session_started", "session_id": 7},
              {"summary": "Wren took 8 damage", "type": "damage", "session_id": 7}]
    state = format_table_state(characters, sheets, events)
    assert "Session: open" in state
    assert "- Wren (player: alice): 4/12 HP, 3 temporary HP, poisoned; at the docks" in state
    assert "- Quell (NPC): 0/20 HP, unconscious" in state
    assert "- Wren took 8 damage\n[Turn order:" in state
    ended = events + [{"summary": "Session ended", "type": "session_ended", "session_id": 7}]
    assert "Session: not open" in format_table_state([], [], ended)
    assert "Characters: none yet" in format_table_state([], [], [])


def test_describe_error_unwraps_groups_and_api_messages():
    import anthropic, httpx2
    from lore.web.app import describe_error
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    api = anthropic.BadRequestError("raw", response=response,
                                    body={"error": {"message": "Your credit balance is too low."}})
    assert describe_error(ExceptionGroup("tg", [ExceptionGroup("inner", [api])])) == (
        "The Game Master couldn't reach the model: Your credit balance is too low.")
    assert describe_error(ValueError("boom")) == "Something went wrong: boom"


def test_english_voices_from_deepgram_catalogue():
    from lore.voice import english_voices
    catalogue = {"tts": [
        {"canonical_name": "aura-2-zeus-en", "architecture": "aura-2", "languages": ["en", "en-US"],
         "metadata": {"display_name": "Zeus", "accent": "American", "tags": ["masculine", "deep", "trustworthy", "smooth", "calm"]}},
        {"canonical_name": "aura-2-agathe-fr", "architecture": "aura-2", "languages": ["fr"], "metadata": {}},
        {"canonical_name": "aura-asteria-en", "architecture": "aura", "languages": ["en"], "metadata": {}},
        {"canonical_name": "aura-2-cora-en", "architecture": "aura-2", "languages": ["en"],
         "metadata": {"display_name": "Cora", "tags": ["feminine", "smooth"]}},
    ]}
    assert english_voices(catalogue) == [
        {"id": "aura-2-cora-en", "name": "Cora", "accent": "", "gender": "feminine", "traits": ["smooth"]},
        {"id": "aura-2-zeus-en", "name": "Zeus", "accent": "American", "gender": "masculine",
         "traits": ["deep", "trustworthy", "smooth"]},
    ]


async def test_voice_resolve_accepts_only_listed_voices():
    import time
    from lore.voice import Voice
    voice = Voice("key", "aura-2-thalia-en")
    voice._voices, voice._voices_at = [{"id": "aura-2-zeus-en"}], time.monotonic()
    assert await voice.resolve("aura-2-zeus-en") == "aura-2-zeus-en"
    assert await voice.resolve("not-a-voice&model=other") == "aura-2-thalia-en"
    assert await voice.resolve(None) == "aura-2-thalia-en"
    await voice.aclose()


async def test_usage_counts_and_estimates_cost(redis):
    from types import SimpleNamespace
    from lore.usage import Usage
    await redis.delete(*[k async for k in redis.scan_iter("lore:usage:*")] or ["x"])
    usage = Usage(redis)
    await usage.llm("claude-opus-5-5", SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000,
                                                       cache_read_input_tokens=2_000_000, cache_creation_input_tokens=0))
    await usage.llm("mystery-model", SimpleNamespace(input_tokens=10, output_tokens=5,
                                                     cache_read_input_tokens=None, cache_creation_input_tokens=None))
    await usage.turn("alice"); await usage.turn("alice"); await usage.tts(1200); await usage.voice(61.4)
    summary = await usage.summary(3)
    today = summary["days"][0]
    assert today["turns"] == {"alice": 2} and today["tts_characters"] == 1200 and today["voice_seconds"] == 61
    assert today["models"]["claude-opus-5-5"]["cache_read"] == 2_000_000
    assert today["cost"] == pytest.approx(4.0 + 2.0 + 0.4)   # input + output + cache reads
    assert summary["unpriced_models"] == ["mystery-model"] and summary["totals"]["turns"] == 2


async def test_presence_tracks_where_and_voice(redis):
    import time
    from lore.usage import Presence
    presence = Presence(redis)
    await presence.seen("alice", "table", 7)
    await presence.voice("alice", True)
    await presence.seen("bob", "lobby")
    await redis.zadd("lore:presence", {"bob": time.time() - 600})  # bob left ten minutes ago
    people = {p["user"]: p for p in await presence.everyone()}
    assert people["alice"]["online"] and people["alice"]["voice"] and people["alice"]["campaign_id"] == 7
    assert not people["bob"]["online"] and people["bob"]["where"] == "lobby"


def test_npc_voice_pick_matches_gender_avoids_narrator_and_is_stable():
    from lore.voice import Voice
    voice = Voice("key", "aura-2-pandora-en")
    voices = [{"id": "aura-2-pandora-en", "gender": "feminine"}, {"id": "aura-2-cora-en", "gender": "feminine"},
              {"id": "aura-2-luna-en", "gender": "feminine"}, {"id": "aura-2-zeus-en", "gender": "masculine"},
              {"id": "aura-2-orion-en", "gender": "masculine"}]
    avoid = {"aura-2-pandora-en"}
    quell = voice.pick_npc_voice(voices, "harrow quell", "masculine", set(), avoid)
    assert quell in {"aura-2-zeus-en", "aura-2-orion-en"}
    assert voice.pick_npc_voice(voices, "harrow quell", "masculine", set(), avoid) == quell     # stable
    other = voice.pick_npc_voice(voices, "brother odran", "masculine", {quell}, avoid)
    assert other != quell and other in {"aura-2-zeus-en", "aura-2-orion-en"}                   # distinct
    marta = voice.pick_npc_voice(voices, "marta", "feminine", set(), avoid)
    assert marta in {"aura-2-cora-en", "aura-2-luna-en"}                                      # never the narrator
    # Everyone taken: reuse rather than fail.
    assert voice.pick_npc_voice(voices, "x", "masculine", {"aura-2-zeus-en", "aura-2-orion-en"}, avoid)


def test_classify_model_errors():
    import anthropic, httpx2
    from lore.metrics import classify
    def status(code, message):
        response = httpx2.Response(code, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
        return anthropic.APIStatusError(message, response=response, body=None)
    credit = status(400, "Your credit balance is too low to access the Anthropic API.")
    assert classify(ExceptionGroup("tg", [credit])) == "billing"
    assert classify(status(401, "invalid x-api-key")) == "auth"
    assert classify(status(429, "rate limited")) == "rate_limit"
    assert classify(status(529, "overloaded")) == "overloaded"
    assert classify(status(500, "boom")) == "server"
    assert classify(anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))) == "connection"
    assert classify(ValueError("x")) == "other"


async def test_health_summarises_recent_errors(redis):
    import time
    from lore.usage import HEALTH_KEY, Health
    await redis.delete(HEALTH_KEY)
    health = Health(redis)
    assert (await health.summary())["last"] is None
    await health.error("billing", "credit balance too low")
    await health.error("billing", "credit balance too low")
    await redis.zadd(HEALTH_KEY, {'{"kind": "server", "message": "old", "at": %d}' % (time.time() - 7200): time.time() - 7200})
    summary = await health.summary()
    assert summary["last_hour"] == {"billing": 2}
    assert summary["last_day"] == {"billing": 2, "server": 1}
    assert summary["last"]["kind"] == "billing"


def test_table_state_shows_identity_pool_and_uses_left():
    from lore.web.app import format_table_state
    characters = [{"name": "Sela", "player": "alice", "hp": 9, "max_hp": 9, "status": "alive", "location": ""}]
    sheets = [{"name": "Sela", "temp_hp": 0, "conditions": [], "race": "Dwarf", "class": "Arcanist",
               "pool_name": "Aether", "pool": 3, "pool_max": 4,
               "abilities": [{"name": "Spark Lance", "max_uses": None, "uses_left": None},
                             {"name": "Stoneblood", "max_uses": 1, "uses_left": 0}]}]
    state = format_table_state(characters, sheets, [])
    assert "- Sela (player: alice, Dwarf Arcanist): 9/9 HP, Aether 3/4, uses left: Stoneblood 0/1" in state


def test_speakers_and_story_text_from_a_reply():
    from lore.web.app import plain_story, speakers
    reply = ('The ferry rocks. <say who="Oskar" voice="masculine">Two coins.</say> '
             '<say who="the guard">Move along.</say> <say who="Oskar">Or a ring.</say>')
    assert speakers(reply) == [{"name": "Oskar", "voice": "masculine", "line": "Two coins."}]
    assert plain_story(reply) == 'The ferry rocks. Oskar: "Two coins." the guard: "Move along." Oskar: "Or a ring."'


def test_world_notes_list_quests_npcs_and_memories():
    from lore.web.app import format_world_notes
    quests = [{"title": "The Lost Lantern", "giver": "Mira Vell", "summary": "Find it.",
               "notes": [{"note": "A heron took it."}]}]
    npcs = [{"title": "Oskar", "disposition": "wary", "location": "the ferry", "appearances": 2},
            {"title": "Mira Vell", "disposition": "friendly", "location": "the mill", "appearances": 1},
            {"title": "Pell", "disposition": "unknown", "location": "", "appearances": 1, "stub": True}]
    moments = [{"player": "alice", "said": "I promise a ring", "narration": "Oskar nods."}]
    notes = format_world_notes(quests, npcs, "The Ferry", moments)
    assert "- The Lost Lantern (from Mira Vell): Find it. Latest note: A heron took it." in notes
    assert "NPCs known at The Ferry: Oskar (wary, at the ferry)" in notes
    assert "could turn up again: Mira Vell (friendly, at the mill); Pell (unknown)" in notes
    assert "stub record (describe them with record_npc when you can): Pell" in notes
    assert "- alice: I promise a ring -> Oskar nods." in notes
    assert format_world_notes([], [], "", []) == ""


async def test_the_gm_cannot_see_or_call_table_only_tools():
    from lore.gm import TABLE_ONLY, ToolSession, Toolbox

    class FakeClient:
        async def list_tools(self):
            class Tool:
                def __init__(self, name):
                    self.name, self.description, self.input_schema = name, "", {"type": "object"}
            class Listing:
                tools = [Tool("apply_damage"), Tool("rollback"), Tool("record_story")]
            return Listing()

    toolbox = Toolbox({"game": "http://unused"})
    await toolbox._load({"game": FakeClient()})
    names = [d["name"] for d in toolbox.definitions()]
    assert "apply_damage" in names and not set(names) & TABLE_ONLY
    result, is_error = await ToolSession({}, {"rollback": "game"}).call("rollback", {"campaign": "x", "event_id": 1})
    assert is_error and "Unknown tool" in result


async def test_undo_turns_trims_conversation_lines_and_feed(redis):
    from lore.web.app import Table
    table = Table(redis, 987654)
    await table.reset()
    await redis.delete(table.chat, *[k async for k in redis.scan_iter("lore:campaign:987654:user:*:lines")])
    await table.save([{"role": "user", "content": str(i)} for i in range(6)], "text", "sys", "v1")
    await table.add_lines("alice", {"role": "player", "text": "kept", "turn": "t1"},
                          {"role": "gm", "text": "undone", "turn": "t2"}, {"role": "gm", "text": "old line"})
    await table.add_lines("bob", {"role": "gm", "text": "undone too", "turn": "t3"})
    for turn in ("t1", "t2", "t3"):
        await redis.xadd(table.chat, {"turn": turn, "user": "alice", "message": "m", "reply": "r"})

    await table.undo_turns({"t2", "t3"}, messages_kept=4)
    assert len((await table.load())["messages"]) == 4
    assert [line["text"] for line in await table.recent_lines("alice", 10)] == ["kept", "old line"]
    assert await table.recent_lines("bob", 10) == []
    assert [f["turn"] for _, f in await redis.xrange(table.chat)] == ["t1"]

    # A conversation shorter than the point rolled back to was reset since: start afresh.
    await table.undo_turns(set(), messages_kept=10)
    assert (await table.load())["messages"] == []
    await redis.delete(table.chat)
