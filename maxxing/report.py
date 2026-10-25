"""Console output: the RCA report printed to the terminal, plus the notifier hooks the pipeline calls."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from maxxing.agent.schema import RCAReport
from maxxing.evidence.models import EvidenceBundle
from maxxing.incidents.store import IncidentRecord, IncidentStore

SEVERITY_STYLE = {"SEV1": "bold red", "SEV2": "bold dark_orange", "SEV3": "bold yellow"}
CATEGORY_EMOJI = {"code": "🐛", "resource": "🔥", "config": "⚙️", "dependency": "🔌", "unknown": "❓"}


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%S")


def culprit_panel(report: RCAReport, bundle: EvidenceBundle) -> Panel:
    if not report.culprit:
        return Panel("No commit in the deploy window explains this outage (nobody is being blamed).",
                     title="Culprit commit", border_style="green")
    c = bundle.commit(report.culprit.sha)
    t = Table.grid(padding=(0, 1))
    t.add_column(style="bold cyan", no_wrap=True)
    t.add_column()
    t.add_row("commit", Text(report.culprit.sha, style="bold"))
    if c:
        t.add_row("author", f"{c.author_name} <{c.author_email}>")
        t.add_row("date", c.authored_at)
        t.add_row("message", c.summary)
        t.add_row("files", ", ".join(f.path for f in c.files) or "–")
    t.add_row("confidence", f"{report.culprit.confidence:.0%}")
    t.add_row("why", report.culprit.why or "–")
    return Panel(t, title="Culprit commit", border_style="red")


def print_report(
    console: Console,
    report: RCAReport,
    bundle: EvidenceBundle,
    incident_id: int | None = None,
    timeline: list[tuple[float, str]] | None = None,
    verbose: bool = False,
) -> None:
    name = f"INC-{incident_id}" if incident_id else "INC-manual"
    emoji = CATEGORY_EMOJI.get(report.category, "❓")
    head = Text.assemble((f"{name} ", "bold"), (f"[{report.severity}] ", SEVERITY_STYLE.get(report.severity, "bold")),
                         report.summary)
    console.rule(head)
    for err in bundle.collection_errors:
        console.print(f"[bold yellow]⚠ {err}[/bold yellow]")

    overview = Table.grid(padding=(0, 1))
    overview.add_column(style="bold cyan", no_wrap=True)
    overview.add_column()
    overview.add_row("category", f"{emoji} {report.category}")
    overview.add_row("impact", report.impact or bundle.impact_hint or "unknown")
    overview.add_row("deployed", f"{(bundle.deployed_sha or '?')[:8]}  (known-good {(bundle.known_good_sha or '?')[:8]}, "
                                 f"{len(bundle.commits)} commits in window)")
    overview.add_row("root cause", report.root_cause)
    console.print(overview)
    console.print(culprit_panel(report, bundle))

    if report.fix and (report.fix.explanation or report.fix.diff.strip()):
        parts: list = []
        if report.fix.explanation:
            parts.append(Text(report.fix.explanation))
        if report.fix.diff.strip():
            parts.append(Syntax(report.fix.diff.strip(), "diff", theme="ansi_dark", word_wrap=True))
        console.print(Panel(Group(*parts), title="Suggested fix", border_style="green"))
    if report.remediation:
        console.print(Panel("\n".join(f"{i}. {r}" for i, r in enumerate(report.remediation, 1)), title="Next steps"))

    if verbose:
        events = [f"{_hms(ts)}  {ev}" for ts, ev in timeline or []] + [f"{e.ts or '--:--:--'}  {e.event}" for e in report.timeline]
        if events:
            console.print(Panel("\n".join(events), title="Timeline", border_style="dim"))
        ranking = Table(title="Commit ranking (heuristic prior)", title_justify="left", box=None)
        for col in ("sha", "score", "author", "summary", "reasons"):
            ranking.add_column(col, overflow="fold")
        for cand in bundle.candidates[:8]:
            c = bundle.commit(cand.sha)
            ranking.add_row(cand.short, str(cand.score), c.author_name if c else "", c.summary if c else "", "; ".join(cand.reasons))
        console.print(ranking)
        if report.notes:
            console.print(Panel("\n".join(f"• {n}" for n in report.notes), title="Agent notes", border_style="dim"))

    by = report.generated_by + (f" · {report.model}" if report.model else "")
    console.print(f"[dim]{by} · {report.steps} steps · 100% local[/dim]")


class Notifier:
    """Hooks the pipeline calls as an incident progresses. The base class does nothing."""

    async def incident_opened(self, inc: IncidentRecord) -> None: ...
    async def report_ready(self, inc: IncidentRecord, bundle: EvidenceBundle, report: RCAReport) -> None: ...
    async def resolved(self, inc: IncidentRecord) -> None: ...


class ConsoleNotifier(Notifier):
    def __init__(self, store: IncidentStore | None = None, console: Console | None = None, verbose: bool = False):
        self.store = store
        self.console = console or Console()
        self.verbose = verbose

    async def incident_opened(self, inc: IncidentRecord) -> None:
        self.console.print(f"[bold red]🚨 {inc.title} opened[/bold red]: {'; '.join(inc.rules)} [dim](triaging…)[/dim]")

    async def report_ready(self, inc: IncidentRecord, bundle: EvidenceBundle, report: RCAReport) -> None:
        timeline = self.store.timeline(inc.id) if self.store else []
        print_report(self.console, report, bundle, inc.id, timeline, self.verbose)

    async def resolved(self, inc: IncidentRecord) -> None:
        self.console.print(f"[bold green]✅ {inc.title} resolved in {format_duration(inc.duration_s())}[/bold green]")
