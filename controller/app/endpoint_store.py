"""Endpoint persistence + correlation/event detection (Phase 2.7).

This module owns all reads and writes against the `endpoints`, `endpoint_events`,
`collection_runs`, and `sweep_runs` tables. Callers (endpoint_collector,
discovery, API endpoints) use this module instead of touching the ORM directly.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import and_, delete, func, select

from app import db
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="endpoint_store")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Endpoint upsert with diff/event generation
# ---------------------------------------------------------------------------

async def _is_watched(session, mac: str) -> bool:
    row = (await session.execute(
        select(db.EndpointWatch).where(db.EndpointWatch.mac_address == mac)
    )).scalar_one_or_none()
    return row is not None


async def upsert_endpoint(ep_dict: dict, source: str = "infrastructure",
                           uplinks: set[tuple[str, str]] | None = None) -> dict:
    """Upsert an endpoint observation, keyed on (mac, switch, port, vlan).

    Behavior:
      - Skips records on uplink ports (logged at DEBUG, no row written).
      - If a row already exists for the exact (mac, switch, port, vlan), update
        its last_seen and metadata in place.
      - If the MAC has other active rows on different (switch, port, vlan)
        combinations, mark them inactive and create a new active row.
      - Generates `appeared`, `moved_port`, `moved_switch`, `ip_changed`, and
        `hostname_changed` events as appropriate.
      - When a watched MAC moves, the resulting event gets `details.watched=true`.

    Returns: {"action": "new"|"updated"|"unchanged"|"skipped_uplink"|"skipped",
              "events": [...]}.
    """
    mac = (ep_dict.get("mac") or ep_dict.get("mac_address") or "").upper()
    if not mac:
        return {"action": "skipped", "events": []}

    new_ip = ep_dict.get("ip") or ep_dict.get("current_ip") or None
    new_additional_ips = list(ep_dict.get("additional_ips") or [])
    if new_ip and new_ip not in new_additional_ips:
        new_additional_ips.append(new_ip)
    new_switch = ep_dict.get("device_name") or ep_dict.get("current_switch") or None
    new_port = ep_dict.get("switch_port") or ep_dict.get("current_port") or None
    new_vlan_raw = ep_dict.get("vlan") or ep_dict.get("current_vlan")
    try:
        new_vlan = int(new_vlan_raw) if new_vlan_raw not in (None, "", "0") else 0
    except (TypeError, ValueError):
        new_vlan = 0
    new_hostname = ep_dict.get("hostname") or None
    now = _now()
    last_seen = _parse_dt(ep_dict.get("last_seen")) or now
    first_seen = _parse_dt(ep_dict.get("first_seen")) or now

    # Composite key requires non-null parts. Sweep-only endpoints (no switch
    # context) cannot be located on a port — store with sentinel values so the
    # MAC identity is still tracked, but they won't move/conflict with infra rows.
    new_switch = new_switch or "(none)"
    new_port = new_port or "(none)"

    # Uplink check: if (switch, port) is in the uplink set, this MAC is just
    # transiting a trunk/cross-link. Don't record it as a host location.
    if uplinks and (new_switch, new_port) in uplinks:
        log.debug("uplink_skip", "Skipping endpoint on uplink port (LLDP/cable)",
                  context={"mac": mac, "switch": new_switch, "port": new_port})
        return {"action": "skipped_uplink", "events": []}

    # Secondary uplink check: the collector tags each endpoint with
    # `is_access_port`. If False, it was learned on a LAG/IRB/loopback/trunk
    # interface — i.e. transit, not an endpoint location. The Nautobot LAG
    # mapping is often incomplete, so this catches LAG members that the
    # primary uplink set missed.
    if ep_dict.get("is_access_port") is False and new_port != "(none)":
        log.debug("uplink_skip", "Skipping endpoint on non-access interface",
                  context={"mac": mac, "switch": new_switch, "port": new_port})
        return {"action": "skipped_uplink", "events": []}

    events: list[tuple[str, str | None, str | None, dict]] = []

    async with db.SessionLocal() as session:
        watched = await _is_watched(session, mac)

        # 1. Try to find an exact match for the new location
        exact = (await session.execute(
            select(db.Endpoint).where(
                db.Endpoint.mac_address == mac,
                db.Endpoint.current_switch == new_switch,
                db.Endpoint.current_port == new_port,
                db.Endpoint.current_vlan == new_vlan,
            )
        )).scalar_one_or_none()

        # 2. Find any other active rows for this MAC (potential prior locations)
        other_active = (await session.execute(
            select(db.Endpoint).where(
                db.Endpoint.mac_address == mac,
                db.Endpoint.active.is_(True),
            )
        )).scalars().all()

        prior = None
        for row in other_active:
            if exact is None or (row.current_switch != new_switch or
                                 row.current_port != new_port or
                                 row.current_vlan != new_vlan):
                prior = row  # remember one prior to derive event metadata
                row.active = False

        action = "unchanged"

        if exact is not None:
            # Update in place — same physical location seen again.
            if not exact.active:
                exact.active = True
                action = "updated"
            if new_ip and exact.current_ip and new_ip != exact.current_ip:
                events.append(("ip_changed", exact.current_ip, new_ip, {}))
                action = "updated"
            if new_hostname and exact.hostname and new_hostname != exact.hostname:
                events.append(("hostname_changed", exact.hostname, new_hostname, {}))
                action = "updated"
            if new_ip:
                exact.current_ip = new_ip
            if new_additional_ips:
                merged = list(exact.additional_ips or [])
                for ip in new_additional_ips:
                    if ip and ip not in merged:
                        merged.append(ip)
                exact.additional_ips = merged
            if ep_dict.get("mac_vendor"):
                exact.mac_vendor = ep_dict["mac_vendor"]
            if new_hostname:
                exact.hostname = new_hostname
            if ep_dict.get("classification"):
                exact.classification = ep_dict["classification"]
            if ep_dict.get("dhcp_server"):
                exact.dhcp_server = ep_dict["dhcp_server"]
            if ep_dict.get("lease_start"):
                exact.dhcp_lease_start = _parse_dt(ep_dict["lease_start"])
            if ep_dict.get("lease_expiry"):
                exact.dhcp_lease_expiry = _parse_dt(ep_dict["lease_expiry"])
            exact.last_seen = last_seen
            if exact.data_source and exact.data_source != source:
                exact.data_source = "both"
            elif not exact.data_source:
                exact.data_source = source
        else:
            # New (mac, switch, port, vlan) — create a new active row.
            ep = db.Endpoint(
                mac_address=mac,
                current_switch=new_switch,
                current_port=new_port,
                current_vlan=new_vlan,
                active=True,
                is_uplink=False,
                current_ip=new_ip,
                additional_ips=new_additional_ips,
                mac_vendor=ep_dict.get("mac_vendor") or "",
                hostname=new_hostname or "",
                classification=ep_dict.get("classification") or "",
                dhcp_server=ep_dict.get("dhcp_server") or "",
                dhcp_lease_start=_parse_dt(ep_dict.get("lease_start")),
                dhcp_lease_expiry=_parse_dt(ep_dict.get("lease_expiry")),
                first_seen=first_seen,
                last_seen=last_seen,
                data_source=source,
            )
            session.add(ep)

            if prior is None:
                events.append(("appeared", None, mac,
                               {"ip": new_ip, "switch": new_switch, "port": new_port}))
                action = "new"
            else:
                # Movement detected
                if prior.current_switch != new_switch:
                    events.append(("moved_switch", prior.current_switch, new_switch,
                                   {"old_port": prior.current_port, "new_port": new_port}))
                else:
                    events.append(("moved_port", prior.current_port, new_port,
                                   {"switch": new_switch}))
                if new_ip and prior.current_ip and new_ip != prior.current_ip:
                    events.append(("ip_changed", prior.current_ip, new_ip, {}))
                if new_hostname and prior.hostname and new_hostname != prior.hostname:
                    events.append(("hostname_changed", prior.hostname, new_hostname, {}))
                action = "updated"

        for ev_type, old, new, details in events:
            details = dict(details or {})
            if watched:
                details["watched"] = True
            session.add(db.EndpointEvent(
                mac_address=mac,
                event_type=ev_type,
                old_value=old,
                new_value=new,
                details=details,
                timestamp=now,
            ))

        # ------------------------------------------------------------------
        # Cross-row IP correlation: the IP belongs to the MAC, not to the
        # (mac, switch, port, vlan) row. A Proxmox upsert that has no IP can
        # inherit one from an infrastructure-source row that learned it from
        # the switch ARP table, and vice versa.
        # ------------------------------------------------------------------
        await session.flush()

        # Pull every active row for this MAC after the flush so newly added
        # rows are visible.
        siblings = (await session.execute(
            select(db.Endpoint).where(
                db.Endpoint.mac_address == mac,
                db.Endpoint.active.is_(True),
            )
        )).scalars().all()

        # Find the canonical IP for this MAC (any active row's current_ip).
        # Prefer the IP we were just given; otherwise pick any non-null one.
        canonical_ip = new_ip
        if not canonical_ip:
            for s in siblings:
                if s.current_ip:
                    canonical_ip = s.current_ip
                    break

        # Build the union of every IP this MAC has been seen with, across
        # all (switch, port, vlan) rows. This is the "all known IPs" view.
        union_ips: list[str] = []
        for s in siblings:
            if s.current_ip and s.current_ip not in union_ips:
                union_ips.append(s.current_ip)
            for ip in (s.additional_ips or []):
                if ip and ip not in union_ips:
                    union_ips.append(ip)
        for ip in new_additional_ips:
            if ip and ip not in union_ips:
                union_ips.append(ip)

        for s in siblings:
            if canonical_ip and not s.current_ip:
                s.current_ip = canonical_ip
            if union_ips:
                # Replace with the union (including the row's own current_ip
                # if it differs) so every row reflects the full picture.
                merged = [ip for ip in union_ips if ip != s.current_ip]
                s.additional_ips = merged

        await session.commit()

    return {"action": action, "events": [e[0] for e in events]}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def list_endpoints(include_inactive: bool = False) -> list[dict]:
    """Default: only the currently-active row per (mac, switch, port, vlan)."""
    async with db.SessionLocal() as session:
        stmt = select(db.Endpoint)
        if not include_inactive:
            stmt = stmt.where(db.Endpoint.active.is_(True))
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


async def count_endpoints() -> int:
    """Count of unique active MACs."""
    async with db.SessionLocal() as session:
        return (await session.execute(
            select(func.count(func.distinct(db.Endpoint.mac_address)))
            .where(db.Endpoint.active.is_(True))
        )).scalar_one()


async def count_distinct_ips() -> int:
    """Count of unique IPs across all active endpoint rows.

    This is the "what does MNM actually know about?" view, as opposed to
    Nautobot IPAM which only contains rows that the sweep + collector mirror
    successfully wrote. Includes Proxmox-source IPs that don't get mirrored
    to Nautobot at all.
    """
    async with db.SessionLocal() as session:
        return (await session.execute(
            select(func.count(func.distinct(db.Endpoint.current_ip)))
            .where(db.Endpoint.active.is_(True), db.Endpoint.current_ip.isnot(None))
        )).scalar_one()


async def get_endpoint(mac: str) -> dict | None:
    """Return the currently-active row for a MAC (most recent if multiple)."""
    mac = mac.upper()
    async with db.SessionLocal() as session:
        row = (await session.execute(
            select(db.Endpoint)
            .where(db.Endpoint.mac_address == mac, db.Endpoint.active.is_(True))
            .order_by(db.Endpoint.last_seen.desc())
        )).scalars().first()
        if row is None:
            # Fall back to most recent inactive row so detail pages still work
            row = (await session.execute(
                select(db.Endpoint)
                .where(db.Endpoint.mac_address == mac)
                .order_by(db.Endpoint.last_seen.desc())
            )).scalars().first()
        return row.to_dict() if row else None


async def get_endpoint_history(mac: str) -> list[dict]:
    """Return all rows (active + inactive) for a MAC, ordered by last_seen.

    This shows every (switch, port, vlan) the MAC has ever been recorded on,
    in chronological order — i.e. its movement history at the row level.
    """
    mac = mac.upper()
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.Endpoint)
            .where(db.Endpoint.mac_address == mac)
            .order_by(db.Endpoint.last_seen.asc())
        )).scalars().all()
        return [r.to_dict() for r in rows]


async def get_endpoint_events(mac: str, limit: int = 500) -> list[dict]:
    mac = mac.upper()
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.EndpointEvent)
            .where(db.EndpointEvent.mac_address == mac)
            .order_by(db.EndpointEvent.timestamp.desc())
            .limit(limit)
        )).scalars().all()
        return [r.to_dict() for r in rows]


async def get_recent_events(event_type: str | None = None, since_hours: int = 24, limit: int = 500) -> list[dict]:
    cutoff = _now() - timedelta(hours=since_hours)
    async with db.SessionLocal() as session:
        stmt = select(db.EndpointEvent).where(db.EndpointEvent.timestamp >= cutoff)
        if event_type:
            stmt = stmt.where(db.EndpointEvent.event_type == event_type)
        stmt = stmt.order_by(db.EndpointEvent.timestamp.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


async def register_mac_ip(mac: str, ip: str) -> bool:
    """Propagate an IP to every active row for a MAC, without creating a new
    row. Used by the infrastructure collector when it learns an IP from a
    switch ARP table for a MAC whose only location is an uplink/LAG port —
    in that case the upsert path skips the row creation, but we still want
    the IP to land on the Proxmox / sweep rows for that same MAC.

    Returns True if at least one row was updated.
    """
    if not mac or not ip:
        return False
    mac = mac.upper()
    updated = False
    async with db.SessionLocal() as session:
        siblings = (await session.execute(
            select(db.Endpoint).where(
                db.Endpoint.mac_address == mac,
                db.Endpoint.active.is_(True),
            )
        )).scalars().all()
        if not siblings:
            return False
        for s in siblings:
            if not s.current_ip:
                s.current_ip = ip
                updated = True
            existing = list(s.additional_ips or [])
            if ip not in existing and ip != s.current_ip:
                existing.append(ip)
                s.additional_ips = existing
                updated = True
        if updated:
            await session.commit()
    return updated


async def _get_stale_threshold_days() -> int:
    """Resolve the stale-endpoint threshold in days.

    Resolution order:
      1. ``anomaly_stale_days`` key in the ``kv_config`` table (operator-set
         override via ``POST /api/config``)
      2. ``ANOMALY_STALE_DAYS`` environment variable
      3. Default: 7 days
    """
    default = 7
    try:
        env_val = int(os.environ.get("ANOMALY_STALE_DAYS", str(default)))
    except (TypeError, ValueError):
        env_val = default

    try:
        async with db.SessionLocal() as session:
            row = (await session.execute(
                select(db.KVConfig).where(db.KVConfig.key == "controller_config")
            )).scalar_one_or_none()
            if row and isinstance(row.value, dict):
                cfg_val = row.value.get("anomaly_stale_days")
                if isinstance(cfg_val, (int, float)) and cfg_val > 0:
                    return int(cfg_val)
    except Exception:
        pass
    return env_val


async def get_anomalies() -> dict:
    """Detect endpoints that look wrong, suspicious, or unidentifiable.

    Categories:
      - ``ip_conflicts``: same IP claimed by more than one MAC (active rows).
      - ``multi_location``: a single MAC active on more than one switch
        simultaneously. Legitimate during a move; suspicious otherwise.
      - ``no_ip``: active endpoint with no current_ip and no additional_ips —
        we know it's there but cannot reach or correlate it with anything.
      - ``unclassified``: active endpoint with classification empty / unknown
        and no hostname — neither sweep nor any connector identified it.
      - ``stale``: active endpoint last seen more than ``anomaly_stale_days``
        ago (default 7, configurable via env var ``ANOMALY_STALE_DAYS`` or
        the ``anomaly_stale_days`` key in kv_config). Either the device was
        decommissioned, the collector stopped seeing it, or it moved to a
        port we aren't watching.

    Returns ``{"ip_conflicts": [...], "multi_location": [...], "no_ip": [...],
    "unclassified": [...], "stale": [...], "summary": {...}}``.
    """
    stale_days = await _get_stale_threshold_days()
    cutoff_stale = _now() - timedelta(days=stale_days)
    result: dict = {
        "ip_conflicts": [],
        "multi_location": [],
        "no_ip": [],
        "unclassified": [],
        "stale": [],
    }

    async with db.SessionLocal() as session:
        # IP conflicts (reuses the same query as get_ip_conflicts)
        rows = await session.execute(
            select(db.Endpoint.current_ip,
                   func.count(func.distinct(db.Endpoint.mac_address)).label("n"))
            .where(db.Endpoint.current_ip.isnot(None), db.Endpoint.active.is_(True))
            .group_by(db.Endpoint.current_ip)
            .having(func.count(func.distinct(db.Endpoint.mac_address)) > 1)
        )
        for ip, n in rows.all():
            macs = (await session.execute(
                select(db.Endpoint).where(
                    db.Endpoint.current_ip == ip, db.Endpoint.active.is_(True)
                )
            )).scalars().all()
            result["ip_conflicts"].append({
                "ip": ip, "mac_count": n,
                "macs": [m.to_dict() for m in macs],
            })

        # Multi-location: MAC with >1 distinct active switch
        rows = await session.execute(
            select(db.Endpoint.mac_address,
                   func.count(func.distinct(db.Endpoint.current_switch)).label("n"))
            .where(db.Endpoint.active.is_(True),
                   db.Endpoint.current_switch != "(none)",
                   db.Endpoint.is_uplink.is_(False))
            .group_by(db.Endpoint.mac_address)
            .having(func.count(func.distinct(db.Endpoint.current_switch)) > 1)
        )
        for mac, n in rows.all():
            locs = (await session.execute(
                select(db.Endpoint).where(
                    db.Endpoint.mac_address == mac, db.Endpoint.active.is_(True)
                )
            )).scalars().all()
            result["multi_location"].append({
                "mac": mac, "location_count": n,
                "locations": [l.to_dict() for l in locs],
            })

        # No-IP: active endpoint with no current_ip AND no additional_ips
        no_ip_rows = (await session.execute(
            select(db.Endpoint).where(
                db.Endpoint.active.is_(True),
                db.Endpoint.current_ip.is_(None),
            )
        )).scalars().all()
        for r in no_ip_rows:
            if r.additional_ips:
                continue
            if r.current_switch == "(none)":
                continue  # sweep-only sentinel rows aren't useful here
            result["no_ip"].append(r.to_dict())

        # Unclassified: active, no classification (or 'unknown'), no hostname
        unclass_rows = (await session.execute(
            select(db.Endpoint).where(db.Endpoint.active.is_(True))
        )).scalars().all()
        for r in unclass_rows:
            classification = (r.classification or "").lower()
            if classification and classification != "unknown":
                continue
            if r.hostname:
                continue
            result["unclassified"].append(r.to_dict())

        # Stale: active but not seen in 7 days
        stale_rows = (await session.execute(
            select(db.Endpoint).where(
                db.Endpoint.active.is_(True),
                db.Endpoint.last_seen < cutoff_stale,
            )
        )).scalars().all()
        result["stale"] = [r.to_dict() for r in stale_rows]

    result["summary"] = {
        "ip_conflicts": len(result["ip_conflicts"]),
        "multi_location": len(result["multi_location"]),
        "no_ip": len(result["no_ip"]),
        "unclassified": len(result["unclassified"]),
        "stale": len(result["stale"]),
        "total": sum(len(v) for k, v in result.items() if k != "summary"),
    }
    return result


async def get_ip_conflicts() -> list[dict]:
    """Find IPs currently claimed by more than one distinct MAC (active rows)."""
    async with db.SessionLocal() as session:
        result = await session.execute(
            select(db.Endpoint.current_ip,
                   func.count(func.distinct(db.Endpoint.mac_address)).label("n"))
            .where(db.Endpoint.current_ip.isnot(None), db.Endpoint.active.is_(True))
            .group_by(db.Endpoint.current_ip)
            .having(func.count(func.distinct(db.Endpoint.mac_address)) > 1)
        )
        conflicts = []
        for ip, n in result.all():
            macs_rows = (await session.execute(
                select(db.Endpoint).where(
                    db.Endpoint.current_ip == ip,
                    db.Endpoint.active.is_(True),
                )
            )).scalars().all()
            conflicts.append({
                "ip": ip,
                "mac_count": n,
                "macs": [m.to_dict() for m in macs_rows],
            })
        return conflicts


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

async def list_watches() -> list[dict]:
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.EndpointWatch).order_by(db.EndpointWatch.created_at.desc())
        )).scalars().all()
        return [r.to_dict() for r in rows]


async def add_watch(mac: str, reason: str = "", created_by: str = "") -> dict:
    mac = mac.upper()
    async with db.SessionLocal() as session:
        existing = (await session.execute(
            select(db.EndpointWatch).where(db.EndpointWatch.mac_address == mac)
        )).scalar_one_or_none()
        if existing:
            existing.reason = reason or existing.reason
            existing.created_by = created_by or existing.created_by
            await session.commit()
            return existing.to_dict()
        row = db.EndpointWatch(mac_address=mac, reason=reason, created_by=created_by)
        session.add(row)
        await session.commit()
        return row.to_dict()


async def remove_watch(mac: str) -> bool:
    mac = mac.upper()
    async with db.SessionLocal() as session:
        existing = (await session.execute(
            select(db.EndpointWatch).where(db.EndpointWatch.mac_address == mac)
        )).scalar_one_or_none()
        if not existing:
            return False
        await session.delete(existing)
        await session.commit()
        return True


async def is_watched(mac: str) -> bool:
    async with db.SessionLocal() as session:
        return await _is_watched(session, mac.upper())


# ---------------------------------------------------------------------------
# Run records
# ---------------------------------------------------------------------------

async def record_collection_run(summary: dict) -> None:
    async with db.SessionLocal() as session:
        session.add(db.CollectionRun(
            started_at=_parse_dt(summary.get("started")) or _now(),
            completed_at=_parse_dt(summary.get("finished")) or _now(),
            duration_seconds=summary.get("duration_seconds"),
            devices_queried=summary.get("devices_queried", 0),
            endpoints_found=summary.get("endpoints_found", 0),
            endpoints_new=summary.get("endpoints_new", 0),
            endpoints_updated=summary.get("endpoints_updated", 0),
            endpoints_moved=summary.get("endpoints_moved", 0),
        ))
        await session.commit()


async def list_collection_runs(limit: int = 20) -> list[dict]:
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.CollectionRun).order_by(db.CollectionRun.started_at.desc()).limit(limit)
        )).scalars().all()
        return [r.to_dict() for r in rows]


async def record_sweep_run(cidr_ranges: list[str], started_at: datetime, finished_at: datetime,
                            summary: dict) -> None:
    async with db.SessionLocal() as session:
        session.add(db.SweepRun(
            cidr=",".join(cidr_ranges) if cidr_ranges else "",
            started_at=started_at,
            completed_at=finished_at,
            duration_seconds=(finished_at - started_at).total_seconds(),
            total_scanned=summary.get("total", 0),
            total_alive=summary.get("alive", 0),
            total_onboarded=summary.get("onboarded", 0),
            total_failed=summary.get("failed", 0),
        ))
        await session.commit()


async def list_sweep_runs(limit: int = 20) -> list[dict]:
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.SweepRun).order_by(db.SweepRun.started_at.desc()).limit(limit)
        )).scalars().all()
        return [r.to_dict() for r in rows]


async def record_ip_observation(ip: str, host: dict) -> None:
    """Append a sweep snapshot for an IP."""
    async with db.SessionLocal() as session:
        session.add(db.IPObservation(
            ip_address=ip,
            mac_address=(host.get("mac_address") or "").upper() or None,
            ports_open=host.get("ports_open") or [],
            banners=host.get("banners") or {},
            snmp_data=host.get("snmp") or {},
            http_headers=host.get("http_headers") or {},
            tls_data={
                "subject": host.get("tls_subject", ""),
                "issuer": host.get("tls_issuer", ""),
                "expiry": host.get("tls_expiry", ""),
                "sans": host.get("tls_sans", ""),
            },
            ssh_banner=host.get("ssh_banner") or "",
            classification=host.get("classification") or "",
            dns_name=host.get("dns_name") or "",
        ))
        await session.commit()


# ---------------------------------------------------------------------------
# JSON migration (one-shot, on first startup with empty DB)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Database hygiene / pruning
# ---------------------------------------------------------------------------
#
# All four prune helpers below take a single ``retention_days`` argument and
# delete rows older than that threshold. They are designed to be safe to run
# concurrently with collection — each operates inside its own short
# transaction and never touches active in-flight rows.
#
# Counts are returned for logging and the admin UI. Structured logging uses
# the dedicated "prune" module so the events are easy to filter in the log
# viewer.

_prune_log = StructuredLogger(__name__ + ".prune", module="prune")


async def prune_old_events(retention_days: int) -> int:
    """Delete endpoint_events older than retention_days. Returns row count."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.EndpointEvent.__table__.delete().where(
                db.EndpointEvent.timestamp < cutoff
            )
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_events", "Pruned old endpoint events",
                    context={"deleted": n, "retention_days": retention_days})
    return n


