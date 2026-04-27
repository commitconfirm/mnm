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

from app import (
    db, nautobot_client, endpoint_store, snmp_collector,
    arp_snmp, mac_snmp, lldp_snmp,
)
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="polling")

# ---------------------------------------------------------------------------
# Default intervals from environment (seconds)
# ---------------------------------------------------------------------------

JOB_TYPES = ("arp", "mac", "dhcp", "lldp", "routes", "bgp")

# One-shot job types are scheduled individually (not seeded by
# ensure_device_polls) and disable themselves on success. ``phase2_populate``
# is seeded by the onboarding orchestrator at Step G.5 and disabled by the
# dispatch branch below once run_phase2 returns success.
ONE_SHOT_JOB_TYPES = ("phase2_populate",)


def _default_interval(job_type: str) -> int:
    defaults = {"arp": 300, "mac": 300, "dhcp": 600, "lldp": 3600,
                "routes": 3600, "bgp": 3600,
                # Retry interval for phase2 (only used after a failure —
                # on success the row is disabled).
                "phase2_populate": 300}
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


async def ensure_phase2_populate_row(device_name: str) -> None:
    """Seed a one-shot ``phase2_populate`` poll row for a freshly-onboarded
    device. Idempotent — re-enables and resets ``next_due`` if a disabled
    row from a previous onboarding still exists.

    Called from the onboarding orchestrator's Step G.5 (Prompt 6). The
    poll loop picks the row up on its next tick (≤ ``MNM_POLL_CHECK_INTERVAL``
    seconds), runs :func:`app.onboarding.network_sync.run_phase2`, and
    disables the row on success.
    """
    if not db.is_ready():
        return
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        existing = (await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name,
                db.DevicePoll.job_type == "phase2_populate",
            )
        )).scalar_one_or_none()
        now = _utcnow()
        if existing is None:
            session.add(db.DevicePoll(
                device_name=device_name,
                job_type="phase2_populate",
                interval_sec=_default_interval("phase2_populate"),
                enabled=True,
                next_due=now,
            ))
        else:
            existing.enabled = True
            existing.next_due = now
            existing.last_error = None
        await session.commit()


async def disable_device_poll(device_name: str, job_type: str) -> None:
    """Mark a poll row disabled — used for one-shot jobs on success."""
    if not db.is_ready():
        return
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        row = (await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name,
                db.DevicePoll.job_type == job_type,
            )
        )).scalar_one_or_none()
        if row:
            row.enabled = False
            row.last_success = _utcnow()
            row.last_attempt = _utcnow()
            row.last_error = None
            await session.commit()


async def get_phase2_state(device_name: str) -> "dict | None":
    """Compute the Phase 2 state for one device.

    Returns ``None`` when no ``phase2_populate`` row exists (legacy
    plugin-onboarded devices). Otherwise returns a dict suitable for
    both the ``GET /api/onboarding/phase2-status/{device_name}`` endpoint
    and the ``/api/nodes`` list response. ``state`` is one of:

      * ``pending`` — row exists, never attempted (``last_attempt`` is
        ``None``).
      * ``running`` — row enabled, ``last_attempt`` is newer than
        ``last_success`` within the last 60 s.
      * ``completed`` — ``enabled=False`` and ``last_success`` populated.
      * ``failed`` — row enabled, ``last_attempt`` newer than
        ``last_success``, outside the running window.
    """
    if not db.is_ready():
        return None
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        row = (await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name,
                db.DevicePoll.job_type == "phase2_populate",
            )
        )).scalar_one_or_none()
    if row is None:
        return None
    now = _utcnow()
    last_attempt = row.last_attempt
    last_success = row.last_success
    enabled = bool(row.enabled)
    if not enabled and last_success is not None:
        state = "completed"
    elif last_attempt is None:
        state = "pending"
    elif (last_success is None or last_attempt > last_success) \
            and last_attempt > now - timedelta(seconds=60):
        state = "running"
    elif last_success is None or (last_attempt and last_attempt > last_success):
        state = "failed"
    else:
        state = "pending"
    return {
        "device_name": device_name,
        "state": state,
        "enabled": enabled,
        "last_attempt": last_attempt.isoformat() if last_attempt else None,
        "last_success": last_success.isoformat() if last_success else None,
        "next_due": row.next_due.isoformat() if row.next_due else None,
        "last_error": row.last_error,
    }


async def defer_device_poll(
    device_name: str, job_type: str, retry_in_seconds: int,
    *, error: "str | None" = None,
) -> None:
    """Push a poll row's ``next_due`` forward — used for one-shot job
    retry backoff on failure. ``error`` is persisted into ``last_error``
    for surfacing via the phase2-status endpoint / operator log search."""
    if not db.is_ready():
        return
    async with db.SessionLocal() as session:
        from sqlalchemy import select as sa_select
        row = (await session.execute(
            sa_select(db.DevicePoll).where(
                db.DevicePoll.device_name == device_name,
                db.DevicePoll.job_type == job_type,
            )
        )).scalar_one_or_none()
        if row:
            row.last_attempt = _utcnow()
            row.next_due = _utcnow() + timedelta(seconds=retry_in_seconds)
            if error:
                row.last_error = error[:500]
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


