"""Modular per-device collection polling.

Replaces the monolithic endpoint_collector.scheduled_collection_loop() with
independent job types (arp, mac, dhcp, lldp) that run on per-device schedules.
Inspired by Netdisco's macsuck/arpnip pattern.

Each job type is a standalone async function that collects data from one device,
updates the device_polls tracking table, and feeds results into the existing
endpoint correlation engine.

The poll_loop() queries device_polls for due jobs and dispatches them with
bounded concurrency, jittered next_due writes, and per-device grouping to
avoid hammering a single device with multiple job types simultaneously.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from datetime import datetime, timezone, timedelta

from app import db, nautobot_client, endpoint_store
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="polling")

# ---------------------------------------------------------------------------
# Default intervals from environment (seconds)
# ---------------------------------------------------------------------------

JOB_TYPES = ("arp", "mac", "dhcp", "lldp")

def _default_interval(job_type: str) -> int:
    defaults = {"arp": 300, "mac": 300, "dhcp": 600, "lldp": 3600}
    env_key = f"MNM_POLL_{job_type.upper()}_INTERVAL"
    # Fall back to old MNM_COLLECTION_INTERVAL (minutes) converted to seconds
    old_fallback = int(os.environ.get("MNM_COLLECTION_INTERVAL", "0")) * 60
    return int(os.environ.get(env_key, str(old_fallback or defaults.get(job_type, 300))))

POLL_CHECK_INTERVAL = int(os.environ.get("MNM_POLL_CHECK_INTERVAL", "30"))
MAX_CONCURRENT = int(os.environ.get("MNM_COLLECTION_CONCURRENCY",
                                     str(max(4, (os.cpu_count() or 4) * 2))))
JITTER_FACTOR = 0.10  # 10% random jitter on next_due


# ---------------------------------------------------------------------------
# Device poll table management
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_device_polls(device_name: str) -> None:
    """Insert default poll rows for a device if they don't exist."""
    if not db.is_ready():
        return
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        for jt in JOB_TYPES:
            existing = await session.execute(
                sa_select(db.DevicePoll).where(
                    db.DevicePoll.device_name == device_name,
                    db.DevicePoll.job_type == jt,
                )
            )
            if existing.scalar_one_or_none() is None:
                interval = _default_interval(jt)
                session.add(db.DevicePoll(
                    device_name=device_name,
                    job_type=jt,
                    interval_sec=interval,
                    enabled=True,
                    next_due=_utcnow(),  # due immediately on first appearance
                ))
        await session.commit()


async def populate_from_nautobot() -> int:
    """Seed device_polls from current Nautobot device inventory.

    Called at startup if the table is empty. Returns number of devices seeded.
    """
    if not db.is_ready():
        return 0
    async with db.SessionLocal() as session:
        from sqlalchemy import func, select as sa_select
        count = (await session.execute(
            sa_select(func.count()).select_from(db.DevicePoll)
        )).scalar() or 0
        if count > 0:
            return 0  # already populated

    try:
        devices = await nautobot_client.get_devices()
    except Exception as exc:
        log.warning("poll_seed_failed", "Could not seed device_polls from Nautobot",
                    context={"error": str(exc)})
        return 0

    seeded = 0
    for dev in devices:
        name = dev.get("name")
        if not name:
            continue
        await ensure_device_polls(name)
        seeded += 1

    if seeded:
        log.info("poll_seed_complete", "Seeded device_polls from Nautobot inventory",
                 context={"devices": seeded, "job_types": len(JOB_TYPES)})
    return seeded


async def get_all_poll_status() -> list[dict]:
    """Return all device_polls rows as dicts, grouped by device."""
    if not db.is_ready():
        return []
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        result = await session.execute(
            sa_select(db.DevicePoll).order_by(
                db.DevicePoll.device_name, db.DevicePoll.job_type
            )
        )
        return [row.to_dict() for row in result.scalars().all()]


async def get_device_poll_status(device_name: str) -> list[dict]:
    """Return poll rows for a single device."""
    if not db.is_ready():
        return []
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        result = await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name
            ).order_by(db.DevicePoll.job_type)
        )
        return [row.to_dict() for row in result.scalars().all()]


