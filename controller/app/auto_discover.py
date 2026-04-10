"""Hop-limited auto-discovery from LLDP neighbors (Phase 2.8).

When onboarding a node, the operator can specify a hop limit (0–5). MNM
will walk LLDP neighbors outward from the seed node, attempting to auto-
onboard each unseen neighbor up to the specified depth.

Guard rails (Rule 6 — Human-in-the-Loop):
  - Default is 0 (disabled) — never auto-discovers unless explicitly requested
  - Hard cap: MNM_AUTO_DISCOVER_MAX (default 10) nodes per run
  - Hop limit: never exceeds the operator-specified depth
  - Loop prevention: visited set checked before every attempt
  - Exclusion list: respects both IP and device_name exclusions
  - Sequential onboarding: one device at a time to avoid credential lockouts
  - All auto-discovered nodes surfaced in a dashboard advisory for operator review
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from datetime import datetime, timedelta, timezone

from app import db, nautobot_client, endpoint_store
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="auto_discover")

MAX_HOPS = 5
DEFAULT_HOPS = int(os.environ.get("MNM_AUTO_DISCOVER_HOPS", "0"))
MAX_PER_RUN = int(os.environ.get("MNM_AUTO_DISCOVER_MAX", "10"))


# ---------------------------------------------------------------------------
# Persistent auto-discovery history (PostgreSQL-backed)
# ---------------------------------------------------------------------------

async def _save_run(summary: dict, triggered_by: str = "manual") -> None:
    """Persist an auto-discovery run to the database."""
    if not db.is_ready():
        return
    async with db.SessionLocal() as session:
        session.add(db.AutoDiscoveryRun(
            seed_node=summary.get("seed_node", ""),
            max_hops=summary.get("max_hops", 0),
            attempted=summary.get("attempted", 0),
            succeeded=summary.get("succeeded", 0),
            failed=summary.get("failed", 0),
            skipped=summary.get("skipped", 0),
            nodes=summary.get("nodes", []),
            triggered_by=triggered_by,
            started_at=datetime.fromisoformat(summary["started_at"]) if summary.get("started_at") else datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        ))
        await session.commit()


async def get_recent_auto_discovered(hours: int = 24) -> list[dict]:
    """Return auto-discovered nodes from the last N hours."""
    if not db.is_ready():
        return []
    from sqlalchemy import select
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.AutoDiscoveryRun)
            .where(db.AutoDiscoveryRun.started_at >= cutoff)
            .order_by(db.AutoDiscoveryRun.started_at.desc())
        )).scalars().all()
    results = []
    for run in rows:
        for node in (run.nodes or []):
            results.append({
                "name": node.get("name", ""),
                "ip": node.get("ip", ""),
                "parent_node": run.seed_node,
                "hop_depth": node.get("hop_depth", 0),
                "status": node.get("status", ""),
                "discovered_at": run.started_at.isoformat() if run.started_at else "",
            })
    return results


async def get_auto_discover_history(limit: int = 20) -> list[dict]:
    """Return recent auto-discovery run summaries from the database."""
    if not db.is_ready():
        return []
    from sqlalchemy import select
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.AutoDiscoveryRun)
            .order_by(db.AutoDiscoveryRun.started_at.desc())
            .limit(limit)
        )).scalars().all()
    return [r.to_dict() for r in rows]


# ---------------------------------------------------------------------------
# LLDP neighbor IP extraction
# ---------------------------------------------------------------------------

def _normalize_mac(mac: str) -> str:
    """Normalize MAC to upper-case colon-separated."""
    if not mac:
        return ""
    clean = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(clean) != 12:
        return mac.upper()
    return ":".join(clean[i:i+2] for i in range(0, 12, 2)).upper()


def _is_valid_ip(s: str) -> bool:
    """Check if a string is a valid IPv4 address (not link-local or loopback)."""
    try:
        addr = ipaddress.ip_address(s)
        return addr.version == 4 and not addr.is_loopback and not addr.is_link_local
    except ValueError:
        return False


def _extract_neighbor_info(iface_name: str, neighbor: dict) -> dict | None:
    """Extract usable identification from a NAPALM LLDP neighbor dict.

    NAPALM get_lldp_neighbors returns per-interface:
      [{hostname: str, port: str}]

    NAPALM get_lldp_neighbors_detail returns per-interface:
      [{remote_system_name, remote_chassis_id, remote_port,
        remote_port_description, remote_system_description,
        remote_system_capab, remote_system_enable_capab}]

    We need to extract:
      - A system name (for exclusion list matching and display)
      - A management IP (for onboarding — this is the tricky part)

    IP extraction priority:
      1. remote_chassis_id if it's a valid IP (some devices use mgmt IP as chassis ID)
      2. hostname if it's a valid IP
      3. DNS resolution of hostname/remote_system_name (attempted later)
    """
    if not isinstance(neighbor, dict):
        return None

    # Extract system name from various fields
    system_name = (
        neighbor.get("remote_system_name")
        or neighbor.get("hostname")
        or neighbor.get("system_name")
        or ""
    ).strip()

    # Strip FQDN to short name for matching
    short_name = system_name.split(".")[0] if system_name else ""

    # Try to find an IP
    candidate_ip = ""

    # 1. Chassis ID might be a management IP
    chassis_id = (neighbor.get("remote_chassis_id") or "").strip()
    if chassis_id and _is_valid_ip(chassis_id):
        candidate_ip = chassis_id

    # 2. hostname field might be an IP
    hostname = (neighbor.get("hostname") or "").strip()
    if not candidate_ip and hostname and _is_valid_ip(hostname):
        candidate_ip = hostname

    # 3. Remote system name might be an IP (rare)
    rsn = (neighbor.get("remote_system_name") or "").strip()
    if not candidate_ip and rsn and _is_valid_ip(rsn):
        candidate_ip = rsn

    if not system_name and not candidate_ip:
        return None

    return {
        "system_name": system_name,
        "short_name": short_name,
        "ip": candidate_ip,
        "chassis_id": chassis_id,
        "local_interface": iface_name,
        "remote_port": neighbor.get("port") or neighbor.get("remote_port") or "",
    }


async def _resolve_neighbor_ip(info: dict) -> str | None:
    """Try to resolve a management IP for a neighbor.

    Priority:
      1. Already have an IP from LLDP data
      2. Look up the system name in Nautobot devices (may already be onboarded)
      3. DNS resolution of system name
    """
    if info.get("ip"):
        return info["ip"]

    system_name = info.get("system_name", "")
    short_name = info.get("short_name", "")

    # Check Nautobot — the device might already exist with a primary IP
    if system_name or short_name:
        try:
            devices = await nautobot_client.get_devices()
            for dev in devices:
                dev_name = (dev.get("name") or "").lower()
                if dev_name and (dev_name == system_name.lower() or dev_name == short_name.lower()):
                    pip = dev.get("primary_ip4") or {}
                    if isinstance(pip, dict):
                        addr = pip.get("display") or pip.get("address") or ""
                        host = addr.split("/")[0] if "/" in addr else addr
                        if host and _is_valid_ip(host):
                            return host
        except Exception:
            pass

    # DNS resolution
    if system_name:
        try:
            loop = asyncio.get_event_loop()
            import socket
            result = await loop.run_in_executor(
                None, lambda: socket.gethostbyname(system_name)
            )
            if _is_valid_ip(result):
                return result
        except Exception:
            pass

    # Try short name too
    if short_name and short_name != system_name:
        try:
            loop = asyncio.get_event_loop()
            import socket
            result = await loop.run_in_executor(
                None, lambda: socket.gethostbyname(short_name)
            )
            if _is_valid_ip(result):
                return result
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Core auto-discovery engine
# ---------------------------------------------------------------------------

async def auto_discover_from_node(
    seed_node_name: str,
    max_hops: int,
    location_id: str,
    secrets_group_id: str,
    snmp_community: str = "",
    log_fn=None,
) -> dict:
    """Walk LLDP neighbors outward from a seed node, auto-onboarding each.

    Args:
        seed_node_name: Name of the already-onboarded node to start from
        max_hops: How many hops deep to walk (0 = disabled, max 5)
        location_id: Nautobot location UUID for new nodes
        secrets_group_id: Nautobot secrets group UUID
        snmp_community: SNMP community string for enrichment
        log_fn: Optional callback for progress messages (sweep _log function)

    Returns:
        Summary dict with attempted/succeeded/failed/skipped counts and node list
    """
    from app.discovery import _onboard_host, _onb_set, _detect_platform
    from app import polling

    def _log(msg: str):
        log.info("auto_discover", msg)
        if log_fn:
            log_fn(f"[Auto-discover] {msg}")

    # Validate
    max_hops = min(max(0, max_hops), MAX_HOPS)
    if max_hops == 0:
        return {"attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0, "nodes": []}

    _log(f"Starting auto-discovery from {seed_node_name}, max_hops={max_hops}")

    # Build visited set: seed + all currently onboarded nodes
    visited: set[str] = set()
    visited_ips: set[str] = set()

    try:
        poll_rows = await polling.get_all_poll_status()
        onboarded_names = {r["device_name"].lower() for r in poll_rows}
        visited.update(onboarded_names)
    except Exception:
        onboarded_names = set()

    visited.add(seed_node_name.lower())

    # Collect known device IPs to avoid re-probing
    try:
        devices = await nautobot_client.get_devices()
        for dev in devices:
            pip = dev.get("primary_ip4") or {}
            if isinstance(pip, dict):
                addr = pip.get("display") or pip.get("address") or ""
                host = addr.split("/")[0] if "/" in addr else addr
                if host:
                    visited_ips.add(host)
    except Exception:
        devices = []

    # Load exclusion lists
    try:
        excluded_ips = await endpoint_store.get_excluded_ips()
    except Exception:
        excluded_ips = set()
    try:
        excluded_names = await endpoint_store.get_excluded_device_names()
    except Exception:
        excluded_names = set()

    # Resolve seed node's device ID
    seed_device_id = None
    for dev in devices:
        if (dev.get("name") or "").lower() == seed_node_name.lower():
            seed_device_id = dev.get("id")
            break

    if not seed_device_id:
        _log(f"Seed node {seed_node_name} not found in Nautobot — cannot read LLDP")
        return {"attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0, "nodes": [],
                "error": f"Seed node {seed_node_name} not found"}

    # BFS queue: (neighbor_info, hops_remaining, parent_node)
    queue: list[tuple[dict, int, str]] = []
    attempted = 0
    succeeded = 0
    failed = 0
    skipped = 0
    result_nodes: list[dict] = []

    async def _fetch_lldp_neighbors(device_id: str) -> list[dict]:
        """Get LLDP neighbors for a device via NAPALM proxy."""
        try:
            data = await nautobot_client.napalm_get(device_id, "get_lldp_neighbors")
            raw = data.get("get_lldp_neighbors", {})
            neighbors = []
            if isinstance(raw, dict):
                for iface, neigh_list in raw.items():
                    if not isinstance(neigh_list, list):
                        continue
                    for n in neigh_list:
                        info = _extract_neighbor_info(iface, n)
                        if info:
                            neighbors.append(info)
            return neighbors
        except Exception as exc:
            _log(f"Failed to get LLDP neighbors: {exc}")
            return []

    # Seed the queue from the seed node's LLDP neighbors
    seed_neighbors = await _fetch_lldp_neighbors(seed_device_id)
    _log(f"Seed node {seed_node_name} has {len(seed_neighbors)} LLDP neighbor(s)")

    for info in seed_neighbors:
        queue.append((info, max_hops - 1, seed_node_name))

    # Process queue (BFS, sequential onboarding)
    while queue and attempted < MAX_PER_RUN:
        info, hops_remaining, parent = queue.pop(0)
        system_name = info.get("system_name", "")
        short_name = info.get("short_name", "")

        # Check if already visited by name
        if short_name.lower() in visited or system_name.lower() in visited:
            skipped += 1
            continue

        # Check exclusion list by name
        if short_name.lower() in {n.lower() for n in excluded_names}:
            _log(f"Skipping {system_name} — on exclusion list")
            skipped += 1
            visited.add(short_name.lower())
            continue

        # Resolve IP
        neighbor_ip = await _resolve_neighbor_ip(info)
        if not neighbor_ip:
            _log(f"Skipping {system_name} — no management IP found (chassis_id={info.get('chassis_id', '')})")
            skipped += 1
            visited.add(short_name.lower())
            result_nodes.append({
                "name": system_name, "ip": "", "status": "skipped_no_ip",
                "hop_depth": max_hops - hops_remaining, "parent": parent,
            })
            continue

        # Check exclusion list by IP
        if neighbor_ip in excluded_ips:
            _log(f"Skipping {system_name} ({neighbor_ip}) — IP on exclusion list")
            skipped += 1
            visited.add(short_name.lower())
            visited_ips.add(neighbor_ip)
            continue

        # Check if IP already visited
        if neighbor_ip in visited_ips:
            skipped += 1
            visited.add(short_name.lower())
            continue

        # Mark visited
        visited.add(short_name.lower())
        if system_name:
            visited.add(system_name.lower())
        visited_ips.add(neighbor_ip)

        hop_depth = max_hops - hops_remaining
        _log(f"Hop {hop_depth}: onboarding {system_name or neighbor_ip} (from {parent}, "
             f"interface {info.get('local_interface', '?')})")

        # Set progress tracking
        _onb_set(neighbor_ip, "submitting",
                 f"Auto-discover hop {hop_depth}: {system_name or neighbor_ip} (from {parent})",
                 auto_discover=True, parent_node=parent, hop_depth=hop_depth)

        # Attempt onboarding
        attempted += 1
        try:
            success = await _onboard_host(
                neighbor_ip, location_id, secrets_group_id,
                snmp_data=None,  # no pre-enrichment during auto-discover
            )
        except Exception as exc:
            _log(f"Onboarding exception for {neighbor_ip}: {exc}")
            success = False

        if success:
            succeeded += 1
            _log(f"Successfully onboarded {system_name or neighbor_ip}")
            result_nodes.append({
                "name": system_name, "ip": neighbor_ip, "status": "succeeded",
                "hop_depth": hop_depth, "parent": parent,
            })

            # Ensure poll tracking exists for the new node
            onb_state = _onb_set.__self__ if hasattr(_onb_set, '__self__') else None
            # Look up the device name from the onboarding state
            from app.discovery import _onboarding_state
            onb = _onboarding_state.get(neighbor_ip, {})
            new_name = onb.get("device_name") or system_name or ""
            if new_name:
                try:
                    await polling.ensure_device_polls(new_name)
                except Exception:
                    pass

            # If hops remaining, fetch this new node's LLDP neighbors
            if hops_remaining > 0:
                # Find the new device ID in Nautobot
                new_dev_id = None
                try:
                    new_devices = await nautobot_client.get_devices()
                    for d in new_devices:
                        pip = d.get("primary_ip4") or {}
                        addr = (pip.get("display") or pip.get("address") or "") if isinstance(pip, dict) else ""
                        host = addr.split("/")[0] if "/" in addr else addr
                        if host == neighbor_ip:
                            new_dev_id = d.get("id")
                            break
                except Exception:
                    pass

                if new_dev_id:
                    new_neighbors = await _fetch_lldp_neighbors(new_dev_id)
                    _log(f"{system_name or neighbor_ip} has {len(new_neighbors)} LLDP neighbor(s), "
                         f"queuing with {hops_remaining - 1} hops remaining")
                    for n_info in new_neighbors:
                        queue.append((n_info, hops_remaining - 1, system_name or neighbor_ip))
        else:
            failed += 1
            _log(f"Failed to onboard {system_name or neighbor_ip}")
            result_nodes.append({
                "name": system_name, "ip": neighbor_ip, "status": "failed",
                "hop_depth": hop_depth, "parent": parent,
            })

    # Log any remaining queue items as skipped (hit MAX_PER_RUN cap)
    if queue:
        _log(f"Auto-discovery cap reached ({MAX_PER_RUN}), {len(queue)} neighbor(s) not attempted")
        skipped += len(queue)

    summary = {
        "seed_node": seed_node_name,
        "max_hops": max_hops,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "nodes": result_nodes,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    _log(f"Auto-discovery complete: {attempted} attempted, {succeeded} succeeded, "
         f"{failed} failed, {skipped} skipped")

    # Persist to database
    triggered_by = "sweep" if log_fn else "manual"
    try:
        await _save_run(summary, triggered_by=triggered_by)
    except Exception as exc:
        log.warning("auto_discover_save_failed", "Failed to persist auto-discovery run",
                    context={"error": str(exc)})

    return summary
