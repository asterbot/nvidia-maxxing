"""Container state: inspect (OOMKilled, ExitCode, RestartCount, health) and a one-shot stats sample."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from maxxing.docker_client import list_containers
from maxxing.evidence.models import ContainerState

log = logging.getLogger(__name__)


def _cpu_pct(stats: dict) -> float | None:
    try:
        cpu, pre = stats["cpu_stats"], stats["precpu_stats"]
        delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu["system_cpu_usage"] - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
        if sys_delta > 0 and delta >= 0:
            return round(delta / sys_delta * ncpu * 100.0, 1)
    except (KeyError, TypeError):
        pass
    return None


def state_from_attrs(attrs: dict, stats: dict | None = None) -> ContainerState:
    """Build a ContainerState from `docker inspect` attrs (+ optional stats). Pure, for testing."""
    st = attrs.get("State", {}) or {}
    cfg = attrs.get("Config", {}) or {}
    labels = cfg.get("Labels") or {}
    env = dict(e.split("=", 1) for e in (cfg.get("Env") or []) if "=" in e)
    health = st.get("Health") or {}
    hc_log = [
        f"exit={h.get('ExitCode')} {str(h.get('Output', '')).strip()[:160]}" for h in (health.get("Log") or [])[-3:]
    ]
    cs = ContainerState(
        name=(attrs.get("Name") or "").lstrip("/"),
        id=(attrs.get("Id") or "")[:12],
        service=labels.get("com.docker.compose.service", ""),
        image=cfg.get("Image", ""),
        sha=env.get("GIT_SHA") or labels.get("org.opencontainers.image.revision") or labels.get("git_sha"),
        status=st.get("Status", ""),
        running=bool(st.get("Running")),
        exit_code=st.get("ExitCode"),
        oom_killed=bool(st.get("OOMKilled")),
        restart_count=int(attrs.get("RestartCount") or 0),
        started_at=st.get("StartedAt"),
        finished_at=None if (st.get("FinishedAt") or "").startswith("0001") else st.get("FinishedAt"),
        health=health.get("Status"),
        health_log=hc_log,
        mem_limit=(attrs.get("HostConfig") or {}).get("Memory") or None,
    )
    if stats:
        mem = stats.get("memory_stats") or {}
        usage = mem.get("usage")
        if usage is not None:
            # match `docker stats`: subtract page cache
            cache = (mem.get("stats") or {}).get("inactive_file") or (mem.get("stats") or {}).get("cache") or 0
            cs.mem_usage = max(0, int(usage) - int(cache))
        if mem.get("limit") and not cs.mem_limit:
            cs.mem_limit = int(mem["limit"])
        cs.cpu_pct = _cpu_pct(stats)
    return cs


def inspect_container(container: Any, with_stats: bool = True) -> ContainerState:
    try:
        container.reload()
        attrs = container.attrs
        stats = None
        if with_stats and attrs.get("State", {}).get("Running"):
            try:
                stats = container.stats(stream=False)
            except Exception as e:  # stats can race with a dying container
                log.debug("stats failed for %s: %s", container.name, e)
        return state_from_attrs(attrs, stats)
    except Exception as e:
        return ContainerState(name=getattr(container, "name", "?"), error=str(e))


def collect_containers(project: str, service: str) -> tuple[list[ContainerState], list[ContainerState]]:
    """Return (service replicas, other project containers such as redis/lb)."""
    containers = list_containers(project)
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(inspect_container, containers))  # stats(stream=False) blocks ~1s each
    states.sort(key=lambda s: s.name)
    return [s for s in states if s.service == service], [s for s in states if s.service != service]
