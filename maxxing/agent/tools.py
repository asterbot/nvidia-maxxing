"""Tools the RCA agent can call. They read from the evidence bundle and, when available, the repo."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from maxxing.evidence.git import GitRepo
from maxxing.evidence.models import EvidenceBundle

log = logging.getLogger(__name__)

MAX_TOOL_CHARS = 6000


def truncate(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.75)]
    tail = text[-int(limit * 0.2):]
    return f"{head}\n... [truncated {len(text) - len(head) - len(tail)} chars] ...\n{tail}"


def _fn(name: str, description: str, props: dict[str, Any], required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required or []},
        },
    }


TOOL_SPECS: list[dict] = [
    _fn("get_logs", "Log lines from the incident window. Filter by level/replica/substring.", {
        "min_level": {"type": "integer", "description": "30=info 40=warn 50=error 60=fatal", "default": 50},
        "contains": {"type": "string", "description": "case-insensitive substring filter"},
        "replica": {"type": "string"},
        "limit": {"type": "integer", "default": 40},
    }),
    _fn("get_container_state", "docker inspect/stats summary for api replicas and dependencies (OOMKilled, exit code, restarts, memory vs limit, health).", {
        "name": {"type": "string", "description": "optional container name filter"},
    }),
    _fn("list_commits", "Commits deployed since the last known-good deploy, newest first, with heuristic scores.", {}),
    _fn("show_commit", "Full diff of one commit.", {"sha": {"type": "string"}}, ["sha"]),
    _fn("read_file", "Read a file from the repo at a revision (default: deployed sha).", {
        "path": {"type": "string"},
        "sha": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
    }, ["path"]),
    _fn("grep_repo", "git grep (extended regex) at the deployed revision.", {"pattern": {"type": "string"}}, ["pattern"]),
    _fn("blame", "git blame for a line range of a file at the deployed revision.", {
        "path": {"type": "string"},
        "line": {"type": "integer"},
        "context": {"type": "integer", "default": 3},
    }, ["path", "line"]),
]


class ToolBox:
    def __init__(self, bundle: EvidenceBundle, repo: GitRepo | None = None):
        self.bundle = bundle
        self.repo = repo
        self.calls: list[tuple[str, dict]] = []

    @property
    def rev(self) -> str:
        return self.bundle.deployed_sha or "HEAD"

    def call(self, name: str, arguments: str | dict | None) -> str:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else (arguments or {})
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            return "error: arguments were not valid JSON"
        self.calls.append((name, args))
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"error: unknown tool {name!r}. Available: {', '.join(s['function']['name'] for s in TOOL_SPECS)}"
        try:
            return truncate(fn(**args))
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}"
        except Exception as e:
            log.debug("tool %s failed", name, exc_info=True)
            return f"error: {name} failed: {e}"

    # -- tools ---------------------------------------------------------------------
    def t_get_logs(self, min_level: int = 50, contains: str | None = None, replica: str | None = None, limit: int = 40, **_) -> str:
        rows = [ln for ln in self.bundle.log_sample if ln.level >= min_level or (ln.status or 0) >= 500]
        if contains:
            rows = [ln for ln in rows if contains.lower() in (ln.model_dump_json()).lower()]
        if replica:
            rows = [ln for ln in rows if replica in (ln.replica or ln.container)]
        out = [ln.render() for ln in rows[-max(1, min(limit, 100)):]]
        clusters = "\n".join(
            f"- x{c.count} {c.err_type}: {c.message[:160]} (replicas: {', '.join(c.replicas[:4])})" for c in self.bundle.clusters[:8]
        )
        stacks = "\n\n".join(f"{c.err_type} stack:\n{c.stack}" for c in self.bundle.clusters[:3] if c.stack)
        return f"error clusters:\n{clusters or '(none)'}\n\nlines:\n" + ("\n".join(out) or "(no matching lines)") + (f"\n\n{stacks}" if stacks else "")

    def t_get_container_state(self, name: str | None = None, **_) -> str:
        rows = []
        for c in self.bundle.containers + self.bundle.dependencies:
            if name and name not in c.name:
                continue
            rows.append(c.one_line() + (f"\n    healthcheck: {' | '.join(c.health_log)}" if c.health_log else ""))
        lb = json.dumps(self.bundle.lb_health)[:800]
        return "\n".join(rows or ["(no containers matched)"]) + f"\nLB probe: {lb}"

    def t_list_commits(self, **_) -> str:
        scores = {c.sha: c for c in self.bundle.candidates}
        rows = []
        for c in self.bundle.commits:
            cand = scores.get(c.sha)
            why = f" score={cand.score} ({'; '.join(cand.reasons)})" if cand else ""
            rows.append(c.one_line() + why)
        head = f"known-good={self.bundle.known_good_sha or '?'} deployed={self.bundle.deployed_sha or '?'}\n"
        return head + ("\n".join(rows) or "(no commits in window)")

    def t_show_commit(self, sha: str, **_) -> str:
        c = self.bundle.commit(sha)
        if c is None:
            return f"error: {sha!r} is not one of the candidate commits; call list_commits"
        if self.repo is not None and c.diff_truncated:
            try:
                return self.repo.show_commit(c.sha, max_chars=20_000)
            except Exception:
                pass
        return f"commit {c.sha}\nAuthor: {c.author_name} <{c.author_email}>\nDate: {c.authored_at}\n\n{c.message.strip()}\n\n{c.diff}"

    def t_read_file(self, path: str, sha: str | None = None, start_line: int | None = None, end_line: int | None = None, **_) -> str:
        if self.repo is None:
            return "error: repository not available in this replay; use show_commit for diffs"
        text = self.repo.read_file(sha or self.rev, path.lstrip("/"))
        lines = text.splitlines()
        s = max(1, start_line or 1)
        e = min(len(lines), end_line or len(lines))
        return "\n".join(f"{i:4d}  {lines[i - 1]}" for i in range(s, e + 1))

    def t_grep_repo(self, pattern: str, **_) -> str:
        if self.repo is not None:
            return self.repo.grep(pattern, self.rev)
        # replay mode: grep the diffs we have
        rx = re.compile(pattern)
        hits = [f"{c.short}: {ln}" for c in self.bundle.commits for ln in c.diff.splitlines() if rx.search(ln)]
        return "\n".join(hits[:60]) or "no matches (replay mode: searched commit diffs only)"

    def t_blame(self, path: str, line: int, context: int = 3, **_) -> str:
        if self.repo is None:
            hits = [b for b in self.bundle.blame if b.path == path and abs(b.line - line) <= context]
            return "\n".join(f"{b.path}:{b.line} {b.sha[:8]} <{b.author_email}> {b.code}" for b in hits) or "no blame data in replay"
        return self.repo.blame_range(self.rev, path.lstrip("/"), max(1, line - context), line + context)
