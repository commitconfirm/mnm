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
from sqlalchemy import delete, func, select

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
                           uplinks: set[tuple[str, str]] | None = None,
                           change_source: str | None = None) -> dict:
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

        # 1b. VLAN upgrade: if no exact match but there's a row on the same
        # switch/port with vlan=0 (sentinel for "unknown"), and we now have a
        # real VLAN, delete the sentinel row and let the code create a new one
        # with the correct VLAN. This prevents stale vlan=0 rows from persisting
        # indefinitely after VLAN inference is added.
        if exact is None and new_vlan != 0:
            sentinel = (await session.execute(
                select(db.Endpoint).where(
                    db.Endpoint.mac_address == mac,
                    db.Endpoint.current_switch == new_switch,
                    db.Endpoint.current_port == new_port,
                    db.Endpoint.current_vlan == 0,
                )
            )).scalar_one_or_none()
            if sentinel is not None:
                await session.delete(sentinel)
                await session.flush()

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
        # Field-level change diff — captured as (field_name, old, new) tuples
        # and written as ChangeHistory rows before commit. This tracks the
        # MAC's life story, orthogonal to EndpointEvent which tracks movement.
        diffs: list[tuple[str, str | None, str | None]] = []

        if exact is not None:
            # Update in place — same physical location seen again.
            if not exact.active:
                exact.active = True
                action = "updated"
            if new_ip and exact.current_ip and new_ip != exact.current_ip:
                events.append(("ip_changed", exact.current_ip, new_ip, {}))
                diffs.append(("ip", exact.current_ip, new_ip))
                action = "updated"
            if new_hostname and exact.hostname and new_hostname != exact.hostname:
                events.append(("hostname_changed", exact.hostname, new_hostname, {}))
                diffs.append(("hostname", exact.hostname, new_hostname))
                action = "updated"
            if ep_dict.get("classification") and exact.classification != ep_dict["classification"]:
                diffs.append(("classification", exact.classification or None, ep_dict["classification"]))
            if ep_dict.get("mac_vendor") and exact.mac_vendor != ep_dict["mac_vendor"]:
                diffs.append(("mac_vendor", exact.mac_vendor or None, ep_dict["mac_vendor"]))
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
            if ep_dict.get("classification") and not exact.classification_override:
                exact.classification = ep_dict["classification"]
            if ep_dict.get("classification_confidence") and not exact.classification_override:
                exact.classification_confidence = ep_dict["classification_confidence"]
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
                classification_confidence=ep_dict.get("classification_confidence") or "",
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
                    diffs.append(("switch", prior.current_switch, new_switch))
                else:
                    events.append(("moved_port", prior.current_port, new_port,
                                   {"switch": new_switch}))
                    diffs.append(("port", prior.current_port, new_port))
                if prior.current_vlan != new_vlan:
                    diffs.append(("vlan", str(prior.current_vlan), str(new_vlan)))
                if new_ip and prior.current_ip and new_ip != prior.current_ip:
                    events.append(("ip_changed", prior.current_ip, new_ip, {}))
                    diffs.append(("ip", prior.current_ip, new_ip))
                if new_hostname and prior.hostname and new_hostname != prior.hostname:
                    events.append(("hostname_changed", prior.hostname, new_hostname, {}))
                    diffs.append(("hostname", prior.hostname, new_hostname))
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

        # Write field-level change history (separate from EndpointEvent)
        effective_source = change_source or source or "unknown"
        for field_name, old_val, new_val in diffs:
            session.add(db.ChangeHistory(
                target_type="endpoint",
                target_id=mac,
                field_name=field_name,
                old_value=str(old_val) if old_val is not None else None,
                new_value=str(new_val) if new_val is not None else None,
                change_source=effective_source,
                changed_at=now,
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
# Discovery exclusions (operator-controlled scope, Rule 6)
# ---------------------------------------------------------------------------
#
# When the operator marks an IP as excluded:
#   - the sweep skips it before probing
#   - the infrastructure collector skips ARP/MAC correlation for it
#   - the Incomplete Devices advisory hides any Nautobot device whose
#     primary IP (or interface IP) is in this list
#
# All three call sites pull the full set into memory once at the start of
# their run rather than hitting the DB per IP. The set is small (typically
# a handful of entries) so we just keep an in-process cache and rely on the
# CRUD helpers to invalidate it on writes.

EXCLUDE_TYPE_IP = "ip"
EXCLUDE_TYPE_DEVICE_NAME = "device_name"
_VALID_EXCLUDE_TYPES = {EXCLUDE_TYPE_IP, EXCLUDE_TYPE_DEVICE_NAME}

_exclude_cache_ip: set[str] | None = None
_exclude_cache_device: set[str] | None = None


def _invalidate_exclude_cache() -> None:
    global _exclude_cache_ip, _exclude_cache_device
    _exclude_cache_ip = None
    _exclude_cache_device = None


async def list_excludes() -> list[dict]:
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.DiscoveryExclude).order_by(db.DiscoveryExclude.created_at.desc())
        )).scalars().all()
        return [r.to_dict() for r in rows]