async def collect_arp(device_name: str, device_id: str,
                      device_ip: "str | None" = None) -> dict:
    """Collect ARP table from one device via direct SNMP (Block C P3).

    Walks ipNetToMediaTable / ipNetToPhysicalTable through ``arp_snmp``,
    then resolves each entry's ifIndex to an interface name via
    ``snmp_collector.collect_ifindex_to_name``. Unresolved ifIndex values
    fall back to ``ifindex:N`` sentinel strings so data is preserved
    (Rule 7) even when ifTable is unwalkable.

    Returns the same {device_name, job_type, success, entries, count,
    duration} shape the dispatcher and ``_correlate_and_record`` expect.
    Each entry dict matches the {ip, mac, interface} contract that
    ``upsert_node_arp_bulk`` and ``endpoint_collector._correlate_endpoints``
    read.
    """
    t0 = time.monotonic()
    await _mark_attempt(device_name, "arp")

    if not device_ip:
        duration = time.monotonic() - t0
        err = "no primary_ip4 on device"
        await _mark_failure(device_name, "arp", err, duration)
        log.warning("arp_snmp_collect_failed",
                    "ARP collection skipped — no device IP",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "arp", "success": False,
                "error": err, "count": 0, "duration": duration}

    community = os.environ.get("SNMP_COMMUNITY", "public")
    log.debug("arp_snmp_collect_start", "Direct-SNMP ARP collection start",
              context={"device": device_name})

    try:
        arp_entries = await arp_snmp.collect_arp(device_ip, community)
    except snmp_collector.SnmpError as exc:
        duration = time.monotonic() - t0
        err = f"{exc.__class__.__name__}: {str(exc)[:400]}"
        await _mark_failure(device_name, "arp", err, duration)
        log.warning("arp_snmp_collect_failed",
                    "Direct-SNMP ARP collection failed",
                    context={"device": device_name, "error": err,
                             "exc_class": exc.__class__.__name__})
        return {"device_name": device_name, "job_type": "arp", "success": False,
                "error": err, "count": 0, "duration": duration}

    # ifIndex → interface name. collect_ifindex_to_name swallows SnmpError
    # internally and returns {} on failure; an empty map means every entry
    # gets the ifindex:N sentinel — degraded but not lost.
    name_map = await snmp_collector.collect_ifindex_to_name(device_ip, community)
    if not name_map:
        log.warning("arp_snmp_ifindex_lookup_empty",
                    "ifIndex→name resolution returned empty; "
                    "entries will use ifindex:N sentinel",
                    context={"device": device_name,
                             "entries": len(arp_entries)})

    # Dedupe by (ip, mac) — the upsert's UNIQUE constraint is
    # (node_name, ip, mac, vrf) and PostgreSQL ON CONFLICT DO UPDATE
    # raises CardinalityViolationError if the same key appears twice
    # in one batch. Junos exposes loopback IPs on multiple lo0.X
    # subinterfaces, producing repeats with different ifIndex values
    # for the same (ip, mac) pair. Keep the first interface seen per
    # key — matches NAPALM's implicit dedup behavior.
    seen_keys: set[tuple[str, str]] = set()
    entries: list[dict] = []
    for e in arp_entries:
        key = (e.ip_address, (e.mac_address or "").upper())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        entries.append({
            "ip": e.ip_address,
            "mac": e.mac_address,
            "interface": name_map.get(e.interface_index,
                                      f"ifindex:{e.interface_index}"),
        })

    try:
        await endpoint_store.upsert_node_arp_bulk(device_name, entries)
    except Exception:  # noqa: BLE001 — persistence failure shouldn't kill collection
        pass

    duration = time.monotonic() - t0
    await _mark_success(device_name, "arp", duration)
    log.info("arp_snmp_collect_complete",
             "Direct-SNMP ARP collection complete",
             context={"device": device_name, "entries": len(entries),
                      "duration": round(duration, 1)})
    return {"device_name": device_name, "job_type": "arp", "success": True,
            "entries": entries, "count": len(entries), "duration": duration}


