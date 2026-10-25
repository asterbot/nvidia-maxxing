"""Docker event stream -> signals. docker-py is blocking, so this runs in a daemon thread."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from maxxing.detector.signals import Kind, Signal

log = logging.getLogger(__name__)

IGNORED_SERVICES = {"traffic", "docker-proxy"}


def event_to_signal(ev: dict, service: str) -> Signal | None:
    """Map one decoded docker event to a Signal (pure, for testing)."""
    if ev.get("Type") != "container":
        return None
    action = str(ev.get("Action") or ev.get("status") or "")
    attrs = (ev.get("Actor") or {}).get("Attributes") or {}
    svc = attrs.get("com.docker.compose.service", "")
    if svc in IGNORED_SERVICES:
        return None
    name = attrs.get("name")
    ts = (ev.get("timeNano") or 0) / 1e9 or float(ev.get("time") or time.time())
    if action == "die":
        code = attrs.get("exitCode")
        return Signal(Kind.CONTAINER_DIE, ts, name, exit_code=int(code) if code not in (None, "") else None, detail=svc)
    if action == "oom":
        return Signal(Kind.CONTAINER_OOM, ts, name, detail=svc)
    if action in ("kill", "stop"):
        return Signal(Kind.CONTAINER_KILL, ts, name, detail=attrs.get("signal", action))
    if action == "start":
        return Signal(Kind.CONTAINER_START, ts, name, detail=svc)
    if action.startswith("health_status"):
        status = action.split(":", 1)[1].strip() if ":" in action else attrs.get("health_status", "")
        if status == "unhealthy":
            return Signal(Kind.CONTAINER_UNHEALTHY, ts, name, detail=svc)
        if status == "healthy":
            return Signal(Kind.CONTAINER_HEALTHY, ts, name, detail=svc)
        return None
    if action in ("create", "destroy") and svc == service:
        return Signal(Kind.DEPLOY, ts, name, detail=action)
    return None


def start_event_thread(
    project: str, service: str, emit: Callable[[Signal], None], stop: threading.Event
) -> threading.Thread:
    def run() -> None:
        from maxxing.docker_client import get_client, project_filter

        while not stop.is_set():
            stream = None
            try:
                filters = project_filter(project) | {"type": ["container"]}
                stream = get_client().events(decode=True, filters=filters)
                for ev in stream:
                    if stop.is_set():
                        break
                    sig = event_to_signal(ev, service)
                    if sig is not None:
                        emit(sig)
            except Exception as e:
                log.warning("docker event stream error (%s); retrying in 3s", e)
                stop.wait(3)
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    t = threading.Thread(target=run, name="docker-events", daemon=True)
    t.start()
    return t
