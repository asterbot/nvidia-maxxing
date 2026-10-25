"""Regenerate the hand-written replay fixtures in this directory.

    uv run python tests/fixtures/build_fixtures.py

Each fixture mirrors one very-important-project scenario: the same code edits are committed
(by the same fake authors, with fixed dates so SHAs are stable) on top of the known-good
sources, and hand-written log lines / container states in the exact shapes the real service
emits (pino JSON, nginx access log, docker inspect) are pushed through the real evidence
pipeline (`maxxing.evidence.collector.analyze`). Real captures from `maxxing triage --now --dump`
or `maxxing eval --dump-dir` on the GB10 can be dropped in next to these.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from maxxing.evidence.collector import analyze  # noqa: E402
from maxxing.evidence.git import GitRepo  # noqa: E402
from maxxing.evidence.logs import parse_lines  # noqa: E402
from maxxing.evidence.models import ContainerState, EvidenceBundle  # noqa: E402

VIP = HERE.parent.parent.parent / "very-important-project"
ARJUN, MARCUS, DANA = "Arjun <arjun@vip.dev>", "Marcus Lee <marcus@vip.dev>", "Dana Ortiz <dana@vip.dev>"
T0 = datetime(2026, 10, 25, 19, 20, 0, tzinfo=UTC).timestamp()
MiB = 1024 * 1024
REPLICAS = ["3f2a9c1d7b44", "8e1d02aa9f10", "c47b5e6d2a83"]


# ----------------------------------------------------------------------------- git helpers
class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.clock = T0 - 3 * 3600

    def git(self, *args: str, env: dict | None = None) -> str:
        return subprocess.check_output(["git", *args], cwd=self.path, env={**os.environ, **(env or {})}, text=True).strip()

    def commit(self, author: str, msg: str) -> str:
        self.clock += 600
        date = datetime.fromtimestamp(self.clock, UTC).isoformat()
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
               "GIT_COMMITTER_NAME": "ci", "GIT_COMMITTER_EMAIL": "ci@vip.dev"}
        self.git("add", "-A")
        self.git("-c", "user.name=ci", "-c", "user.email=ci@vip.dev", "commit", "-q", f"--author={author}", "-m", msg, env=env)
        return self.git("rev-parse", "HEAD")

    def edit(self, rel: str, old: str, new: str) -> None:
        p = self.path / rel
        src = p.read_text()
        assert src.count(old) == 1, f"edit target not unique in {rel}: {old[:60]!r}"
        p.write_text(src.replace(old, new))

    def append(self, rel: str, text: str) -> None:
        with open(self.path / rel, "a") as f:
            f.write(text + "\n")

    def line_of(self, rel: str, needle: str) -> int:
        for i, line in enumerate((self.path / rel).read_text().splitlines(), 1):
            if needle in line:
                return i
        raise AssertionError(needle)


def base_repo(tmp: Path) -> Repo:
    for rel in ("src/server.js", "src/config.js", "package.json", "README.md", "public/index.html", "docker-compose.yml"):
        dst = tmp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(VIP / rel, dst)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
    r = Repo(tmp)
    r.commit("Dana Ortiz <dana@vip.dev>", "initial commit")
    r.git("tag", "known-good")
    return r


# noise commits (same as scenarios/lib.sh)
def noise_readme_troubleshooting(r: Repo, who: str) -> str:
    r.append("README.md", "\n## Troubleshooting\n\nIf the stats panel is red, check `docker compose -p vip ps` and `docker compose -p vip logs api`.")
    return r.commit(who, "docs: add troubleshooting section to README")


def noise_readme_ports(r: Repo, who: str) -> str:
    r.append("README.md", "\n> Note: the API replicas listen on port 3000 internally; only the LB is published (8080).")
    return r.commit(who, "docs: clarify which ports are exposed")


def noise_log_wording(r: Repo, who: str) -> str:
    r.edit("src/server.js", "logger.info({ port: config.port }, 'api listening');", "logger.info({ port: config.port }, 'api server listening for requests');")
    return r.commit(who, "chore: clearer startup log message")


def noise_footer(r: Repo, who: str) -> str:
    r.edit("public/index.html", "<footer>very-important-project &middot; do not break</footer>",
           "<footer>very-important-project &middot; please do not break &middot; on-call: #incidents</footer>")
    return r.commit(who, "ui: point footer at #incidents channel")


def noise_version(r: Repo, who: str) -> str:
    r.edit("package.json", '"version": "1.0.0",', '"version": "1.0.1",')
    return r.commit(who, "chore: bump version to 1.0.1")


def noise_placeholder(r: Repo, who: str) -> str:
    r.edit("public/index.html", 'placeholder="https://example.com/some/long/path"', 'placeholder="Paste a long URL to shorten"')
    return r.commit(who, "ui: friendlier placeholder text on shorten form")


# ----------------------------------------------------------------------------- log helpers
def dts(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


def pino(t: float, replica: str, sha: str, level: int, msg: str, **extra) -> str:
    d = {"level": level, "time": int(t * 1000), "pid": 1, "hostname": replica, "replica": replica, "sha": sha, **extra, "msg": msg}
    return f"{dts(t)} {json.dumps(d)}"


def req(t: float, replica: str, sha: str, status: int, url: str, method: str = "GET", rid: int = 1, ms: int = 3, err: dict | None = None) -> str:
    level = 50 if status >= 500 else 40 if status >= 400 else 30
    extra = {"req": {"id": rid, "method": method, "url": url}, "res": {"statusCode": status}, "responseTime": ms}
    if err:
        extra["err"] = err
    return pino(t, replica, sha, level, "request errored" if status >= 500 else "request completed", **extra)


def nginx(t: float, status: int, url: str, method: str = "GET") -> str:
    stamp = datetime.fromtimestamp(t, UTC).strftime("%d/%b/%Y:%H:%M:%S +0000")
    return f'{dts(t)} 172.19.0.7 - - [{stamp}] "{method} {url} HTTP/1.1" {status} {157 if status >= 500 else 23} "-" "vip-traffic/1.0"'


def healthy_traffic(sha: str, start: float, end: float, rps: float = 6.0, rid0: int = 1) -> tuple[list[str], list[str]]:
    """(api lines, lb lines) for normal traffic: mostly 302/201 with ~5% 404."""
    api, lb = [], []
    t, i = start, rid0
    while t < end:
        rep = REPLICAS[i % 3]
        kind = i % 20
        if kind == 0:
            url, status = f"/zz{i:05d}", 404
        elif kind in (1, 2, 3, 4):
            url, status = "/links", 201
        else:
            url, status = f"/k{i % 500:04d}x", 302
        method = "POST" if status == 201 else "GET"
        api.append(req(t, rep, sha, status, url, method, rid=i))
        lb.append(nginx(t + 0.002, status, url, method))
        t += 1 / rps
        i += 1
    return api, lb


def container(name: str, sha: str, **kw) -> ContainerState:
    base = dict(name=name, id=name[-12:].replace("-", "0").rjust(12, "a"), service="api", image=f"vip-api:{sha}", sha=sha,
                status="running", running=True, exit_code=0, health="healthy", mem_usage=78 * MiB, mem_limit=256 * MiB,
                cpu_pct=7.5, started_at=dts(T0 - 900))
    base.update(kw)
    return ContainerState(**base)


def deps(redis_running: bool = True) -> list[ContainerState]:
    redis = ContainerState(name="vip-redis-1", id="b0d1e2f3a4c5", service="redis", image="redis:7-alpine",
                           status="running" if redis_running else "exited", running=redis_running,
                           exit_code=0, health=None, mem_usage=9 * MiB if redis_running else None,
                           mem_limit=None, finished_at=None if redis_running else dts(T0 - 4))
    lb = ContainerState(name="vip-lb-1", id="aa11bb22cc33", service="lb", image="nginx:1.27-alpine", status="running",
                        running=True, exit_code=0, mem_usage=4 * MiB)
    return [redis, lb]


def build(name: str, repo: Repo, culprit: str | None, category: str | None, raw_lines: list[str], containers: list[ContainerState],
          dependencies: list[ContainerState], trigger: list[str], lb_health: dict) -> None:
    head = repo.git("rev-parse", "HEAD")
    good = repo.git("rev-parse", "known-good")
    bundle = EvidenceBundle(
        incident_id=1, collected_at=datetime.fromtimestamp(T0 + 8, UTC), window_start=T0 - 180, window_end=T0 + 8,
        trigger=trigger, deployed_sha=head, lb_health=lb_health, containers=containers, dependencies=dependencies,
    )
    deploys = [{"sha": good, "ts": "2026-10-25T18:40:12Z", "status": "healthy"},
               {"sha": head, "ts": "2026-10-25T19:18:41Z", "status": "deployed"}]
    lines = sorted(parse_lines(raw_lines), key=lambda ln: ln.ts)
    bundle = analyze(bundle, lines, GitRepo(repo.path), deploys)
    (HERE / f"{name}.json").write_text(bundle.model_dump_json(indent=2) + "\n")
    expected = {"id": name, "name": name, "expected_sha": culprit, "expected_category": category,
                "expect_incident": True, "started_at": "2026-10-25T19:18:30Z"}
    (HERE / f"{name}.expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    top = bundle.candidates[0] if bundle.candidates else None
    print(f"{name}: category_hint={bundle.category_hint} top={top.short if top else None} score={top.score if top else None} "
          f"expected={culprit[:8] if culprit else None}/{category} commits={len(bundle.commits)} clusters={len(bundle.clusters)}")


# ----------------------------------------------------------------------------- scenarios
def memory_leak(tmp: Path) -> None:
    r = base_repo(tmp)
    noise_readme_troubleshooting(r, MARCUS)
    r.edit("src/server.js", """app.get('/:code', async (req, res, next) => {
  try {
    const raw = await redis.get(`link:${req.params.code}`);
    if (!raw) return res.status(404).json({ error: 'not found' });
    const link = JSON.parse(raw);
    await redis.incr(`hits:${req.params.code}`);
    res.redirect(302, link.url);""", """// Hot links get hammered by campaigns; keep resolved entries in-process so
// repeat visits skip the redis round-trip. Each entry reserves a preview
// buffer for the upcoming link-unfurl feature (placeholder: the raw record).
const PREVIEW_BYTES = 1024 * 1024;
const linkCache = new Map();

app.get('/:code', async (req, res, next) => {
  try {
    const cached = linkCache.get(req.originalUrl);
    if (cached) {
      cached.hits++;
      return res.redirect(302, cached.url);
    }
    const raw = await redis.get(`link:${req.params.code}`);
    if (!raw) return res.status(404).json({ error: 'not found' });
    const link = JSON.parse(raw);
    linkCache.set(req.originalUrl, { url: link.url, hits: 1, preview: Buffer.alloc(PREVIEW_BYTES, raw) });
    await redis.incr(`hits:${req.params.code}`);
    res.redirect(302, link.url);""")
    culprit = r.commit(ARJUN, "perf: cache resolved links in memory")
    noise_footer(r, DANA)
    sha = r.git("rev-parse", "HEAD")

    api, lb = healthy_traffic(sha, T0 - 120, T0 - 6)
    # replicas die one after another (no log line: the kernel kills them), LB starts 502ing
    for i, t in enumerate([T0 - 6, T0 - 3, T0 - 1]):
        api.append(pino(t + 2.5, REPLICAS[i], sha, 30, "api listening", port=3000))
    for k in range(40):
        t = T0 - 6 + k * 0.35
        lb.append(nginx(t, 502 if k % 4 else 302, f"/k{k:04d}x"))
    lb.append(f"{dts(T0 - 5.5)} 2026/10/25 19:19:54 [error] 29#29: *8812 connect() failed (111: Connection refused) while connecting to upstream, client: 172.19.0.7, server: , request: \"GET /k0012x HTTP/1.1\", upstream: \"http://172.19.0.4:3000/k0012x\"")
    containers = [
        container("vip-api-1", sha, status="running", exit_code=137, oom_killed=True, restart_count=3, health="starting", mem_usage=61 * MiB, cpu_pct=31.0),
        container("vip-api-2", sha, status="restarting", running=False, exit_code=137, oom_killed=True, restart_count=2, health="unhealthy", mem_usage=None),
        container("vip-api-3", sha, status="running", exit_code=137, oom_killed=True, restart_count=2, health="healthy", mem_usage=241 * MiB, cpu_pct=44.0),
    ]
    build("1-memory-leak", r, culprit, "resource", api + lb, containers, deps(), [
        "oom: vip-api-1 OOM-killed", "crash_loop: 3 container deaths in 60s", "container_oom vip-api-1 api",
    ], {"/health": {"status": 502, "ms": 4, "body": "<html>502 Bad Gateway</html>"}})


def null_deref(tmp: Path) -> None:
    r = base_repo(tmp)
    noise_version(r, DANA)
    noise_log_wording(r, ARJUN)
    r.edit("src/server.js", """app.get('/:code', async (req, res, next) => {
  try {
    const raw = await redis.get(`link:${req.params.code}`);
    if (!raw) return res.status(404).json({ error: 'not found' });
    const link = JSON.parse(raw);
    await redis.incr(`hits:${req.params.code}`);
    res.redirect(302, link.url);
  } catch (err) {
    next(err);
  }
});""", """async function resolveLink(code) {
  const raw = await redis.get(`link:${code}`);
  return JSON.parse(raw);
}

app.get('/:code', async (req, res) => {
  const link = await resolveLink(req.params.code);
  await redis.incr(`hits:${req.params.code}`);
  res.redirect(302, link.url);
});""")
    culprit = r.commit(MARCUS, "refactor: extract resolveLink helper from redirect route")
    noise_readme_ports(r, DANA)
    sha = r.git("rev-parse", "HEAD")
    line = r.line_of("src/server.js", "res.redirect(302, link.url);")
    stack = (
        "TypeError: Cannot read properties of null (reading 'url')\n"
        f"    at /app/src/server.js:{line}:31\n"
        "    at process.processTicksAndRejections (node:internal/process/task_queues:105:5)"
    )
    err = {"type": "TypeError", "message": "Cannot read properties of null (reading 'url')", "stack": stack}
    api, lb = healthy_traffic(sha, T0 - 120, T0 - 20)
    # every request for an unknown code takes a replica down
    for k, t in enumerate([T0 - 19, T0 - 15, T0 - 12, T0 - 9, T0 - 6, T0 - 4, T0 - 2]):
        rep = REPLICAS[k % 3]
        api.append(pino(t, rep, sha, 60, "unhandled rejection, shutting down", err=err))
        api.append(pino(t + 1.4, rep, sha, 30, "api server listening for requests", port=3000))
        lb.append(nginx(t + 0.01, 502, f"/zz{k:05d}"))
    for k in range(30):
        lb.append(nginx(T0 - 18 + k * 0.6, 502 if k % 3 == 0 else 302, f"/k{k:04d}x"))
    containers = [
        container("vip-api-1", sha, status="restarting", running=False, exit_code=1, restart_count=5, health="unhealthy", mem_usage=None, cpu_pct=None),
        container("vip-api-2", sha, status="running", exit_code=1, restart_count=4, health="starting", mem_usage=52 * MiB),
        container("vip-api-3", sha, status="running", exit_code=1, restart_count=4, health="healthy", mem_usage=55 * MiB),
    ]
    build("2-null-deref", r, culprit, "code", api + lb, containers, deps(), [
        "crash_loop: 4 container deaths in 60s", "fatal_logs: 4 fatal log lines in 30s", "container_die vip-api-1 exit=1 api",
    ], {"/health": {"status": 200, "ms": 6, "body": json.dumps({"ok": True, "replica": REPLICAS[2], "sha": sha})}})


def cpu_hog(tmp: Path) -> None:
    r = base_repo(tmp)
    noise_placeholder(r, ARJUN)
    r.edit("src/server.js", "app.get('/:code', async (req, res, next) => {", """// Sign redirect targets so the edge can verify they weren't tampered with.
// pbkdf2 instead of a bare HMAC (600k iterations, per OWASP guidance).
const SIGNING_SECRET = process.env.LINK_SIGNING_SECRET || 'vip-dev-signing-secret';
function signTarget(url) {
  return crypto.pbkdf2Sync(url, SIGNING_SECRET, 600000, 32, 'sha256').toString('hex');
}

app.get('/:code', async (req, res, next) => {""")
    r.edit("src/server.js", """    await redis.incr(`hits:${req.params.code}`);
    res.redirect(302, link.url);""", """    await redis.incr(`hits:${req.params.code}`);
    res.set('X-Link-Signature', signTarget(link.url));
    res.redirect(302, link.url);""")
    culprit = r.commit(DANA, "feat: sign redirect targets with X-Link-Signature")
    noise_readme_troubleshooting(r, MARCUS)
    noise_version(r, ARJUN)
    sha = r.git("rev-parse", "HEAD")
    api, lb = healthy_traffic(sha, T0 - 120, T0 - 40, rps=3)
    for k in range(60):
        t = T0 - 40 + k * 0.7
        rep = REPLICAS[k % 3]
        if k % 3 == 0:
            api.append(req(t, rep, sha, 302, f"/k{k:04d}x", ms=2900 + k * 7))
            lb.append(nginx(t + 2.9, 302, f"/k{k:04d}x"))
        else:
            lb.append(nginx(t + 3.0, 504, f"/k{k:04d}x"))
    lb.append(f"{dts(T0 - 30)} 2026/10/25 19:19:30 [error] 29#29: *9120 upstream timed out (110: Operation timed out) while reading response header from upstream, client: 172.19.0.7, server: , request: \"GET /k0007x HTTP/1.1\", upstream: \"http://172.19.0.5:3000/k0007x\"")
    hc = ["exit=-1 Health check exceeded timeout (3s)", "exit=-1 Health check exceeded timeout (3s)", "exit=1 wget: download timed out"]
    containers = [
        container("vip-api-1", sha, health="unhealthy", health_log=hc, cpu_pct=49.8, mem_usage=96 * MiB),
        container("vip-api-2", sha, health="unhealthy", health_log=hc, cpu_pct=50.1, mem_usage=99 * MiB),
        container("vip-api-3", sha, health="unhealthy", health_log=hc[:2], cpu_pct=49.6, mem_usage=94 * MiB),
    ]
    build("3-cpu-hog", r, culprit, "resource", api + lb, containers, deps(), [
        "unhealthy: 3 replicas unhealthy (vip-api-1, vip-api-2, vip-api-3)", "error_rate: 64% 5xx over 58 requests in 30s",
    ], {"/health": {"error": "ReadTimeout: timed out"}})


def bad_config(tmp: Path) -> None:
    r = base_repo(tmp)
    noise_log_wording(r, MARCUS)
    r.edit("src/config.js", """  redisPort: Number(process.env.REDIS_PORT || 6379),
  redisTimeoutMs: Number(process.env.REDIS_TIMEOUT_MS || 1000),""", """  redisPort: Number(process.env.REDIS_PORT || 6380), // match managed redis in staging
  redisTimeoutMs: Number(process.env.REDIS_TIMEOUT_MS || 1500),""")
    culprit = r.commit(ARJUN, "chore(config): align redis client defaults with staging")
    noise_footer(r, DANA)
    sha = r.git("rev-parse", "HEAD")
    route_line = r.line_of("src/server.js", "const raw = await redis.get(`link:${req.params.code}`);")
    conn = {"type": "Error", "message": "connect ECONNREFUSED 172.19.0.3:6380",
            "stack": "Error: connect ECONNREFUSED 172.19.0.3:6380\n    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1615:16)"}
    closed = {"type": "ClientClosedError", "message": "The client is closed",
              "stack": "ClientClosedError: The client is closed\n"
                       "    at RedisClient._RedisClient_sendCommand (/app/node_modules/@redis/client/dist/lib/client/index.js:510:31)\n"
                       f"    at async /app/src/server.js:{route_line}:17"}
    api, lb = [], []
    for k in range(80):
        t = T0 - 60 + k * 0.7
        rep = REPLICAS[k % 3]
        if k % 4 == 0:
            api.append(pino(t, rep, sha, 50, "redis connection error", err=conn))
        else:
            api.append(req(t, rep, sha, 500, f"/k{k:04d}x", rid=k, err=closed))
            lb.append(nginx(t + 0.003, 500, f"/k{k:04d}x"))
    hc = ["exit=1 wget: server returned error: HTTP/1.1 503 Service Unavailable"] * 3
    containers = [container(f"vip-api-{i}", sha, health="unhealthy", health_log=hc, mem_usage=60 * MiB) for i in (1, 2, 3)]
    build("4-bad-config", r, culprit, "config", api + lb, containers, deps(), [
        "lb_down: 3 consecutive LB health failures", "error_rate: 100% 5xx over 57 requests in 30s",
    ], {"/health": {"status": 503, "ms": 1503, "body": json.dumps({"ok": False, "replica": REPLICAS[0], "sha": sha, "error": "The client is closed"})}})


def redis_down(tmp: Path) -> None:
    r = base_repo(tmp)
    noise_readme_ports(r, ARJUN)
    noise_placeholder(r, MARCUS)
    noise_version(r, DANA)
    sha = r.git("rev-parse", "HEAD")
    route_line = r.line_of("src/server.js", "const raw = await redis.get(`link:${req.params.code}`);")
    api, lb = healthy_traffic(sha, T0 - 120, T0 - 30)
    gone = {"type": "Error", "message": "getaddrinfo ENOTFOUND redis",
            "stack": "Error: getaddrinfo ENOTFOUND redis\n    at GetAddrInfoReqWrap.onlookupall [as oncomplete] (node:dns:120:26)"}
    closed = {"type": "Error", "message": "Socket closed unexpectedly",
              "stack": "Error: Socket closed unexpectedly\n    at Socket.<anonymous> (/app/node_modules/@redis/client/dist/lib/client/socket.js:194:118)\n"
                       f"    at async /app/src/server.js:{route_line}:17"}
    for k in range(70):
        t = T0 - 30 + k * 0.45
        rep = REPLICAS[k % 3]
        if k % 5 == 0:
            api.append(pino(t, rep, sha, 50, "redis connection error", err=gone))
        else:
            api.append(req(t, rep, sha, 500, f"/k{k:04d}x", rid=k, err=closed))
            lb.append(nginx(t + 0.003, 500, f"/k{k:04d}x"))
    hc = ["exit=1 wget: server returned error: HTTP/1.1 503 Service Unavailable"] * 3
    containers = [container(f"vip-api-{i}", sha, health="unhealthy", health_log=hc) for i in (1, 2, 3)]
    build("5-redis-down", r, None, "dependency", api + lb, containers, deps(redis_running=False), [
        "lb_down: 3 consecutive LB health failures", "error_rate: 100% 5xx over 44 requests in 30s",
    ], {"/health": {"status": 503, "ms": 1004, "body": json.dumps({"ok": False, "replica": REPLICAS[1], "sha": sha, "error": "getaddrinfo ENOTFOUND redis"})}})


if __name__ == "__main__":
    if not (VIP / "src" / "server.js").exists():
        sys.exit(f"need the known-good very-important-project sources at {VIP}")
    for fn in (memory_leak, null_deref, cpu_hog, bad_config, redis_down):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
