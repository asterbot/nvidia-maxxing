"""Follows container logs and emits REQUEST / LOG_ERROR signals for the rolling error-rate window.

Request outcomes are taken from the LB access log when an `lb` container exists (that is what
users see, including 502/504s from dead replicas), otherwise from the api request lines.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from maxxing.detector.signals import Kind, Signal
from maxxing.evidence.logs import parse_line

log = logging.getLogger(__name__)


def line_signals(raw: str, container: str, service: str, count_requests: bool) -> list[Signal]:
    ln = parse_line(raw, container, default_ts=time.time())
    if ln is None:
        return []
    out: list[Signal] = []
    if count_requests and ln.status is not None:
        out.append(Signal(Kind.REQUEST, ln.ts or time.time(), container, status=ln.status))
    if service != "lb" and ln.level >= 50:
        out.append(Signal(Kind.LOG_ERROR, ln.ts or time.time(), container, level=ln.level, detail=(ln.err_message or ln.msg)[:120]))
    return out


class LogTailer:
    def __init__(self, project: str, service: str, emit: Callable[[Signal], None], stop: threading.Event):
        self.project, self.service, self.emit, self.stop = project, service, emit, stop
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def _follow(self, container, svc: str, count_requests: bool) -> None:
        name = container.name
        try:
            stream = container.logs(stream=True, follow=True, since=int(time.time()), timestamps=False)
            buf = ""
            for chunk in stream:
                if self.stop.is_set():
                    break
                buf += chunk.decode("utf-8", "replace")
                *lines, buf = buf.split("\n")
                for raw in lines:
                    for sig in line_signals(raw, name, svc, count_requests):
                        self.emit(sig)
        except Exception as e:
            log.debug("log follow for %s ended: %s", name, e)
        finally:
            with self._lock:
                self._active.discard(container.id)

    def scan(self) -> None:
        from maxxing.docker_client import list_containers

        containers = list_containers(self.project, all=False)
        has_lb = any(c.labels.get("com.docker.compose.service") == "lb" for c in containers)
        for c in containers:
            svc = c.labels.get("com.docker.compose.service", "")
            if svc not in (self.service, "lb"):
                continue
            count_requests = svc == "lb" or not has_lb
            with self._lock:
                if c.id in self._active:
                    continue
                self._active.add(c.id)
            threading.Thread(target=self._follow, args=(c, svc, count_requests), name=f"tail-{c.name}", daemon=True).start()

    def run(self, rescan_s: float = 3.0) -> None:
        while not self.stop.is_set():
            try:
                self.scan()
            except Exception as e:
                log.warning("log tail scan failed: %s", e)
            self.stop.wait(rescan_s)


def start_log_thread(project: str, service: str, emit: Callable[[Signal], None], stop: threading.Event) -> threading.Thread:
    t = threading.Thread(target=LogTailer(project, service, emit, stop).run, name="log-tail", daemon=True)
    t.start()
    return t