async def collect_mac(device_name: str, device_id: str,
                      device_ip: "str | None" = None) -> dict:
    """Collect MAC/FDB table from one device via direct SNMP (Block C P4).

    Walks dot1qTpFdbTable / dot1dTpFdbTable through ``mac_snmp``, then
    resolves each entry's ``bridge_port`` to an interface name through two
    paired walks: ``snmp_collector.collect_bridgeport_to_ifindex`` (BRIDGE-MIB
    dot1dBasePortIfIndex) and ``snmp_collector.collect_ifindex_to_name``
    (IF-MIB ifName / ifDescr fallback). Unresolved bridge ports fall back to
    ``ifindex:N`` sentinel strings so data is preserved (Rule 7) even when the
    bridge-port table or ifTable is unwalkable.

    Returns the same {device_name, job_type, success, entries, count,
    duration} shape the dispatcher and ``_correlate_and_record`` expect.
    Each entry dict matches the {mac, interface, vlan, static} contract that
    ``upsert_node_mac_bulk`` and ``endpoint_collector._correlate_endpoints``
    read.
    """
    t0 = time.monotonic()
    await _mark_attempt(device_name, "mac")

    if not device_ip:
        duration = time.monotonic() - t0
        err = "no primary_ip4 on device"
        await _mark_failure(device_name, "mac", err, duration)
        log.warning("mac_snmp_collect_failed",
                    "MAC collection skipped — no device IP",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "mac", "success": False,
                "error": err, "count": 0, "duration": duration}

    community = os.environ.get("SNMP_COMMUNITY", "public")
    log.debug("mac_snmp_collect_start", "Direct-SNMP MAC collection start",
              context={"device": device_name})

    try:
        mac_entries = await mac_snmp.collect_mac(device_ip, community)
    except snmp_collector.SnmpError as exc:
        duration = time.monotonic() - t0
        err = f"{exc.__class__.__name__}: {str(exc)[:400]}"
        await _mark_failure(device_name, "mac", err, duration)
        log.warning("mac_snmp_collect_failed",
                    "Direct-SNMP MAC collection failed",
                    context={"device": device_name, "error": err,
                             "exc_class": exc.__class__.__name__})
        return {"device_name": device_name, "job_type": "mac", "success": False,
                "error": err, "count": 0, "duration": duration}

    # Bridge-port → ifIndex and ifIndex → name. Both helpers swallow SnmpError
    # internally and return {} on failure; an empty map means affected entries
    # get the ifindex:N sentinel — degraded but data preserved.
    bridge_to_ifindex = await snmp_collector.collect_bridgeport_to_ifindex(
        device_ip, community,
    )
    name_map = await snmp_collector.collect_ifindex_to_name(device_ip, community)

    sentinel_count = 0
    # Dedupe by (mac.upper(), interface, vlan_int) — matches the upsert's
    # uq_mac_node_mac_iface_vlan unique constraint. Same dedup discipline as
    # the ARP path (P3): PostgreSQL ON CONFLICT DO UPDATE raises
    # CardinalityViolationError if the same constrained tuple appears twice
    # in one batch. Within the FDB table this is rare, but defensive against
    # orphan-FDB rows that all dedupe to vlan=0.
    seen_keys: set[tuple[str, str, int]] = set()
    entries: list[dict] = []
    for entry in mac_entries:
        ifindex = bridge_to_ifindex.get(entry.bridge_port)
        if ifindex is None:
            # Bridge-port not in the dot1dBasePortTable — keep bridge_port in
            # the sentinel so operators can correlate back to the FDB row.
            interface = f"ifindex:{entry.bridge_port}"
            sentinel_count += 1
        else:
            resolved = name_map.get(ifindex)
            if resolved is None:
                interface = f"ifindex:{ifindex}"
                sentinel_count += 1
            else:
                interface = resolved

        vlan_int = int(entry.vlan or 0)
        mac_upper = (entry.mac_address or "").upper()
        key = (mac_upper, interface, vlan_int)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        entries.append({
            "mac": entry.mac_address,
            "interface": interface,
            "vlan": vlan_int,
            # static = entry was administratively configured (entry_status
            # of "self" or "mgmt"); learned/other map to dynamic.
            "static": entry.entry_status in ("self", "mgmt"),
        })

    try:
        await endpoint_store.upsert_node_mac_bulk(device_name, entries)
    except Exception:  # noqa: BLE001 — persistence failure shouldn't kill collection
        pass

    duration = time.monotonic() - t0
    await _mark_success(device_name, "mac", duration)
    log.info("mac_snmp_collect_complete",
             "Direct-SNMP MAC collection complete",
             context={"device": device_name, "entries": len(entries),
                      "sentinel_entries": sentinel_count,
                      "duration": round(duration, 1)})
    return {"device_name": device_name, "job_type": "mac", "success": True,
            "entries": entries, "count": len(entries), "duration": duration}


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