async def get_excluded_ips() -> set[str]:
    """Return the full set of excluded IP identifiers.

    Cached in-process; the cache is invalidated on add/remove. Callers
    should refresh once at the start of a run, not per IP.
    """
    global _exclude_cache_ip
    if _exclude_cache_ip is not None:
        return _exclude_cache_ip
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.DiscoveryExclude.identifier)
            .where(db.DiscoveryExclude.type == EXCLUDE_TYPE_IP)
        )).scalars().all()
    _exclude_cache_ip = {r for r in rows if r}
    return _exclude_cache_ip


async def get_mac_ip_map_from_observations() -> dict[str, set[str]]:
    """Build MAC→IPs map from ip_observations table (sweep data).

    This supplements ARP-derived IPs with IPs discovered during sweeps,
    enabling multi-IP endpoint correlation for devices like the SRX320
    that have IPs only visible through sweep, not through any node's ARP table.
    """
    if not db.is_ready():
        return {}
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.IPObservation.mac_address, db.IPObservation.ip_address)
            .where(db.IPObservation.mac_address.isnot(None))
        )).all()
    result: dict[str, set[str]] = {}
    for mac, ip in rows:
        if mac and ip:
            result.setdefault(mac.upper(), set()).add(ip)
    return result


async def get_excluded_device_names() -> set[str]:
    """Return the full set of excluded device-name identifiers."""
    global _exclude_cache_device
    if _exclude_cache_device is not None:
        return _exclude_cache_device
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.DiscoveryExclude.identifier)
            .where(db.DiscoveryExclude.type == EXCLUDE_TYPE_DEVICE_NAME)
        )).scalars().all()
    _exclude_cache_device = {r for r in rows if r}
    return _exclude_cache_device


async def add_exclude(identifier: str, type: str,
                       reason: str = "", created_by: str = "") -> dict:
    if not identifier:
        raise ValueError("identifier is required")
    if type not in _VALID_EXCLUDE_TYPES:
        raise ValueError(f"type must be one of {sorted(_VALID_EXCLUDE_TYPES)}")
    async with db.SessionLocal() as session:
        existing = (await session.execute(
            select(db.DiscoveryExclude).where(db.DiscoveryExclude.identifier == identifier)
        )).scalar_one_or_none()
        if existing:
            existing.type = type
            existing.reason = reason or existing.reason
            existing.created_by = created_by or existing.created_by
            await session.commit()
            _invalidate_exclude_cache()
            return existing.to_dict()
        row = db.DiscoveryExclude(
            identifier=identifier,
            type=type,
            reason=reason,
            created_by=created_by,
        )
        session.add(row)
        await session.commit()
        _invalidate_exclude_cache()
        return row.to_dict()


async def remove_exclude(identifier: str) -> bool:
    async with db.SessionLocal() as session:
        existing = (await session.execute(
            select(db.DiscoveryExclude).where(db.DiscoveryExclude.identifier == identifier)
        )).scalar_one_or_none()
        if not existing:
            return False
        await session.delete(existing)
        await session.commit()
        _invalidate_exclude_cache()
        return True


async def is_excluded_ip(ip: str) -> bool:
    """Single-IP check. For hot loops, prefer get_excluded_ips() once and
    test set membership locally."""
    if not ip:
        return False
    return ip in (await get_excluded_ips())


async def is_excluded_device(name: str) -> bool:
    if not name:
        return False
    return name in (await get_excluded_device_names())


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

# ---------------------------------------------------------------------------
# Node raw data persistence (Phase 2.85)
# ---------------------------------------------------------------------------

