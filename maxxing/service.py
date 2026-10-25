"""Application services shared by the CLI, the HTTP API and the daemon (no duplicated logic)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from maxxing.agent.ask import answer_question
from maxxing.agent.llm import make_client
from maxxing.agent.rca import build_agent
from maxxing.agent.schema import RCAReport
from maxxing.config import Settings
from maxxing.evidence.collector import collect_evidence
from maxxing.evidence.git import GitRepo
from maxxing.evidence.models import EvidenceBundle
from maxxing.incidents.store import IncidentRecord, IncidentStore

log = logging.getLogger(__name__)

RECENT_INCIDENT_S = 3600


def open_repo(settings: Settings) -> GitRepo | None:
    try:
        return GitRepo(settings.repo_path)
    except Exception as e:
        log.info("repo %s not available: %s", settings.repo_path, e)
        return None


async def analyze_bundle(settings: Settings, bundle: EvidenceBundle) -> RCAReport:
    agent = build_agent(settings, repo=open_repo(settings))
    return await agent.run(bundle)


@dataclass
class TriageResult:
    bundle: EvidenceBundle
    report: RCAReport | None
    incident: IncidentRecord | None
    collect_s: float
    rca_s: float


async def triage_now(
    settings: Settings,
    store: IncidentStore | None = None,
    trigger: list[str] | None = None,
    run_llm: bool = True,
) -> TriageResult:
    """Collect evidence right now and (optionally) run the RCA agent; persists to the store if given."""
    t0 = time.monotonic()
    inc = store.open(trigger or ["manual triage"]) if store is not None else None
    bundle = await asyncio.to_thread(collect_evidence, settings, trigger or ["manual triage"], None, inc.id if inc else None)
    t1 = time.monotonic()
    report = await analyze_bundle(settings, bundle) if run_llm else None
    t2 = time.monotonic()
    if store is not None and inc is not None and report is not None:
        store.save_triage(inc.id, bundle, report)
        inc = store.get(inc.id)
    return TriageResult(bundle, report, inc, t1 - t0, t2 - t1)


async def ask(
    settings: Settings,
    question: str,
    store: IncidentStore | None = None,
    incident_id: int | None = None,
    bundle: EvidenceBundle | None = None,
) -> str:
    """Answer a question about a given incident, the most recent one (< 1h old), or fresh evidence."""
    report: RCAReport | None = None
    if bundle is None and store is not None:
        inc = store.get(incident_id) if incident_id is not None else store.latest(with_bundle=True)
        if incident_id is not None and inc is None:
            return f"INC-{incident_id} not found"
        if inc is not None and inc.bundle is not None and (incident_id is not None or time.time() - inc.opened_at < RECENT_INCIDENT_S):
            bundle, report = inc.bundle, inc.report
    if bundle is None:
        bundle = await asyncio.to_thread(collect_evidence, settings, ["ask"])
    client = make_client(settings)
    return await answer_question(client, settings.llm_model, question, bundle, report, repo=open_repo(settings))
