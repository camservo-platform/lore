import asyncio
import json

import pytest

from lore.web.conversation import Conversation, Utterance, is_echo, words


def test_words_normalises():
    assert words("The Gate—it's OPEN!") == ["the", "gate", "it's", "open"]


def test_echo_is_the_gm_heard_through_the_mic():
    gm = "The torchlight flickers as you step into the drowned keep. What do you do?"
    assert is_echo("as you step into the drowned keep", gm)
    assert is_echo("", gm)                                   # nothing intelligible
    assert not is_echo("I draw my sword and charge the guard", gm)
    assert not is_echo("stop, I want to go back", gm)
    assert not is_echo("hello", "")                          # GM silent: never echo

INTRO = "Welcome to the drowned keep. Speak to act; ask me anything. Your first task: find the lantern."


class Browser:
    """The browser end of the socket: scripted messages in, everything sent recorded."""

    def __init__(self, messages):
        self.incoming = asyncio.Queue()
        for message in messages:
            self.incoming.put_nowait(message)
        self.sent: list[dict] = []

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, message):
        self.sent.append(message)


class Flux:
    def __init__(self, messages=()):
        self.messages = [json.dumps(m) for m in messages]

    async def send(self, data):
        pass

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for message in self.messages:
            yield message


def heard(event, transcript, index=0):
    return {"type": "TurnInfo", "event": event, "transcript": transcript, "turn_index": index}


async def test_opening_turn_runs_in_the_session_and_can_be_talked_over():
    played = []
    intro_spoken = asyncio.Event()

    async def play(utterance, emit):
        played.append(utterance)
        if utterance.intro:
            await emit({"type": "text", "text": INTRO})
            intro_spoken.set()

    browser = Browser([
        {"type": "websocket.receive", "text": json.dumps({"type": "intro"})},
        {"type": "websocket.receive", "text": json.dumps({"type": "playback", "speaking": True})},
    ])
    conversation = Conversation(browser, "key", "model", play)
    turns = asyncio.create_task(conversation._turns())
    reading = asyncio.create_task(conversation._from_browser(Flux()))
    await asyncio.wait_for(intro_spoken.wait(), 2)
    await asyncio.sleep(0.05)
    assert played == [Utterance("", intro=True)]
    assert conversation._gm_speaking

    # The intro leaking back through the mic isn't an interruption; the player talking is.
    await conversation._from_flux(Flux([
        heard("Update", "to the drowned keep speak to act", 0),
        heard("Update", "wait, how do I roll", 1),
        heard("EndOfTurn", "wait, how do I roll dice?", 1),
    ]))
    assert browser.sent[-3:] == [
        {"type": "heard", "text": "wait, how do I roll"},
        {"type": "barge_in"},
        {"type": "heard", "text": "wait, how do I roll dice?", "final": True},
    ]
    await asyncio.sleep(0.05)
    assert played[-1] == Utterance("wait, how do I roll dice?", interrupted=True)

    browser.incoming.put_nowait({"type": "websocket.disconnect"})
    with pytest.raises(Exception):
        await reading
    turns.cancel()


async def test_spoken_turns_take_the_act_or_ask_setting_from_when_they_ended():
    played = []

    async def play(utterance, emit):
        played.append(utterance)

    browser = Browser([{"type": "websocket.receive", "text": json.dumps({"type": "aside", "on": True})}])
    conversation = Conversation(browser, "key", "model", play)
    turns = asyncio.create_task(conversation._turns())
    reading = asyncio.create_task(conversation._from_browser(Flux()))
    await asyncio.sleep(0.05)
    await conversation._from_flux(Flux([heard("EndOfTurn", "how many potions do I have", 0)]))
    await asyncio.sleep(0.05)
    browser.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "aside", "on": False})})
    await asyncio.sleep(0.05)
    await conversation._from_flux(Flux([heard("EndOfTurn", "I drink one", 1)]))
    await asyncio.sleep(0.05)
    assert played == [Utterance("how many potions do I have", aside=True), Utterance("I drink one")]

    browser.incoming.put_nowait({"type": "websocket.disconnect"})
    with pytest.raises(Exception):
        await reading
    turns.cancel()


def test_utterances_said_together_join_into_one_turn():
    joined = Utterance("wait", interrupted=True).join(Utterance("what was that rule?", aside=True))
    assert joined == Utterance("wait what was that rule?", interrupted=True, aside=True)
    assert Utterance("", intro=True).join(Utterance("hello")) == Utterance("hello", intro=True)
