"""End-to-end eval harness: break the service with each scenario and score the bot.

For every ../very-important-project/scenarios/<n>-<slug>.sh:
  reset.sh (known-good + redeploy) -> wait for a healthy baseline -> start a fresh detector
  pipeline -> run the scenario -> wait for an incident + RCA -> compare with .scenario.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from maxxing.config import Settings
from maxxing.detector.incident import IncidentEngine, Thresholds
from maxxing.incidents.store import IncidentStore
from maxxing.pipeline import Pipeline, TriageOutcome
from maxxing.report import ConsoleNotifier, Notifier

log = logging.getLogger(__name__)


def parse_ts(value) -> float | None:
    """ISO-8601 (e.g. 2026-10-25T19:17:30Z), epoch seconds or epoch ms."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def discover_scenarios(repo: Path) -> list[Path]:
    def num(p: Path) -> int:
        m = re.match(r"(\d+)-", p.name)
        return int(m[1]) if m else 1_000
    return sorted((p for p in (repo / "scenarios").glob("*-*.sh") if re.match(r"\d+-", p.name)), key=num)


@dataclass
class ScenarioResult:
    id: str
    expected: dict = field(default_factory=dict)
    detected: bool = False
    culprit: str | None = None
    category: str | None = None
    ttd_s: float | None = None
    ttrca_s: float | None = None
    error: str | None = None
    generated_by: str | None = None

    @property
    def expect_incident(self) -> bool:
        return bool(self.expected.get("expect_incident", True))

    @property
    def detect_ok(self) -> bool:
        return self.detected == self.expect_incident

    @property
    def culprit_ok(self) -> bool | None:
        if not self.expect_incident:
            return None if not self.detected else self.culprit is None
        if not self.detected:
            return False
        exp = self.expected.get("expected_sha")
        if exp is None:
            return self.culprit is None
        return bool(self.culprit) and (self.culprit.startswith(exp) or exp.startswith(self.culprit))

    @property
    def category_ok(self) -> bool | None:
        exp = self.expected.get("expected_category")
        if not self.expect_incident or exp is None:
            return None
        return self.category == exp

    @property
    def passed(self) -> bool:
        return self.error is None and self.detect_ok and self.culprit_ok is not False and self.category_ok is not False


async def _run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    # stdin=DEVNULL so scenario scripts skip their interactive author prompt.
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def wait_healthy(lb_url: str, streak_needed: int = 5, timeout: float = 120) -> bool:
    deadline, streak = time.monotonic() + timeout, 0
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                ok = (await client.get(lb_url.rstrip("/") + "/health")).status_code == 200
            except httpx.HTTPError:
                ok = False
            streak = streak + 1 if ok else 0
            if streak >= streak_needed:
                return True
            await asyncio.sleep(1)
    return False