async def upsert_node_arp_bulk(node_name: str, entries: list[dict]) -> int:
    """Bulk upsert ARP entries for a node. Returns count."""
    if not db.is_ready() or not entries:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    now = _now()
    rows = [{
        "node_name": node_name,
        "ip": e.get("ip", ""),
        "mac": (e.get("mac", "") or "").upper(),
        "interface": e.get("interface", ""),
        "vrf": "default",
        "collected_at": now,
    } for e in entries if e.get("ip") and e.get("mac")]

    if not rows:
        return 0
    async with db.SessionLocal() as session:
        stmt = pg_insert(db.NodeArpEntry).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_arp_node_ip_mac_vrf",
            set_={"interface": stmt.excluded.interface, "collected_at": stmt.excluded.collected_at},
        )
        await session.execute(stmt)
        await session.commit()
    return len(rows)


async def upsert_node_mac_bulk(node_name: str, entries: list[dict]) -> int:
    """Bulk upsert MAC table entries for a node. Returns count."""
    if not db.is_ready() or not entries:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    now = _now()
    rows = [{
        "node_name": node_name,
        "mac": (e.get("mac", "") or "").upper(),
        "interface": e.get("interface", ""),
        "vlan": int(e.get("vlan", 0) or 0),
        "entry_type": "static" if e.get("static") else "dynamic",
        "collected_at": now,
    } for e in entries if e.get("mac")]

    if not rows:
        return 0
    async with db.SessionLocal() as session:
        stmt = pg_insert(db.NodeMacEntry).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_mac_node_mac_iface_vlan",
            set_={"entry_type": stmt.excluded.entry_type, "collected_at": stmt.excluded.collected_at},
        )
        await session.execute(stmt)
        await session.commit()
    return len(rows)


async def upsert_node_lldp_bulk(node_name: str, lldp_data: dict) -> int:
    """Bulk upsert LLDP neighbor entries for a node.

    lldp_data is the NAPALM-shaped per-interface dict
    ``{interface: [{hostname, port, ...}, ...]}``. The SNMP path
    (``polling.collect_lldp`` from Block C P5 onward) builds the same
    shape and additionally supplies the 5 expansion fields added by P2
    migration ``c3208527926f``: ``local_port_ifindex``,
    ``local_port_name``, ``remote_chassis_id_subtype``,
    ``remote_port_id_subtype``, ``remote_system_description``.

    Expansion fields are read from ``n.get(...)`` with ``None`` default —
    NAPALM-shaped callers omit them and the columns stay NULL, the SNMP
    path supplies them. on_conflict_do_update refreshes the expansion
    fields alongside the legacy columns so a re-poll picks up changes
    when a neighbor's chassis/port subtypes or system description shift.
    """
    if not db.is_ready() or not lldp_data:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    now = _now()
    rows = []
    for iface, neighbors in lldp_data.items():
        if not isinstance(neighbors, list):
            continue
        for n in neighbors:
            if not isinstance(n, dict):
                continue
            rows.append({
                "node_name": node_name,
                "local_interface": iface,
                "remote_system_name": n.get("hostname") or n.get("remote_system_name") or "",
                "remote_port": n.get("port") or n.get("remote_port") or "",
                "remote_chassis_id": n.get("remote_chassis_id") or "",
                "remote_management_ip": n.get("remote_management_ip") or "",
                # Block C P2 expansion columns. None default keeps
                # NAPALM-shaped callers (no expansion data) writing NULL.
                "local_port_ifindex": n.get("local_port_ifindex"),
                "local_port_name": n.get("local_port_name"),
                "remote_chassis_id_subtype": n.get("remote_chassis_id_subtype"),
                "remote_port_id_subtype": n.get("remote_port_id_subtype"),
                "remote_system_description": n.get("remote_system_description"),
                "collected_at": now,
            })

    if not rows:
        return 0
    async with db.SessionLocal() as session:
        stmt = pg_insert(db.NodeLldpEntry).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_lldp_node_iface_remote",
            set_={
                "remote_chassis_id": stmt.excluded.remote_chassis_id,
                "remote_management_ip": stmt.excluded.remote_management_ip,
                "local_port_ifindex": stmt.excluded.local_port_ifindex,
                "local_port_name": stmt.excluded.local_port_name,
                "remote_chassis_id_subtype": stmt.excluded.remote_chassis_id_subtype,
                "remote_port_id_subtype": stmt.excluded.remote_port_id_subtype,
                "remote_system_description": stmt.excluded.remote_system_description,
                "collected_at": stmt.excluded.collected_at,
            },
        )
        await session.execute(stmt)
        await session.commit()
    return len(rows)