async def collect_lldp(device_name: str, device_id: str,
                       device_ip: "str | None" = None) -> dict:
    """Collect LLDP neighbors from one device via direct SNMP (Block C P5).

    Walks lldpRemTable + lldpRemManAddrTable through ``lldp_snmp``. Uses
    ``snmp_collector.collect_ifindex_to_name`` to resolve each neighbor's
    ``local_port_ifindex`` to a Junos-style local-interface name; sentinel
    ``ifindex:N`` on resolution miss preserves the entry per Rule 7.

    Adapter groups by local interface name to match the existing
    ``upsert_node_lldp_bulk`` dict-shape contract (NAPALM-shape held during
    P5/P6 soak window). All 5 expansion columns added by P2 migration
    (``local_port_ifindex``, ``local_port_name``,
    ``remote_chassis_id_subtype``, ``remote_port_id_subtype``,
    ``remote_system_description``) populate from the LldpNeighbor fields.

    Returns the same {device_name, job_type, success, entries, count,
    duration} shape the dispatcher expects. ``entries`` is the grouped
    dict for downstream readers.
    """
    t0 = time.monotonic()
    await _mark_attempt(device_name, "lldp")

    if not device_ip:
        duration = time.monotonic() - t0
        err = "no primary_ip4 on device"
        await _mark_failure(device_name, "lldp", err, duration)
        log.warning("lldp_snmp_collect_failed",
                    "LLDP collection skipped — no device IP",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "lldp", "success": False,
                "error": err, "count": 0, "duration": duration}

    community = os.environ.get("SNMP_COMMUNITY", "public")
    log.debug("lldp_snmp_collect_start", "Direct-SNMP LLDP collection start",
              context={"device": device_name})

    try:
        neighbors = await lldp_snmp.collect_lldp(device_ip, community)
    except snmp_collector.SnmpError as exc:
        duration = time.monotonic() - t0
        err = f"{exc.__class__.__name__}: {str(exc)[:400]}"
        await _mark_failure(device_name, "lldp", err, duration)
        log.warning("lldp_snmp_collect_failed",
                    "Direct-SNMP LLDP collection failed",
                    context={"device": device_name, "error": err,
                             "exc_class": exc.__class__.__name__})
        return {"device_name": device_name, "job_type": "lldp", "success": False,
                "error": err, "count": 0, "duration": duration}

    # ifIndex → name resolution. lldp_snmp.collect_lldp already resolves
    # local_port_name internally via the same helper, but we re-run here
    # to derive the *grouping key* (the dict key the upsert uses to derive
    # local_interface). Empty map → all entries get ifindex:N sentinels.
    name_map = await snmp_collector.collect_ifindex_to_name(device_ip, community)

    sentinel_count = 0
    grouped: dict[str, list[dict]] = {}
    # Dedupe by the upsert's UNIQUE constraint
    # (node_name, local_interface, remote_system_name, remote_port).
    # Two distinct neighbors on the same local port can collapse to the
    # same key when both have remote_system_name=None (None → "" via the
    # `or ""` coercion below) and identical remote_port_id — observed on
    # ex4300-48t ge-0/0/24 with two unmanaged neighbors advertising the
    # same port-MAC identifier but different chassis IDs. PostgreSQL ON
    # CONFLICT DO UPDATE raises CardinalityViolationError on a single
    # batch with duplicate keys; same class of bug as P3's Junos lo0.X
    # collision. Keep first seen — matches NAPALM's implicit dedup.
    seen_keys: set[tuple[str, str, str]] = set()
    for n in neighbors:
        local_idx = n.local_port_ifindex
        local_iface = name_map.get(local_idx, f"ifindex:{local_idx}")
        if local_iface.startswith("ifindex:"):
            sentinel_count += 1

        sys_name = n.remote_system_name or ""
        remote_port = n.remote_port_id or ""
        key = (local_iface, sys_name, remote_port)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        grouped.setdefault(local_iface, []).append({
            # Existing NAPALM-shape fields the upsert reads
            "remote_system_name": sys_name,
            "remote_port": remote_port,
            "remote_chassis_id": n.remote_chassis_id or "",
            "remote_management_ip": n.management_ip or "",
            # Block C P2 expansion columns (P5 first populator)
            "local_port_ifindex": local_idx,
            "local_port_name": n.local_port_name or local_iface,
            "remote_chassis_id_subtype": n.remote_chassis_id_subtype,
            "remote_port_id_subtype": n.remote_port_id_subtype,
            "remote_system_description": n.remote_system_description,
        })

    try:
        await endpoint_store.upsert_node_lldp_bulk(device_name, grouped)
    except Exception:  # noqa: BLE001 — persistence failure shouldn't kill collection
        pass

    persisted_count = sum(len(v) for v in grouped.values())
    duration = time.monotonic() - t0
    await _mark_success(device_name, "lldp", duration)
    log.info("lldp_snmp_collect_complete",
             "Direct-SNMP LLDP collection complete",
             context={"device": device_name,
                      "neighbors_raw": len(neighbors),
                      "neighbors_persisted": persisted_count,
                      "local_interfaces": len(grouped),
                      "sentinel_neighbors": sentinel_count,
                      "duration": round(duration, 1)})
    return {"device_name": device_name, "job_type": "lldp", "success": True,
            "entries": grouped, "count": persisted_count, "duration": duration}


