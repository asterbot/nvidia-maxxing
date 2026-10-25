"""OpenAI-compatible client for the *local* model server (Ollama / NIM / vLLM on the GB10)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from maxxing.config import Settings

log = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal", "host.openshell.internal"}


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".local") or host.startswith(("10.", "192.168.", "172."))


def make_client(settings: Settings) -> Any:
    """AsyncOpenAI pointed at the local server, or the deterministic fake when LLM_FAKE=1."""
    if settings.llm_fake:
        from maxxing.agent.fake import FakeChatClient

        return FakeChatClient()
    if not is_local_url(settings.llm_base_url):
        log.warning("LLM_BASE_URL %s does not look local; this project must not call remote LLMs", settings.llm_base_url)
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "local",
        timeout=settings.llm_timeout_s,
        max_retries=1,
    )


def llm_reachable(settings: Settings, timeout: float = 2.0) -> bool:
    """True if the model server answers /models and lists the configured model (or any model)."""
    try:
        r = httpx.get(settings.llm_base_url.rstrip("/") + "/models", timeout=timeout)
        if r.status_code != 200:
            return False
        ids = [m.get("id", "") for m in r.json().get("data", [])]
        return any(settings.llm_model in i or i in settings.llm_model for i in ids) or bool(ids)
    except (httpx.HTTPError, ValueError):
        return False