async def list_node_arp(node_name: str, ip: str | None = None,
                        mac: str | None = None) -> list[dict]:
    if not db.is_ready():
        return []
    async with db.SessionLocal() as session:
        stmt = select(db.NodeArpEntry).where(
            db.NodeArpEntry.node_name == node_name
        ).order_by(db.NodeArpEntry.ip)
        if ip:
            stmt = stmt.where(db.NodeArpEntry.ip.contains(ip))
        if mac:
            stmt = stmt.where(db.NodeArpEntry.mac.contains(mac.upper()))
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


async def list_node_mac(node_name: str, mac: str | None = None,
                        interface: str | None = None,
                        vlan: int | None = None) -> list[dict]:
    if not db.is_ready():
        return []
    async with db.SessionLocal() as session:
        stmt = select(db.NodeMacEntry).where(
            db.NodeMacEntry.node_name == node_name
        ).order_by(db.NodeMacEntry.mac)
        if mac:
            stmt = stmt.where(db.NodeMacEntry.mac.contains(mac.upper()))
        if interface:
            stmt = stmt.where(db.NodeMacEntry.interface == interface)
        if vlan is not None:
            stmt = stmt.where(db.NodeMacEntry.vlan == vlan)
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


async def list_node_lldp(node_name: str) -> list[dict]:
    if not db.is_ready():
        return []
    async with db.SessionLocal() as session:
        stmt = select(db.NodeLldpEntry).where(
            db.NodeLldpEntry.node_name == node_name
        ).order_by(db.NodeLldpEntry.local_interface)
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


async def upsert_node_fib_bulk(node_name: str, routes: list[dict],
                               source: str = "snmp_rib") -> int:
    """Bulk upsert FIB entries for a node. Returns count."""
    if not db.is_ready() or not routes:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    now = _now()
    rows = [{
        "node_name": node_name,
        "prefix": r.get("prefix", ""),
        "next_hop": r.get("next_hop", ""),
        "interface": r.get("outgoing_interface") or r.get("interface") or "",
        "vrf": r.get("vrf", "default"),
        "source": source,
        "collected_at": now,
    } for r in routes if r.get("prefix")]

    if not rows:
        return 0
    async with db.SessionLocal() as session:
        stmt = pg_insert(db.NodeFibEntry).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_fib_node_prefix_nh_vrf",
            set_={"interface": stmt.excluded.interface, "source": stmt.excluded.source,
                   "collected_at": stmt.excluded.collected_at},
        )
        await session.execute(stmt)
        await session.commit()
    return len(rows)


async def list_node_fib(node_name: str, prefix: str | None = None) -> list[dict]:
    if not db.is_ready():
        return []
    async with db.SessionLocal() as session:
        stmt = select(db.NodeFibEntry).where(
            db.NodeFibEntry.node_name == node_name
        ).order_by(db.NodeFibEntry.prefix)
        if prefix:
            stmt = stmt.where(db.NodeFibEntry.prefix.contains(prefix))
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


# ---------------------------------------------------------------------------
# Route and BGP persistence (Phase 2.8)
# ---------------------------------------------------------------------------

_route_log = StructuredLogger(__name__ + ".routes", module="routes")


async def upsert_route(route: dict) -> str:
    """Upsert a single route into the routes table.

    Unique key: (node_name, prefix, next_hop, vrf).
    Returns "created" or "updated".
    """
    if not db.is_ready():
        return "skipped"
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = _now()
    row = {
        "node_name": route.get("node_name", ""),
        "prefix": route.get("prefix", ""),
        "next_hop": route.get("next_hop", ""),
        "protocol": route.get("protocol", "unknown"),
        "vrf": route.get("vrf", "default"),
        "metric": route.get("metric"),
        "preference": route.get("preference"),
        "outgoing_interface": route.get("outgoing_interface"),
        "active": route.get("active", True),
        "collected_at": now,
    }

    async with db.SessionLocal() as session:
        stmt = pg_insert(db.Route).values(**row)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_routes_node_prefix_nh_vrf",
            set_={
                "protocol": stmt.excluded.protocol,
                "metric": stmt.excluded.metric,
                "preference": stmt.excluded.preference,
                "outgoing_interface": stmt.excluded.outgoing_interface,
                "active": stmt.excluded.active,
                "collected_at": stmt.excluded.collected_at,
            },
        )
        await session.execute(stmt)
        await session.commit()
    return "upserted"


