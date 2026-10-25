"""RCAReport: the structured output of the triage agent, plus lenient parsing and guards."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from maxxing.evidence.models import EvidenceBundle

Category = Literal["code", "resource", "config", "dependency", "unknown"]
Severity = Literal["SEV1", "SEV2", "SEV3"]

_CATEGORY_ALIASES = {
    "bug": "code", "regression": "code", "application": "code", "software": "code",
    "oom": "resource", "memory": "resource", "cpu": "resource", "capacity": "resource", "performance": "resource",
    "configuration": "config", "misconfiguration": "config", "env": "config",
    "infra": "dependency", "infrastructure": "dependency", "external": "dependency", "network": "dependency",
    "downstream": "dependency",
}
_NULLISH = {"", "none", "null", "n/a", "na", "unknown", "nil", "-"}


class Culprit(BaseModel):
    sha: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    why: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> float:
        if isinstance(v, str):
            words = {"very high": 0.95, "high": 0.85, "medium": 0.6, "moderate": 0.6, "low": 0.3, "very low": 0.15}
            if v.strip().lower() in words:
                return words[v.strip().lower()]
            v = v.strip().rstrip("%")
            try:
                v = float(v)
            except ValueError:
                return 0.5
        if isinstance(v, (int, float)):
            v = float(v)
            if v > 1.0:
                v = v / 100.0
            return min(max(v, 0.0), 1.0)
        return 0.5


class Fix(BaseModel):
    explanation: str = ""
    diff: str = ""


class TimelineEntry(BaseModel):
    ts: str = ""
    event: str


class RCAReport(BaseModel):
    summary: str
    severity: Severity = "SEV2"
    impact: str = ""
    category: Category = "unknown"
    timeline: list[TimelineEntry] = Field(default_factory=list)
    root_cause: str
    culprit: Culprit | None = None
    fix: Fix | None = None
    remediation: list[str] = Field(default_factory=list)
    # filled in by us, not by the model
    model: str | None = None
    generated_by: Literal["llm", "llm-single-shot", "fake", "heuristic-fallback"] = "llm"
    steps: int = 0
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _lenient(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # model instances passed in Python (not JSON) -> plain dicts so the coercions below apply
        d = {k: (v.model_dump() if isinstance(v, BaseModel) else
                 [x.model_dump() if isinstance(x, BaseModel) else x for x in v] if isinstance(v, list) else v)
             for k, v in data.items()}
        if "rca" in d and isinstance(d["rca"], dict) and "summary" not in d:
            d = {**d["rca"], **{k: v for k, v in d.items() if k != "rca"}}
        d.setdefault("root_cause", d.get("rootCause") or d.get("cause") or d.get("summary") or "")
        d.setdefault("summary", d.get("title") or d.get("root_cause") or "")

        cat = str(d.get("category") or "unknown").strip().lower()
        d["category"] = _CATEGORY_ALIASES.get(cat, cat) if cat in _CATEGORY_ALIASES or cat in {
            "code", "resource", "config", "dependency"} else "unknown"

        sev = str(d.get("severity") or "SEV2").upper().replace("-", "").replace(" ", "")
        if sev in {"1", "2", "3"}:
            sev = f"SEV{sev}"
        sev = {"CRITICAL": "SEV1", "HIGH": "SEV1", "MEDIUM": "SEV2", "LOW": "SEV3", "P1": "SEV1", "P2": "SEV2", "P3": "SEV3"}.get(sev, sev)
        d["severity"] = sev if sev in {"SEV1", "SEV2", "SEV3"} else "SEV2"

        c = d.get("culprit")
        if isinstance(c, str):
            c = {"sha": c}
        if isinstance(c, dict):
            sha = c.get("sha") or c.get("commit") or c.get("hash")
            c = None if sha is None or str(sha).strip().lower() in _NULLISH else {**c, "sha": str(sha).strip()}
        elif c is not None:
            c = None
        d["culprit"] = c

        f = d.get("fix")
        if isinstance(f, str):
            d["fix"] = {"explanation": f}
        elif isinstance(f, dict):
            d["fix"] = {"explanation": str(f.get("explanation") or f.get("description") or ""), "diff": str(f.get("diff") or f.get("patch") or "")}

        tl = d.get("timeline") or []
        if isinstance(tl, list):
            d["timeline"] = [
                {"event": t} if isinstance(t, str) else {"ts": str(t.get("ts") or t.get("time") or ""), "event": str(t.get("event") or t.get("description") or t)}
                for t in tl if isinstance(t, (str, dict))
            ]
        else:
            d["timeline"] = []

        rem = d.get("remediation")
        if isinstance(rem, str):
            d["remediation"] = [rem]
        elif isinstance(rem, list):
            d["remediation"] = [str(r) for r in rem]
        else:
            d["remediation"] = []
        for key in ("summary", "root_cause", "impact"):
            if key in d and not isinstance(d[key], str):
                d[key] = json.dumps(d[key]) if d[key] is not None else ""
        return d


# JSON schema we show the model (trimmed to the fields it should fill)
REPORT_SCHEMA_HINT = """{
  "summary": "one sentence: what broke and why",
  "severity": "SEV1|SEV2|SEV3",
  "impact": "who/what is affected, with numbers from the evidence",
  "category": "code|resource|config|dependency",
  "timeline": [{"ts": "HH:MM:SS", "event": "..."}],
  "root_cause": "2-4 sentences, cite the specific evidence (stack frame, OOMKilled, log line, diff hunk)",
  "culprit": {"sha": "<full or >=8 char sha from the candidate commits>", "confidence": 0.0-1.0, "why": "..."} or null,
  "fix": {"explanation": "...", "diff": "unified diff against the culprit's files, or empty"},
  "remediation": ["immediate step", "follow-up step"]
}"""


class ReportParseError(ValueError):
    pass


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of model output (handles <think>, code fences, chatter)."""
    if not text:
        raise ReportParseError("empty response")
    text = _THINK.sub("", text)
    if "<think>" in text and "</think>" not in text:  # truncated reasoning
        text = text.split("<think>", 1)[0]
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    dec = json.JSONDecoder()
    for blob in candidates:
        try:
            obj = json.loads(blob)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    best: dict | None = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and (best is None or len(json.dumps(obj)) > len(json.dumps(best))):
            best = obj
            if "summary" in obj or "root_cause" in obj:
                return obj
    if best is None:
        raise ReportParseError("no JSON object found in response")
    return best


def parse_report(text: str) -> RCAReport:
    data = extract_json(text)
    try:
        report = RCAReport.model_validate(data)
    except ValidationError as e:
        raise ReportParseError(f"schema validation failed: {e.errors(include_url=False)[:5]}") from e
    if not report.root_cause.strip() or not report.summary.strip():
        raise ReportParseError("summary and root_cause must be non-empty")
    return report


def apply_guards(report: RCAReport, bundle: EvidenceBundle) -> RCAReport:
    """Never let the model blame a commit that is not in the evidence window."""
    if report.culprit is not None:
        commit = bundle.commit(report.culprit.sha)
        if commit is None:
            report.notes.append(
                f"model named culprit {report.culprit.sha!r}, which is not among the candidate commits; dropped"
            )
            report.culprit = None
        else:
            report.culprit.sha = commit.sha
    if report.category == "dependency" and report.culprit is not None:
        report.notes.append("dependency outage: culprit commit dropped (no code change caused it)")
        report.culprit = None
    if report.category == "unknown" and bundle.category_hint:
        report.category = bundle.category_hint  # type: ignore[assignment]
        report.notes.append(f"category filled from heuristic hint: {bundle.category_hint}")
    if not report.impact:
        report.impact = bundle.impact_hint
    return report
