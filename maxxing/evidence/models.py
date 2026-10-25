"""Evidence bundle data model.

Everything the agent sees is captured here, so a bundle can be dumped to JSON and replayed
later (tests/fixtures) without Docker or the original repo.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class ContainerState(BaseModel):
    name: str
    id: str = ""
    service: str = ""
    image: str = ""
    sha: str | None = None  # GIT_SHA the container was built from
    status: str = ""  # running | exited | restarting | ...
    running: bool = False
    exit_code: int | None = None
    oom_killed: bool = False
    restart_count: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    health: str | None = None  # healthy | unhealthy | starting
    health_log: list[str] = Field(default_factory=list)
    mem_usage: int | None = None  # bytes
    mem_limit: int | None = None
    cpu_pct: float | None = None
    error: str | None = None

    @property
    def mem_pct(self) -> float | None:
        if self.mem_usage is None or not self.mem_limit:
            return None
        return round(100.0 * self.mem_usage / self.mem_limit, 1)

    def one_line(self) -> str:
        parts = [f"{self.name}: {self.status or '?'}"]
        if self.health:
            parts.append(f"health={self.health}")
        if self.exit_code not in (None, 0):
            parts.append(f"exit={self.exit_code}")
        if self.oom_killed:
            parts.append("OOMKilled=true")
        if self.restart_count:
            parts.append(f"restarts={self.restart_count}")
        if self.mem_pct is not None:
            parts.append(f"mem={self.mem_usage // (1024 * 1024)}MiB/{self.mem_limit // (1024 * 1024)}MiB ({self.mem_pct}%)")
        if self.cpu_pct is not None:
            parts.append(f"cpu={self.cpu_pct:.0f}%")
        if self.sha:
            parts.append(f"sha={self.sha[:8]}")
        return " ".join(parts)


class LogLine(BaseModel):
    ts: float  # epoch seconds
    container: str = ""
    replica: str | None = None
    level: int = 30
    msg: str = ""
    status: int | None = None  # HTTP status for request lines
    method: str | None = None
    url: str | None = None
    err_type: str | None = None
    err_message: str | None = None
    err_stack: str | None = None
    raw: str | None = None  # set only for non-JSON lines

    @property
    def is_error(self) -> bool:
        return self.level >= 50 or (self.status is not None and self.status >= 500)

    def render(self) -> str:
        t = datetime.fromtimestamp(self.ts, UTC).strftime("%H:%M:%S.%f")[:-3]
        who = self.replica or self.container
        if self.raw is not None:
            return f"{t} [{who}] {self.raw}"
        lvl = {30: "INFO", 40: "WARN", 50: "ERROR", 60: "FATAL"}.get(self.level, str(self.level))
        head = f"{t} [{who}] {lvl} {self.msg}"
        if self.status is not None:
            head += f" {self.method or ''} {self.url or ''} -> {self.status}"
        if self.err_type or self.err_message:
            head += f" | {self.err_type or 'Error'}: {self.err_message or ''}"
        return head


class StackFrame(BaseModel):
    function: str | None = None
    file: str
    line: int | None = None
    col: int | None = None
    repo_path: str | None = None  # file mapped into the repo (None = node internals / node_modules)


class ErrorCluster(BaseModel):
    signature: str
    err_type: str
    message: str
    count: int
    first_seen: float
    last_seen: float
    replicas: list[str] = Field(default_factory=list)
    levels: list[int] = Field(default_factory=list)
    stack: str | None = None
    frames: list[StackFrame] = Field(default_factory=list)

    @property
    def app_frames(self) -> list[StackFrame]:
        return [f for f in self.frames if f.repo_path]


class LogStats(BaseModel):
    lines: int = 0
    requests: int = 0
    status_2xx: int = 0
    status_4xx: int = 0
    status_5xx: int = 0
    errors: int = 0  # level >= 50
    fatals: int = 0  # level >= 60

    @property
    def error_rate(self) -> float:
        return self.status_5xx / self.requests if self.requests else 0.0


class FileChange(BaseModel):
    path: str
    insertions: int = 0
    deletions: int = 0


class CommitInfo(BaseModel):
    sha: str
    author_name: str
    author_email: str
    authored_at: str  # ISO 8601
    summary: str
    message: str = ""
    files: list[FileChange] = Field(default_factory=list)
    diff: str = ""
    diff_truncated: bool = False
    # new-side line ranges per file, from the diff hunks: {"src/server.js": [[10, 24], ...]}
    hunks: dict[str, list[tuple[int, int]]] = Field(default_factory=dict)

    @property
    def short(self) -> str:
        return self.sha[:8]

    def one_line(self) -> str:
        files = ", ".join(f"{f.path}(+{f.insertions}/-{f.deletions})" for f in self.files[:6])
        return f"{self.short} {self.author_name} <{self.author_email}> {self.authored_at} \"{self.summary}\" [{files}]"


class Candidate(BaseModel):
    sha: str
    score: float
    reasons: list[str] = Field(default_factory=list)

    @property
    def short(self) -> str:
        return self.sha[:8]


class BlameHit(BaseModel):
    path: str
    line: int
    sha: str
    author_email: str = ""
    code: str = ""


class EvidenceBundle(BaseModel):
    version: int = 1
    incident_id: int | None = None
    collected_at: datetime = Field(default_factory=utcnow)
    window_start: float = 0.0
    window_end: float = 0.0
    trigger: list[str] = Field(default_factory=list)
    project: str = "vip"
    service: str = "api"
    deployed_sha: str | None = None
    known_good_sha: str | None = None
    lb_health: dict = Field(default_factory=dict)
    containers: list[ContainerState] = Field(default_factory=list)  # api replicas
    dependencies: list[ContainerState] = Field(default_factory=list)  # redis, lb, ...
    log_stats: LogStats = Field(default_factory=LogStats)
    clusters: list[ErrorCluster] = Field(default_factory=list)
    log_sample: list[LogLine] = Field(default_factory=list)
    commits: list[CommitInfo] = Field(default_factory=list)
    blame: list[BlameHit] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    category_hint: str | None = None
    severity_hint: str = "SEV2"
    impact_hint: str = ""
    hints: list[str] = Field(default_factory=list)
    collection_errors: list[str] = Field(default_factory=list)

    def commit(self, sha_or_prefix: str | None) -> CommitInfo | None:
        if not sha_or_prefix:
            return None
        s = sha_or_prefix.strip().lower()
        if len(s) < 4:
            return None
        matches = [c for c in self.commits if c.sha.lower().startswith(s)]
        return matches[0] if len(matches) == 1 else None