async def upsert_routes_bulk(routes: list[dict]) -> int:
    """Upsert many routes in a single transaction. Returns count."""
    if not db.is_ready() or not routes:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = _now()
    rows = []
    for r in routes:
        rows.append({
            "node_name": r.get("node_name", ""),
            "prefix": r.get("prefix", ""),
            "next_hop": r.get("next_hop", ""),
            "protocol": r.get("protocol", "unknown"),
            "vrf": r.get("vrf", "default"),
            "metric": r.get("metric"),
            "preference": r.get("preference"),
            "outgoing_interface": r.get("outgoing_interface"),
            "active": r.get("active", True),
            "collected_at": now,
        })

    async with db.SessionLocal() as session:
        stmt = pg_insert(db.Route).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_routes_node_prefix_nh_vrf",
            set_={
                "protocol": stmt.excluded.protocol,
                "metric": stmt.excluded.metric,
                "preference": stmt.excluded.preference,
                "outgoing_interface": stmt.excluded.outgoing_interface,
                "active": stmt.excluded.active,
                "collected_at": stmt.excluded.collected_at,
            },
        )
        await session.execute(stmt)
        await session.commit()
    return len(rows)


async def upsert_bgp_neighbor(neighbor: dict) -> str:
    """Upsert a single BGP neighbor row.

    Unique key: (node_name, neighbor_ip, vrf, address_family).
    """
    if not db.is_ready():
        return "skipped"
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = _now()
    row = {
        "node_name": neighbor.get("node_name", ""),
        "neighbor_ip": neighbor.get("neighbor_ip", ""),
        "remote_asn": neighbor.get("remote_asn", 0),
        "local_asn": neighbor.get("local_asn"),
        "state": neighbor.get("state", "Unknown"),
        "prefixes_received": neighbor.get("prefixes_received"),
        "prefixes_sent": neighbor.get("prefixes_sent"),
        "uptime_seconds": neighbor.get("uptime_seconds"),
        "vrf": neighbor.get("vrf", "default"),
        "address_family": neighbor.get("address_family", "ipv4 unicast"),
        "collected_at": now,
    }

    async with db.SessionLocal() as session:
        stmt = pg_insert(db.BGPNeighbor).values(**row)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_bgp_node_neighbor_vrf_af",
            set_={
                "remote_asn": stmt.excluded.remote_asn,
                "local_asn": stmt.excluded.local_asn,
                "state": stmt.excluded.state,
                "prefixes_received": stmt.excluded.prefixes_received,
                "prefixes_sent": stmt.excluded.prefixes_sent,
                "uptime_seconds": stmt.excluded.uptime_seconds,
                "collected_at": stmt.excluded.collected_at,
            },
        )
        await session.execute(stmt)
        await session.commit()
    return "upserted"


async def upsert_bgp_neighbors_bulk(neighbors: list[dict]) -> int:
    """Upsert many BGP neighbors. Returns count."""
    if not db.is_ready() or not neighbors:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = _now()
    rows = []
    for n in neighbors:
        rows.append({
            "node_name": n.get("node_name", ""),
            "neighbor_ip": n.get("neighbor_ip", ""),
            "remote_asn": n.get("remote_asn", 0),
            "local_asn": n.get("local_asn"),
            "state": n.get("state", "Unknown"),
            "prefixes_received": n.get("prefixes_received"),
            "prefixes_sent": n.get("prefixes_sent"),
            "uptime_seconds": n.get("uptime_seconds"),
            "vrf": n.get("vrf", "default"),
            "address_family": n.get("address_family", "ipv4 unicast"),
            "collected_at": now,
        })

    async with db.SessionLocal() as session:
        stmt = pg_insert(db.BGPNeighbor).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_bgp_node_neighbor_vrf_af",
            set_={
                "remote_asn": stmt.excluded.remote_asn,
                "local_asn": stmt.excluded.local_asn,
                "state": stmt.excluded.state,
                "prefixes_received": stmt.excluded.prefixes_received,
                "prefixes_sent": stmt.excluded.prefixes_sent,
                "uptime_seconds": stmt.excluded.uptime_seconds,
                "collected_at": stmt.excluded.collected_at,
            },
        )
        await session.execute(stmt)
        await session.commit()
    return len(rows)


