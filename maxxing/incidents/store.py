"""SQLite incident store: timeline, evidence and report per incident."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field

from maxxing.agent.schema import RCAReport
from maxxing.evidence.models import EvidenceBundle

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at REAL NOT NULL,
    triaged_at REAL,
    resolved_at REAL,
    status TEXT NOT NULL DEFAULT 'open',
    rules TEXT NOT NULL DEFAULT '[]',
    bundle TEXT,
    report TEXT
);
CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    ts REAL NOT NULL,
    event TEXT NOT NULL
);
"""


class IncidentRecord(BaseModel):
    id: int
    opened_at: float
    triaged_at: float | None = None
    resolved_at: float | None = None
    status: str = "open"  # open | triaged | resolved
    rules: list[str] = Field(default_factory=list)
    bundle: EvidenceBundle | None = None
    report: RCAReport | None = None

    @property
    def title(self) -> str:
        return f"INC-{self.id}"

    def duration_s(self) -> float | None:
        return None if self.resolved_at is None else self.resolved_at - self.opened_at

    def summary(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "opened_at": self.opened_at,
            "triaged_at": self.triaged_at,
            "resolved_at": self.resolved_at,
            "rules": self.rules,
            "summary": self.report.summary if self.report else None,
            "category": self.report.category if self.report else None,
            "severity": self.report.severity if self.report else None,
            "culprit": self.report.culprit.sha if self.report and self.report.culprit else None,
        }


class IncidentStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _row(self, r: sqlite3.Row) -> IncidentRecord:
        return IncidentRecord(
            id=r["id"], opened_at=r["opened_at"], triaged_at=r["triaged_at"], resolved_at=r["resolved_at"],
            status=r["status"], rules=json.loads(r["rules"] or "[]"),
            bundle=EvidenceBundle.model_validate_json(r["bundle"]) if r["bundle"] else None,
            report=RCAReport.model_validate_json(r["report"]) if r["report"] else None,
        )

    # -- writes ----------------------------------------------------------------------
    def open(self, rules: list[str], opened_at: float | None = None) -> IncidentRecord:
        ts = opened_at or time.time()
        cur = self._exec("INSERT INTO incidents (opened_at, rules) VALUES (?, ?)", (ts, json.dumps(rules)))
        iid = int(cur.lastrowid)
        for r in rules:
            self.add_event(iid, f"detector: {r}", ts)
        return self.get(iid)  # type: ignore[return-value]

    def add_rules(self, iid: int, rules: list[str]) -> None:
        inc = self.get(iid)
        if inc:
            self._exec("UPDATE incidents SET rules=? WHERE id=?", (json.dumps(inc.rules + rules), iid))
            for r in rules:
                self.add_event(iid, f"detector: {r}")

    def save_triage(self, iid: int, bundle: EvidenceBundle, report: RCAReport) -> None:
        self._exec(
            "UPDATE incidents SET bundle=?, report=?, triaged_at=?, status=CASE WHEN status='open' THEN 'triaged' ELSE status END WHERE id=?",
            (bundle.model_dump_json(), report.model_dump_json(), time.time(), iid),
        )
        self.add_event(iid, f"RCA ready ({report.generated_by}): {report.summary}")

    def resolve(self, iid: int, ts: float | None = None) -> IncidentRecord | None:
        ts = ts or time.time()
        self._exec("UPDATE incidents SET resolved_at=?, status='resolved' WHERE id=?", (ts, iid))
        self.add_event(iid, "recovered: health checks passing, error rate normal", ts)
        return self.get(iid)

    def add_event(self, iid: int, event: str, ts: float | None = None) -> None:
        self._exec("INSERT INTO timeline (incident_id, ts, event) VALUES (?, ?, ?)", (iid, ts or time.time(), event))

    # -- reads -----------------------------------------------------------------------
    def get(self, iid: int) -> IncidentRecord | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
        return self._row(r) if r else None

    def recent(self, limit: int = 20) -> list[IncidentRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def latest(self, with_bundle: bool = True) -> IncidentRecord | None:
        q = "SELECT * FROM incidents WHERE bundle IS NOT NULL ORDER BY id DESC LIMIT 1" if with_bundle else "SELECT * FROM incidents ORDER BY id DESC LIMIT 1"
        with self._lock:
            r = self._db.execute(q).fetchone()
        return self._row(r) if r else None

    def unresolved(self) -> list[IncidentRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM incidents WHERE status != 'resolved' ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def timeline(self, iid: int) -> list[tuple[float, str]]:
        with self._lock:
            rows = self._db.execute("SELECT ts, event FROM timeline WHERE incident_id=? ORDER BY ts, id", (iid,)).fetchall()
        return [(r["ts"], r["event"]) for r in rows]

    def close(self) -> None:
        self._db.close()