async def prune_old_observations(retention_days: int) -> int:
    """Delete ip_observations older than retention_days. Returns row count."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.IPObservation.__table__.delete().where(
                db.IPObservation.observed_at < cutoff
            )
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_observations", "Pruned old IP observations",
                    context={"deleted": n, "retention_days": retention_days})
    return n


async def prune_orphaned_watches() -> int:
    """Delete endpoint_watches whose MAC no longer appears in endpoints.

    A watchlist entry is orphaned when its target MAC has been purged from
    the endpoint store entirely (e.g. after a manual cleanup or because the
    device was decommissioned and its sentinel rows aged out).
    """
    async with db.SessionLocal() as session:
        # Subquery: every distinct MAC currently in endpoints
        active_macs = select(db.Endpoint.mac_address.distinct())
        result = await session.execute(
            db.EndpointWatch.__table__.delete().where(
                db.EndpointWatch.mac_address.notin_(active_macs)
            )
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_watches", "Pruned orphaned watches",
                    context={"deleted": n})
    return n


async def prune_stale_sentinels(retention_days: int) -> int:
    """Delete sweep-only sentinel endpoint rows older than retention_days.

    Sentinel rows are created for endpoints that have a MAC but no
    infrastructure context (no switch/port). They use ``(none)/(none)/0``
    placeholders so the composite PK is satisfied. After the retention
    window the sentinel is no longer useful and can be reaped.
    """
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.Endpoint.__table__.delete().where(
                db.Endpoint.current_switch == "(none)",
                db.Endpoint.current_port == "(none)",
                db.Endpoint.last_seen < cutoff,
            )
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_sentinels", "Pruned stale sentinel endpoints",
                    context={"deleted": n, "retention_days": retention_days})
    return n


async def prune_all(retention_days: int) -> dict:
    """Run every prune helper and return a summary dict."""
    events = await prune_old_events(retention_days)
    observations = await prune_old_observations(retention_days)
    watches = await prune_orphaned_watches()
    sentinels = await prune_stale_sentinels(retention_days)
    summary = {
        "events": events,
        "observations": observations,
        "watches": watches,
        "sentinels": sentinels,
    }
    _prune_log.info("prune_complete",
                    f"Daily prune: {events} events, {observations} observations, "
                    f"{watches} watches, {sentinels} sentinels removed",
                    context=summary)
    return summary


async def prune_preview(retention_days: int) -> dict:
    """Return what *would* be pruned without deleting anything.

    Mirrors prune_all() but uses SELECT COUNT(*) instead of DELETE so the
    operator can preview the impact before committing to a real prune.
    """
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        events = (await session.execute(
            select(func.count()).select_from(db.EndpointEvent)
            .where(db.EndpointEvent.timestamp < cutoff)
        )).scalar_one()
        observations = (await session.execute(
            select(func.count()).select_from(db.IPObservation)
            .where(db.IPObservation.observed_at < cutoff)
        )).scalar_one()
        active_macs = select(db.Endpoint.mac_address.distinct())
        watches = (await session.execute(
            select(func.count()).select_from(db.EndpointWatch)
            .where(db.EndpointWatch.mac_address.notin_(active_macs))
        )).scalar_one()
        sentinels = (await session.execute(
            select(func.count()).select_from(db.Endpoint)
            .where(
                db.Endpoint.current_switch == "(none)",
                db.Endpoint.current_port == "(none)",
                db.Endpoint.last_seen < cutoff,
            )
        )).scalar_one()
    return {
        "events": int(events),
        "observations": int(observations),
        "watches": int(watches),
        "sentinels": int(sentinels),
    }


async def maintenance_stats() -> dict:
    """Return current row counts and the oldest event/observation timestamps
    for the Database Maintenance dashboard card."""
    async with db.SessionLocal() as session:
        endpoint_count = (await session.execute(
            select(func.count()).select_from(db.Endpoint)
        )).scalar_one()
        event_count = (await session.execute(
            select(func.count()).select_from(db.EndpointEvent)
        )).scalar_one()
        observation_count = (await session.execute(
            select(func.count()).select_from(db.IPObservation)
        )).scalar_one()
        oldest_event = (await session.execute(
            select(func.min(db.EndpointEvent.timestamp))
        )).scalar_one()
        oldest_observation = (await session.execute(
            select(func.min(db.IPObservation.observed_at))
        )).scalar_one()
        watch_count = (await session.execute(
            select(func.count()).select_from(db.EndpointWatch)
        )).scalar_one()
    return {
        "endpoint_rows": int(endpoint_count),
        "event_rows": int(event_count),
        "observation_rows": int(observation_count),
        "watch_rows": int(watch_count),
        "oldest_event": oldest_event.isoformat() if oldest_event else None,
        "oldest_observation": oldest_observation.isoformat() if oldest_observation else None,
    }


async def migrate_from_json(data_dir: Path) -> dict:
    """If endpoints.json/config.json exist and DB tables are empty, copy them in.

    Renames the old files to .json.migrated when complete.
    """
    result = {"endpoints_imported": 0, "config_imported": False}
    if not db.is_ready():
        return result

    # Skip if endpoints already populated
    if (await count_endpoints()) > 0:
        return result

    endpoints_path = data_dir / "endpoints.json"
    config_path = data_dir / "config.json"

    if endpoints_path.exists():
        try:
            with open(endpoints_path) as f:
                data = json.load(f)
            endpoints = data.get("endpoints", {}) if isinstance(data, dict) else {}
            for ip, ep in endpoints.items():
                if not ep.get("mac"):
                    continue
                ep.setdefault("ip", ip)
                ep.setdefault("source", "infrastructure")
                await upsert_endpoint(ep, source=ep.get("source", "infrastructure"))
                result["endpoints_imported"] += 1
            log.info("migration_endpoints", "Migrated endpoints from JSON",
                     context={"count": result["endpoints_imported"]})
            try:
                endpoints_path.rename(endpoints_path.with_suffix(".json.migrated"))
            except OSError:
                pass
        except Exception as e:
            log.warning("migration_endpoints_failed", "Failed migrating endpoints.json",
                        context={"error": str(e)})

    if config_path.exists():
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            async with db.SessionLocal() as session:
                row = (await session.execute(
                    select(db.KVConfig).where(db.KVConfig.key == "controller_config")
                )).scalar_one_or_none()
                if row:
                    row.value = cfg
                else:
                    session.add(db.KVConfig(key="controller_config", value=cfg))
                await session.commit()
            result["config_imported"] = True
            try:
                config_path.rename(config_path.with_suffix(".json.migrated"))
            except OSError:
                pass
        except Exception as e:
            log.warning("migration_config_failed", "Failed migrating config.json",
                        context={"error": str(e)})

    return result
