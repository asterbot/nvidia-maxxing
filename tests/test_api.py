"""HTTP API handlers (aiohttp test client) with LLM_FAKE and an isolated SQLite store."""

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from maxxing.agent.fake import heuristic_report
from maxxing.api import create_app
from maxxing.incidents.store import IncidentStore

from .conftest import FIXTURES, load_fixture


@pytest.fixture
def store(settings):
    s = IncidentStore(settings.db_path)
    bundle, _ = load_fixture(FIXTURES / "2-null-deref.json")
    # opened "now": /ask only reuses incidents less than an hour old
    inc = s.open(["crash_loop: 4 container deaths in 60s"], time.time())
    s.save_triage(inc.id, bundle, heuristic_report(bundle))
    yield s
    s.close()


async def client_for(settings, store) -> TestClient:
    client = TestClient(TestServer(create_app(settings, store)))
    await client.start_server()
    return client


async def test_health_and_incidents(settings, store):
    c = await client_for(settings, store)
    try:
        r = await c.get("/health")
        assert r.status == 200 and (await r.json())["llm_fake"] is True
        r = await c.get("/incidents?limit=5")
        items = await r.json()
        assert len(items) == 1 and items[0]["category"] == "code" and items[0]["culprit"]
        r = await c.get(f"/incidents/{items[0]['id']}")
        body = await r.json()
        assert body["report"]["culprit"]["sha"] == items[0]["culprit"]
        assert body["evidence"]["error_clusters"][0]["type"] == "TypeError"
        assert any("RCA ready" in t["event"] for t in body["timeline"])
        assert (await c.get("/incidents/999")).status == 404
        assert (await c.get("/incidents/abc")).status == 400
        assert (await c.get("/incidents?limit=x")).status == 400
    finally:
        await c.close()


async def test_ask_uses_latest_incident(settings, store):
    c = await client_for(settings, store)
    try:
        r = await c.post("/ask", json={"question": "who broke it?"})
        answer = (await r.json())["answer"]
        assert r.status == 200 and "Marcus Lee" in answer
        r = await c.post("/ask", json={"question": "why?", "incident_id": 1})
        assert "fake LLM" in (await r.json())["answer"]
        assert (await c.post("/ask", json={"question": ""})).status == 400
        assert (await c.post("/ask", data="not json")).status == 400
        r = await c.post("/ask", json={"question": "x", "incident_id": 42})
        assert "not found" in (await r.json())["answer"]
    finally:
        await c.close()


async def test_triage_endpoint_without_docker(settings, store):
    """No Docker/LB/repo here: triage still returns a (degraded) report instead of failing."""
    c = await client_for(settings, store)
    try:
        r = await c.post("/triage")
        body = await r.json()
        assert r.status == 200 and body["report"]["summary"] and body["incident_id"] == 2
        assert body["evidence"]["collection_errors"]
    finally:
        await c.close()


async def test_bearer_token(settings, store):
    settings.maxxing_api_token = "s3cret"
    c = await client_for(settings, store)
    try:
        assert (await c.get("/health")).status == 200  # health stays open for probes
        assert (await c.get("/incidents")).status == 401
        assert (await c.get("/incidents", headers={"Authorization": "Bearer wrong"})).status == 401
        assert (await c.get("/incidents", headers={"Authorization": "Bearer s3cret"})).status == 200
    finally:
        await c.close()
