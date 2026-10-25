"""The RCA agent: an OpenAI-compatible tool-calling loop over the evidence bundle."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from maxxing.agent import prompts
from maxxing.agent.fake import FakeChatClient, heuristic_report
from maxxing.agent.schema import RCAReport, ReportParseError, apply_guards, parse_report
from maxxing.agent.tools import TOOL_SPECS, ToolBox, truncate
from maxxing.evidence.git import GitRepo
from maxxing.evidence.models import EvidenceBundle

log = logging.getLogger(__name__)

_TEXT_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


class ToolsUnsupported(Exception):
    pass


def _is_tools_unsupported(err: Exception) -> bool:
    text = str(err).lower()
    return "tool" in text and any(k in text for k in ("not support", "unsupported", "does not support", "invalid"))


def parse_text_tool_calls(content: str | None) -> list[Any]:
    """Some local models emit tool calls inline as <tool_call>{"name":..,"arguments":..}</tool_call>."""
    calls = []
    for i, m in enumerate(_TEXT_TOOL_CALL.finditer(content or "")):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("name"):
            args = d.get("arguments", {})
            calls.append(SimpleNamespace(
                id=f"text_call_{i}", type="function",
                function=SimpleNamespace(name=d["name"], arguments=args if isinstance(args, str) else json.dumps(args)),
            ))
    return calls


@dataclass
class AgentTrace:
    steps: int = 0
    tool_calls: list[str] = field(default_factory=list)
    repairs: int = 0
    seconds: float = 0.0


class RCAAgent:
    def __init__(
        self,
        client: Any,
        model: str,
        max_steps: int = 8,
        temperature: float = 0.1,
        max_repairs: int = 2,
        repo: GitRepo | None = None,
    ):
        self.client = client
        self.model = model
        self.max_steps = max_steps
        self.temperature = temperature
        self.max_repairs = max_repairs
        self.repo = repo
        self.trace = AgentTrace()

    async def _create(self, messages: list[dict], tools: bool) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if tools:
            kwargs["tools"] = TOOL_SPECS
            kwargs["tool_choice"] = "auto"
        try:
            return await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            if tools and _is_tools_unsupported(e):
                raise ToolsUnsupported(str(e)) from e
            raise

    async def run(self, bundle: EvidenceBundle) -> RCAReport:
        t0 = time.monotonic()
        if isinstance(self.client, FakeChatClient):
            self.client.bundle = bundle
        try:
            report = await self._run(bundle)
        except Exception as e:
            log.exception("RCA agent failed; falling back to heuristic report")
            report = heuristic_report(bundle)
            report.generated_by = "heuristic-fallback"
            report.notes.append(f"LLM unavailable or failed: {type(e).__name__}: {str(e)[:200]}")
        self.trace.seconds = time.monotonic() - t0
        if isinstance(self.client, FakeChatClient) and report.generated_by == "llm":
            report.generated_by = "fake"
        report.model = None if report.generated_by in ("fake", "heuristic-fallback") else self.model
        report.steps = self.trace.steps
        return apply_guards(report, bundle)

    async def _run(self, bundle: EvidenceBundle) -> RCAReport:
        toolbox = ToolBox(bundle, self.repo)
        messages: list[dict] = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": prompts.render_evidence(bundle)
             + "\n\nInvestigate with the tools (verify the top candidates' diffs), then output the JSON report."},
        ]
        for _ in range(self.max_steps):
            self.trace.steps += 1
            try:
                resp = await self._create(messages, tools=True)
            except ToolsUnsupported as e:
                log.warning("model does not support tool calls (%s); single-shot with full evidence", e)
                return await self._single_shot(bundle)
            msg = resp.choices[0].message
            native = list(msg.tool_calls or [])
            calls = native or parse_text_tool_calls(msg.content)
            if not calls:
                return await self._finalize(messages, msg.content or "", "llm")
            if native:
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                        for c in calls
                    ],
                })
            else:
                messages.append({"role": "assistant", "content": msg.content or ""})
            for c in calls:
                self.trace.tool_calls.append(c.function.name)
                result = toolbox.call(c.function.name, c.function.arguments)
                if native:
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                else:
                    messages.append({"role": "user", "content": f"[tool result: {c.function.name}]\n{result}"})
        messages.append({"role": "user", "content": prompts.FINAL_NUDGE})
        self.trace.steps += 1
        resp = await self._create(messages, tools=False)
        return await self._finalize(messages, resp.choices[0].message.content or "", "llm")

    async def _single_shot(self, bundle: EvidenceBundle) -> RCAReport:
        messages = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": prompts.render_evidence(bundle, full=True) + "\n\nOutput the JSON report now."},
        ]
        self.trace.steps += 1
        resp = await self._create(messages, tools=False)
        return await self._finalize(messages, resp.choices[0].message.content or "", "llm-single-shot")

    async def _finalize(self, messages: list[dict], content: str, mode: str) -> RCAReport:
        """Parse the final answer, asking the model to repair malformed JSON up to `max_repairs` times."""
        last_err: Exception | None = None
        for attempt in range(self.max_repairs + 1):
            try:
                report = parse_report(content)
                report.generated_by = mode  # type: ignore[assignment]
                if attempt:
                    report.notes.append(f"output repaired after {attempt} retr{'y' if attempt == 1 else 'ies'}")
                return report
            except ReportParseError as e:
                last_err = e
                if attempt == self.max_repairs:
                    break
                self.trace.repairs += 1
                messages.append({"role": "assistant", "content": truncate(content or "(empty)", 4000)})
                messages.append({"role": "user", "content": prompts.REPAIR_TEMPLATE.format(error=str(e)[:300])})
                self.trace.steps += 1
                resp = await self._create(messages, tools=False)
                content = resp.choices[0].message.content or ""
        raise ReportParseError(f"could not get a valid report after {self.max_repairs} repairs: {last_err}")


def build_agent(settings, repo: GitRepo | None = None) -> RCAAgent:
    from maxxing.agent.llm import make_client

    return RCAAgent(
        make_client(settings),
        settings.llm_model,
        max_steps=settings.llm_max_steps,
        temperature=settings.llm_temperature,
        repo=repo,
    )