async def collect_routes(device_name: str, device_id: str,
                         device_ip: str | None = None) -> dict:
    """Collect routing table from one device using tiered fallback.

    Tier 1: NAPALM get_route_to (structured, but requires destination arg
             which the Nautobot proxy may not support)
    Tier 2: Direct SNMP walk of ipCidrRouteTable / inetCidrRouteTable
             (universal, reliable, works across all vendors with SNMP)
    Tier 3: Reserved for future CLI fallback

    Stops at the first tier that returns data.
    """
    t0 = time.monotonic()
    await _mark_attempt(device_name, "routes")
    tier_used = "none"

    try:
        routes: list[dict] = []

        # --- Tier 1: NAPALM get_route_to ---
        try:
            data = await nautobot_client.napalm_get(device_id, "get_route_to")
            raw_routes = data.get("get_route_to", {})
            # Check for error response (Junos returns {"error": "..."} when no destination given)
            if isinstance(raw_routes, dict) and "error" not in raw_routes and raw_routes:
                for prefix, entries in raw_routes.items():
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        routes.append(_parse_napalm_route(device_name, prefix, entry))
                if routes:
                    tier_used = "napalm"
                    log.debug("poll_routes_tier1", "NAPALM get_route_to succeeded",
                              context={"device": device_name, "routes": len(routes)})
        except Exception as exc:
            log.debug("poll_routes_tier1_failed", "NAPALM route collection failed, trying SNMP",
                      context={"device": device_name, "error": str(exc)[:200]})

        # --- Tier 2: SNMP route table walk ---
        if not routes and device_ip:
            try:
                snmp_routes = await _collect_routes_snmp(device_name, device_ip)
                if snmp_routes:
                    routes = snmp_routes
                    tier_used = "snmp"
            except Exception as exc:
                log.debug("poll_routes_tier2_failed", "SNMP route collection failed",
                          context={"device": device_name, "error": str(exc)[:200]})

        if not routes:
            tier_used = "none"
            log.info("poll_routes_empty", "No routes collected from any tier",
                     context={"device": device_name, "has_ip": bool(device_ip)})

        # Upsert in bulk
        count = await endpoint_store.upsert_routes_bulk(routes)
        # Also populate FIB from SNMP data (SNMP RIB ≈ FIB on most platforms)
        if routes and tier_used == "snmp":
            try:
                await endpoint_store.upsert_node_fib_bulk(device_name, routes, source="snmp_rib")
            except Exception:
                pass

        duration = time.monotonic() - t0
        await _mark_success(device_name, "routes", duration)
        log.info("poll_routes_done", "Route collection complete",
                 context={"device": device_name, "routes": count,
                          "tier": tier_used, "duration": round(duration, 1)})
        return {"device_name": device_name, "job_type": "routes", "success": True,
                "count": count, "duration": duration, "tier": tier_used}
    except Exception as exc:
        duration = time.monotonic() - t0
        err = str(exc)[:500]
        await _mark_failure(device_name, "routes", err, duration)
        log.warning("poll_routes_failed", "Route collection failed",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "routes", "success": False,
                "error": err, "count": 0, "duration": duration}


def _parse_napalm_route(device_name: str, prefix: str, entry: dict) -> dict:
    """Parse a single NAPALM route entry into our standard dict."""
    protocol = entry.get("protocol", "unknown")
    proto_lower = protocol.lower()
    if "direct" in proto_lower or "connected" in proto_lower or "local" in proto_lower:
        protocol = "connected" if "local" not in proto_lower else "local"
    elif "static" in proto_lower:
        protocol = "static"
    elif "ospf" in proto_lower:
        protocol = "ospf"
    elif "bgp" in proto_lower:
        protocol = "bgp"
    elif "isis" in proto_lower:
        protocol = "isis"
    return {
        "node_name": device_name,
        "prefix": prefix,
        "next_hop": entry.get("next_hop", "") or "",
        "protocol": protocol,
        "vrf": "default",
        "metric": entry.get("metric"),
        "preference": entry.get("preference") or entry.get("administrative_distance"),
        "outgoing_interface": entry.get("outgoing_interface") or entry.get("interface"),
        "active": entry.get("current_active", True),
    }


# SNMP route protocol integer → string mapping (RFC 4292 / RFC 2096)
_SNMP_ROUTE_PROTO = {
    1: "other", 2: "local", 3: "static", 4: "icmp",
    8: "rip", 9: "igrp", 10: "eigrp", 11: "ospf",
    12: "ospf", 13: "ospf", 14: "bgp", 15: "bgp",
    16: "idpr", 17: "idrp",
}


async def _collect_routes_snmp(device_name: str, device_ip: str) -> list[dict]:
    """Collect routes via direct SNMP walk of the IP routing MIBs.

    This is the first direct-to-device SNMP path in the controller (distinct
    from snmp_exporter which is Prometheus-native). Uses pysnmp async HLAPI
    with bulkCmd for efficiency.

    Tries MIBs in order:
      1. ipCidrRouteTable (1.3.6.1.2.1.4.24.4) — RFC 2096, widely supported
      2. ipRouteTable (1.3.6.1.2.1.4.21) — RFC 1213 legacy fallback
    """
    community = os.environ.get("SNMP_COMMUNITY", "public")
    routes: list[dict] = []

    # Try ipCidrRouteTable first (RFC 2096: 1.3.6.1.2.1.4.24.4)
    routes = await _snmp_walk_ip_cidr_route(device_name, device_ip, community)
    if routes:
        log.debug("poll_routes_snmp_cidr", "SNMP ipCidrRouteTable walk succeeded",
                  context={"device": device_name, "routes": len(routes)})
        return routes

    # Fallback: ipRouteTable (RFC 1213: 1.3.6.1.2.1.4.21)
    routes = await _snmp_walk_ip_route(device_name, device_ip, community)
    if routes:
        log.debug("poll_routes_snmp_legacy", "SNMP ipRouteTable walk succeeded",
                  context={"device": device_name, "routes": len(routes)})
    return routes