async def update_poll_config(device_name: str, job_type: str,
                             interval_sec: int | None = None,
                             enabled: bool | None = None) -> dict | None:
    """Update interval or enabled flag for a specific device/job_type."""
    if not db.is_ready():
        return None
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        result = await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name,
                db.DevicePoll.job_type == job_type,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        if interval_sec is not None:
            row.interval_sec = max(30, interval_sec)
        if enabled is not None:
            row.enabled = enabled
        await session.commit()
        await session.refresh(row)
        return row.to_dict()


async def trigger_device(device_name: str, job_type: str | None = None) -> int:
    """Set next_due = now() for a device (all job types or a specific one).

    Returns number of rows updated.
    """
    if not db.is_ready():
        return 0
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select, update as sa_update
        now = _utcnow()
        stmt = sa_update(db.DevicePoll).where(
            db.DevicePoll.device_name == device_name,
            db.DevicePoll.enabled == True,
        ).values(next_due=now)
        if job_type:
            stmt = stmt.where(db.DevicePoll.job_type == job_type)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount


# ---------------------------------------------------------------------------
# Individual job functions
# ---------------------------------------------------------------------------

async def _mark_attempt(device_name: str, job_type: str) -> None:
    if not db.is_ready():
        return
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        result = await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name,
                db.DevicePoll.job_type == job_type,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.last_attempt = _utcnow()
            await session.commit()


async def _mark_success(device_name: str, job_type: str, duration: float) -> None:
    if not db.is_ready():
        return
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        result = await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name,
                db.DevicePoll.job_type == job_type,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            now = _utcnow()
            jitter = row.interval_sec * JITTER_FACTOR * random.random()
            row.last_success = now
            row.last_attempt = now
            row.last_duration = round(duration, 2)
            row.last_error = None
            row.next_due = now + timedelta(seconds=row.interval_sec + jitter)
            await session.commit()


