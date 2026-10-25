"""Lazy docker-py client so the package imports (and unit tests run) without Docker."""

from __future__ import annotations

from functools import lru_cache
from typing import Any


class DockerUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_client() -> Any:
    try:
        import docker  # imported lazily on purpose
    except ImportError as e:  # pragma: no cover
        raise DockerUnavailable("docker-py is not installed") from e
    try:
        client = docker.from_env()
        client.ping()
    except Exception as e:  # docker.errors.DockerException, connection errors, ...
        raise DockerUnavailable(f"cannot reach Docker daemon: {e}") from e
    return client


def project_filter(project: str, service: str | None = None) -> dict[str, list[str]]:
    labels = [f"com.docker.compose.project={project}"]
    if service:
        labels.append(f"com.docker.compose.service={service}")
    return {"label": labels}


def list_containers(project: str, service: str | None = None, all: bool = True) -> list[Any]:
    return get_client().containers.list(all=all, filters=project_filter(project, service))
