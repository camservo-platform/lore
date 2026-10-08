"""Live, interruptible voice conversation with the Game Master.

The browser streams microphone audio (16 kHz, 16-bit PCM) over a WebSocket; we relay it
to Deepgram Flux, which detects when the player starts and finishes a turn. A finished
turn becomes a GM turn whose narration streams back over the same socket (the browser
speaks it). If the player starts talking while the GM's voice is playing, we tell the
browser to stop playback at once ("barge-in"); what they say is queued and sent as the
next turn, flagged as an interruption. The interrupted GM turn still completes, so game
state is never left half-applied.

A world's opening turn (how-to-play, opening scene, starter quest) is asked for over the
same socket, so the player can talk over it like any other reply. The browser also says
whether the player is acting in character or asking the GM something out of character;
each turn takes the setting it had when the player finished speaking.

The GM's own voice leaking from speakers into the mic is filtered out by comparing what
Flux heard with what the GM has been saying.

When the server starts draining for a deploy, the session waits until the player isn't
mid-sentence and no turn is running, then tells the browser to reconnect (it lands on
the new pod).
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

FLUX_URL = "wss://api.deepgram.com/v2/listen"
SAMPLE_RATE = 16000
# Words heard before we treat speech over the GM as an interruption (one stray word is
# usually a cough, a backchannel "mm", or echo).
BARGE_IN_WORDS = 2

WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return WORD.findall(text.lower())


def is_echo(heard: str, spoken: str, threshold: float = 0.75) -> bool:
    """True if `heard` is (nearly) all words the GM just said: its voice picked up by the mic."""
    heard_words = words(heard)
    if not heard_words:
        return True
    spoken_words = set(words(spoken))
    if not spoken_words:
        return False
    return sum(w in spoken_words for w in heard_words) / len(heard_words) >= threshold


@dataclass
class Utterance:
    said: str
    interrupted: bool = False  # spoken over the GM
    intro: bool = False        # the world's opening turn, asked for by the browser
    aside: bool = False        # out of character, to the GM rather than in the story

    def join(self, other: "Utterance") -> "Utterance":
        return Utterance(f"{self.said} {other.said}".strip(), self.interrupted or other.interrupted,
                         self.intro or other.intro, self.aside or other.aside)


# play(utterance, emit): runs a GM turn for what the player said (or the world's opening
# turn), streaming its events to emit. It must keep going if this session ends (the
# caller shields it).
PlayTurn = Callable[[Utterance, Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[None]]


class Conversation:
    """One player's live voice session at one table."""

    def __init__(
        self, ws: WebSocket, deepgram_key: str, model: str, play: PlayTurn, draining: asyncio.Event | None = None,
    ):
        self._ws = ws
        self._key = deepgram_key
        self._model = model
        self._play = play
        self._send_lock = asyncio.Lock()
        self._utterances: asyncio.Queue[Utterance] = asyncio.Queue()
        self._aside = False          # browser's Act / Ask the GM setting
        self._gm_speaking = False    # browser reports playback state
        self._turn_running = False
        self._gm_text = ""           # what the GM said most recently, for the echo filter
        self._new_reply = False
        self._barged_turn: int | None = None
        self._closed = False
        self._hearing = False        # the player is mid-turn (Flux started one, not ended it)
        self._draining = draining

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            return
        async with self._send_lock:
            try:
                await self._ws.send_json(message)
            except (WebSocketDisconnect, RuntimeError):
                self._closed = True  # the player left; a running turn finishes without them

    async def run(self) -> None:
        url = f"{FLUX_URL}?model={self._model}&encoding=linear16&sample_rate={SAMPLE_RATE}"
        async with websockets.connect(url, additional_headers={"Authorization": f"Token {self._key}"}) as flux:
            await self.send({"type": "ready"})
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._from_browser(flux))
                tasks.create_task(self._from_flux(flux))
                tasks.create_task(self._turns())
                if self._draining is not None:
                    tasks.create_task(self._close_when_drained())

    def idle(self) -> bool:
        """Nothing would be lost by ending the session now."""
        return not (self._hearing or self._turn_running or not self._utterances.empty())

    async def _close_when_drained(self) -> None:
        await self._draining.wait()
        while not self.idle():
            await asyncio.sleep(0.25)
        await self.send({"type": "reconnect"})
        self._closed = True
        raise _SessionOver

    async def _from_browser(self, flux) -> None:
        try:
            while True:
                message = await self._ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes"):
                    await flux.send(message["bytes"])
                elif message.get("text"):
                    control = json.loads(message["text"])
                    if control.get("type") == "playback":
                        self._gm_speaking = bool(control.get("speaking"))
                    elif control.get("type") == "intro":
                        await self._utterances.put(Utterance("", intro=True))
                    elif control.get("type") == "aside":
                        self._aside = bool(control.get("on"))
                    elif control.get("type") == "stop":
                        break
        except WebSocketDisconnect:
            pass
        # Ending the session: close Flux so its reader finishes, then stop the turn worker.
        self._closed = True
        try:
            await flux.send(json.dumps({"type": "CloseStream"}))
        except websockets.ConnectionClosed:
            pass
        raise _SessionOver

    async def _from_flux(self, flux) -> None:
        async for raw in flux:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "Error":
                await self.send({"type": "error", "text": f"Speech recognition failed: {message.get('description')}"})
                raise _SessionOver
            if kind != "TurnInfo":
                continue
            event, heard, index = message["event"], message["transcript"], message["turn_index"]
            echo = self._gm_busy() and is_echo(heard, self._gm_text)
            if event in ("StartOfTurn", "Update"):
                if not echo:
                    self._hearing = True
                    await self.send({"type": "heard", "text": heard})
                    if self._gm_speaking and len(words(heard)) >= BARGE_IN_WORDS and self._barged_turn != index:
                        self._barged_turn = index
                        self._gm_speaking = False
                        await self.send({"type": "barge_in"})
            elif event == "EndOfTurn":
                self._hearing = False
                if echo or not words(heard):
                    await self.send({"type": "heard", "text": ""})
                    continue
                interrupted = self._barged_turn == index or self._turn_running
                await self.send({"type": "heard", "text": heard, "final": True})
                await self._utterances.put(Utterance(heard.strip(), interrupted, aside=self._aside))

    def _gm_busy(self) -> bool:
        return self._gm_speaking or self._turn_running

    async def _turns(self) -> None:
        while True:
            utterance = await self._utterances.get()
            # Anything else said meanwhile joins this turn.
            while not self._utterances.empty():
                utterance = utterance.join(self._utterances.get_nowait())
            self._turn_running = True
            self._new_reply = True
            try:
                await self._play(utterance, self._emit)
            finally:
                self._turn_running = False

    async def _emit(self, event: dict[str, Any]) -> None:
        if event["type"] == "text":
            # Keep the previous reply for the echo filter until this one starts: its audio
            # may still be playing.
            if self._new_reply:
                self._gm_text, self._new_reply = "", False
            self._gm_text += event["text"]
        await self.send(event)


class _SessionOver(Exception):
    pass


async def serve(
    ws: WebSocket, deepgram_key: str, model: str, play: PlayTurn, draining: asyncio.Event | None = None,
) -> None:
    """Runs a session until the browser leaves (or the server drains), reporting failures to it."""
    conversation = Conversation(ws, deepgram_key, model, play, draining)
    try:
        await conversation.run()
    except* _SessionOver:
        pass
    except* websockets.InvalidStatus as group:
        log.error("Deepgram refused the connection: %s", group.exceptions[0])
        await _try_send(conversation, {"type": "error", "text": "Couldn't connect to speech recognition."})
    except* Exception:
        log.exception("voice session failed")
        await _try_send(conversation, {"type": "error", "text": "The voice connection failed."})


async def _try_send(conversation: Conversation, message: dict[str, Any]) -> None:
    try:
        await conversation.send(message)
    except Exception:
        pass
