"""Git analysis: commits since the known-good deploy, diffs, hunk ranges and blame."""

from __future__ import annotations

import json
import logging
import re
from functools import cached_property
from pathlib import Path

import git

from maxxing.evidence.models import BlameHit, CommitInfo, FileChange

log = logging.getLogger(__name__)

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
MAX_DIFF_CHARS = 12_000


def parse_hunks(diff: str) -> dict[str, list[tuple[int, int]]]:
    """New-side line ranges [start, end] touched per file in a unified diff."""
    out: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for row in diff.splitlines():
        if row.startswith("+++ "):
            target = row[4:].strip()
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif current and (m := _HUNK.match(row)):
            start, length = int(m[1]), int(m[2] if m[2] is not None else 1)
            out.setdefault(current, []).append((start, start + max(length, 1) - 1))
    return out


def load_deploys(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


class GitRepo:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.repo = git.Repo(self.path)

    # -- basics ---------------------------------------------------------------
    def head_sha(self) -> str:
        return self.repo.head.commit.hexsha

    def resolve(self, rev: str | None) -> str | None:
        if not rev:
            return None
        try:
            return self.repo.commit(rev).hexsha
        except (git.BadName, ValueError, git.GitCommandError):
            return None

    def fetch(self) -> None:
        """Update every remote branch and tag (tags forced: scenarios move `known-good`)."""
        self.repo.git.fetch("--quiet", "--force", "--tags", "origin", "+refs/heads/*:refs/remotes/origin/*")

    def is_ancestor(self, a: str, b: str) -> bool:
        try:
            return self.repo.is_ancestor(a, b)
        except git.GitCommandError:
            return False

    @cached_property
    def _head_files(self) -> set[str]:
        return self.ls_files("HEAD")

    def ls_files(self, rev: str = "HEAD") -> set[str]:
        try:
            return set(self.repo.git.ls_tree("-r", "--name-only", rev).splitlines())
        except git.GitCommandError:
            return set()

    def map_path(self, container_path: str) -> str | None:
        """Map `/app/src/server.js` (path inside the container) onto `src/server.js` in the repo."""
        return map_container_path(container_path, self._head_files)

    # -- deploy history -------------------------------------------------------
    def known_good_sha(self, deploys: list[dict], deployed_sha: str | None) -> str | None:
        """Latest deploy marked healthy that differs from (and is an ancestor of) what is deployed.

        A bad deploy can briefly pass health checks (e.g. a slow memory leak), so we skip
        healthy entries for the currently deployed SHA. Falls back to the `known-good` tag.
        """
        target = self.resolve(deployed_sha) or self.head_sha()
        for entry in reversed(deploys):
            if entry.get("status") != "healthy":
                continue
            sha = self.resolve(entry.get("sha"))
            if sha and sha != target and self.is_ancestor(sha, target):
                return sha
        return self.resolve("known-good")

    # -- commits --------------------------------------------------------------
    def commit_info(self, sha: str, max_diff: int = MAX_DIFF_CHARS) -> CommitInfo:
        c = self.repo.commit(sha)
        diff = self.repo.git.show("--format=", "--patch", "--no-color", "-U3", c.hexsha)
        files = [
            FileChange(path=p, insertions=s.get("insertions", 0), deletions=s.get("deletions", 0))
            for p, s in c.stats.files.items()
        ]
        return CommitInfo(
            sha=c.hexsha,
            author_name=c.author.name or "",
            author_email=(c.author.email or "").lower(),
            authored_at=c.authored_datetime.isoformat(),
            summary=c.summary if isinstance(c.summary, str) else c.summary.decode(),
            message=c.message if isinstance(c.message, str) else c.message.decode(),
            files=files,
            diff=diff[:max_diff],
            diff_truncated=len(diff) > max_diff,
            hunks=parse_hunks(diff),
        )

    def commits_between(self, good: str | None, head: str | None = None, limit: int = 15) -> list[CommitInfo]:
        """Commits in (good, head], newest first. Without a good SHA, the last `limit` commits."""
        head = self.resolve(head) or self.head_sha()
        rev = f"{good}..{head}" if good else head
        shas = [c.hexsha for c in self.repo.iter_commits(rev, max_count=limit, no_merges=True)]
        return [self.commit_info(s) for s in shas]

    def show_commit(self, sha: str, max_chars: int = MAX_DIFF_CHARS) -> str:
        out = self.repo.git.show("--stat", "--patch", "--no-color", sha)
        return out[:max_chars]

    def read_file(self, rev: str, path: str) -> str:
        return self.repo.git.show(f"{rev}:{path}")

    def grep(self, pattern: str, rev: str = "HEAD", max_lines: int = 80) -> str:
        try:
            out = self.repo.git.grep("-n", "-I", "-E", "-e", pattern, rev)
        except git.GitCommandError as e:
            return "no matches" if e.status == 1 else f"grep failed: {e.stderr.strip()}"
        lines = [ln.removeprefix(f"{rev}:") for ln in out.splitlines()]
        more = f"\n... ({len(lines) - max_lines} more)" if len(lines) > max_lines else ""
        return "\n".join(lines[:max_lines]) + more

    def blame_line(self, rev: str, path: str, line: int) -> BlameHit | None:
        try:
            out = self.repo.git.blame("--porcelain", "-L", f"{line},{line}", rev, "--", path)
        except git.GitCommandError as e:
            log.debug("blame failed %s:%s@%s: %s", path, line, rev, e)
            return None
        rows = out.splitlines()
        if not rows:
            return None
        sha = rows[0].split()[0]
        email = next((r.split(" ", 1)[1].strip("<>") for r in rows if r.startswith("author-mail ")), "")
        code = next((r[1:] for r in rows if r.startswith("\t")), "")
        return BlameHit(path=path, line=line, sha=sha, author_email=email.lower(), code=code.strip())

    def blame_range(self, rev: str, path: str, start: int, end: int) -> str:
        out = self.repo.git.blame("-s", "-L", f"{start},{end}", rev, "--", path)
        return out


def map_container_path(container_path: str, repo_files: set[str]) -> str | None:
    """Longest path suffix of `container_path` that exists in the repo."""
    parts = container_path.strip("/").split("/")
    for i in range(len(parts)):
        candidate = "/".join(parts[i:])
        if candidate in repo_files:
            return candidate
    return None
