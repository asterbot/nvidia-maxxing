"""Incident rule engine: turns a stream of signals into at most one open incident per outage.

Pure and clock-injected (every signal carries `ts`, `tick(now)` drives recovery) so it is
fully unit-testable.

Rules (any one opens an incident):
  * oom          - any service container OOM-killed
  * crash_loop   - >= `crash_count` unplanned container deaths within `crash_window_s`
  * lb_down      - >= `health_fail_streak` consecutive failed LB health checks
  * error_rate   - 5xx ratio >= `error_ratio` over `rate_window_s` with >= `min_requests`
  * fatal_logs   - >= `fatal_count` fatal log lines within `rate_window_s`
  * unhealthy    - >= `unhealthy_replicas` distinct replicas reported unhealthy

4xx never count, so a healthy traffic surge with some 404s does not page anyone. Right after a
deploy (`deploy_grace_s`), the LB/5xx rules are muted because rolling restarts blip; crash and
OOM rules stay armed so a bad deploy that crash-loops is still caught.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from maxxing.detector.signals import Kind, Signal


@dataclass
class Thresholds:
    crash_count: int = 2
    crash_window_s: float = 60.0
    health_fail_streak: int = 3
    rate_window_s: float = 30.0
    error_ratio: float = 0.2
    min_requests: int = 10
    fatal_count: int = 2
    unhealthy_replicas: int = 2
    kill_grace_s: float = 15.0  # a die within this long after a kill is planned
    deploy_grace_s: float = 20.0
    recovery_s: float = 20.0
    cooldown_s: float = 90.0


class EventType(StrEnum):
    OPENED = "opened"
    UPDATED = "updated"
    RESOLVED = "resolved"


@dataclass
class OpenIncident:
    opened_at: float
    rules: list[str]
    signals: list[Signal] = field(default_factory=list)
    last_bad: float = 0.0
    seq: int = 0

    def add(self, sig: Signal, keep: int = 200) -> None:
        if len(self.signals) < keep:
            self.signals.append(sig)


@dataclass
class IncidentEvent:
    type: EventType
    incident: OpenIncident
    ts: float
    rules: list[str] = field(default_factory=list)


_BAD = {Kind.HEALTH_FAIL, Kind.CONTAINER_OOM, Kind.CONTAINER_UNHEALTHY, Kind.LOG_ERROR}


class IncidentEngine:
    def __init__(self, thresholds: Thresholds | None = None):
        self.t = thresholds or Thresholds()
        self.current: OpenIncident | None = None
        self.last_resolved_at: float | None = None
        self._seq = 0
        self._deaths: deque[float] = deque()
        self._kills: dict[str, float] = {}
        self._requests: deque[tuple[float, bool]] = deque()  # (ts, is_5xx)
        self._fatals: deque[float] = deque()
        self._unhealthy: dict[str, float] = {}
        self._fail_streak = 0
        self._ok_streak = 0
        self._last_deploy = float("-inf")
        self._last_health_ok = float("-inf")

    # -- windows ---------------------------------------------------------------
    def _trim(self, now: float) -> None:
        while self._deaths and self._deaths[0] < now - self.t.crash_window_s:
            self._deaths.popleft()
        while self._requests and self._requests[0][0] < now - self.t.rate_window_s:
            self._requests.popleft()
        while self._fatals and self._fatals[0] < now - self.t.rate_window_s:
            self._fatals.popleft()
        for name, ts in list(self._unhealthy.items()):
            if ts < now - self.t.crash_window_s:
                del self._unhealthy[name]

    def error_ratio(self) -> tuple[float, int]:
        n = len(self._requests)
        bad = sum(1 for _, is5xx in self._requests if is5xx)
        return (bad / n if n else 0.0), n

    def _in_deploy_grace(self, now: float) -> bool:
        return now - self._last_deploy < self.t.deploy_grace_s

    # -- ingestion ---------------------------------------------------------------
    def _record(self, s: Signal) -> bool:
        """Update windows; return True if the signal is evidence of badness."""
        k = s.kind
        if k == Kind.HEALTH_OK:
            self._fail_streak, self._ok_streak = 0, self._ok_streak + 1
            self._last_health_ok = s.ts
            return False
        if k == Kind.HEALTH_FAIL:
            self._fail_streak, self._ok_streak = self._fail_streak + 1, 0
            return True
        if k in (Kind.CONTAINER_KILL, Kind.DEPLOY):
            if s.container:
                self._kills[s.container] = s.ts
            self._last_deploy = s.ts if k == Kind.DEPLOY else self._last_deploy
            return False
        if k == Kind.CONTAINER_DIE:
            planned = s.container is not None and s.ts - self._kills.get(s.container, float("-inf")) <= self.t.kill_grace_s
            if planned or s.exit_code == 0:
                return False
            self._deaths.append(s.ts)
            return True
        if k == Kind.CONTAINER_OOM:
            return True
        if k == Kind.CONTAINER_UNHEALTHY:
            self._unhealthy[s.container or "?"] = s.ts
            return True
        if k == Kind.CONTAINER_HEALTHY:
            self._unhealthy.pop(s.container or "?", None)
            return False
        if k == Kind.REQUEST and s.status is not None:
            is5xx = s.status >= 500
            self._requests.append((s.ts, is5xx))
            return is5xx
        if k == Kind.LOG_ERROR:
            if (s.level or 0) >= 60:
                self._fatals.append(s.ts)
            return (s.level or 0) >= 60
        return False

    def _rules_firing(self, s: Signal) -> list[str]:
        now = s.ts
        fired: list[str] = []
        if s.kind == Kind.CONTAINER_OOM:
            fired.append(f"oom: {s.container} OOM-killed")
        if len(self._deaths) >= self.t.crash_count:
            fired.append(f"crash_loop: {len(self._deaths)} container deaths in {self.t.crash_window_s:.0f}s")
        if len(self._fatals) >= self.t.fatal_count:
            fired.append(f"fatal_logs: {len(self._fatals)} fatal log lines in {self.t.rate_window_s:.0f}s")
        if len(self._unhealthy) >= self.t.unhealthy_replicas:
            fired.append(f"unhealthy: {len(self._unhealthy)} replicas unhealthy ({', '.join(sorted(self._unhealthy))})")
        if not self._in_deploy_grace(now):
            if self._fail_streak >= self.t.health_fail_streak:
                fired.append(f"lb_down: {self._fail_streak} consecutive LB health failures")
            ratio, n = self.error_ratio()
            if n >= self.t.min_requests and ratio >= self.t.error_ratio:
                fired.append(f"error_rate: {ratio:.0%} 5xx over {n} requests in {self.t.rate_window_s:.0f}s")
        return fired

    def observe(self, s: Signal) -> list[IncidentEvent]:
        self._trim(s.ts)
        bad = self._record(s)
        if self.current is not None:
            if bad and s.kind == Kind.REQUEST:
                # a stray 5xx while recovering shouldn't keep the incident open
                bad = self.error_ratio()[0] >= self.t.error_ratio / 2
            if bad:
                self.current.last_bad = s.ts
                self.current.add(s)
            new_rules = [r for r in self._rules_firing(s) if r.split(":")[0] not in {x.split(":")[0] for x in self.current.rules}]
            if new_rules:
                self.current.rules.extend(new_rules)
                return [IncidentEvent(EventType.UPDATED, self.current, s.ts, new_rules)]
            return []
        if not bad:
            return []
        fired = self._rules_firing(s)
        if not fired:
            return []
        if self.last_resolved_at is not None and s.ts - self.last_resolved_at < self.t.cooldown_s:
            return []
        self._seq += 1
        self.current = OpenIncident(opened_at=s.ts, rules=fired, last_bad=s.ts, seq=self._seq)
        self.current.add(s)
        return [IncidentEvent(EventType.OPENED, self.current, s.ts, fired)]

    def tick(self, now: float) -> list[IncidentEvent]:
        """Call periodically: resolves the open incident once things have been healthy long enough."""
        self._trim(now)
        inc = self.current
        if inc is None:
            return []
        ratio, n = self.error_ratio()
        healthy = (
            now - inc.last_bad >= self.t.recovery_s
            and self._ok_streak >= self.t.health_fail_streak
            and now - self._last_health_ok < self.t.recovery_s / 2
            and not (n >= self.t.min_requests and ratio >= self.t.error_ratio / 2)
            and not self._unhealthy
        )
        if not healthy:
            return []
        self.current = None
        self.last_resolved_at = now
        self._deaths.clear()
        self._fatals.clear()
        return [IncidentEvent(EventType.RESOLVED, inc, now)]