async def _mark_failure(device_name: str, job_type: str, error: str, duration: float) -> None:
    if not db.is_ready():
        return
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        result = await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name,
                db.DevicePoll.job_type == job_type,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            now = _utcnow()
            # On failure, retry after half the interval (backoff is the caller's job)
            row.last_attempt = now
            row.last_error = error[:500]
            row.last_duration = round(duration, 2)
            row.next_due = now + timedelta(seconds=row.interval_sec // 2)
            await session.commit()


def _resolve_device_id(devices: list[dict], device_name: str) -> str | None:
    """Find Nautobot device ID by name from a pre-fetched device list."""
    for d in devices:
        if (d.get("name") or "").lower() == device_name.lower():
            return d.get("id")
    return None


async def collect_arp(device_name: str, device_id: str) -> dict:
    """Collect ARP table from one device. Returns result dict."""
    t0 = time.monotonic()
    await _mark_attempt(device_name, "arp")
    try:
        data = await nautobot_client.napalm_get(device_id, "get_arp_table")
        entries = data.get("get_arp_table", [])
        duration = time.monotonic() - t0
        await _mark_success(device_name, "arp", duration)
        log.info("poll_arp_done", "ARP collection complete",
                 context={"device": device_name, "entries": len(entries), "duration": round(duration, 1)})
        return {"device_name": device_name, "job_type": "arp", "success": True,
                "entries": entries, "count": len(entries), "duration": duration}
    except Exception as exc:
        duration = time.monotonic() - t0
        err = str(exc)[:500]
        await _mark_failure(device_name, "arp", err, duration)
        log.warning("poll_arp_failed", "ARP collection failed",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "arp", "success": False,
                "error": err, "count": 0, "duration": duration}


async def collect_mac(device_name: str, device_id: str) -> dict:
    """Collect MAC address table from one device."""
    t0 = time.monotonic()
    await _mark_attempt(device_name, "mac")
    try:
        data = await nautobot_client.napalm_get(device_id, "get_mac_address_table")
        entries = data.get("get_mac_address_table", [])
        duration = time.monotonic() - t0
        await _mark_success(device_name, "mac", duration)
        log.info("poll_mac_done", "MAC collection complete",
                 context={"device": device_name, "entries": len(entries), "duration": round(duration, 1)})
        return {"device_name": device_name, "job_type": "mac", "success": True,
                "entries": entries, "count": len(entries), "duration": duration}
    except Exception as exc:
        duration = time.monotonic() - t0
        err = str(exc)[:500]
        await _mark_failure(device_name, "mac", err, duration)
        log.warning("poll_mac_failed", "MAC collection failed",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "mac", "success": False,
                "error": err, "count": 0, "duration": duration}


async def collect_dhcp(device_name: str, device_id: str, device_ip: str | None = None,
                       is_junos: bool = False) -> dict:
    """Collect DHCP bindings from one device (Junos only via PyEZ)."""
    t0 = time.monotonic()
    await _mark_attempt(device_name, "dhcp")

    if not is_junos or not device_ip:
        duration = time.monotonic() - t0
        await _mark_success(device_name, "dhcp", duration)
        return {"device_name": device_name, "job_type": "dhcp", "success": True,
                "entries": [], "count": 0, "duration": duration, "skipped": True}

    try:
        # Reuse existing DHCP collection from endpoint_collector
        from app.endpoint_collector import _get_dhcp_bindings_sync
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(None, _get_dhcp_bindings_sync, device_ip, device_name)
        duration = time.monotonic() - t0
        await _mark_success(device_name, "dhcp", duration)
        log.info("poll_dhcp_done", "DHCP collection complete",
                 context={"device": device_name, "entries": len(entries), "duration": round(duration, 1)})
        return {"device_name": device_name, "job_type": "dhcp", "success": True,
                "entries": entries, "count": len(entries), "duration": duration}
    except Exception as exc:
        duration = time.monotonic() - t0
        err = str(exc)[:500]
        await _mark_failure(device_name, "dhcp", err, duration)
        log.warning("poll_dhcp_failed", "DHCP collection failed",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "dhcp", "success": False,
                "error": err, "count": 0, "duration": duration}


async def collect_lldp(device_name: str, device_id: str) -> dict:
    """Collect LLDP neighbors from one device."""
    t0 = time.monotonic()
    await _mark_attempt(device_name, "lldp")
    try:
        data = await nautobot_client.napalm_get(device_id, "get_lldp_neighbors")
        neighbors = data.get("get_lldp_neighbors", {})
        count = sum(len(v) if isinstance(v, list) else 1 for v in neighbors.values())
        duration = time.monotonic() - t0
        await _mark_success(device_name, "lldp", duration)
        log.info("poll_lldp_done", "LLDP collection complete",
                 context={"device": device_name, "neighbors": count, "duration": round(duration, 1)})
        return {"device_name": device_name, "job_type": "lldp", "success": True,
                "entries": neighbors, "count": count, "duration": duration}
    except Exception as exc:
        duration = time.monotonic() - t0
        err = str(exc)[:500]
        await _mark_failure(device_name, "lldp", err, duration)
        log.warning("poll_lldp_failed", "LLDP collection failed",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "lldp", "success": False,
                "error": err, "count": 0, "duration": duration}


# ---------------------------------------------------------------------------
# Per-device orchestrator (runs all due job types for one device)
# ---------------------------------------------------------------------------

async def poll_device(device_name: str, device_id: str,
                      due_jobs: list[str],
                      device_ip: str | None = None,
                      is_junos: bool = False) -> list[dict]:
    """Run all due job types for a single device sequentially.

    Sequential per-device avoids hammering the same device with overlapping
    NAPALM sessions. Cross-device parallelism is handled by the poll_loop.
    """
    results = []
    for jt in due_jobs:
        if jt == "arp":
            results.append(await collect_arp(device_name, device_id))
        elif jt == "mac":
            results.append(await collect_mac(device_name, device_id))
        elif jt == "dhcp":
            results.append(await collect_dhcp(device_name, device_id, device_ip, is_junos))
        elif jt == "lldp":
            results.append(await collect_lldp(device_name, device_id))
    return results


# ---------------------------------------------------------------------------
# Endpoint correlation bridge
# ---------------------------------------------------------------------------

async def _correlate_and_record(all_results: list[dict]) -> dict:
    """Feed poll results into the existing endpoint correlation engine.

    Groups ARP + MAC + DHCP results by device, runs the correlator, then
    records endpoints to the controller DB and Nautobot IPAM — same pipeline
    as the monolithic collector but driven by per-device poll results.

    Multi-IP fix: builds a cross-device MAC→IPs map from ALL nodes' ARP tables
    before upserting, so that a MAC with IPs learned from different nodes (e.g.
    SRX320 with multiple interfaces) gets all its IPs merged into additional_ips.
    """
    from app.endpoint_collector import (
        _correlate_endpoints, _record_endpoint, _normalize_mac, _mac_vendor,
    )

    # Group by device
    by_device: dict[str, dict[str, list]] = {}
    for r in all_results:
        dn = r["device_name"]
        jt = r["job_type"]
        if dn not in by_device:
            by_device[dn] = {"arp": [], "mac": [], "dhcp": [], "lldp": []}
        if r.get("success") and r.get("entries"):
            by_device[dn][jt] = r["entries"]

    # --- Build cross-device MAC→IPs map from all ARP + DHCP data ---
    # This is the key fix for multi-IP endpoints: a MAC seen in ARP tables
    # of multiple nodes now gets all its IPs unioned before upsert.
    mac_all_ips: dict[str, set[str]] = {}
    for data in by_device.values():
        for arp_entry in data.get("arp", []):
            mac = _normalize_mac(arp_entry.get("mac", ""))
            ip = arp_entry.get("ip", "")
            if mac and ip:
                mac_all_ips.setdefault(mac, set()).add(ip)
        for dhcp_entry in data.get("dhcp", []):
            mac = _normalize_mac(dhcp_entry.get("mac", ""))
            ip = dhcp_entry.get("ip", "")
            if mac and ip:
                mac_all_ips.setdefault(mac, set()).add(ip)

    # Also include IPs from sweep observations (ip_observations table)
    try:
        sweep_mac_ips = await endpoint_store.get_mac_ip_map_from_observations()
        for mac, ips in sweep_mac_ips.items():
            mac_all_ips.setdefault(mac, set()).update(ips)
    except Exception:
        pass  # non-fatal: sweep data is supplementary

    # Pre-fetch uplinks and excludes once
    try:
        uplinks = await nautobot_client.get_uplinks()
    except Exception:
        uplinks = set()
    try:
        excluded_ips = await endpoint_store.get_excluded_ips()
    except Exception:
        excluded_ips = set()

    total_found = 0
    total_recorded = 0
    total_failed = 0

    for device_name, data in by_device.items():
        arp_entries = data.get("arp", [])
        mac_entries = data.get("mac", [])
        dhcp_entries = data.get("dhcp", [])

        if not arp_entries and not mac_entries:
            continue

        try:
            endpoints = _correlate_endpoints(arp_entries, mac_entries, dhcp_entries, device_name)
        except Exception as exc:
            log.warning("poll_correlate_failed", "Endpoint correlation failed",
                        context={"device": device_name, "error": str(exc)})
            continue

        total_found += len(endpoints)

        for ep in endpoints:
            ip = ep.get("ip", "")
            if ip in excluded_ips:
                continue

            # Inject cross-device IPs for this MAC into additional_ips
            mac = _normalize_mac(ep.get("mac", ""))
            cross_ips = mac_all_ips.get(mac, set())
            if cross_ips:
                existing_additional = list(ep.get("additional_ips") or [])
                for cip in cross_ips:
                    if cip and cip not in existing_additional:
                        existing_additional.append(cip)
                ep["additional_ips"] = existing_additional

            try:
                await endpoint_store.upsert_endpoint(ep, source="infrastructure", uplinks=uplinks)
                total_recorded += 1
            except Exception:
                total_failed += 1

            # Mirror to Nautobot IPAM
            if ip:
                try:
                    await _record_endpoint(ip, ep)
                except Exception:
                    pass

    return {
        "endpoints_found": total_found,
        "endpoints_recorded": total_recorded,
        "endpoints_failed": total_failed,
    }


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

_poll_state: dict = {
    "running": False,
    "last_check": None,
    "polls_dispatched": 0,
}


def get_poll_state() -> dict:
    return _poll_state.copy()


async def poll_loop() -> None:
    """Background loop: query device_polls for due jobs, dispatch them."""
    log.info("poll_loop_started", "Modular poll loop started",
             context={"check_interval": POLL_CHECK_INTERVAL, "max_concurrent": MAX_CONCURRENT})

    # Seed from Nautobot on first run
    await populate_from_nautobot()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    while True:
        try:
            _poll_state["last_check"] = _utcnow().isoformat()

            if not db.is_ready():
                await asyncio.sleep(POLL_CHECK_INTERVAL)
                continue

            # Find all due jobs
            now = _utcnow()
            async with db.SessionLocal() as session:
                from sqlalchemy import select as sa_select
                result = await session.execute(
                    sa_select(db.DevicePoll).where(
                        db.DevicePoll.enabled == True,
                        db.DevicePoll.next_due <= now,
                    ).order_by(db.DevicePoll.device_name, db.DevicePoll.job_type)
                )
                due_rows = result.scalars().all()

            if not due_rows:
                await asyncio.sleep(POLL_CHECK_INTERVAL)
                continue

            # Group by device
            device_jobs: dict[str, list[str]] = {}
            for row in due_rows:
                device_jobs.setdefault(row.device_name, []).append(row.job_type)

            # Pre-fetch device list for ID resolution
            try:
                devices = await nautobot_client.get_devices()
            except Exception as exc:
                log.warning("poll_device_fetch_failed", "Could not fetch devices",
                            context={"error": str(exc)})
                await asyncio.sleep(POLL_CHECK_INTERVAL)
                continue

            # Build device info map
            device_info: dict[str, dict] = {}
            for d in devices:
                name = d.get("name", "")
                did = d.get("id", "")
                pip = d.get("primary_ip4") or {}
                ip_display = pip.get("display") or pip.get("address") or "" if isinstance(pip, dict) else ""
                ip_addr = ip_display.split("/")[0] if "/" in ip_display else ip_display
                platform = d.get("platform") or {}
                plat_name = platform.get("name") or "" if isinstance(platform, dict) else ""
                device_info[name.lower()] = {
                    "id": did, "ip": ip_addr,
                    "is_junos": "junos" in plat_name.lower(),
                }

            # Dispatch per-device tasks with bounded concurrency
            all_results: list[dict] = []

            async def _run_device(dev_name: str, job_types: list[str]):
                async with semaphore:
                    info = device_info.get(dev_name.lower(), {})
                    dev_id = info.get("id")
                    if not dev_id:
                        # Ensure poll rows exist even if device disappeared
                        for jt in job_types:
                            await _mark_failure(dev_name, jt, "Device not found in Nautobot", 0)
                        return
                    results = await poll_device(
                        dev_name, dev_id, job_types,
                        device_ip=info.get("ip"),
                        is_junos=info.get("is_junos", False),
                    )
                    all_results.extend(results)

            tasks = [_run_device(dn, jts) for dn, jts in device_jobs.items()]
            _poll_state["running"] = True
            _poll_state["polls_dispatched"] = len(tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

            # Feed results into endpoint correlation
            if all_results:
                try:
                    summary = await _correlate_and_record(all_results)
                    log.info("poll_cycle_complete", "Poll cycle complete",
                             context={"devices": len(device_jobs), "jobs": len(due_rows),
                                      **summary})
                except Exception as exc:
                    log.warning("poll_correlate_error", "Correlation failed after poll cycle",
                                context={"error": str(exc)})

            _poll_state["running"] = False

        except Exception as exc:
            _poll_state["running"] = False
            log.error("poll_loop_error", "Poll loop error",
                      context={"error": str(exc)}, exc_info=True)

        await asyncio.sleep(POLL_CHECK_INTERVAL)
