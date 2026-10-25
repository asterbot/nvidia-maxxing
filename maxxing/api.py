"""Small read-only HTTP API so a sandboxed OpenClaw/NemoClaw agent can query the triage bot
without the Docker socket or repo access. Endpoints call the same service functions as the CLI.

  GET  /health
  GET  /incidents?limit=N
  GET  /incidents/{id}
  POST /ask      {"question": str, "incident_id"?: int} -> {"answer": str}
  POST /triage   -> {"incident_id", "report", "evidence"}
"""

from __future__ import annotations

import hmac
import logging
import time

from aiohttp import web

from maxxing import service
from maxxing.config import Settings
from maxxing.evidence.models import EvidenceBundle
from maxxing.incidents.store import IncidentStore

log = logging.getLogger(__name__)

SETTINGS_KEY: web.AppKey[Settings] = web.AppKey("settings", Settings)
STORE_KEY: web.AppKey[IncidentStore] = web.AppKey("store", IncidentStore)


def evidence_summary(b: EvidenceBundle) -> dict:
    return {
        "trigger": b.trigger,
        "impact": b.impact_hint,
        "category_hint": b.category_hint,
        "hints": b.hints,
        "deployed_sha": b.deployed_sha,
        "known_good_sha": b.known_good_sha,
        "containers": [c.one_line() for c in b.containers + b.dependencies],
        "log_stats": b.log_stats.model_dump(),
        "error_clusters": [
            {"count": c.count, "type": c.err_type, "message": c.message[:300], "replicas": c.replicas,
             "frames": [f"{f.repo_path}:{f.line}" for f in c.app_frames[:4]]}
            for c in b.clusters[:8]
        ],
        "commits": [
            {"sha": c.sha, "author": f"{c.author_name} <{c.author_email}>", "summary": c.summary,
             "files": [f.path for f in c.files]}
            for c in b.commits
        ],
        "candidates": [cand.model_dump() for cand in b.candidates[:5]],
        "collection_errors": b.collection_errors,
    }


@web.middleware
async def auth_middleware(request: web.Request, handler):
    token = request.app[SETTINGS_KEY].maxxing_api_token
    if token and request.path != "/health":
        got = request.headers.get("Authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    s = request.app[SETTINGS_KEY]
    return web.json_response({"ok": True, "model": s.llm_model, "llm_fake": s.llm_fake, "time": time.time()})


async def list_incidents(request: web.Request) -> web.Response:
    try:
        limit = max(1, min(int(request.query.get("limit", "10")), 100))
    except ValueError:
        return web.json_response({"error": "limit must be an integer"}, status=400)
    return web.json_response([i.summary() for i in request.app[STORE_KEY].recent(limit)])


async def get_incident(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    try:
        iid = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "id must be an integer"}, status=400)
    inc = store.get(iid)
    if inc is None:
        return web.json_response({"error": f"INC-{iid} not found"}, status=404)
    return web.json_response({
        **inc.summary(),
        "report": inc.report.model_dump(mode="json") if inc.report else None,
        "evidence": evidence_summary(inc.bundle) if inc.bundle else None,
        "timeline": [{"ts": ts, "event": ev} for ts, ev in store.timeline(iid)],
    })


async def ask(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    question = (body or {}).get("question") if isinstance(body, dict) else None
    if not isinstance(question, str) or not question.strip():
        return web.json_response({"error": "question (string) is required"}, status=400)
    iid = body.get("incident_id")
    if iid is not None and not isinstance(iid, int):
        return web.json_response({"error": "incident_id must be an integer"}, status=400)
    answer = await service.ask(request.app[SETTINGS_KEY], question.strip()[:2000], request.app[STORE_KEY], iid)
    return web.json_response({"answer": answer})


async def triage(request: web.Request) -> web.Response:
    result = await service.triage_now(request.app[SETTINGS_KEY], request.app[STORE_KEY], ["api triage"])
    return web.json_response({
        "incident_id": result.incident.id if result.incident else None,
        "report": result.report.model_dump(mode="json") if result.report else None,
        "evidence": evidence_summary(result.bundle),
        "timings": {"collect_s": round(result.collect_s, 2), "rca_s": round(result.rca_s, 2)},
    })


def create_app(settings: Settings, store: IncidentStore) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app[SETTINGS_KEY] = settings
    app[STORE_KEY] = store
    app.add_routes([
        web.get("/health", health),
        web.get("/incidents", list_incidents),
        web.get("/incidents/{id}", get_incident),
        web.post("/ask", ask),
        web.post("/triage", triage),
    ])
    return app


async def start_server(settings: Settings, store: IncidentStore, host: str | None = None, port: int | None = None) -> web.AppRunner:
    runner = web.AppRunner(create_app(settings, store), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host or settings.api_host, port or settings.api_port)
    await site.start()
    log.info("maxxing API listening on http://%s:%s", host or settings.api_host, port or settings.api_port)
    return runner
