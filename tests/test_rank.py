"""Commit ranking + classification over synthetic git histories built in tmp_path."""

from maxxing.evidence.collector import analyze
from maxxing.evidence.git import GitRepo, parse_hunks
from maxxing.evidence.logs import parse_lines
from maxxing.evidence.models import ContainerState, EvidenceBundle
from maxxing.evidence.rank import CULPRIT_THRESHOLD, classify, rank_commits

import json

SERVER = """const express = require('express');
const app = express();

app.get('/:code', async (req, res, next) => {
  try {
    const raw = await redis.get(`link:${req.params.code}`);
    if (!raw) return res.status(404).json({ error: 'not found' });
    const link = JSON.parse(raw);
    res.redirect(302, link.url);
  } catch (err) {
    next(err);
  }
});

app.listen(3000, () => console.log('listening'));
"""
CONFIG = "module.exports = {\n  redisHost: process.env.REDIS_HOST || 'redis',\n  redisPort: Number(process.env.REDIS_PORT || 6379),\n};\n"


def test_blame_points_at_commit_that_wrote_the_crashing_line(synth_repo):
    base(synth_repo)
    buggy = SERVER.replace("    res.redirect(302, link.url);", "    res.redirect(302, link.target.url);")
    synth_repo.write("src/server.js", buggy)
    culprit = synth_repo.commit("feat: nested targets", "Marcus Lee", "marcus@vip.dev")
    line = next(i for i, l in enumerate(buggy.splitlines(), 1) if "link.target.url" in l)
    stack = f"TypeError: Cannot read properties of undefined (reading 'url')\n    at /app/src/server.js:{line}:38"
    bundle = analyze(EvidenceBundle(containers=[api(exit_code=1)]), parse_lines([fatal(stack, "x")] * 3), GitRepo(synth_repo.path))
    assert bundle.blame[0].sha == culprit and bundle.blame[0].author_email == "marcus@vip.dev"
    assert any("blame" in r for r in bundle.candidates[0].reasons) and bundle.candidates[0].sha == culprit


def base(synth):
    synth.write("src/server.js", SERVER)
    synth.write("src/config.js", CONFIG)
    synth.write("README.md", "# vip\n")
    good = synth.commit("initial", "Dana Ortiz", "dana@vip.dev")
    synth.repo.create_tag("known-good")
    return good


def api(**kw):
    return ContainerState(name=kw.pop("name", "vip-api-1"), service="api", running=True, status="running", **kw)


def fatal(stack, msg="Cannot read properties of null (reading 'url')"):
    return json.dumps({"level": 60, "time": 1761420000000, "replica": "r1", "msg": "unhandled rejection, shutting down",
                       "err": {"type": "TypeError", "message": msg, "stack": stack}})


def test_parse_hunks():
    diff = "--- a/src/x.js\n+++ b/src/x.js\n@@ -1,3 +1,5 @@\n a\n+b\n@@ -10 +12,2 @@\n+c\n--- a/old.js\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    assert parse_hunks(diff) == {"src/x.js": [(1, 5), (12, 13)]}


def test_code_bug_ranked_by_stack_and_blame(synth_repo):
    base(synth_repo)
    synth_repo.write("README.md", "# vip\n\nnow with docs\n")
    synth_repo.commit("docs: readme", "Dana Ortiz", "dana@vip.dev")
    # noise that touches the same file, far away from the crash
    synth_repo.write("src/server.js", SERVER.replace("console.log('listening')", "console.log('api listening on 3000')"))
    synth_repo.commit("chore: log wording", "Arjun", "arjun@vip.dev")
    # the bug: drop the null check
    buggy = (synth_repo.path / "src/server.js").read_text().replace("    if (!raw) return res.status(404).json({ error: 'not found' });\n", "")
    synth_repo.write("src/server.js", buggy)
    culprit = synth_repo.commit("refactor: simplify redirect", "Marcus Lee", "marcus@vip.dev")
    synth_repo.write("README.md", "# vip\n\nnow with more docs\n")
    synth_repo.commit("docs: more", "Dana Ortiz", "dana@vip.dev")

    line = next(i for i, l in enumerate(buggy.splitlines(), 1) if "link.url" in l)
    stack = f"TypeError: Cannot read properties of null (reading 'url')\n    at /app/src/server.js:{line}:31\n    at process.processTicksAndRejections (node:internal/process/task_queues:105:5)"
    lines = parse_lines([fatal(stack)] * 4)
    bundle = analyze(EvidenceBundle(containers=[api(exit_code=1, restart_count=4)]), lines, GitRepo(synth_repo.path))

    assert len(bundle.commits) == 4  # known-good tag found
    assert bundle.category_hint == "code"
    assert bundle.candidates[0].sha == culprit
    assert bundle.candidates[0].score >= CULPRIT_THRESHOLD
    assert any("crashing line" in r for r in bundle.candidates[0].reasons)
    # the crashing line itself predates the bug (the null check above it was deleted), so blame
    # honestly points at the base commit; ranking relies on hunk overlap instead
    assert bundle.blame and bundle.blame[0].author_email == "dana@vip.dev"
    docs = [c for c in bundle.candidates if bundle.commit(c.sha).summary.startswith("docs")]
    assert all(c.score < 0 for c in docs)


