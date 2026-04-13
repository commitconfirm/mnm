"""ICMP/TCP endpoint probe engine.

Lightweight latency baseline for known endpoints. Probes are operator-triggered
(not automatic). ICMP via system ping, TCP via asyncio.open_connection.
Read-only: connect, measure, disconnect.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from datetime import datetime, timezone

from app import db, endpoint_store
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="probes")

_MAX_CONCURRENT = 20
_ICMP_COUNT = 3
_ICMP_TIMEOUT = 1  # per-packet timeout in seconds
_TCP_TIMEOUT = 2
_TCP_FALLBACK_PORTS = [80, 443, 22]

_HAS_PING = shutil.which("ping") is not None

# Probe run state (in-memory, single-instance)
_state = {
    "running": False,
    "total": 0,
    "completed": 0,
    "reachable": 0,
    "unreachable": 0,
    "started_at": None,
}


def get_state() -> dict:
    return dict(_state)


# ---------------------------------------------------------------------------
# ICMP probe via system ping
# ---------------------------------------------------------------------------

async def _ping(ip: str) -> dict:
    """Run system ping and parse results.

    Returns: {reachable, latency_ms, packet_loss, probe_type: "icmp"}
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", str(_ICMP_COUNT), "-W", str(_ICMP_TIMEOUT), ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_ICMP_COUNT * _ICMP_TIMEOUT + 3)
        output = stdout.decode("utf-8", errors="replace")

        # Parse packet loss: "3 packets transmitted, 3 received, 0% packet loss"
        loss_match = re.search(r"(\d+)% packet loss", output)
        loss = float(loss_match.group(1)) / 100.0 if loss_match else 1.0

        # Parse avg latency: "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.012 ms"
        rtt_match = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", output)
        latency = float(rtt_match.group(1)) if rtt_match else None

        return {
            "reachable": loss < 1.0,
            "latency_ms": round(latency, 2) if latency is not None else None,
            "packet_loss": round(loss, 2),
            "probe_type": "icmp",
            "tcp_port": None,
        }
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return {"reachable": False, "latency_ms": None, "packet_loss": 1.0,
                "probe_type": "icmp", "tcp_port": None}


# ---------------------------------------------------------------------------
# TCP probe
# ---------------------------------------------------------------------------

async def _tcp_probe(ip: str, port: int) -> dict:
    """Measure TCP connect latency to a single port.

    Returns: {reachable, latency_ms, probe_type: "tcp", tcp_port}
    """
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=_TCP_TIMEOUT,
        )
        latency = (time.monotonic() - t0) * 1000
        writer.close()
        await writer.wait_closed()
        return {
            "reachable": True,
            "latency_ms": round(latency, 2),
            "packet_loss": None,
            "probe_type": "tcp",
            "tcp_port": port,
        }
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return {
            "reachable": False,
            "latency_ms": None,
            "packet_loss": None,
            "probe_type": "tcp",
            "tcp_port": port,
        }


# ---------------------------------------------------------------------------
# Composite probe: ICMP first, fall back to TCP
# ---------------------------------------------------------------------------

async def _probe_one(target: dict) -> dict:
    """Probe a single endpoint. ICMP first, TCP fallback."""
    ip = target.get("ip", "")
    mac = target.get("mac", "")
    if not ip:
        return {"mac": mac, "ip": "", "reachable": False, "latency_ms": None,
                "packet_loss": None, "probe_type": "none", "tcp_port": None}

    # Try ICMP first (if ping is available)
    if _HAS_PING:
        result = await _ping(ip)
        if result["reachable"]:
            result["mac"] = mac
            result["ip"] = ip
            return result

    # Fall back to TCP: try known open ports first, then common defaults
    open_ports = target.get("open_ports", [])
    tcp_ports = []
    for p in open_ports:
        if isinstance(p, str) and p.startswith("tcp/"):
            try:
                tcp_ports.append(int(p.split("/")[1]))
            except (ValueError, IndexError):
                pass
        elif isinstance(p, int):
            tcp_ports.append(p)
    if not tcp_ports:
        tcp_ports = list(_TCP_FALLBACK_PORTS)

    for port in tcp_ports[:5]:  # max 5 ports
        result = await _tcp_probe(ip, port)
        if result["reachable"]:
            result["mac"] = mac
            result["ip"] = ip
            return result

    return {"mac": mac, "ip": ip, "reachable": False, "latency_ms": None,
            "packet_loss": None, "probe_type": "tcp", "tcp_port": tcp_ports[0] if tcp_ports else None}


# ---------------------------------------------------------------------------
# Bulk probe
# ---------------------------------------------------------------------------

async def probe_endpoints(targets: list[dict] | None = None) -> dict:
    """Probe endpoints and store results.

    If targets is None, probes all active endpoints with IPs.
    targets: list of {mac, ip, open_ports (optional)}
    """
    if _state["running"]:
        return {"status": "already_running"}

    _state["running"] = True
    _state["completed"] = 0
    _state["reachable"] = 0
    _state["unreachable"] = 0
    _state["started_at"] = datetime.now(timezone.utc).isoformat()

    # Build target list
    if targets is None:
        eps = await endpoint_store.list_endpoints()
        targets = []
        for ep in eps:
            ip = ep.get("ip") or ep.get("current_ip")
            if not ip:
                continue
            targets.append({
                "mac": ep.get("mac") or ep.get("mac_address", ""),
                "ip": ip,
                "open_ports": [],
            })
        # Enrich with known open ports from ip_observations
        try:
            from sqlalchemy import select
            async with db.SessionLocal() as session:
                for t in targets:
                    row = (await session.execute(
                        select(db.IPObservation)
                        .where(db.IPObservation.ip == t["ip"])
                        .order_by(db.IPObservation.observed_at.desc())
                        .limit(1)
                    )).scalar_one_or_none()
                    if row and row.ports_open:
                        t["open_ports"] = row.ports_open if isinstance(row.ports_open, list) else []
        except Exception:
            pass

    _state["total"] = len(targets)
    now = datetime.now(timezone.utc)

    log.info("probe_started", f"Probing {len(targets)} endpoints",
             context={"count": len(targets), "has_ping": _HAS_PING})

    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _limited(t):
        async with sem:
            return await _probe_one(t)

    results = await asyncio.gather(*[_limited(t) for t in targets], return_exceptions=True)

    # Store results
    stored = 0
    try:
        async with db.SessionLocal() as session:
            for r in results:
                if isinstance(r, Exception):
                    _state["unreachable"] += 1
                    _state["completed"] += 1
                    continue
                _state["completed"] += 1
                if r.get("reachable"):
                    _state["reachable"] += 1
                else:
                    _state["unreachable"] += 1
                session.add(db.EndpointProbe(
                    mac=r.get("mac", ""),
                    ip=r.get("ip", ""),
                    probe_type=r.get("probe_type", "tcp"),
                    tcp_port=r.get("tcp_port"),
                    latency_ms=r.get("latency_ms"),
                    reachable=r.get("reachable", False),
                    packet_loss=r.get("packet_loss"),
                    probed_at=now,
                ))
                stored += 1
            await session.commit()
    except Exception as e:
        log.error("probe_store_failed", "Failed to store probe results",
                  context={"error": str(e)})

    _state["running"] = False
    log.info("probe_complete", f"Probing complete: {_state['reachable']} reachable, {_state['unreachable']} unreachable",
             context={"total": _state["total"], "reachable": _state["reachable"],
                       "unreachable": _state["unreachable"], "stored": stored})

    return {
        "status": "complete",
        "total": _state["total"],
        "reachable": _state["reachable"],
        "unreachable": _state["unreachable"],
    }
