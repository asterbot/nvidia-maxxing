"""Deterministic stand-ins for the LLM.

* `heuristic_report(bundle)` builds an RCAReport from the heuristic ranking alone.
* `FakeChatClient` mimics `openai.AsyncOpenAI().chat.completions.create` and drives the real
  agent loop (a couple of tool calls, then the heuristic report as JSON), so LLM_FAKE=1 still
  exercises tools, parsing and guards end to end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import count
from types import SimpleNamespace
from typing import Any

from maxxing.agent.schema import Culprit, Fix, RCAReport, TimelineEntry
from maxxing.evidence.models import EvidenceBundle
from maxxing.evidence.rank import CULPRIT_THRESHOLD


def revert_diff(diff: str) -> str:
    """Invert a unified diff (the simplest possible fix: undo the culprit's change)."""
    out = []
    for row in diff.splitlines():
        if row.startswith(("--- ", "+++ ", "diff --git", "index ")):
            out.append(row)  # same file on both sides; keep headers
        elif row.startswith("@@"):
            parts = row.split(" ")
            if len(parts) >= 3:
                parts[1], parts[2] = "-" + parts[2][1:], "+" + parts[1][1:]
            out.append(" ".join(parts))
        elif row.startswith("+"):
            out.append("-" + row[1:])
        elif row.startswith("-"):
            out.append("+" + row[1:])
        else:
            out.append(row)
    return "\n".join(out)


def _hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%S") if ts else ""


def heuristic_report(bundle: EvidenceBundle) -> RCAReport:
    cat = bundle.category_hint or "unknown"
    top = bundle.candidates[0] if bundle.candidates else None
    commit = bundle.commit(top.sha) if top else None
    runner_up = bundle.candidates[1].score if len(bundle.candidates) > 1 else 0.0
    blame_ok = (
        cat != "dependency" and top is not None and commit is not None
        and top.score >= CULPRIT_THRESHOLD and top.score - runner_up >= 1.0  # clear winner only
    )

    signals = "; ".join(bundle.hints) or "error-rate/health anomaly"
    cluster = bundle.clusters[0] if bundle.clusters else None
    err = f"{cluster.err_type}: {cluster.message[:160]} (x{cluster.count})" if cluster else "no dominant error signature"
    down = [d for d in bundle.dependencies if not d.running]

    if cat == "dependency":
        dep = down[0].name if down else "a downstream dependency"
        root = f"{dep} is unavailable; the api replicas fail every request that needs it ({err}). No commit in the window touches the connection path, so this is not a code regression."
        summary = f"Dependency outage: {dep} is down"
        remediation = [f"Restart {dep} (docker compose -p {bundle.project} start {down[0].service if down else 'redis'})", "Add alerting on dependency health and a circuit breaker / degraded mode in the api"]
    elif blame_ok and commit:
        root = (
            f"{cat.capitalize()} outage. Signals: {signals}. Dominant error: {err}. "
            f"Commit {commit.short} by {commit.author_name} (\"{commit.summary}\") best explains it: {'; '.join(top.reasons) or 'highest heuristic score'}."
        )
        summary = f"{commit.short} \"{commit.summary}\" caused a {cat} outage"
        remediation = [f"Roll back {commit.short} (git revert {commit.short} && scripts/deploy.sh)", "Add a regression test / load test covering this path before re-landing"]
    else:
        root = f"Outage with signals: {signals}. Dominant error: {err}. No candidate commit is clearly implicated."
        summary = f"{cat.capitalize() if cat != 'unknown' else 'Unclassified'} outage, culprit unclear"
        remediation = ["Inspect the error clusters and container state", "Consider rolling back to the known-good sha"]

    timeline = [TimelineEntry(ts=_hhmmss(bundle.window_start), event="evidence window start")]
    if cluster:
        timeline.append(TimelineEntry(ts=_hhmmss(cluster.first_seen), event=f"first {cluster.err_type}"))
    timeline += [TimelineEntry(ts=_hhmmss(bundle.window_end), event=f"incident detected: {t}") for t in bundle.trigger[:2]]

    return RCAReport(
        summary=summary,
        severity=bundle.severity_hint,  # type: ignore[arg-type]
        impact=bundle.impact_hint,
        category=cat,  # type: ignore[arg-type]
        timeline=timeline,
        root_cause=root,
        culprit=Culprit(sha=commit.sha, confidence=min(0.95, 0.4 + top.score / 20), why="; ".join(top.reasons)) if blame_ok and commit and top else None,
        fix=Fix(explanation=f"Revert {commit.short}", diff=revert_diff(commit.diff)[:3000]) if blame_ok and commit else None,
        remediation=remediation,
        generated_by="fake",
    )


class FakeChatClient:
    """Duck-types `AsyncOpenAI` closely enough for maxxing.agent.rca."""

    def __init__(self) -> None:
        self.bundle: EvidenceBundle | None = None
        self._ids = count(1)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @staticmethod
    def _resp(content: str | None = None, tool_calls: list | None = None) -> Any:
        msg = SimpleNamespace(content=content, tool_calls=tool_calls or None, role="assistant")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls" if tool_calls else "stop")])

    def _call(self, name: str, args: dict) -> Any:
        return SimpleNamespace(id=f"call_{next(self._ids)}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))

    async def _create(self, *, messages: list[dict], tools: list | None = None, **_: Any) -> Any:
        if self.bundle is None:
            raise RuntimeError("FakeChatClient.bundle must be set")
        tool_turns = sum(1 for m in messages if m.get("role") == "tool")
        system = messages[0].get("content", "") if messages else ""
        if "on-call SRE assistant" in system:  # ask mode
            return self._resp(fake_answer(self.bundle, messages[-1].get("content", "")))
        if tools and tool_turns == 0:
            return self._resp(tool_calls=[self._call("get_container_state", {}), self._call("list_commits", {})])
        if tools and tool_turns == 2 and self.bundle.candidates:
            return self._resp(tool_calls=[self._call("show_commit", {"sha": self.bundle.candidates[0].sha})])
        return self._resp(heuristic_report(self.bundle).model_dump_json(exclude={"model", "generated_by", "steps", "notes"}))


def fake_answer(bundle: EvidenceBundle, question: str) -> str:
    r = heuristic_report(bundle)
    lines = [f"(fake LLM) {r.summary}.", r.root_cause]
    if r.culprit:
        c = bundle.commit(r.culprit.sha)
        lines.append(f"Culprit: {c.short} by {c.author_name} <{c.author_email}>." if c else "")
    others = [c for c in bundle.candidates[1:4]]
    if others:
        lines.append("Other candidates: " + ", ".join(f"{c.short} (score {c.score})" for c in others))
    return "\n".join(x for x in lines if x)
