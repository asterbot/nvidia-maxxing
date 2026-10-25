"""Incident store + pipeline wiring (detector events -> triage -> console report) without Docker."""

import asyncio

from maxxing.agent.schema import RCAReport
from maxxing.detector.incident import EventType, IncidentEngine
from maxxing.detector.signals import Kind, Signal
from maxxing.incidents.store import IncidentStore
from maxxing.pipeline import Pipeline
from maxxing.report import ConsoleNotifier, Notifier, format_duration

from .conftest import FIXTURES, load_fixture


def test_store_roundtrip(tmp_path):
    s = IncidentStore(tmp_path / "x.db")
    bundle, _ = load_fixture(FIXTURES / "1-memory-leak.json")
    inc = s.open(["oom: vip-api-1"], 100.0)
    s.save_triage(inc.id, bundle, RCAReport(summary="s", root_cause="r", category="resource"))
    got = s.resolve(inc.id, 160.0)
    assert got.status == "resolved" and got.duration_s() == 60.0
    assert got.bundle.containers[0].oom_killed and s.latest().id == inc.id
    assert [e for _, e in s.timeline(inc.id)][0] == "detector: oom: vip-api-1"
    assert s.unresolved() == []


class Recorder(Notifier):
    def __init__(self):
        self.calls = []

    async def incident_opened(self, inc):
        self.calls.append(("opened", inc.id))

    async def report_ready(self, inc, bundle, report):
        self.calls.append(("report", report.culprit.sha if report.culprit else None))

    async def resolved(self, inc):
        self.calls.append(("resolved", inc.id))


async def test_pipeline_open_triage_resolve(settings, tmp_path):
    bundle, exp = load_fixture(FIXTURES / "1-memory-leak.json")
    settings.settle_s = 0
    store = IncidentStore(tmp_path / "p.db")
    rec = Recorder()
    p = Pipeline(settings, store, rec, IncidentEngine(), collect=lambda *a, **k: bundle.model_copy(deep=True))
    engine = IncidentEngine()
    for ev in engine.observe(Signal(Kind.CONTAINER_OOM, 1000.0, "vip-api-1")):
        await p.handle(ev)
    outcome = await asyncio.wait_for(p.outcomes.get(), 10)
    assert outcome.report.culprit.sha == exp["expected_sha"]
    for i in range(30):
        engine.observe(Signal(Kind.HEALTH_OK, 1001.0 + i))
    for ev in engine.tick(1031.0):
        assert ev.type == EventType.RESOLVED
        await p.handle(ev)
    assert rec.calls == [("opened", 1), ("report", exp["expected_sha"]), ("resolved", 1)]
    assert store.get(1).status == "resolved"


async def test_console_report_names_culprit_and_author(capsys):
    from rich.console import Console

    from maxxing.agent.fake import heuristic_report

    bundle, exp = load_fixture(FIXTURES / "2-null-deref.json")
    store = IncidentStore(":memory:")
    inc = store.open(["crash_loop"])
    console = Console(force_terminal=False, width=140)
    await ConsoleNotifier(store, console, verbose=True).report_ready(inc, bundle, heuristic_report(bundle))
    out = capsys.readouterr().out
    assert exp["expected_sha"] in out and "Marcus Lee <marcus@vip.dev>" in out
    assert "Culprit commit" in out and "Commit ranking" in out


def test_console_report_no_culprit_blames_nobody(capsys):
    from rich.console import Console

    from maxxing.agent.fake import heuristic_report
    from maxxing.report import print_report

    bundle, _ = load_fixture(FIXTURES / "5-redis-down.json")
    print_report(Console(force_terminal=False, width=140), heuristic_report(bundle), bundle)
    assert "nobody is being blamed" in capsys.readouterr().out


def test_format_duration():
    assert format_duration(42) == "42s" and format_duration(3725) == "1h 02m"
