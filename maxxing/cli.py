"""`maxxing` command line: run | serve | triage | ask | eval | replay."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from maxxing.config import Settings, get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Autonomous outage triage on local Nemotron.")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=Console(stderr=True), show_path=False, rich_tracebacks=True)],
    )
    for noisy in ("httpx", "httpcore", "urllib3", "docker", "openai", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    fake_llm: Annotated[bool, typer.Option("--fake-llm", help="Deterministic fake LLM (same as LLM_FAKE=1)")] = False,
) -> None:
    _setup_logging(verbose)
    s = get_settings()
    if fake_llm:
        s.llm_fake = True


def _store(s: Settings):
    from maxxing.incidents.store import IncidentStore

    return IncidentStore(s.db_path)


def _install_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass


@app.command()
def run(
    serve: Annotated[bool, typer.Option("--serve", help="Also serve the read-only HTTP API")] = False,
    port: Annotated[int | None, typer.Option(help="API port (default API_PORT / 7070)")] = None,
    details: Annotated[bool, typer.Option("--details", help="Also print timeline, commit ranking and agent notes")] = False,
) -> None:
    """Daemon: detector -> evidence -> RCA agent -> report printed to the terminal."""
    from maxxing.pipeline import Pipeline
    from maxxing.report import ConsoleNotifier

    s = get_settings()
    store = _store(s)

    async def main_() -> None:

        stop = asyncio.Event()
        _install_stop(stop)
        runner = None
        if serve:
            from maxxing.api import start_server

            runner = await start_server(s, store, port=port)
        console.print(f"[bold green]maxxing[/bold green] watching compose project [bold]{s.compose_project}[/bold] via {s.lb_url} "
                      f"(model: {'FAKE' if s.llm_fake else s.llm_model} @ {s.llm_base_url})")
        try:
            await Pipeline(s, store, ConsoleNotifier(store, console, details)).run(stop)
        finally:
            if runner:
                await runner.cleanup()

    asyncio.run(main_())


@app.command()
def serve(
    host: Annotated[str | None, typer.Option()] = None,
    port: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Read-only HTTP API for sandboxed agents (GET /incidents, POST /ask, POST /triage)."""
    s = get_settings()
    store = _store(s)

    async def main_() -> None:
        from maxxing.api import start_server

        stop = asyncio.Event()
        _install_stop(stop)
        runner = await start_server(s, store, host, port)
        console.print(f"maxxing API on http://{host or s.api_host}:{port or s.api_port}"
                      + (" (bearer token required)" if s.maxxing_api_token else ""))
        await stop.wait()
        await runner.cleanup()

    asyncio.run(main_())


@app.command()
def triage(
    now: Annotated[bool, typer.Option("--now", help="Collect evidence immediately")] = True,
    dump: Annotated[Path | None, typer.Option(help="Write the evidence bundle JSON here (+ .expected.json if .scenario.json exists)")] = None,
    fixture: Annotated[Path | None, typer.Option(help="Replay a saved evidence bundle instead of collecting")] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Only collect/print evidence")] = False,
    details: Annotated[bool, typer.Option("--details", help="Also print timeline, commit ranking and agent notes")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON (for scripts/agents)")] = False,
) -> None:
    """Run one triage pass right now (or over a fixture) and print the RCA."""
    from maxxing import service
    from maxxing.agent.prompts import render_evidence
    from maxxing.evidence.models import EvidenceBundle
    from maxxing.report import print_report

    s = get_settings()

    async def go() -> None:
        store = None if fixture else _store(s)
        timeline: list[tuple[float, str]] = []
        if fixture:
            bundle = EvidenceBundle.model_validate_json(fixture.read_text())
            report = None if no_llm else await service.analyze_bundle(s, bundle)
            incident_id = bundle.incident_id
        else:
            result = await service.triage_now(s, store, ["manual triage"], run_llm=not no_llm)
            bundle, report = result.bundle, result.report
            incident_id = result.incident.id if result.incident else None
            if result.incident and store:
                timeline = store.timeline(result.incident.id)
        if dump:
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(bundle.model_dump_json(indent=2))
            if s.scenario_file.exists():
                dump.with_name(dump.stem + ".expected.json").write_text(s.scenario_file.read_text())
            console.print(f"[dim]evidence bundle written to {dump}[/dim]")
        if as_json:
            print(report.model_dump_json(indent=2) if report else bundle.model_dump_json(indent=2))
            return
        if report is None:
            console.print(render_evidence(bundle, full=True), markup=False, highlight=False)
            return
        print_report(console, report, bundle, incident_id, timeline, details)

    asyncio.run(go())


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Question about the current/most recent incident")],
    incident: Annotated[int | None, typer.Option("--incident", "-i", help="Incident id (default: most recent)")] = None,
    fixture: Annotated[Path | None, typer.Option(help="Answer over a saved evidence bundle")] = None,
) -> None:
    """One-shot Q&A over the current evidence (used by the OpenClaw skill)."""
    from maxxing import service
    from maxxing.evidence.models import EvidenceBundle

    s = get_settings()
    bundle = EvidenceBundle.model_validate_json(fixture.read_text()) if fixture else None
    answer = asyncio.run(service.ask(s, question, _store(s), incident, bundle))
    print(answer)


