import asyncio
import signal

import pytest
import uvicorn
from starlette.applications import Starlette

from lore.web.app import parse_feed_id, stream_id_le
from lore.web.conversation import Conversation
from lore.web.drain import Drain, Draining, Server


async def test_drain_waits_for_work_in_progress():
    drain = Drain(timeout=5)
    finished = asyncio.Event()

    async def turn():
        with drain.hold():
            await asyncio.sleep(0.2)
            finished.set()

    task = asyncio.create_task(turn())
    await asyncio.sleep(0)
    await drain.run()
    assert finished.is_set() and not task.cancelled()


async def test_drain_cancels_work_at_the_deadline_letting_it_clean_up():
    drain = Drain(timeout=0.1)
    cleaned_up = asyncio.Event()

    async def stuck_turn():
        with drain.hold():
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0.01)  # e.g. saving history, releasing the lock
                cleaned_up.set()

    task = asyncio.create_task(stuck_turn())
    await asyncio.sleep(0)
    await asyncio.wait_for(drain.run(), 2)
    assert cleaned_up.is_set() and task.cancelled()


async def test_hurrying_cancels_without_waiting_for_the_deadline():
    drain = Drain(timeout=60)

    async def turn():
        with drain.hold():
            await asyncio.sleep(60)

    task = asyncio.create_task(turn())
    await asyncio.sleep(0)
    running = asyncio.create_task(drain.run())
    await asyncio.sleep(0.05)
    assert not running.done()
    drain.hurry()
    await asyncio.wait_for(running, 2)
    assert task.cancelled()


async def test_interruptible_stops_waiting_when_the_drain_starts():
    drain = Drain()
    assert await drain.interruptible(asyncio.sleep(0, "result")) == "result"
    waiting = asyncio.create_task(drain.interruptible(asyncio.sleep(60)))
    await asyncio.sleep(0)
    await drain.run()
    with pytest.raises(Draining):
        await waiting
    with pytest.raises(Draining):
        await drain.interruptible(asyncio.sleep(0))


async def test_server_drains_before_shutting_down_and_a_second_signal_hurries():
    drain = Drain(timeout=60)
    server = Server(uvicorn.Config(Starlette(), host="127.0.0.1", port=0, log_level="warning"), drain)
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    async def turn():
        with drain.hold():
            await asyncio.sleep(60)

    task = asyncio.create_task(turn())
    await asyncio.sleep(0)
    server.handle_exit(signal.SIGTERM, None)
    await asyncio.sleep(0.2)
    assert drain.draining.is_set() and not server.should_exit  # still serving the turn
    server.handle_exit(signal.SIGTERM, None)
    await asyncio.wait_for(serving, 5)
    assert task.cancelled() and server.should_exit


def test_feed_ids():
    assert parse_feed_id("1712-0,1713-4") == ("1712-0", "1713-4")
    for bad in (None, "", "$,$", "1712-0", "1-0,2-0,3-0", "1712-0,abc"):
        assert parse_feed_id(bad) is None
    assert stream_id_le("5-1", "5-1") and stream_id_le("5-1", "5-2") and stream_id_le("5-9", "10-0")
    assert not stream_id_le("10-0", "9-99")


class FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, message):
        self.sent.append(message)


async def test_voice_session_waits_for_the_turn_before_asking_to_reconnect():
    draining = asyncio.Event()
    ws = FakeSocket()
    conversation = Conversation(ws, "key", "model", play=None, draining=draining)
    conversation._turn_running = True
    closing = asyncio.create_task(conversation._close_when_drained())
    draining.set()
    await asyncio.sleep(0.3)
    assert not closing.done()  # the GM is still answering
    conversation._turn_running = False
    conversation._hearing = True
    await asyncio.sleep(0.3)
    assert not closing.done()  # the player is mid-sentence
    conversation._hearing = False
    with pytest.raises(Exception) as over:
        await asyncio.wait_for(closing, 2)
    assert type(over.value).__name__ == "_SessionOver"
    assert ws.sent == [{"type": "reconnect"}]
