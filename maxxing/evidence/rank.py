"""Heuristic prior over candidate commits and a coarse outage classification.

This is deliberately simple and explainable: the LLM gets the ranking plus the reasons and
is expected to confirm or overrule it by reading the diffs.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from maxxing.evidence.models import BlameHit, Candidate, CommitInfo, ContainerState, ErrorCluster

CULPRIT_THRESHOLD = 3.0
DEPENDENCY_SERVICES = ("redis", "postgres", "db", "mysql", "mongo", "rabbitmq", "kafka")

_NON_RUNTIME = re.compile(
    r"(^|/)(docs?/|tests?/|__tests__/|\.github/)|\.(md|txt|rst|png|jpg|svg)$|(^|/)(LICENSE|CHANGELOG|\.gitignore|\.editorconfig)$|\.test\.[jt]s$|\.spec\.[jt]s$",
    re.I,
)
_CONN_ERR = re.compile(
    r"ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNRESET|connect(ion)? (refused|timeout)|Connection is closed"
    r"|getaddrinfo|client is closed|ClientClosedError|ClientOfflineError|offline queue|Socket closed unexpectedly",
    re.I,
)
_MEM_KW = {
    "unbounded Map/Set": re.compile(r"new (Map|Set|WeakRef)\("),
    "cache": re.compile(r"cache", re.I),
    "array push": re.compile(r"\.push\("),
    "setInterval": re.compile(r"setInterval\("),
    "global state": re.compile(r"\bglobal(This)?\.|module-level|^\+\s*(let|var|const) \w+ = (\[\]|\{\})"),
    "Buffer alloc": re.compile(r"Buffer\.(alloc|from)"),
}
_CPU_KW = {
    "sync crypto/hash": re.compile(r"(Sync\(|bcrypt|pbkdf2|scrypt|createHash)"),
    "busy loop": re.compile(r"while\s*\(|for\s*\(\s*let \w+ = 0; \w+ < \d{4,}"),
    "heavy JSON": re.compile(r"JSON\.(parse|stringify)"),
    "sort/regex in hot path": re.compile(r"\.sort\(|new RegExp\(|\.repeat\("),
}
_CFG_KW = {
    "redis connection settings": re.compile(r"REDIS_(HOST|PORT|URL)|redis://|createClient|new Redis", re.I),
    "host/port change": re.compile(r"\b(host|port|hostname)\b", re.I),
    "env default": re.compile(r"process\.env\.\w+\s*(\|\||\?\?)"),
}
_CFG_FILES = re.compile(r"(^|/)(\.env[^/]*|config[^/]*\.(js|json|ya?ml|ts)|docker-compose[^/]*\.ya?ml|nginx\.conf)$", re.I)


def is_runtime_file(path: str) -> bool:
    return not _NON_RUNTIME.search(path)


def changed_lines(commit: CommitInfo, runtime_only: bool = True) -> list[str]:
    """Added/removed lines of the diff, optionally only from runtime (non-doc/test) files."""
    out: list[str] = []
    current_ok = True
    for row in commit.diff.splitlines():
        if row.startswith("+++ "):
            path = row[4:].removeprefix("b/")
            current_ok = (not runtime_only) or (path != "/dev/null" and is_runtime_file(path))
        elif row.startswith("--- "):
            continue
        elif current_ok and row[:1] in "+-" and not row.startswith(("+++", "---")):
            out.append(row)
    return out


def _dep_down(deps: Iterable[ContainerState]) -> list[ContainerState]:
    return [d for d in deps if any(k in (d.service or d.name).lower() for k in DEPENDENCY_SERVICES) and not d.running]


def is_dependency_error(c: ErrorCluster) -> bool:
    return bool(_CONN_ERR.search(f"{c.err_type} {c.message}"))


def _has_conn_errors(clusters: Iterable[ErrorCluster]) -> bool:
    return any(_CONN_ERR.search(f"{c.err_type} {c.message} {c.stack or ''}") for c in clusters)


def _is_config_commit(c: CommitInfo) -> bool:
    if any(_CFG_FILES.search(f.path) for f in c.files):
        return True
    lines = changed_lines(c)
    return any(_CFG_KW["redis connection settings"].search(ln) for ln in lines)


_HEAP_OOM = re.compile(r"heap out of memory|Reached heap limit|ERR_MEMORY_ALLOCATION_FAILED", re.I)


def _memory_pressure(containers: Iterable[ContainerState], clusters: Iterable[ErrorCluster] = ()) -> bool:
    """Kernel OOM kill (137), memory near the cgroup limit, or V8 running out of heap (exit 134)."""
    if any(c.oom_killed or c.exit_code == 137 or (c.mem_pct or 0) >= 85 for c in containers):
        return True
    return any(_HEAP_OOM.search(f"{c.message} {c.stack or ''}") for c in clusters)


_TIMEOUT = re.compile(r"time(d)?[ -]?out|exceeded", re.I)


def _cpu_pressure(containers: Iterable[ContainerState], clusters: Iterable[ErrorCluster] = ()) -> bool:
    """Healthchecks timing out, a hot unhealthy replica, or the LB timing out (504) on upstreams."""
    for c in containers:
        if any(_TIMEOUT.search(h) for h in c.health_log):
            return True
        if c.health == "unhealthy" and c.running and (c.cpu_pct or 0) >= 40:
            return True
    return any(c.err_type == "HTTP5xx" and "504" in c.message and c.count >= 5 for c in clusters)


def classify(
    containers: list[ContainerState],
    deps: list[ContainerState],
    clusters: list[ErrorCluster],
    commits: list[CommitInfo],
) -> tuple[str | None, list[str]]:
    """Coarse category hint (code|resource|config|dependency|None) plus human-readable hints."""
    hints: list[str] = []
    down = _dep_down(deps)
    conn = _has_conn_errors(clusters)
    app_stack = any(c.app_frames and c.err_type not in ("HTTP5xx",) and not _CONN_ERR.search(c.message) for c in clusters)
    for d in down:
        hints.append(f"dependency container {d.name} is {d.status or 'down'} (exit={d.exit_code})")
    mem = _memory_pressure(containers, clusters)
    if mem:
        hints.append("memory pressure: OOMKilled / exit 137 / memory near limit on api replicas")
    if conn:
        hints.append("connection errors to a downstream dependency in logs")
    if app_stack:
        hints.append("application stack traces with frames in repo source files")
    cpu = _cpu_pressure(containers, clusters)
    if cpu:
        hints.append("health checks / upstream requests timing out (event loop or CPU saturation)")

    if down:
        return "dependency", hints
    if mem:
        return "resource", hints
    if conn and any(_is_config_commit(c) for c in commits):
        return "config", hints
    if app_stack:
        return "code", hints
    if cpu:
        return "resource", hints
    if conn:
        hints.append("no config change in the commit window: likely the dependency itself")
        return "dependency", hints
    return None, hints


def rank_commits(
    commits: list[CommitInfo],
    clusters: list[ErrorCluster],
    containers: list[ContainerState],
    blame: list[BlameHit] | None = None,
    category: str | None = None,
) -> list[Candidate]:
    """Score each commit in the window; highest first."""
    blame = blame or []
    mem = _memory_pressure(containers, clusters)
    cpu = _cpu_pressure(containers, clusters)
    if category == "resource" and not (mem or cpu):
        mem = cpu = True
    conn = _has_conn_errors(clusters)
    n = len(commits)
    out: list[Candidate] = []

    for idx, c in enumerate(commits):  # newest first
        score, reasons = 0.0, []
        runtime_files = [f.path for f in c.files if is_runtime_file(f.path)]
        if not runtime_files:
            score -= 3
            reasons.append("touches only docs/tests/non-runtime files")
        else:
            score += 0.5
        lines = changed_lines(c)

        # 1. Stack traces vs. the diff
        for cl in clusters:
            if is_dependency_error(cl):
                continue  # the throw site of a dependency error says nothing about the culprit
            weight = min(2.0, 1.0 + math.log10(max(cl.count, 1)))
            frames = cl.app_frames[:5]
            hit_file = hit_hunk = False
            for fr in frames:
                if fr.repo_path in {f.path for f in c.files}:
                    hit_file = True
                    for start, end in c.hunks.get(fr.repo_path, []):
                        if fr.line is not None and start - 2 <= fr.line <= end + 2:
                            hit_hunk = True
            if hit_hunk:
                score += 4 * weight
                reasons.append(f"diff touches the crashing line ({cl.err_type}: {cl.message[:60]})")
            elif hit_file:
                score += 1.5 * weight
                reasons.append(f"modifies a file in the stack trace of {cl.err_type}")
        blamed = {b.path + ":" + str(b.line) for b in blame if b.sha.startswith(c.sha[:7]) or c.sha.startswith(b.sha[:7])}
        if blamed:
            score += 6
            reasons.append(f"git blame on stack frame {sorted(blamed)[0]} points here")

        # 2. Resource signals vs. the kind of code added
        def kw(table: dict[str, re.Pattern], cap: float, label: str) -> None:
            nonlocal score
            found = [name for name, pat in table.items() if any(pat.search(ln) for ln in lines if ln.startswith("+"))]
            if found:
                score += min(cap, 3.0 + 1.5 * (len(found) - 1))
                reasons.append(f"{label}: adds {', '.join(found)}")

        if mem:
            kw(_MEM_KW, 6, "memory pressure")
        if cpu:
            kw(_CPU_KW, 6, "CPU saturation")
        if conn:
            found = [name for name, pat in _CFG_KW.items() if any(pat.search(ln) for ln in lines)]
            cfg_files = [f.path for f in c.files if _CFG_FILES.search(f.path)]
            if found or cfg_files:
                score += min(6, 2 * len(found) + 2 * len(cfg_files))
                reasons.append(f"connection errors + changes {', '.join(found + cfg_files)}")

        # 3. Recency tiebreak
        if n:
            score += 0.2 * (n - idx) / n
        out.append(Candidate(sha=c.sha, score=round(score, 2), reasons=reasons))

    return sorted(out, key=lambda x: -x.score)
