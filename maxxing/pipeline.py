"""The daemon: detector -> evidence -> RCA agent -> console report, with recovery tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from maxxing.agent.schema import RCAReport
from maxxing.config import Settings
from maxxing.detector.incident import EventType, IncidentEngine, IncidentEvent
from maxxing.detector.runner import Detector
from maxxing.evidence.collector import collect_evidence
from maxxing.evidence.models import EvidenceBundle
from maxxing.incidents.store import IncidentRecord, IncidentStore
from maxxing.report import Notifier
from maxxing.service import analyze_bundle

log = logging.getLogger(__name__)


@dataclass
class TriageOutcome:
    incident_id: int
    opened_at: float
    triaged_at: float
    bundle: EvidenceBundle
    report: RCAReport


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: IncidentStore,
        notifier: Notifier,
        engine: IncidentEngine | None = None,
        collect: Callable[..., EvidenceBundle] = collect_evidence,
    ):
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.engine = engine
        self.collect = collect
        self._ids: dict[int, int] = {}  # engine seq -> store id
        self._tasks: set[asyncio.Task] = set()
        self.outcomes: asyncio.Queue[TriageOutcome] = asyncio.Queue()  # for the eval harness
        self.opened: asyncio.Queue[IncidentRecord] = asyncio.Queue()

    async def handle(self, ev: IncidentEvent) -> None:
        seq = ev.incident.seq
        if ev.type == EventType.OPENED:
            rec = self.store.open(ev.rules, ev.incident.opened_at)
            self._ids[seq] = rec.id
            log.warning("%s opened: %s", rec.title, "; ".join(ev.rules))
            self.opened.put_nowait(rec)
            await self._safe(self.notifier.incident_opened(rec))
            task = asyncio.create_task(self.triage(rec, ev))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif ev.type == EventType.UPDATED and seq in self._ids:
            self.store.add_rules(self._ids[seq], ev.rules)
        elif ev.type == EventType.RESOLVED and seq in self._ids:
            rec = self.store.resolve(self._ids.pop(seq), ev.ts)
            if rec:
                log.warning("%s resolved after %.0fs", rec.title, rec.duration_s() or 0)
                await self._safe(self.notifier.resolved(rec))

    async def triage(self, rec: IncidentRecord, ev: IncidentEvent) -> None:
        try:
            await self._triage(rec, ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("triage of %s failed", rec.title)
            self.store.add_event(rec.id, f"triage failed: {e}")

    async def _triage(self, rec: IncidentRecord, ev: IncidentEvent) -> None:
        await asyncio.sleep(self.settings.settle_s)  # let the log window fill with the failure
        trigger = list(ev.incident.rules) + [s.describe() for s in ev.incident.signals[:5]]
        bundle = await asyncio.to_thread(self.collect, self.settings, trigger, ev.incident.opened_at, rec.id)
        self.store.add_event(rec.id, f"evidence collected: {len(bundle.commits)} commits, {len(bundle.clusters)} error clusters")
        report = await analyze_bundle(self.settings, bundle)
        self.store.save_triage(rec.id, bundle, report)
        fresh = self.store.get(rec.id) or rec
        self.outcomes.put_nowait(TriageOutcome(rec.id, rec.opened_at, time.time(), bundle, report))
        await self._safe(self.notifier.report_ready(fresh, bundle, report))

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("notifier failed")

    async def run(self, stop: asyncio.Event) -> None:
        detector = Detector(self.settings, self.handle, self.engine)
        try:
            await detector.run(stop)
        finally:
            for t in list(self._tasks):
                t.cancel()