async def _snmp_walk_ip_cidr_route(
    device_name: str, device_ip: str, community: str
) -> list[dict]:
    """Walk ipCidrRouteTable (OID 1.3.6.1.2.1.4.24.4).

    Index: ipCidrRouteDest.ipCidrRouteMask.ipCidrRouteTos.ipCidrRouteNextHop
    Columns: .5=ifIndex, .6=type, .7=proto, .11=metric1, .16=status
    """
    import ipaddress as _ipaddress

    BASE_OID = "1.3.6.1.2.1.4.24.4"
    raw_results = await snmp_collector.walk_table(device_ip, community, BASE_OID)
    if not raw_results:
        return []

    # Group by index key. walk_table suffixes have the form "1.<col>.<index>"
    # (the leading "1" is the table-entry subidentifier); strip it to get "<col>.<index>".
    raw: dict[str, dict] = {}
    for row in raw_results:
        for oid_suffix, val in row.items():
            parts = oid_suffix[2:].split(".", 1)  # skip "1."
            if len(parts) < 2:
                continue
            col, index_key = parts[0], parts[1]
            raw.setdefault(index_key, {})[col] = val

    routes = []
    for index_key, cols in raw.items():
        idx_parts = index_key.split(".")
        if len(idx_parts) < 13:
            continue
        dest = ".".join(idx_parts[0:4])
        mask = ".".join(idx_parts[4:8])
        nexthop = ".".join(idx_parts[9:13])

        try:
            prefix_len = _ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
        except Exception:
            prefix_len = 0

        proto_int = int(cols.get("7", 0) or 0)
        metric_val = int(cols.get("11", 0) or 0)

        routes.append({
            "node_name": device_name,
            "prefix": f"{dest}/{prefix_len}",
            "next_hop": nexthop if nexthop != "0.0.0.0" else "",
            "protocol": _SNMP_ROUTE_PROTO.get(proto_int, "unknown"),
            "vrf": "default",
            "metric": metric_val or None,
            "preference": None,
            "outgoing_interface": None,
            "active": True,
        })
    return routes


async def _snmp_walk_ip_route(
    device_name: str, device_ip: str, community: str
) -> list[dict]:
    """Walk ipRouteTable (OID 1.3.6.1.2.1.4.21) — RFC 1213 legacy fallback.

    Index is the destination IP. Columns: .1=dest, .3=metric, .7=nexthop,
    .9=proto, .11=mask.
    """
    import ipaddress as _ipaddress

    BASE_OID = "1.3.6.1.2.1.4.21"
    raw_results = await snmp_collector.walk_table(device_ip, community, BASE_OID)
    if not raw_results:
        return []

    raw: dict[str, dict] = {}
    for row in raw_results:
        for oid_suffix, val in row.items():
            parts = oid_suffix[2:].split(".", 1)  # skip "1."
            if len(parts) < 2:
                continue
            col, index_key = parts[0], parts[1]
            raw.setdefault(index_key, {})[col] = val

    routes = []
    for index_key, cols in raw.items():
        dest = str(cols.get("1", index_key) or index_key)
        nexthop = str(cols.get("7", "0.0.0.0") or "0.0.0.0")
        mask = str(cols.get("11", "0.0.0.0") or "0.0.0.0")
        metric_val = int(cols.get("3", 0) or 0)
        proto_int = int(cols.get("9", 0) or 0)

        try:
            prefix_len = _ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
        except Exception:
            prefix_len = 0

        routes.append({
            "node_name": device_name,
            "prefix": f"{dest}/{prefix_len}",
            "next_hop": nexthop if nexthop != "0.0.0.0" else "",
            "protocol": _SNMP_ROUTE_PROTO.get(proto_int, "unknown"),
            "vrf": "default",
            "metric": metric_val or None,
            "preference": None,
            "outgoing_interface": None,
            "active": True,
        })
    return routes