@app.command("eval")
def eval_cmd(
    only: Annotated[list[str] | None, typer.Option("--only", help="Scenario id/number filter (repeatable)")] = None,
    wait: Annotated[float, typer.Option(help="Seconds to wait for an incident after the scenario script")] = 60,
    show: Annotated[bool, typer.Option("--show", help="Print each rendered report")] = False,
    dump_dir: Annotated[Path | None, typer.Option(help="Save each scenario's evidence bundle as a replay fixture")] = None,
    json_out: Annotated[Path | None, typer.Option("--json-out", help="Write results JSON here")] = None,
) -> None:
    """Run every scenario in ../very-important-project/scenarios and print a scorecard."""
    from maxxing.eval import run_eval

    s = get_settings()
    results = asyncio.run(run_eval(s, only, wait, show, dump_dir, console))
    if json_out:
        json_out.write_text(json.dumps([
            {"id": r.id, "passed": r.passed, "detected": r.detected, "culprit": r.culprit, "category": r.category,
             "ttd_s": r.ttd_s, "ttrca_s": r.ttrca_s, "expected": r.expected, "error": r.error} for r in results
        ], indent=2))
    raise typer.Exit(0 if results and all(r.passed for r in results) else 1)


@app.command()
def replay(
    fixtures: Annotated[list[Path], typer.Argument(help="Evidence bundle JSON files")],
) -> None:
    """Score the agent over saved fixtures (fast prompt-tuning loop; no Docker needed)."""
    from rich.table import Table

    from maxxing import service
    from maxxing.evidence.models import EvidenceBundle

    s = get_settings()
    table = Table(title=f"replay ({'FAKE' if s.llm_fake else s.llm_model})")
    for col in ("fixture", "category", "culprit", "expected", "ok", "by"):
        table.add_column(col)

    async def go() -> int:
        ok_count = 0
        for f in fixtures:
            if f.name.endswith(".expected.json"):
                continue
            bundle = EvidenceBundle.model_validate_json(f.read_text())
            exp_file = f.with_name(f.stem + ".expected.json")
            exp = json.loads(exp_file.read_text()) if exp_file.exists() else {}
            report = await service.analyze_bundle(s, bundle)
            got = report.culprit.sha if report.culprit else None
            want = exp.get("expected_sha")
            ok = (got == want or (got and want and got.startswith(want))) and report.category == exp.get("expected_category", report.category)
            ok_count += bool(ok)
            table.add_row(f.stem, report.category, (got or "none")[:8], f"{(want or 'none')[:8]}/{exp.get('expected_category')}",
                          "[green]✔[/green]" if ok else "[red]✘[/red]", report.generated_by)
        console.print(table)
        return ok_count

    asyncio.run(go())


if __name__ == "__main__":
    app()
