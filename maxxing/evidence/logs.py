"""Log parsing (pino JSON + nginx access lines) and error clustering by stack signature."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime

from maxxing.evidence.models import ErrorCluster, LogLine, LogStats, StackFrame

_DOCKER_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})\s")
_NGINX_STATUS = re.compile(r'"(?P<method>[A-Z]+) (?P<url>\S+) HTTP/[\d.]+" (?P<status>\d{3}) ')
# "    at handler (/app/src/server.js:42:17)" | "    at /app/src/server.js:42:17" | "    at async /app/..."
_FRAME = re.compile(r"^\s*at\s+(?:async\s+)?(?:(?P<fn>.+?)\s+\()?(?P<file>[^()\s]+?):(?P<line>\d+):(?P<col>\d+)\)?\s*$")
_VOLATILE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<id>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
]


def _parse_docker_ts(prefix: str) -> float | None:
    m = _DOCKER_TS.match(prefix)
    if not m:
        return None
    base, frac, tz = m.groups()
    frac = (frac or ".0")[:7]  # python handles up to microseconds
    tz = "+00:00" if tz == "Z" else tz
    try:
        return datetime.fromisoformat(f"{base}{frac}{tz}").timestamp()
    except ValueError:
        return None


def _get(d: dict, dotted: str):
    """Read `a.b.c` from either nested dicts or a flat dotted key."""
    if dotted in d:
        return d[dotted]
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def parse_line(raw: str, container: str = "", default_ts: float | None = None) -> LogLine | None:
    """Parse one log line. Accepts an optional docker `timestamps=True` prefix."""
    line = raw.rstrip("\n")
    if not line.strip():
        return None
    ts = default_ts
    m = _DOCKER_TS.match(line)
    if m:
        ts = _parse_docker_ts(line) or ts
        line = line[m.end():]

    if line.lstrip().startswith("{"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            d = None
        if isinstance(d, dict):
            t = d.get("time")
            if isinstance(t, (int, float)):
                ts = t / 1000.0 if t > 1e11 else float(t)
            err = d.get("err") if isinstance(d.get("err"), dict) else {}
            status = _get(d, "res.statusCode")
            try:
                level = int(d.get("level", 30))
            except (TypeError, ValueError):
                level = 30
            return LogLine(
                ts=ts or 0.0,
                container=container,
                replica=d.get("replica") or d.get("hostname"),
                level=level,
                msg=str(d.get("msg", "")),
                status=int(status) if isinstance(status, (int, float, str)) and str(status).isdigit() else None,
                method=_get(d, "req.method"),
                url=_get(d, "req.url"),
                err_type=err.get("type") or d.get("err.type"),
                err_message=err.get("message") or d.get("err.message"),
                err_stack=err.get("stack") or d.get("err.stack"),
            )

    # Non-JSON: nginx access log, node's own crash output, etc.
    level = 30
    status = method = url = None
    nm = _NGINX_STATUS.search(line)
    if nm:
        status, method, url = int(nm["status"]), nm["method"], nm["url"]
    elif re.search(r"\b(FATAL|Fatal|Uncaught|Error:|ECONNREFUSED|heap out of memory|\[error\]|\[crit\]|\[emerg\])", line):
        level = 60 if re.search(r"FATAL|heap out of memory|Uncaught|\[crit\]|\[emerg\]", line) else 50
    return LogLine(ts=ts or 0.0, container=container, level=level, raw=line, status=status, method=method, url=url)


def parse_lines(lines: Iterable[str], container: str = "") -> list[LogLine]:
    out: list[LogLine] = []
    for raw in lines:
        parsed = parse_line(raw, container)
        if parsed is not None:
            out.append(parsed)
    return out


def compute_stats(lines: Iterable[LogLine]) -> LogStats:
    """Counts per line. Requests come from the LB access log when present (what users saw,
    and it avoids double counting the same request in the api log)."""
    lines = list(lines)
    lb_only = any(ln.raw is not None and ln.status is not None for ln in lines)
    s = LogStats()
    for ln in lines:
        s.lines += 1
        if ln.status is not None and (ln.raw is not None or not lb_only):
            s.requests += 1
            if 200 <= ln.status < 400:
                s.status_2xx += 1
            elif 400 <= ln.status < 500:
                s.status_4xx += 1
            elif ln.status >= 500:
                s.status_5xx += 1
        if ln.level >= 50:
            s.errors += 1
        if ln.level >= 60:
            s.fatals += 1
    return s


def parse_stack(stack: str | None, map_path: Callable[[str], str | None] | None = None) -> list[StackFrame]:
    frames: list[StackFrame] = []
    for row in (stack or "").splitlines()[1:]:
        m = _FRAME.match(row)
        if not m:
            continue
        file = m["file"].removeprefix("file://")
        repo_path = None
        if map_path and not file.startswith("node:") and "node_modules" not in file:
            repo_path = map_path(file)
        frames.append(
            StackFrame(function=m["fn"], file=file, line=int(m["line"]), col=int(m["col"]), repo_path=repo_path)
        )
    return frames


def normalize_message(msg: str) -> str:
    for pat, repl in _VOLATILE:
        msg = pat.sub(repl, msg)
    return msg.strip()[:200]


def _bare_5xx(line: LogLine) -> bool:
    return bool(line.status and line.status >= 500 and not line.err_message and not line.err_type)


def describe(line: LogLine) -> str:
    if _bare_5xx(line):
        return f"HTTP {line.status} on {line.method or '?'} ({'lb' if line.raw is not None else 'app'})"
    return line.err_message or line.msg or line.raw or ""


def signature(line: LogLine, frames: list[StackFrame]) -> str:
    """Stable id for "the same error": type + normalized message + top app frames (file:fn)."""
    msg = normalize_message(describe(line))
    etype = line.err_type or ("HTTP5xx" if _bare_5xx(line) else "Error")
    top = [f"{(f.repo_path or f.file).rsplit('/', 1)[-1]}:{f.function or '?'}" for f in frames if "node:" not in f.file][:3]
    return f"{etype}|{msg}|{'>'.join(top)}"


def cluster_errors(lines: Iterable[LogLine], map_path: Callable[[str], str | None] | None = None) -> list[ErrorCluster]:
    """Group error-level lines (and bare 5xx responses) by signature, most frequent first."""
    clusters: dict[str, ErrorCluster] = {}
    for ln in lines:
        if not ln.is_error:
            continue
        frames = parse_stack(ln.err_stack, map_path)
        sig = signature(ln, frames)
        who = ln.replica or ln.container
        c = clusters.get(sig)
        if c is None:
            clusters[sig] = ErrorCluster(
                signature=sig,
                err_type=sig.split("|", 1)[0],
                message=describe(ln)[:500],
                count=1,
                first_seen=ln.ts,
                last_seen=ln.ts,
                replicas=[who] if who else [],
                levels=[ln.level],
                stack=ln.err_stack[:3000] if ln.err_stack else None,
                frames=frames,
            )
            continue
        c.count += 1
        c.first_seen = min(c.first_seen, ln.ts)
        c.last_seen = max(c.last_seen, ln.ts)
        if who and who not in c.replicas:
            c.replicas.append(who)
        if ln.level not in c.levels:
            c.levels.append(ln.level)
        if c.stack is None and ln.err_stack:
            c.stack, c.frames = ln.err_stack[:3000], frames
    return sorted(clusters.values(), key=lambda c: (-max(c.levels), -c.count))


def sample_lines(lines: list[LogLine], limit: int = 60) -> list[LogLine]:
    """Keep the most informative lines: every distinct error first, then recent context."""
    errors = [ln for ln in lines if ln.level >= 50]
    seen: set[str] = set()
    picked: list[LogLine] = []
    for ln in reversed(errors):
        key = normalize_message(f"{ln.err_type}{ln.err_message}{ln.msg}{ln.raw}")
        if key in seen:
            continue
        seen.add(key)
        picked.append(ln)
        if len(picked) >= limit // 2:
            break
    picked_ids = {id(ln) for ln in picked}
    rest = [ln for ln in lines if id(ln) not in picked_ids and (ln.level >= 40 or (ln.status or 0) >= 500)]
    picked.extend(rest[-(limit - len(picked)):] if limit > len(picked) else [])
    return sorted(picked, key=lambda ln: ln.ts)[-limit:]
