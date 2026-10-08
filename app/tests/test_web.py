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
