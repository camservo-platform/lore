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