async def list_routes(
    node_name: str | None = None,
    vrf: str | None = None,
    protocol: str | None = None,
    prefix_search: str | None = None,
) -> list[dict]:
    """Query routes with optional filters."""
    if not db.is_ready():
        return []
    async with db.SessionLocal() as session:
        stmt = select(db.Route).order_by(db.Route.prefix)
        if node_name:
            stmt = stmt.where(db.Route.node_name == node_name)
        if vrf:
            stmt = stmt.where(db.Route.vrf == vrf)
        if protocol:
            stmt = stmt.where(db.Route.protocol == protocol)
        if prefix_search:
            stmt = stmt.where(db.Route.prefix.contains(prefix_search))
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


async def list_bgp_neighbors(
    node_name: str | None = None,
    state: str | None = None,
    vrf: str | None = None,
) -> list[dict]:
    """Query BGP neighbors with optional filters."""
    if not db.is_ready():
        return []
    async with db.SessionLocal() as session:
        stmt = select(db.BGPNeighbor).order_by(db.BGPNeighbor.node_name, db.BGPNeighbor.neighbor_ip)
        if node_name:
            stmt = stmt.where(db.BGPNeighbor.node_name == node_name)
        if state:
            stmt = stmt.where(db.BGPNeighbor.state == state)
        if vrf:
            stmt = stmt.where(db.BGPNeighbor.vrf == vrf)
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


async def route_advisories(known_ips: set[str] | None = None) -> list[dict]:
    """Return routes with next-hops that don't match any known IP.

    These are discovery candidates — next-hops the node knows about but
    MNM hasn't seen. If known_ips is not provided, builds the set from
    Nautobot IPAM + endpoint records.
    """
    if not db.is_ready():
        return []
    if known_ips is None:
        known_ips = set()
        # Collect IPs from endpoints
        async with db.SessionLocal() as session:
            rows = (await session.execute(
                select(db.Endpoint.current_ip).where(
                    db.Endpoint.active.is_(True),
                    db.Endpoint.current_ip.isnot(None),
                )
            )).scalars().all()
            known_ips.update(ip for ip in rows if ip)

    async with db.SessionLocal() as session:
        routes = (await session.execute(
            select(db.Route).where(
                db.Route.active.is_(True),
                db.Route.next_hop != "",
                db.Route.protocol != "connected",
                db.Route.protocol != "local",
            ).order_by(db.Route.node_name, db.Route.prefix)
        )).scalars().all()

    advisories = []
    for r in routes:
        if r.next_hop and r.next_hop not in known_ips:
            advisories.append(r.to_dict())
    return advisories


async def route_count() -> int:
    """Total route rows."""
    if not db.is_ready():
        return 0
    async with db.SessionLocal() as session:
        return (await session.execute(
            select(func.count()).select_from(db.Route)
        )).scalar_one()


async def bgp_neighbor_count() -> int:
    """Total BGP neighbor rows."""
    if not db.is_ready():
        return 0
    async with db.SessionLocal() as session:
        return (await session.execute(
            select(func.count()).select_from(db.BGPNeighbor)
        )).scalar_one()


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


async def prune_old_node_arp(retention_days: int) -> int:
    """Delete node_arp_entries older than retention_days."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.NodeArpEntry.__table__.delete().where(db.NodeArpEntry.collected_at < cutoff))
        await session.commit()
        return result.rowcount or 0


async def prune_old_node_mac(retention_days: int) -> int:
    """Delete node_mac_entries older than retention_days."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.NodeMacEntry.__table__.delete().where(db.NodeMacEntry.collected_at < cutoff))
        await session.commit()
        return result.rowcount or 0


async def prune_old_node_lldp(retention_days: int) -> int:
    """Delete node_lldp_entries older than retention_days."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.NodeLldpEntry.__table__.delete().where(db.NodeLldpEntry.collected_at < cutoff))
        await session.commit()
        return result.rowcount or 0


async def prune_old_routes(retention_days: int) -> int:
    """Delete routes older than retention_days. Returns row count."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.Route.__table__.delete().where(
                db.Route.collected_at < cutoff
            )
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_routes", "Pruned old routes",
                    context={"deleted": n, "retention_days": retention_days})
    return n


async def prune_old_bgp_neighbors(retention_days: int) -> int:
    """Delete bgp_neighbors older than retention_days. Returns row count."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.BGPNeighbor.__table__.delete().where(
                db.BGPNeighbor.collected_at < cutoff
            )
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_bgp", "Pruned old BGP neighbors",
                    context={"deleted": n, "retention_days": retention_days})
    return n


async def prune_old_auto_discovery_runs(retention_days: int) -> int:
    """Delete auto_discovery_runs older than retention_days. Returns row count."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            db.AutoDiscoveryRun.__table__.delete().where(
                db.AutoDiscoveryRun.started_at < cutoff
            )
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_auto_discovery", "Pruned old auto-discovery runs",
                    context={"deleted": n, "retention_days": retention_days})
    return n


