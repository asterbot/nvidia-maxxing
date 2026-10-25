"""RCAReport parsing, repair-retry, guards, tool loop and fallbacks, with a scripted fake model."""

import json
from types import SimpleNamespace

import pytest

from maxxing.agent.rca import RCAAgent, parse_text_tool_calls
from maxxing.agent.schema import RCAReport, ReportParseError, apply_guards, extract_json, parse_report
from maxxing.agent.tools import ToolBox, truncate

from .conftest import FIXTURES, load_fixture

NULL_DEREF = FIXTURES / "2-null-deref.json"
REDIS_DOWN = FIXTURES / "5-redis-down.json"


def good_json(sha: str | None, category="code") -> str:
    return json.dumps({
        "summary": "Null deref in redirect route", "severity": "SEV1", "impact": "all replicas crash-looping",
        "category": category, "timeline": [{"ts": "19:19:41", "event": "first TypeError"}],
        "root_cause": "resolveLink returns null for unknown codes and link.url throws.",
        "culprit": {"sha": sha, "confidence": 0.9, "why": "removed null check"} if sha else None,
        "fix": {"explanation": "restore null check", "diff": "+ if (!link) return res.status(404).end();"},
        "remediation": ["revert", "add test"],
    })


class ScriptedClient:
    """Returns pre-baked responses in order; records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, list):  # tool calls
            calls = [SimpleNamespace(id=f"c{i}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                     for i, (n, a) in enumerate(r)]
            msg = SimpleNamespace(content=None, tool_calls=calls)
        else:
            msg = SimpleNamespace(content=r, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


# -- parsing -----------------------------------------------------------------------------
def test_extract_json_handles_think_fences_and_chatter():
    text = "<think>let me reason {not json}</think>Sure! Here it is:\n```json\n" + good_json("abc12345") + "\n```\nHope that helps."
    assert extract_json(text)["summary"] == "Null deref in redirect route"
    assert extract_json("prefix " + good_json(None) + " suffix")["culprit"] is None


def test_parse_report_is_lenient():
    r = parse_report(json.dumps({
        "summary": "x", "root_cause": "y", "category": "Infrastructure", "severity": "critical",
        "culprit": {"sha": "none"}, "timeline": ["deploy", {"time": "1", "description": "boom"}],
        "remediation": "restart redis", "fix": "restart", "confidence": "high",
    }))
    assert r.category == "dependency" and r.severity == "SEV1" and r.culprit is None
    assert r.timeline[0].event == "deploy" and r.timeline[1].ts == "1"
    assert r.remediation == ["restart redis"] and r.fix.explanation == "restart"
    r2 = parse_report(json.dumps({"summary": "x", "root_cause": "y", "culprit": {"sha": "abc12345", "confidence": "85%"}}))
    assert r2.culprit.confidence == pytest.approx(0.85) and r2.category == "unknown"


@pytest.mark.parametrize("bad", ["", "no json here", '{"summary": ""}', "[1,2,3]"])
def test_parse_report_rejects_garbage(bad):
    with pytest.raises(ReportParseError):
        parse_report(bad)


# -- guards ------------------------------------------------------------------------------
def test_guard_drops_unknown_sha_and_expands_prefix():
    bundle, exp = load_fixture(NULL_DEREF)
    r = apply_guards(parse_report(good_json("deadbeefcafe")), bundle)
    assert r.culprit is None and "not among the candidate commits" in r.notes[0]
    r = apply_guards(parse_report(good_json(exp["expected_sha"][:8])), bundle)
    assert r.culprit.sha == exp["expected_sha"]


def test_guard_never_blames_on_dependency_outage():
    bundle, _ = load_fixture(REDIS_DOWN)
    some_commit = bundle.commits[0].sha
    r = apply_guards(parse_report(good_json(some_commit, category="dependency")), bundle)
    assert r.culprit is None


# -- agent loop ----------------------------------------------------------------------------
async def test_agent_tool_loop_then_report():
    bundle, exp = load_fixture(NULL_DEREF)
    client = ScriptedClient(
        [("get_logs", {"min_level": 60}), ("get_container_state", {})],
        [("show_commit", {"sha": exp["expected_sha"][:10]})],
        good_json(exp["expected_sha"]),
    )
    agent = RCAAgent(client, "nemotron-test")
    report = await agent.run(bundle)
    assert report.culprit.sha == exp["expected_sha"] and report.category == "code"
    assert report.generated_by == "llm" and report.model == "nemotron-test" and report.steps == 3
    assert agent.trace.tool_calls == ["get_logs", "get_container_state", "show_commit"]
    tool_msgs = [m for m in client.requests[-1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 3 and "TypeError" in tool_msgs[0]["content"]
    assert "resolveLink" in tool_msgs[2]["content"]  # the culprit diff was shown
    assert "tools" in client.requests[0]


async def test_agent_repair_retry():
    bundle, exp = load_fixture(NULL_DEREF)
    client = ScriptedClient("I think it's the refactor commit!", '{"summary": "x", ', good_json(exp["expected_sha"]))
    report = await RCAAgent(client, "m", max_repairs=2).run(bundle)
    assert report.culprit.sha == exp["expected_sha"]
    assert any("repaired after 2" in n for n in report.notes)
    assert "could not be parsed" in client.requests[1]["messages"][-1]["content"]
    assert "tools" not in client.requests[1]


async def test_agent_falls_back_to_heuristics_after_failed_repairs():
    bundle, exp = load_fixture(NULL_DEREF)
    client = ScriptedClient("nope", "still nope", "nope again")
    report = await RCAAgent(client, "m", max_repairs=2).run(bundle)
    assert report.generated_by == "heuristic-fallback"
    assert report.culprit.sha == exp["expected_sha"]  # heuristic prior is right here


async def test_agent_single_shot_when_tools_unsupported():
    bundle, exp = load_fixture(NULL_DEREF)
    err = Exception("Error code: 400 - registry.ollama.ai/library/foo does not support tools")
    client = ScriptedClient(err, good_json(exp["expected_sha"]))
    report = await RCAAgent(client, "m").run(bundle)
    assert report.generated_by == "llm-single-shot" and report.culprit.sha == exp["expected_sha"]
    assert "tools" not in client.requests[1]
    assert "## Diff" in client.requests[1]["messages"][1]["content"]  # full evidence inlined


async def test_agent_max_steps_forces_final_answer():
    bundle, exp = load_fixture(NULL_DEREF)
    client = ScriptedClient(*([[("list_commits", {})]] * 3), good_json(exp["expected_sha"]))
    report = await RCAAgent(client, "m", max_steps=3).run(bundle)
    assert report.culprit is not None and "tools" not in client.requests[-1]
    assert "Stop calling tools" in client.requests[-1]["messages"][-1]["content"]


async def test_agent_survives_unreachable_model():
    bundle, _ = load_fixture(REDIS_DOWN)
    report = await RCAAgent(ScriptedClient(ConnectionError("connection refused")), "m").run(bundle)
    assert report.generated_by == "heuristic-fallback" and report.culprit is None and report.category == "dependency"


def test_text_tool_calls_are_parsed():
    calls = parse_text_tool_calls('ok <tool_call>{"name": "show_commit", "arguments": {"sha": "abc"}}</tool_call>')
    assert calls[0].function.name == "show_commit" and json.loads(calls[0].function.arguments) == {"sha": "abc"}


# -- tools -------------------------------------------------------------------------------------
def test_toolbox_replay_mode_and_truncation():
    bundle, exp = load_fixture(NULL_DEREF)
    tb = ToolBox(bundle)
    assert "OOM" not in tb.call("get_container_state", "{}") and "vip-api-1" in tb.call("get_container_state", {})
    assert exp["expected_sha"][:8] in tb.call("list_commits", None)
    assert "not one of the candidate commits" in tb.call("show_commit", {"sha": "ffffffff"})
    assert tb.call("read_file", {"path": "src/server.js"}).startswith("error")
    assert "resolveLink" in tb.call("grep_repo", {"pattern": "resolveLink"})
    assert tb.call("nope", {}).startswith("error: unknown tool")
    assert tb.call("show_commit", "{bad json").startswith("error")
    long = truncate("x" * 10_000, 1000)
    assert len(long) < 1100 and "truncated" in long


def test_fake_report_matches_expected_on_all_fixtures():
    from maxxing.agent.fake import heuristic_report

    from .conftest import SCENARIO_FIXTURES

    for path in SCENARIO_FIXTURES:
        bundle, exp = load_fixture(path)
        r = heuristic_report(bundle)
        assert (r.culprit.sha if r.culprit else None) == exp["expected_sha"], path.name
        assert r.category == exp["expected_category"], path.name
        assert isinstance(RCAReport.model_validate_json(r.model_dump_json()), RCAReport)
