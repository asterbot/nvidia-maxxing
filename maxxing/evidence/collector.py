"""Builds an EvidenceBundle from Docker, the LB and the git repo. Every step is best-effort."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx

from maxxing.config import Settings
from maxxing.evidence import logs as logmod
from maxxing.evidence.containers import collect_containers
from maxxing.evidence.git import GitRepo, load_deploys
from maxxing.evidence.models import BlameHit, ContainerState, EvidenceBundle, LogLine
from maxxing.evidence.rank import classify, is_dependency_error, rank_commits

log = logging.getLogger(__name__)


def fetch_logs(project: str, since: float, until: float | None = None) -> list[LogLine]:
    """Logs from every container of the compose project in [since, until]."""
    from maxxing.docker_client import list_containers

    def one(container) -> list[LogLine]:
        try:
            kwargs = {"since": datetime.fromtimestamp(since, UTC), "timestamps": True, "stream": False}
            if until:
                kwargs["until"] = datetime.fromtimestamp(until, UTC)
            raw = container.logs(**kwargs).decode("utf-8", "replace")
        except Exception as e:
            log.warning("log fetch failed for %s: %s", container.name, e)
            return []
        return logmod.parse_lines(raw.splitlines(), container.name)

    containers = list_containers(project)
    with ThreadPoolExecutor(max_workers=8) as pool:
        lines = [ln for chunk in pool.map(one, containers) for ln in chunk]
    return sorted(lines, key=lambda ln: ln.ts)


def probe_lb(lb_url: str, timeout: float = 3.0) -> dict:
    out: dict = {"url": lb_url}
    for path in ("/health", "/stats"):
        try:
            t0 = time.monotonic()
            r = httpx.get(lb_url.rstrip("/") + path, timeout=timeout)
            out[path] = {"status": r.status_code, "ms": round((time.monotonic() - t0) * 1000), "body": r.text[:300]}
        except httpx.HTTPError as e:
            out[path] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return out


def severity_and_impact(replicas: list[ContainerState], lb: dict, stats) -> tuple[str, str]:
    total = len(replicas) or 0
    bad = sum(1 for r in replicas if not r.running or r.health == "unhealthy" or r.oom_killed or r.restart_count)
    lb_ok = lb.get("/health", {}).get("status") == 200
    parts = []
    if total:
        parts.append(f"{bad}/{total} api replicas unhealthy, crashed or restarting")
    if stats.requests:
        parts.append(f"{stats.error_rate:.0%} of {stats.requests} requests returned 5xx in the window")
    parts.append("LB /health " + ("OK" if lb_ok else "FAILING"))
    if not lb_ok or (stats.requests and stats.error_rate >= 0.5) or (total and bad == total):
        sev = "SEV1"
    elif bad or stats.error_rate >= 0.05:
        sev = "SEV2"
    else:
        sev = "SEV3"
    return sev, "; ".join(parts)


def blame_frames(repo: GitRepo, rev: str, clusters, limit: int = 6) -> list[BlameHit]:
    hits: list[BlameHit] = []
    seen: set[tuple[str, int]] = set()
    for cl in clusters:
        if is_dependency_error(cl):
            continue
        for fr in cl.app_frames[:3]:
            key = (fr.repo_path, fr.line)
            if fr.line is None or key in seen:
                continue
            seen.add(key)
            hit = repo.blame_line(rev, fr.repo_path, fr.line)
            if hit:
                hits.append(hit)
            if len(hits) >= limit:
                return hits
    return hits


def analyze(
    bundle: EvidenceBundle,
    lines: Iterable[LogLine],
    repo: GitRepo | None,
    deploys: list[dict] | None = None,
    max_commits: int = 15,
) -> EvidenceBundle:
    """The Docker-free half of collection: logs -> clusters, git -> commits, ranking, classification."""
    lines = list(lines)
    map_path = repo.map_path if repo else None
    bundle.log_stats = logmod.compute_stats(lines)
    bundle.clusters = logmod.cluster_errors(lines, map_path)[:12]
    bundle.log_sample = logmod.sample_lines(lines)

    if repo is not None:
        try:
            shas = {c.sha for c in bundle.containers if c.sha}
            deployed = bundle.deployed_sha or (sorted(shas)[0] if len(shas) == 1 else None)
            bundle.deployed_sha = repo.resolve(deployed) or repo.head_sha()
            bundle.known_good_sha = repo.known_good_sha(deploys or [], bundle.deployed_sha)
            bundle.commits = repo.commits_between(bundle.known_good_sha, bundle.deployed_sha, max_commits)
            bundle.blame = blame_frames(repo, bundle.deployed_sha, bundle.clusters)
        except Exception as e:
            log.warning("git analysis failed: %s", e)
            bundle.collection_errors.append(f"git: {e}")

    bundle.category_hint, bundle.hints = classify(bundle.containers, bundle.dependencies, bundle.clusters, bundle.commits)
    bundle.candidates = rank_commits(
        bundle.commits, bundle.clusters, bundle.containers, bundle.blame, bundle.category_hint
    )
    bundle.severity_hint, bundle.impact_hint = severity_and_impact(bundle.containers, bundle.lb_health, bundle.log_stats)
    return bundle


def sync_repo(repo: GitRepo, shas: set[str], timeout_s: float) -> None:
    """Fetch until every deployed SHA exists locally (the push may land a few seconds late)."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            repo.fetch()
        except Exception as e:
            log.warning("git fetch failed: %s", e)
        missing = [s for s in shas if not repo.resolve(s)]
        if not missing or time.monotonic() >= deadline:
            if missing:
                log.warning("deployed sha %s not found after fetching for %.0fs", missing[0][:8], timeout_s)
            return
        time.sleep(3)


def collect_evidence(
    settings: Settings,
    trigger: list[str] | None = None,
    t0: float | None = None,
    incident_id: int | None = None,
) -> EvidenceBundle:
    """Blocking; run with asyncio.to_thread from async code."""
    now = time.time()
    t0 = t0 or now
    bundle = EvidenceBundle(
        incident_id=incident_id,
        window_start=t0 - settings.log_window_s,
        window_end=now,
        trigger=trigger or ["manual"],
        project=settings.compose_project,
        service=settings.api_service,
    )
    lines: list[LogLine] = []
    try:
        bundle.containers, bundle.dependencies = collect_containers(settings.compose_project, settings.api_service)
        lines = fetch_logs(settings.compose_project, bundle.window_start)
    except Exception as e:
        log.warning("docker evidence unavailable: %s", e)
        bundle.collection_errors.append(f"docker: {e}")

    bundle.lb_health = probe_lb(settings.lb_url, settings.health_timeout_s)
    repo = None
    try:
        repo = GitRepo(settings.repo_path)
        if settings.git_auto_fetch:
            sync_repo(repo, {c.sha for c in bundle.containers if c.sha}, settings.git_fetch_timeout_s)
    except Exception as e:
        bundle.collection_errors.append(f"git repo {settings.repo_path}: {e}")
    return analyze(bundle, lines, repo, load_deploys(settings.deploys_file), settings.max_commits)
