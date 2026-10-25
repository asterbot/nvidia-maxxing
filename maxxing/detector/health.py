"""Async LB health polling.

The api replicas are only reachable through the LB from the host, so per-replica health comes
from Docker healthcheck events (docker_events.py); here we poll the LB, which round-robins
across replicas, so consecutive polls sample different replicas.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx

from maxxing.detector.signals import Kind, Signal

log = logging.getLogger(__name__)


async def check_once(client: httpx.AsyncClient, url: str) -> Signal:
    t0 = time.monotonic()
    try:
        r = await client.get(url)
        ms = (time.monotonic() - t0) * 1000
        body = {}
        try:
            body = r.json()
        except ValueError:
            pass
        replica = body.get("replica") if isinstance(body, dict) else None
        ok = r.status_code == 200 and (not isinstance(body, dict) or body.get("ok", True) is not False)
        return Signal(
            Kind.HEALTH_OK if ok else Kind.HEALTH_FAIL,
            container=replica,
            status=r.status_code,
            detail=f"{ms:.0f}ms",
        )
    except httpx.HTTPError as e:
        return Signal(Kind.HEALTH_FAIL, detail=f"{type(e).__name__}")


async def poll_health(
    lb_url: str,
    emit: Callable[[Signal], None],
    stop: asyncio.Event,
    interval: float = 2.0,
    timeout: float = 3.0,
) -> None:
    url = lb_url.rstrip("/") + "/health"
    async with httpx.AsyncClient(timeout=timeout) as client:
        while not stop.is_set():
            emit(await check_once(client, url))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
