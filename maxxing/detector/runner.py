"""Wires the detector sources into the incident engine."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable

from maxxing.config import Settings
from maxxing.detector.docker_events import start_event_thread
from maxxing.detector.health import poll_health
from maxxing.detector.incident import IncidentEngine, IncidentEvent, Thresholds
from maxxing.detector.log_tail import start_log_thread
from maxxing.detector.signals import Signal

log = logging.getLogger(__name__)

EventHandler = Callable[[IncidentEvent], Awaitable[None]]


class Detector:
    def __init__(self, settings: Settings, on_event: EventHandler, engine: IncidentEngine | None = None):
        self.settings = settings
        self.on_event = on_event
        self.engine = engine or IncidentEngine(
            Thresholds(cooldown_s=settings.cooldown_s, recovery_s=settings.recovery_s)
        )
        self.queue: asyncio.Queue[Signal] = asyncio.Queue()
        self._thread_stop = threading.Event()
        self.docker_ok = False

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()

        def emit_threadsafe(sig: Signal) -> None:
            loop.call_soon_threadsafe(self.queue.put_nowait, sig)

        try:
            from maxxing.docker_client import get_client

            await asyncio.to_thread(get_client)
            start_event_thread(self.settings.compose_project, self.settings.api_service, emit_threadsafe, self._thread_stop)
            start_log_thread(self.settings.compose_project, self.settings.api_service, emit_threadsafe, self._thread_stop)
            self.docker_ok = True
        except Exception as e:
            log.warning("Docker unavailable, running with LB health checks only: %s", e)

        tasks = [
            asyncio.create_task(
                poll_health(self.settings.lb_url, self.queue.put_nowait, stop,
                            self.settings.health_interval_s, self.settings.health_timeout_s)
            ),
            asyncio.create_task(self._ticker(stop)),
            asyncio.create_task(self._consume(stop)),
        ]
        try:
            await stop.wait()
        finally:
            self._thread_stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _dispatch(self, events: list[IncidentEvent]) -> None:
        for ev in events:
            try:
                await self.on_event(ev)
            except Exception:
                log.exception("incident handler failed for %s", ev.type)

    async def _consume(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            sig = await self.queue.get()
            await self._dispatch(self.engine.observe(sig))

    async def _ticker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(1.0)
            await self._dispatch(self.engine.tick(time.time()))
