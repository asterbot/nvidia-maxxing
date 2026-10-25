"""One-shot Q&A over an evidence bundle (backs `maxxing ask`, POST /ask and the OpenClaw skill)."""

from __future__ import annotations

import logging
import re
from typing import Any

from maxxing.agent import prompts
from maxxing.agent.fake import FakeChatClient, fake_answer
from maxxing.agent.rca import parse_text_tool_calls
from maxxing.agent.schema import RCAReport
from maxxing.agent.tools import TOOL_SPECS, ToolBox
from maxxing.evidence.git import GitRepo
from maxxing.evidence.models import EvidenceBundle

log = logging.getLogger(__name__)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


async def answer_question(
    client: Any,
    model: str,
    question: str,
    bundle: EvidenceBundle,
    report: RCAReport | None = None,
    repo: GitRepo | None = None,
    max_steps: int = 4,
) -> str:
    if isinstance(client, FakeChatClient):
        client.bundle = bundle
    toolbox = ToolBox(bundle, repo)
    context = prompts.render_evidence(bundle, max_chars=12_000)
    if report is not None:
        context += "\n\n# Current RCA report\n" + report.model_dump_json(indent=1, exclude={"notes"})[:4000]
    messages: list[dict] = [
        {"role": "system", "content": prompts.ASK_SYSTEM_PROMPT},
        {"role": "user", "content": f"{context}\n\n# Question\n{question}"},
    ]
    try:
        for step in range(max_steps):
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.2}
            if step < max_steps - 1:
                kwargs |= {"tools": TOOL_SPECS, "tool_choice": "auto"}
            try:
                resp = await client.chat.completions.create(**kwargs)
            except Exception as e:
                if "tools" in kwargs and "tool" in str(e).lower():
                    kwargs.pop("tools"), kwargs.pop("tool_choice")
                    resp = await client.chat.completions.create(**kwargs)
                else:
                    raise
            msg = resp.choices[0].message
            calls = list(msg.tool_calls or [])
            if not calls and parse_text_tool_calls(msg.content):
                calls = parse_text_tool_calls(msg.content)
                messages.append({"role": "assistant", "content": msg.content or ""})
                for c in calls:
                    messages.append({"role": "user", "content": f"[tool result: {c.function.name}]\n{toolbox.call(c.function.name, c.function.arguments)}"})
                continue
            if not calls:
                return _THINK.sub("", msg.content or "").strip() or "(empty answer)"
            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}} for c in calls],
            })
            for c in calls:
                messages.append({"role": "tool", "tool_call_id": c.id, "content": toolbox.call(c.function.name, c.function.arguments)})
        return "I could not reach a conclusion within the step budget; see the evidence with `maxxing triage --now`."
    except Exception as e:
        log.warning("ask failed, answering from heuristics: %s", e)
        return f"(LLM unavailable: {type(e).__name__}) " + fake_answer(bundle, question)