# ---------------------------------------------------------------------------
# Comments + change history (Phase 2.9)
# ---------------------------------------------------------------------------

async def add_comment(target_type: str, target_id: str, comment_text: str,
                       created_by: str = "admin") -> dict:
    """Create a comment on an endpoint or node. Also writes a change_history
    entry so the comment add appears in the audit trail."""
    now = _now()
    async with db.SessionLocal() as session:
        comment = db.Comment(
            target_type=target_type,
            target_id=target_id,
            comment_text=comment_text,
            created_by=created_by,
            created_at=now,
        )
        session.add(comment)
        session.add(db.ChangeHistory(
            target_type=target_type,
            target_id=target_id,
            field_name="comment_added",
            old_value=None,
            new_value=comment_text,
            change_source="manual",
            changed_at=now,
        ))
        await session.commit()
        await session.refresh(comment)
        return comment.to_dict()


async def list_comments(target_type: str, target_id: str) -> list[dict]:
    """Return all comments for an endpoint or node, newest first."""
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(db.Comment)
            .where(db.Comment.target_type == target_type,
                   db.Comment.target_id == target_id)
            .order_by(db.Comment.created_at.desc())
        )).scalars().all()
        return [r.to_dict() for r in rows]


async def delete_comment(comment_id: str) -> bool:
    """Delete a comment by ID. Writes a change_history entry for the delete."""
    async with db.SessionLocal() as session:
        row = (await session.execute(
            select(db.Comment).where(db.Comment.id == comment_id)
        )).scalar_one_or_none()
        if row is None:
            return False
        # Record the deletion in change_history before dropping the row
        session.add(db.ChangeHistory(
            target_type=row.target_type,
            target_id=row.target_id,
            field_name="comment_deleted",
            old_value=row.comment_text,
            new_value=None,
            change_source="manual",
            changed_at=_now(),
        ))
        await session.delete(row)
        await session.commit()
        return True


async def list_change_history(target_type: str, target_id: str,
                                field_name: str | None = None,
                                change_source: str | None = None,
                                limit: int = 50) -> list[dict]:
    """Return change history for an endpoint or node, newest first."""
    async with db.SessionLocal() as session:
        stmt = (
            select(db.ChangeHistory)
            .where(db.ChangeHistory.target_type == target_type,
                   db.ChangeHistory.target_id == target_id)
        )
        if field_name:
            stmt = stmt.where(db.ChangeHistory.field_name == field_name)
        if change_source:
            stmt = stmt.where(db.ChangeHistory.change_source == change_source)
        stmt = stmt.order_by(db.ChangeHistory.changed_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]


