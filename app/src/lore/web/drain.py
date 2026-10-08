"""Graceful shutdown for the web table, so a deploy doesn't cut players off.

On SIGTERM the pod starts draining instead of exiting: live feeds end (browsers reconnect
to the new pod and resume where they left off), idle voice sessions are asked to
reconnect, and work in progress (GM turns, world forging) is held open until it
finishes. Anything still running at the deadline, or when told to hurry (a second
SIGTERM, or SIGUSR1 from `./deploy.sh deploy --now`), is cancelled; a cancelled turn
still saves its history and releases the table's lock on the way out. Only then does
uvicorn shut down, which closes whatever connections are left.
"""

import asyncio
import contextlib
import inspect
import logging
import signal
from collections.abc import Awaitable, Iterator
from types import FrameType
from typing import TypeVar

import uvicorn

log = logging.getLogger(__name__)

T = TypeVar("T")

# How long cancelled work gets to clean up (save history, release locks) before shutdown.
CANCEL_GRACE = 10


class Draining(Exception):
    """Raised by Drain.interruptible when the pod starts draining first."""


class Drain:
    def __init__(self, timeout: float = 240):
        self.timeout = timeout
        self.draining = asyncio.Event()
        self.hurrying = asyncio.Event()
        self._holds: set[asyncio.Task] = set()
        self._idle = asyncio.Event()
        self._idle.set()

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        """Keeps the pod alive while the current task runs this block (it may be cancelled
        if the drain runs out of time)."""
        task = asyncio.current_task()
        assert task is not None
        self._holds.add(task)
        self._idle.clear()
        try:
            yield
        finally:
            self._holds.discard(task)
            if not self._holds:
                self._idle.set()

    async def interruptible(self, awaitable: Awaitable[T]) -> T:
        """Awaits `awaitable`, or cancels it and raises Draining if the drain starts first."""
        if self.draining.is_set():
            if inspect.iscoroutine(awaitable):
                awaitable.close()  # never started; avoids a "never awaited" warning
            raise Draining
        work = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(self.draining.wait())
        try:
            done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            work.cancel()
            raise
        finally:
            stop.cancel()
        if work in done:
            return work.result()
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work
        raise Draining

    def hurry(self) -> None:
        self.hurrying.set()

    async def run(self) -> None:
        """Drains: waits for held work to finish, cancelling it at the deadline or when hurried."""
        self.draining.set()
        log.info("draining: %d task(s) in progress, up to %ss", len(self._holds), self.timeout)
        idle = asyncio.ensure_future(self._idle.wait())
        hurried = asyncio.ensure_future(self.hurrying.wait())
        await asyncio.wait({idle, hurried}, timeout=self.timeout, return_when=asyncio.FIRST_COMPLETED)
        hurried.cancel()
        if not idle.done():
            log.warning("cancelling %d task(s) still in progress", len(self._holds))
            for task in list(self._holds):
                task.cancel()
            await asyncio.wait({idle}, timeout=CANCEL_GRACE)
        idle.cancel()
        log.info("drained")


class Server(uvicorn.Server):
    """uvicorn, but the first SIGTERM/SIGINT drains before shutting down. A second one, or
    SIGUSR1, hurries the drain along."""

    def __init__(self, config: uvicorn.Config, drain: Drain):
        super().__init__(config)
        self.drain = drain
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drainer: asyncio.Task | None = None

    async def startup(self, sockets=None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().startup(sockets=sockets)

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        previous = signal.signal(signal.SIGUSR1, self.handle_exit)
        try:
            with super().capture_signals():
                yield
        finally:
            signal.signal(signal.SIGUSR1, previous)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self._loop is None or self.should_exit:
            super().handle_exit(sig, frame)
            return
        # Signal handlers run between bytecodes; hand over to the event loop.
        self._loop.call_soon_threadsafe(self._on_signal, sig)

    def _on_signal(self, sig: int) -> None:
        if sig == signal.SIGUSR1 or self._drainer is not None:
            log.info("hurrying the drain (signal %d)", sig)
            self.drain.hurry()
        if self._drainer is None:
            self._drainer = asyncio.create_task(self._drain_then_exit())

    async def _drain_then_exit(self) -> None:
        try:
            await self.drain.run()
        finally:
            self.should_exit = True
