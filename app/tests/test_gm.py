from lore.gm import echo_content, instructions_version, style_note, system_prompt, updated_instructions
from lore.web.app import _complete_history


def test_echo_content_keeps_everything_without_fallback():
    blocks = [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "Hi"},
              {"type": "tool_use", "id": "t1", "name": "roll_dice", "input": {}}]
    assert echo_content(blocks) == blocks


def test_echo_content_drops_model_internal_blocks_before_last_fallback():
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "a"},
        {"type": "text", "text": "partial"},
        {"type": "tool_use", "id": "t0", "name": "x", "input": {}},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "", "signature": "b"},
        {"type": "tool_use", "id": "t1", "name": "roll_dice", "input": {}},
    ]
    assert [b["type"] for b in echo_content(blocks)] == ["text", "fallback", "thinking", "tool_use"]


def test_complete_history_drops_unanswered_tool_call_only():
    user = {"role": "user", "content": "alice: hi"}
    answered = {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}
    dangling = {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}
    assert _complete_history([user, answered]) == [user, answered]
    assert _complete_history([user, dangling]) == [user]
    assert _complete_history([]) == []


def test_style_notes():
    assert "markdown" in style_note("speech")
    assert "text mode" in style_note("text")


def test_instructions_are_versioned_per_campaign():
    assert instructions_version("A") == instructions_version("A") != instructions_version("B")
    assert updated_instructions("A").endswith(system_prompt("A"))


# --- what reaches the players from one streamed model response --------------------------

from types import SimpleNamespace as NS

from lore.gm import NARRATE, _Round


class FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()

    async def get_final_message(self):
        return NS(stop_reason="end_turn", content=[])


def start(kind, name=None):
    return NS(type="content_block_start", content_block=NS(type=kind, name=name))


def text(t):
    return NS(type="text", text=t)


def narrate_json(snapshot_text):
    return NS(type="input_json", snapshot={"text": snapshot_text})


async def shown(events, narration=None, mute=False):
    narration = [] if narration is None else narration
    out = [e["text"] for e in [ev async for ev in _Round(FakeStream(events), narration, mute=mute)] if e["type"] == "text"]
    return "".join(out)


async def test_working_notes_before_a_tool_call_are_dropped():
    events = [start("text"), text("Hit for 4; "), text("bandit hits back for 2."), start("tool_use", "apply_damage")]
    assert await shown(events) == ""


async def test_text_ending_the_response_is_narration():
    events = [start("text"), text("Your blade bites deep. "), text("What do you do?")]
    assert await shown(events) == "Your blade bites deep. What do you do?"


async def test_narrate_streams_as_it_is_written():
    events = [start("tool_use", NARRATE), narrate_json("Your bl"), narrate_json("Your blade bites deep."),
              start("tool_use", "log_event")]
    assert await shown(events) == "Your blade bites deep."


async def test_muted_rounds_show_nothing_but_narrate_still_does():
    assert await shown([start("text"), text("I logged that.")], narration=["Earlier."], mute=True) == ""
    events = [start("tool_use", NARRATE), narrate_json("Still heard.")]
    assert await shown(events, narration=["Earlier."], mute=True) == "\n\nStill heard."