async def collect_bgp(device_name: str, device_id: str) -> dict:
    """Collect BGP neighbors from one device via NAPALM get_bgp_neighbors.

    Returns empty successfully if the device has no BGP (common for L2 switches).
    NAPALM get_bgp_neighbors returns:
    {global: {peers: {ip: {remote_as, ..., address_family: {af: {received, sent, ...}}}}}}
    """
    t0 = time.monotonic()
    await _mark_attempt(device_name, "bgp")
    try:
        data = await nautobot_client.napalm_get(device_id, "get_bgp_neighbors")
        raw = data.get("get_bgp_neighbors", {})

        neighbors = []
        for vrf_name, vrf_data in raw.items():
            if not isinstance(vrf_data, dict):
                continue
            peers = vrf_data.get("peers", {})
            if not isinstance(peers, dict):
                continue
            vrf = "default" if vrf_name in ("global", "default", "") else vrf_name
            for peer_ip, peer_data in peers.items():
                if not isinstance(peer_data, dict):
                    continue
                # Address families are nested under the peer
                af_dict = peer_data.get("address_family", {})
                if af_dict and isinstance(af_dict, dict):
                    for af_name, af_data in af_dict.items():
                        neighbors.append({
                            "node_name": device_name,
                            "neighbor_ip": peer_ip,
                            "remote_asn": peer_data.get("remote_as", 0),
                            "local_asn": peer_data.get("local_as"),
                            "state": _normalize_bgp_state(
                                peer_data.get("is_up"), peer_data.get("is_enabled")),
                            "prefixes_received": af_data.get("received_prefixes")
                                                 if isinstance(af_data, dict) else None,
                            "prefixes_sent": af_data.get("sent_prefixes")
                                             if isinstance(af_data, dict) else None,
                            "uptime_seconds": peer_data.get("uptime"),
                            "vrf": vrf,
                            "address_family": af_name,
                        })
                else:
                    # No AF breakdown — record one row
                    neighbors.append({
                        "node_name": device_name,
                        "neighbor_ip": peer_ip,
                        "remote_asn": peer_data.get("remote_as", 0),
                        "local_asn": peer_data.get("local_as"),
                        "state": _normalize_bgp_state(
                            peer_data.get("is_up"), peer_data.get("is_enabled")),
                        "prefixes_received": peer_data.get("received_prefixes"),
                        "prefixes_sent": peer_data.get("sent_prefixes"),
                        "uptime_seconds": peer_data.get("uptime"),
                        "vrf": vrf,
                        "address_family": "ipv4 unicast",
                    })

        count = await endpoint_store.upsert_bgp_neighbors_bulk(neighbors)

        duration = time.monotonic() - t0
        await _mark_success(device_name, "bgp", duration)
        log.info("poll_bgp_done", "BGP collection complete",
                 context={"device": device_name, "neighbors": count, "duration": round(duration, 1)})
        return {"device_name": device_name, "job_type": "bgp", "success": True,
                "count": count, "duration": duration}
    except Exception as exc:
        duration = time.monotonic() - t0
        err = str(exc)[:500]
        # If it's a 4xx error, the device likely doesn't support BGP — mark success
        if "400" in err or "404" in err or "405" in err or "not supported" in err.lower():
            await _mark_success(device_name, "bgp", duration)
            log.debug("poll_bgp_unsupported", "BGP not supported/configured on device",
                      context={"device": device_name})
            return {"device_name": device_name, "job_type": "bgp", "success": True,
                    "count": 0, "duration": duration, "skipped": True}
        await _mark_failure(device_name, "bgp", err, duration)
        log.warning("poll_bgp_failed", "BGP collection failed",
                    context={"device": device_name, "error": err})
        return {"device_name": device_name, "job_type": "bgp", "success": False,
                "error": err, "count": 0, "duration": duration}


def _normalize_bgp_state(is_up: bool | None, is_enabled: bool | None) -> str:
    """Convert NAPALM is_up/is_enabled booleans to a human-readable state."""
    if is_up is True:
        return "Established"
    if is_enabled is False:
        return "Shutdown"
    if is_up is False:
        return "Active"
    return "Unknown"


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
            results.append(await collect_arp(device_name, device_id,
                                             device_ip=device_ip))
        elif jt == "mac":
            results.append(await collect_mac(device_name, device_id,
                                             device_ip=device_ip))
        elif jt == "dhcp":
            results.append(await collect_dhcp(device_name, device_id, device_ip, is_junos))
        elif jt == "lldp":
            results.append(await collect_lldp(device_name, device_id,
                                              device_ip=device_ip))
        elif jt == "routes":
            results.append(await collect_routes(device_name, device_id, device_ip=device_ip))
        elif jt == "bgp":
            results.append(await collect_bgp(device_name, device_id))
        elif jt == "phase2_populate":
            results.append(await _run_phase2_populate(
                device_name, device_id, device_ip,
            ))
    return results


