"""Drive RCAAgent through the real `openai` SDK against a fake Ollama-style /v1 server.

Verifies request/response wire shapes (tool calls, tool messages, Ollama's 400 "does not
support tools" error) without a model.
"""

import json

from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from maxxing.agent.rca import RCAAgent

from .conftest import FIXTURES, load_fixture


def completion(message: dict, finish="stop") -> dict:
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "nemotron",
            "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def make_server(culprit: str, support_tools: bool = True):
    seen: list[dict] = []

    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        seen.append(body)
        if body.get("tools") and not support_tools:
            return web.json_response({"error": {"message": "registry.ollama.ai/library/nemotron does not support tools",
                                                "type": "api_error"}}, status=400)
        if body.get("tools") and not any(m["role"] == "tool" for m in body["messages"]):
            return web.json_response(completion({"content": "", "tool_calls": [{
                "id": "call_0", "type": "function",
                "function": {"name": "show_commit", "arguments": json.dumps({"sha": culprit[:8]})}}]}, "tool_calls"))
        report = {"summary": "null deref", "category": "code", "severity": "SEV1", "root_cause": "missing null check",
                  "culprit": {"sha": culprit[:12], "confidence": 0.9, "why": "dropped null check"},
                  "fix": {"explanation": "restore check", "diff": ""}, "remediation": ["revert"]}
        return web.json_response(completion({"content": "<think>checking</think>" + json.dumps(report)}))

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return TestServer(app), seen


async def test_tool_loop_over_real_openai_sdk():
    bundle, exp = load_fixture(FIXTURES / "2-null-deref.json")
    server, seen = make_server(exp["expected_sha"])
    await server.start_server()
    try:
        client = AsyncOpenAI(base_url=str(server.make_url("/v1")), api_key="ollama", max_retries=0)
        report = await RCAAgent(client, "nemotron").run(bundle)
        assert report.culprit.sha == exp["expected_sha"] and report.generated_by == "llm"
        tool_msg = next(m for m in seen[1]["messages"] if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "call_0" and "resolveLink" in tool_msg["content"]
        assert seen[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "show_commit"
    finally:
        await server.close()


async def test_ollama_no_tools_error_falls_back_to_single_shot():
    bundle, exp = load_fixture(FIXTURES / "2-null-deref.json")
    server, seen = make_server(exp["expected_sha"], support_tools=False)
    await server.start_server()
    try:
        client = AsyncOpenAI(base_url=str(server.make_url("/v1")), api_key="ollama", max_retries=0)
        report = await RCAAgent(client, "nemotron").run(bundle)
        assert report.generated_by == "llm-single-shot" and report.culprit.sha == exp["expected_sha"]
        assert "tools" not in seen[-1]
    finally:
        await server.close()
