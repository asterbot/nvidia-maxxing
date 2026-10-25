"""Replay saved evidence bundles through the full agent loop.

* the fake-LLM variant always runs (deterministic, no model)
* the `llm` variant runs against the local model at LLM_BASE_URL and is skipped when it is
  unreachable:  uv run pytest -m llm
"""

import pytest

from maxxing.agent.fake import FakeChatClient
from maxxing.agent.llm import llm_reachable, make_client
from maxxing.agent.rca import RCAAgent
from maxxing.config import Settings

from .conftest import SCENARIO_FIXTURES, load_fixture

IDS = [p.stem for p in SCENARIO_FIXTURES]


def check(report, exp):
    got = report.culprit.sha if report.culprit else None
    assert got == exp["expected_sha"], f"culprit {got} != {exp['expected_sha']} ({report.summary})"
    assert report.category == exp["expected_category"], f"category {report.category} != {exp['expected_category']}"


@pytest.mark.parametrize("path", SCENARIO_FIXTURES, ids=IDS)
async def test_replay_fake_llm(path):
    bundle, exp = load_fixture(path)
    agent = RCAAgent(FakeChatClient(), "fake")
    report = await agent.run(bundle)
    assert report.generated_by == "fake"
    assert "get_container_state" in agent.trace.tool_calls  # the real loop ran
    check(report, exp)


@pytest.mark.llm
@pytest.mark.parametrize("path", SCENARIO_FIXTURES, ids=IDS)
async def test_replay_local_llm(path):
    s = Settings(llm_fake=False)
    if not llm_reachable(s):
        pytest.skip(f"no LLM reachable at {s.llm_base_url}")
    bundle, exp = load_fixture(path)
    report = await RCAAgent(make_client(s), s.llm_model, max_steps=s.llm_max_steps).run(bundle)
    assert report.generated_by in ("llm", "llm-single-shot"), report.notes
    check(report, exp)
