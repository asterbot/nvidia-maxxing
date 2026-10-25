"""System prompt and evidence rendering for the RCA agent."""

from __future__ import annotations

from datetime import UTC, datetime

from maxxing.agent.schema import REPORT_SCHEMA_HINT
from maxxing.evidence.models import EvidenceBundle

SYSTEM_PROMPT = f"""You are a senior Site Reliability Engineer doing root-cause analysis of a live production outage.
The service is a Node.js/Express API (3 replicas behind nginx, Redis for storage) running in Docker with
memory and CPU limits. You are given an evidence bundle and tools to dig deeper. Work like an on-call expert:

1. Classify the outage FIRST from hard signals:
   - resource: OOMKilled=true or exit code 137, memory climbing toward the limit, CPU saturation,
     health checks timing out without stack traces. Look for commits that add unbounded state
     (Maps/arrays/caches never evicted, setInterval accumulators) or heavy synchronous work on hot paths.
   - code: application exceptions (TypeError, ReferenceError, ...) with stack frames in repo files,
     crash loops (exit code 1). Match the crashing file:line to a commit's diff hunk or git blame.
   - config: connection errors (ECONNREFUSED/ENOTFOUND) where a commit changed hosts/ports/env defaults
     or config files.
   - dependency: a dependency container (e.g. redis) is stopped/crashed, or connection errors with NO
     relevant config/code change in the window. Then the culprit is null: do NOT blame anyone.
2. Use the tools to verify: read the diff of the top heuristic candidates (show_commit), check the
   crashing lines (read_file/blame), and look at container state. The heuristic ranking is a prior,
   not the answer. Noise commits (docs, tests, refactors far from the failing code) are innocent.
3. The culprit MUST be one of the listed candidate commits (use its sha) or null. Blaming an innocent
   engineer is worse than saying null. Only name a culprit when the diff plausibly explains the signals.
4. Propose a minimal, concrete fix as a unified diff against the culprit's change (often: restore the
   null check, bound/evict the cache, move sync work off the hot path, revert the config value).
5. Be specific and quantitative in impact and root_cause; cite evidence (log line, frame, OOMKilled, hunk).

When you are done investigating, reply with ONLY a JSON object (no prose, no markdown) of this shape:
{REPORT_SCHEMA_HINT}
"""

FINAL_NUDGE = "Stop calling tools now. Reply with ONLY the final JSON report object."
REPAIR_TEMPLATE = (
    "Your previous reply could not be parsed as the required JSON report: {error}. "
    "Reply again with ONLY a valid JSON object matching the schema. No prose, no code fences."
)

ASK_SYSTEM_PROMPT = """You are the on-call SRE assistant for this service. Answer the engineer's question about the
current or most recent incident using the evidence and tools. Be concise (under 200 words), concrete, cite
commits by short sha and author, and say plainly when the evidence does not support a conclusion."""


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%S") if ts else "?"


def render_evidence(bundle: EvidenceBundle, full: bool = False, max_chars: int = 24_000) -> str:
    b = bundle
    out: list[str] = [
        "# Incident evidence",
        f"trigger: {'; '.join(b.trigger)}",
        f"window: {_t(b.window_start)}-{_t(b.window_end)} UTC",
        f"impact (measured): {b.impact_hint}",
        f"deployed sha: {b.deployed_sha or '?'} | known-good sha: {b.known_good_sha or '?'}",
        "",
        "## Containers (api replicas)",
        *[f"- {c.one_line()}" + (f" | healthcheck: {' | '.join(c.health_log[-2:])}" if c.health_log else "") for c in b.containers],
        "## Dependencies",
        *[f"- {c.one_line()}" for c in b.dependencies],
        "",
        f"## Logs: {b.log_stats.lines} lines, {b.log_stats.requests} requests, 5xx={b.log_stats.status_5xx} "
        f"4xx={b.log_stats.status_4xx} errors={b.log_stats.errors} fatals={b.log_stats.fatals}",
        "## Error clusters (by stack signature)",
    ]
    for c in b.clusters[:8]:
        out.append(f"- x{c.count} [{_t(c.first_seen)}..{_t(c.last_seen)}] {c.err_type}: {c.message[:200]} replicas={','.join(c.replicas[:4])}")
        frames = [f"{f.repo_path}:{f.line} ({f.function or '?'})" for f in c.app_frames[:4]]
        if frames:
            out.append(f"  app frames: {' <- '.join(frames)}")
    if b.blame:
        out.append("## git blame of crashing lines")
        out += [f"- {h.path}:{h.line} -> {h.sha[:8]} <{h.author_email}>: {h.code[:120]}" for h in b.blame]
    if b.hints:
        out.append("## Detector hints")
        out += [f"- {h}" for h in b.hints]
        out.append(f"- heuristic category: {b.category_hint or 'unclear'}")
    out.append("## Candidate commits since known-good (newest first), with heuristic score")
    scores = {c.sha: c for c in b.candidates}
    for c in b.commits:
        cand = scores.get(c.sha)
        out.append(f"- {c.one_line()} score={cand.score if cand else 0}" + (f" reasons: {'; '.join(cand.reasons)}" if cand and cand.reasons else ""))
    if not b.commits:
        out.append("- (none: no code changes since the last healthy deploy)")
    if full:
        out.append("\n## Recent log sample")
        out += [ln.render()[:300] for ln in b.log_sample[-30:]]
        for c in b.clusters[:2]:
            if c.stack:
                out.append(f"\n## Stack ({c.err_type})\n{c.stack[:1500]}")
        top = [b.commit(c.sha) for c in b.candidates[:4]]
        for c in top:
            if c:
                out.append(f"\n## Diff {c.short} ({c.author_name}): {c.summary}\n{c.diff[:3000]}")
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[:max_chars] + "\n...[evidence truncated]"
