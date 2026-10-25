"""Runtime configuration, loaded from the environment and an optional `.env` file."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Target service
    repo_path: Path = Field(default=PACKAGE_ROOT.parent / "very-important-project")
    compose_project: str = "vip"
    api_service: str = "api"
    lb_url: str = "http://localhost:8080"
    # Docker API of the machine running the service. Empty = the local socket / $DOCKER_HOST.
    # e.g. https://<id>.ngrok-free.app when the containers run on another machine (read-only proxy).
    docker_host: str = ""
    # When the service runs elsewhere: `git fetch` the repo clone until the deployed SHA shows up.
    git_auto_fetch: bool = False
    git_fetch_timeout_s: float = 60.0

    # Local LLM (OpenAI-compatible: Ollama, NIM, vLLM...). Never point this at a remote API.
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "nemotron-3-super"
    llm_api_key: str = "ollama"
    llm_fake: bool = False
    llm_max_steps: int = 8
    llm_timeout_s: float = 180.0
    llm_temperature: float = 0.1

    # Detector tuning
    health_interval_s: float = 2.0
    health_timeout_s: float = 3.0
    settle_s: float = 6.0  # wait after opening an incident so the log window fills in
    cooldown_s: float = 90.0
    recovery_s: float = 20.0

    # Evidence
    log_window_s: int = 180
    max_commits: int = 15

    # Read-only HTTP API for sandboxed agents (OpenClaw/NemoClaw in OpenShell)
    api_host: str = "0.0.0.0"
    api_port: int = 7070
    maxxing_api_token: str = ""  # env MAXXING_API_TOKEN; empty = no auth

    # Storage
    db_path: Path = Field(default=PACKAGE_ROOT / "incidents.db")

    def model_post_init(self, __context) -> None:
        if self.docker_host:  # docker-py reads it from the environment
            os.environ["DOCKER_HOST"] = self.docker_host

    @property
    def deploys_file(self) -> Path:
        return self.repo_path / ".deploys.json"

    @property
    def scenario_file(self) -> Path:
        return self.repo_path / ".scenario.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