async def _run_phase2_populate(
    device_name: str, device_id: str, device_ip: "str | None",
) -> dict:
    """Dispatch the one-shot Phase 2 network sync for a device.

    On success: disable the poll row (one-shot semantic). On failure:
    defer ``next_due`` by 5 minutes for retry and transition the device
    status to ``Onboarding Incomplete`` so Prompt 9's status-gated
    polling will skip the core collectors until Phase 2 succeeds.
    """
    from app.onboarding import network_sync

    if not device_ip:
        await defer_device_poll(device_name, "phase2_populate", 300)
        return {"device_name": device_name, "job_type": "phase2_populate",
                "success": False, "error": "no primary_ip4 on device",
                "count": 0, "duration": 0.0}

    community = os.environ.get("SNMP_COMMUNITY", "public")
    t0 = time.monotonic()
    result = await network_sync.run_phase2(
        device_id=device_id,
        device_name=device_name,
        device_ip=device_ip,
        snmp_community=community,
    )
    duration = time.monotonic() - t0

    if result.success:
        await disable_device_poll(device_name, "phase2_populate")
        # Transition device status back to Active if a prior failed
        # attempt had flipped it to Onboarding Incomplete. Best-effort.
        try:
            active = await nautobot_client.get_status_by_name(
                "Active", content_type="dcim.device",
            )
            if active and active.get("id"):
                await nautobot_client.set_device_status(
                    device_id, active["id"],
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("phase2_status_restore_failed",
                        "could not restore device status to Active",
                        context={"device_id": device_id, "error": str(exc)})
    else:
        await defer_device_poll(
            device_name, "phase2_populate", 300,
            error=result.error,
        )
        # Transition device status so Prompt 9's gate short-circuits the
        # core collectors. Best-effort: if the status flip itself fails,
        # log and continue — the retry mechanism still drives toward
        # eventual success.
        try:
            incomplete = await nautobot_client.get_status_by_name(
                "Onboarding Incomplete", content_type="dcim.device",
            )
            if incomplete and incomplete.get("id"):
                await nautobot_client.set_device_status(
                    device_id, incomplete["id"],
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("phase2_status_flip_failed",
                        "could not mark device Onboarding Incomplete",
                        context={"device_id": device_id, "error": str(exc)})

    return {"device_name": device_name, "job_type": "phase2_populate",
            "success": result.success,
            "error": result.error,
            "count": result.interfaces_added + result.ips_added,
            "duration": round(duration, 2)}


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

            # Derive change_source from which job types contributed to this
            # device's data. If multiple jobs provided data (ARP + MAC + DHCP
            # is the common case), tag the change as "poll" rather than a
            # single job type — a field change could have come from any of
            # them after correlation.
            contributing = [
                jt for jt in ("arp", "mac", "dhcp")
                if data.get(jt)
            ]
            if len(contributing) == 1:
                change_source = "poll_" + contributing[0]
            else:
                change_source = "poll"

            try:
                await endpoint_store.upsert_endpoint(
                    ep, source="infrastructure", uplinks=uplinks,
                    change_source=change_source,
                )
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

            # Build device info map (name lowercased -> id/ip/platform/status)
            device_info: dict[str, dict] = {}
            for d in devices:
                name = d.get("name", "")
                did = d.get("id", "")
                pip = d.get("primary_ip4") or {}
                ip_display = pip.get("display") or pip.get("address") or "" if isinstance(pip, dict) else ""
                ip_addr = ip_display.split("/")[0] if "/" in ip_display else ip_display
                platform = d.get("platform") or {}
                plat_name = platform.get("name") or "" if isinstance(platform, dict) else ""
                status = d.get("status") or {}
                status_name = status.get("display") or status.get("name") or "" \
                    if isinstance(status, dict) else ""
                device_info[name.lower()] = {
                    "id": did, "ip": ip_addr,
                    "is_junos": "junos" in plat_name.lower(),
                    "status_name": status_name,
                }

            # Status-gated dispatch: core collectors (arp/mac/lldp/routes/bgp/
            # dhcp) only run for devices with Nautobot status=Active. Devices
            # in Onboarding Incomplete / Onboarding Failed would otherwise
            # generate collector-failure noise while the orchestrator drives
            # them toward Active. phase2_populate is exempt — by definition
            # it runs on not-yet-Active devices.
            gated_device_jobs: dict[str, list[str]] = {}
            for dn, jts in device_jobs.items():
                info = device_info.get(dn.lower(), {})
                is_active = (info.get("status_name") == "Active")
                kept = [
                    jt for jt in jts
                    if jt == "phase2_populate" or is_active
                ]
                skipped = [jt for jt in jts if jt not in kept]
                if skipped:
                    log.debug(
                        "poll_skip_status_gated",
                        "Skipped non-Active device's core poll rows",
                        context={
                            "device": dn,
                            "status": info.get("status_name") or "(unknown)",
                            "skipped": skipped,
                        },
                    )
                if kept:
                    gated_device_jobs[dn] = kept
            device_jobs = gated_device_jobs

            if not device_jobs:
                _poll_state["polls_dispatched"] = 0
                await asyncio.sleep(POLL_CHECK_INTERVAL)
                continue

            # Dispatch per-device tasks with bounded concurrency
            all_results: list[dict] = []

            async def _run_device(dev_name: str, job_types: list[str]):
                async with semaphore:
                    info = device_info.get(dev_name.lower(), {})
                    dev_id = info.get("id")
                    # Cache-miss retry: phase2_populate is dispatched
                    # immediately after Phase 1 Step H clears the cache, but
                    # the polling loop's own get_devices() call may have
                    # refilled it before Step H ran. Clear + retry once so
                    # the freshly-created device resolves.
                    if not dev_id and "phase2_populate" in job_types:
                        log.info(
                            "phase2_dispatch_cache_miss_retry",
                            "Device missing from Nautobot cache; "
                            "clearing cache and retrying once",
                            context={"device": dev_name},
                        )
                        nautobot_client.clear_cache()
                        try:
                            fresh = await nautobot_client.get_devices()
                        except Exception as exc:  # noqa: BLE001
                            fresh = []
                            log.warning(
                                "phase2_dispatch_cache_miss_refetch_failed",
                                "Cache-clear refetch failed",
                                context={"device": dev_name, "error": str(exc)},
                            )
                        for d in fresh:
                            if (d.get("name") or "").lower() == dev_name.lower():
                                pip = d.get("primary_ip4") or {}
                                ip_display = (pip.get("display")
                                              or pip.get("address") or "") \
                                    if isinstance(pip, dict) else ""
                                ip_addr = (ip_display.split("/")[0]
                                           if "/" in ip_display else ip_display)
                                plat = d.get("platform") or {}
                                plat_name = (plat.get("name") or "") \
                                    if isinstance(plat, dict) else ""
                                info = {
                                    "id": d.get("id", ""),
                                    "ip": ip_addr,
                                    "is_junos": "junos" in plat_name.lower(),
                                }
                                dev_id = info["id"]
                                break
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