def test_memory_leak_ranked_by_keywords(synth_repo):
    base(synth_repo)
    leak = SERVER.replace("app.get('/:code'", "const linkCache = new Map();\n\napp.get('/:code'").replace(
        "    res.redirect(302, link.url);", "    linkCache.set(req.originalUrl, { url: link.url, preview: Buffer.alloc(1024 * 1024) });\n    res.redirect(302, link.url);")
    synth_repo.write("src/server.js", leak)
    culprit = synth_repo.commit("perf: cache resolved links in memory")
    synth_repo.write("README.md", "# vip\ncache docs\n")
    synth_repo.commit("docs: mention the cache", "Dana Ortiz", "dana@vip.dev")
    containers = [api(exit_code=137, oom_killed=True, restart_count=2)]
    bundle = analyze(EvidenceBundle(containers=containers), [], GitRepo(synth_repo.path))
    assert bundle.category_hint == "resource"
    assert bundle.candidates[0].sha == culprit
    assert "memory pressure" in bundle.candidates[0].reasons[0]


def test_config_change_vs_dependency_down(synth_repo):
    base(synth_repo)
    synth_repo.write("src/config.js", CONFIG.replace("6379", "6380"))
    culprit = synth_repo.commit("chore(config): align redis defaults")
    lines = parse_lines([json.dumps({"level": 50, "time": 1761420000000, "msg": "redis connection error",
                                     "err": {"type": "Error", "message": "connect ECONNREFUSED 172.19.0.3:6380"}})] * 10)
    repo = GitRepo(synth_repo.path)
    bundle = analyze(EvidenceBundle(containers=[api(health="unhealthy")]), lines, repo)
    assert bundle.category_hint == "config"
    assert bundle.candidates[0].sha == culprit

    # same errors but redis itself is down -> dependency, regardless of commits
    redis = ContainerState(name="vip-redis-1", service="redis", running=False, status="exited", exit_code=0)
    cat, hints = classify([api()], [redis], bundle.clusters, bundle.commits)
    assert cat == "dependency" and any("vip-redis-1" in h for h in hints)


def test_conn_errors_without_config_change_is_dependency(synth_repo):
    base(synth_repo)
    synth_repo.write("README.md", "# vip\nports docs\n")
    synth_repo.commit("docs: ports")
    lines = parse_lines([json.dumps({"level": 50, "time": 1761420000000, "msg": "redis connection error",
                                     "err": {"type": "Error", "message": "getaddrinfo ENOTFOUND redis"}})] * 5)
    bundle = analyze(EvidenceBundle(containers=[api()]), lines, GitRepo(synth_repo.path))
    assert bundle.category_hint == "dependency"
    assert all(c.score < CULPRIT_THRESHOLD for c in bundle.candidates)


def test_known_good_from_deploys_skips_currently_deployed(synth_repo):
    good = base(synth_repo)
    synth_repo.write("src/a.js", "1\n")
    mid = synth_repo.commit("a")
    synth_repo.write("src/b.js", "2\n")
    head = synth_repo.commit("b")
    repo = GitRepo(synth_repo.path)
    deploys = [{"sha": good, "ts": "2026-10-25T19:00:00Z", "status": "healthy"},
               {"sha": mid, "ts": "2026-10-25T19:05:00Z", "status": "healthy"},
               {"sha": head, "ts": "2026-10-25T19:10:00Z", "status": "healthy"}]  # leak passed health briefly
    assert repo.known_good_sha(deploys, head) == mid
    assert repo.known_good_sha([], head) == good  # falls back to the known-good tag


def test_rank_without_signals_prefers_runtime_changes():
    from maxxing.evidence.models import CommitInfo, FileChange
    docs = CommitInfo(sha="a" * 40, author_name="x", author_email="x@vip.dev", authored_at="", summary="docs",
                      files=[FileChange(path="README.md", insertions=3)], diff="+++ b/README.md\n+cache\n")
    code = CommitInfo(sha="b" * 40, author_name="y", author_email="y@vip.dev", authored_at="", summary="code",
                      files=[FileChange(path="src/server.js", insertions=1)], diff="+++ b/src/server.js\n+x\n")
    ranked = rank_commits([docs, code], [], [api(oom_killed=True)])
    assert ranked[0].sha == code.sha  # "cache" in README doesn't count


def test_v8_heap_oom_counts_as_memory_pressure():
    lines = parse_lines(["FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory"])
    from maxxing.evidence.logs import cluster_errors

    cat, hints = classify([api(exit_code=134)], [], cluster_errors(lines), [])
    assert cat == "resource" and any("memory" in h for h in hints)