async def prune_old_change_history(retention_days: int) -> int:
    """Delete change_history rows older than the retention threshold.
    Comments are NOT pruned — only the change log."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            delete(db.ChangeHistory).where(db.ChangeHistory.changed_at < cutoff)
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_change_history", "Pruned old change history",
                    context={"deleted": n, "retention_days": retention_days})
    return n


async def prune_old_probes(retention_days: int) -> int:
    """Delete endpoint_probes rows older than the retention threshold."""
    cutoff = _now() - timedelta(days=retention_days)
    async with db.SessionLocal() as session:
        result = await session.execute(
            delete(db.EndpointProbe).where(db.EndpointProbe.probed_at < cutoff)
        )
        await session.commit()
        n = result.rowcount or 0
    _prune_log.info("prune_probes", "Pruned old probe results",
                    context={"deleted": n, "retention_days": retention_days})
    return n


async def prune_all(retention_days: int) -> dict:
    """Run every prune helper and return a summary dict."""
    events = await prune_old_events(retention_days)
    observations = await prune_old_observations(retention_days)
    watches = await prune_orphaned_watches()
    sentinels = await prune_stale_sentinels(retention_days)
    routes = await prune_old_routes(retention_days)
    bgp = await prune_old_bgp_neighbors(retention_days)
    auto_disc = await prune_old_auto_discovery_runs(retention_days)
    node_arp = await prune_old_node_arp(retention_days)
    node_mac = await prune_old_node_mac(retention_days)
    node_lldp = await prune_old_node_lldp(retention_days)
    change_history = await prune_old_change_history(retention_days)
    probe_results = await prune_old_probes(retention_days)
    summary = {
        "events": events,
        "observations": observations,
        "watches": watches,
        "sentinels": sentinels,
        "routes": routes,
        "bgp_neighbors": bgp,
        "auto_discovery_runs": auto_disc,
        "node_arp": node_arp,
        "node_mac": node_mac,
        "node_lldp": node_lldp,
        "change_history": change_history,
        "endpoint_probes": probe_results,
    }
    _prune_log.info("prune_complete",
                    f"Daily prune: {events} events, {observations} observations, "
                    f"{watches} watches, {sentinels} sentinels, "
                    f"{routes} routes, {bgp} bgp_neighbors, "
                    f"{auto_disc} auto_discovery_runs removed",
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
        routes = (await session.execute(
            select(func.count()).select_from(db.Route)
            .where(db.Route.collected_at < cutoff)
        )).scalar_one()
        bgp = (await session.execute(
            select(func.count()).select_from(db.BGPNeighbor)
            .where(db.BGPNeighbor.collected_at < cutoff)
        )).scalar_one()
        auto_disc = (await session.execute(
            select(func.count()).select_from(db.AutoDiscoveryRun)
            .where(db.AutoDiscoveryRun.started_at < cutoff)
        )).scalar_one()
        node_arp = (await session.execute(
            select(func.count()).select_from(db.NodeArpEntry)
            .where(db.NodeArpEntry.collected_at < cutoff)
        )).scalar_one()
        node_mac = (await session.execute(
            select(func.count()).select_from(db.NodeMacEntry)
            .where(db.NodeMacEntry.collected_at < cutoff)
        )).scalar_one()
        node_lldp = (await session.execute(
            select(func.count()).select_from(db.NodeLldpEntry)
            .where(db.NodeLldpEntry.collected_at < cutoff)
        )).scalar_one()
        change_history = (await session.execute(
            select(func.count()).select_from(db.ChangeHistory)
            .where(db.ChangeHistory.changed_at < cutoff)
        )).scalar_one()
        probe_results = (await session.execute(
            select(func.count()).select_from(db.EndpointProbe)
            .where(db.EndpointProbe.probed_at < cutoff)
        )).scalar_one()
    return {
        "events": int(events),
        "observations": int(observations),
        "watches": int(watches),
        "sentinels": int(sentinels),
        "routes": int(routes),
        "bgp_neighbors": int(bgp),
        "auto_discovery_runs": int(auto_disc),
        "node_arp": int(node_arp),
        "node_mac": int(node_mac),
        "node_lldp": int(node_lldp),
        "change_history": int(change_history),
        "endpoint_probes": int(probe_results),
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
        route_count_val = (await session.execute(
            select(func.count()).select_from(db.Route)
        )).scalar_one()
        bgp_count_val = (await session.execute(
            select(func.count()).select_from(db.BGPNeighbor)
        )).scalar_one()
        auto_disc_count = (await session.execute(
            select(func.count()).select_from(db.AutoDiscoveryRun)
        )).scalar_one()
        arp_count = (await session.execute(
            select(func.count()).select_from(db.NodeArpEntry)
        )).scalar_one()
        mac_count = (await session.execute(
            select(func.count()).select_from(db.NodeMacEntry)
        )).scalar_one()
        lldp_count = (await session.execute(
            select(func.count()).select_from(db.NodeLldpEntry)
        )).scalar_one()
        fib_count = (await session.execute(
            select(func.count()).select_from(db.NodeFibEntry)
        )).scalar_one()
        comment_count = (await session.execute(
            select(func.count()).select_from(db.Comment)
        )).scalar_one()
        change_history_count = (await session.execute(
            select(func.count()).select_from(db.ChangeHistory)
        )).scalar_one()
        probe_count = (await session.execute(
            select(func.count()).select_from(db.EndpointProbe)
        )).scalar_one()
    return {
        "endpoint_rows": int(endpoint_count),
        "event_rows": int(event_count),
        "observation_rows": int(observation_count),
        "watch_rows": int(watch_count),
        "route_rows": int(route_count_val),
        "bgp_neighbor_rows": int(bgp_count_val),
        "auto_discovery_rows": int(auto_disc_count),
        "arp_rows": int(arp_count),
        "mac_rows": int(mac_count),
        "lldp_rows": int(lldp_count),
        "fib_rows": int(fib_count),
        "comment_rows": int(comment_count),
        "change_history_rows": int(change_history_count),
        "probe_rows": int(probe_count),
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
