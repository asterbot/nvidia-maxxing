"""Signals emitted by detector sources and consumed by the incident rule engine."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    HEALTH_OK = "health_ok"
    HEALTH_FAIL = "health_fail"
    CONTAINER_DIE = "container_die"
    CONTAINER_OOM = "container_oom"
    CONTAINER_KILL = "container_kill"  # docker stop/kill sent a signal: planned, not a crash
    CONTAINER_START = "container_start"
    CONTAINER_UNHEALTHY = "container_unhealthy"
    CONTAINER_HEALTHY = "container_healthy"
    DEPLOY = "deploy"  # create/destroy of service containers (compose up)
    REQUEST = "request"  # one HTTP request line, with status
    LOG_ERROR = "log_error"  # level >= 50 log line


@dataclass(slots=True)
class Signal:
    kind: Kind
    ts: float = field(default_factory=time.time)
    container: str | None = None
    status: int | None = None  # REQUEST: HTTP status
    level: int | None = None  # LOG_ERROR: pino level
    exit_code: int | None = None  # CONTAINER_DIE
    detail: str = ""

    def describe(self) -> str:
        bits = [self.kind.value]
        if self.container:
            bits.append(self.container)
        if self.exit_code is not None:
            bits.append(f"exit={self.exit_code}")
        if self.status is not None:
            bits.append(str(self.status))
        if self.detail:
            bits.append(self.detail)
        return " ".join(bits)
