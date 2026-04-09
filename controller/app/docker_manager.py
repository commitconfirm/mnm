"""Docker SDK wrapper for MNM Controller — read-only container management."""

import time

import docker

from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="docker")


def _client():
    return docker.from_env()


def get_containers() -> list[dict]:
    """List all containers on the mnm-network with status and health."""
    t0 = time.time()
    client = _client()
    containers = client.containers.list(all=True)
    results = []
    for c in containers:
        networks = c.attrs.get("NetworkSettings", {}).get("Networks", {})
        if "mnm-network" not in networks:
            continue

        health = "N/A"
        health_data = c.attrs.get("State", {}).get("Health")
        if health_data:
            health = health_data.get("Status", "N/A")

        ports = []
        port_bindings = c.attrs.get("NetworkSettings", {}).get("Ports") or {}
        for container_port, host_bindings in port_bindings.items():
            if host_bindings:
                for hb in host_bindings:
                    ports.append(f"{hb['HostPort']}->{container_port}")
            else:
                ports.append(container_port)

        try:
            img = c.image
            image_name = img.tags[0] if img.tags else img.short_id
        except Exception:
            # After a rebuild the old image may be pruned; c.image raises
            # ImageNotFound. Fall back to the image ID from container attrs.
            image_name = (c.attrs.get("Config") or {}).get("Image", "unknown")

        results.append({
            "name": c.name,
            "status": c.status,
            "health": health,
            "image": image_name,
            "ports": ports,
        })

    results.sort(key=lambda x: x["name"])
    duration_ms = round((time.time() - t0) * 1000)
    log.debug("containers_listed", "Listed containers on mnm-network", context={
        "count": len(results), "duration_ms": duration_ms,
    })
    return results


def get_container_logs(name: str, lines: int = 100) -> str:
    """Tail logs for a container."""
    try:
        client = _client()
        container = client.containers.get(name)
        log.debug("container_logs", "Fetching container logs", context={"container": name, "lines": lines})
        return container.logs(tail=lines).decode("utf-8", errors="replace")
    except Exception as exc:
        log.error("container_logs_error", "Failed to fetch container logs", context={"container": name, "error": str(exc)}, exc_info=True)
        raise