async def run_scenario(
    settings: Settings,
    script: Path,
    console: Console,
    wait_s: float = 60,
    rca_timeout_s: float = 300,
    notifier: Notifier | None = None,
    dump_dir: Path | None = None,
) -> ScenarioResult:
    repo = settings.repo_path
    sid = script.stem
    res = ScenarioResult(id=sid)
    console.print(f"[bold]▶ {sid}[/bold]: resetting to known-good…")
    code, out = await _run(["bash", str(repo / "scenarios" / "reset.sh")], repo, 600)
    if code != 0:
        res.error = f"reset failed: {out.strip()[-300:]}"
        return res
    if not await wait_healthy(settings.lb_url):
        res.error = "service never became healthy after reset"
        return res

    store = IncidentStore(":memory:")
    engine = IncidentEngine(Thresholds(cooldown_s=0, recovery_s=settings.recovery_s))
    pipeline = Pipeline(settings, store, notifier or Notifier(), engine)
    stop = asyncio.Event()
    runner = asyncio.create_task(pipeline.run(stop))
    await asyncio.sleep(5)  # detector baseline
    try:
        console.print(f"  running {script.name}…")
        script_task = asyncio.create_task(_run(["bash", str(script)], repo, 900))
        opened_task = asyncio.create_task(pipeline.opened.get())
        done, _ = await asyncio.wait({script_task, opened_task}, return_when=asyncio.FIRST_COMPLETED)
        if opened_task not in done:
            code, out = await script_task
            if code != 0:
                res.error = f"scenario script failed: {out.strip()[-300:]}"
            try:
                await asyncio.wait_for(asyncio.shield(opened_task), wait_s)
            except TimeoutError:
                pass
        if not opened_task.done():
            opened_task.cancel()
        try:
            res.expected = json.loads(settings.scenario_file.read_text())
            res.id = res.expected.get("id", sid)
        except (OSError, json.JSONDecodeError):
            res.error = res.error or ".scenario.json missing"
        started = parse_ts(res.expected.get("started_at"))

        if opened_task.done() and not opened_task.cancelled():
            rec = opened_task.result()
            res.detected = True
            res.ttd_s = rec.opened_at - started if started else None
            try:
                outcome: TriageOutcome = await asyncio.wait_for(pipeline.outcomes.get(), rca_timeout_s)
                res.culprit = outcome.report.culprit.sha if outcome.report.culprit else None
                res.category = outcome.report.category
                res.ttrca_s = outcome.triaged_at - outcome.opened_at
                res.generated_by = outcome.report.generated_by
                if dump_dir:
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    (dump_dir / f"{res.id}.json").write_text(outcome.bundle.model_dump_json(indent=2))
                    (dump_dir / f"{res.id}.expected.json").write_text(json.dumps(res.expected, indent=2))
            except TimeoutError:
                res.error = "incident opened but RCA timed out"
        if not script_task.done():
            await script_task
    finally:
        stop.set()
        await asyncio.gather(runner, return_exceptions=True)
        store.close()
    return res


def scorecard(results: list[ScenarioResult]) -> Table:
    def mark(v: bool | None) -> str:
        return "[dim]–[/dim]" if v is None else ("[green]✔[/green]" if v else "[red]✘[/red]")

    def secs(v: float | None) -> str:
        return f"{v:.0f}s" if v is not None else "–"

    t = Table(title="nvidia-maxxing eval scorecard", show_lines=False)
    for col in ("scenario", "expect", "detected", "culprit", "category", "time to detect", "time to RCA", "by", "result"):
        t.add_column(col)
    for r in results:
        exp = r.expected
        expect = ("incident" if r.expect_incident else "no incident") + (f" / {exp.get('expected_category')}" if exp.get("expected_category") else "")
        culprit_txt = f"{mark(r.culprit_ok)} {(r.culprit or 'none')[:8]}"
        cat_txt = f"{mark(r.category_ok)} {r.category or '–'}"
        t.add_row(
            r.id, expect, mark(r.detect_ok) + (" yes" if r.detected else " no"), culprit_txt, cat_txt,
            secs(r.ttd_s), secs(r.ttrca_s), r.generated_by or "–",
            "[bold green]PASS[/bold green]" if r.passed else f"[bold red]FAIL[/bold red] {r.error or ''}",
        )
    passed = sum(r.passed for r in results)
    t.caption = f"{passed}/{len(results)} scenarios passed"
    return t


async def run_eval(
    settings: Settings,
    only: list[str] | None = None,
    wait_s: float = 60,
    show_reports: bool = False,
    dump_dir: Path | None = None,
    console: Console | None = None,
) -> list[ScenarioResult]:
    console = console or Console()
    scripts = discover_scenarios(settings.repo_path)
    if only:
        scripts = [s for s in scripts if any(s.stem == o or s.stem.startswith(f"{o}-") or o in s.stem for o in only)]
    if not scripts:
        console.print(f"[red]no scenarios found under {settings.repo_path / 'scenarios'}[/red]")
        return []
    notifier = ConsoleNotifier(console=console) if show_reports else None
    results = []
    for script in scripts:
        res = await run_scenario(settings, script, console, wait_s, notifier=notifier, dump_dir=dump_dir)
        console.print(f"  → {'PASS' if res.passed else 'FAIL'} detected={res.detected} culprit={(res.culprit or 'none')[:8]} category={res.category}")
        results.append(res)
    console.print(scorecard(results))
    console.print("[dim]resetting to known-good…[/dim]")
    await _run(["bash", str(settings.repo_path / "scenarios" / "reset.sh")], settings.repo_path, 600)
    return results
