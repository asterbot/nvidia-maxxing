"""Lazy docker-py client so the package imports (and unit tests run) without Docker."""

from __future__ import annotations

from functools import lru_cache
from typing import Any


# Plumbing that lives in the compose project but isn't part of the service being triaged.
INFRA_SERVICES = {"docker-proxy"}


class DockerUnavailable(RuntimeError):
    pass


# Event stream + one log follow per container + parallel inspect/log fetches easily exceed the
# default pool of 10 ("connection pool is full, discarding connection" warnings, extra handshakes).
POOL_SIZE = 32


def _widen_http_pool(client: Any) -> None:
    """docker-py only applies max_pool_size to unix/ssh sockets; a tcp/https DOCKER_HOST (e.g. the
    remote read-only proxy) uses requests' default adapters, so mount bigger ones ourselves."""
    import requests

    api = client.api
    if getattr(api, "_custom_adapter", None) is None and api.base_url.startswith(("http://", "https://")):
        for prefix in ("http://", "https://"):
            api.mount(prefix, requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=POOL_SIZE))


@lru_cache(maxsize=1)
def get_client() -> Any:
    try:
        import docker  # imported lazily on purpose
    except ImportError as e:  # pragma: no cover
        raise DockerUnavailable("docker-py is not installed") from e
    try:
        client = docker.from_env(max_pool_size=POOL_SIZE)
        _widen_http_pool(client)
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
    containers = get_client().containers.list(all=all, filters=project_filter(project, service))
    return [c for c in containers if c.labels.get("com.docker.compose.service") not in INFRA_SERVICES]
