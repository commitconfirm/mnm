"""SNMP-based MAC/FDB table collector for MNM Controller.

Walks dot1qTpFdbTable (Q-BRIDGE-MIB: 1.3.6.1.2.1.17.7.1.2.2.1) for
VLAN-aware switches. Falls back to dot1dTpFdbTable (BRIDGE-MIB:
1.3.6.1.2.1.17.4.3.1) if the primary table is empty — simpler switches
(and some firewalls) only implement the older BRIDGE-MIB or no bridging
MIBs at all.

Uses snmp_collector.walk_table() — no direct pysnmp calls here.
MAC normalization uses snmp_collector.mac_from_bytes and
snmp_collector.mac_from_dotted_decimal (shared with arp_snmp and the
future lldp_snmp collector).
Integration into the polling loop is handled separately.

Q-BRIDGE-MIB row index structure
---------------------------------
dot1qTpFdbTable index: dot1qFdbId (filter-DB integer, NOT the 802.1Q VLAN
ID on Junos — see FDB ID resolution below) followed by six MAC bytes in
dotted-decimal form. Walk_table suffix for a row:
  "<col>.<fdb_id>.<b0>.<b1>.<b2>.<b3>.<b4>.<b5>"
e.g. "2.100.170.187.204.221.238.255" for fdb_id 100, port col, MAC aa:...

FDB ID resolution
-----------------
RFC 4363 allows dot1qFdbId to be any opaque integer assigned by the
agent, not necessarily equal to the 802.1Q VLAN ID. Junos, for example,
uses multiples of 65536 as FDB IDs. After collecting the FDB table,
collect_mac() walks dot1qVlanCurrentTable (1.3.6.1.2.1.17.7.1.4.2.1)
column .3 (dot1qVlanFdbId) to build a {fdb_id: vlan_id} map and resolve
each entry to its real 802.1Q VLAN ID.

BRIDGE-MIB row index structure
-------------------------------
dot1dTpFdbTable index: six MAC bytes in dotted-decimal form only (no VLAN).
Walk_table suffix for a row:
  "<col>.<b0>.<b1>.<b2>.<b3>.<b4>.<b5>"
e.g. "2.170.187.204.221.238.255" for MAC aa:..., port col.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app import snmp_collector
from app.snmp_collector import (
    SnmpAuthError,
    SnmpError,
    SnmpTimeoutError,
    mac_from_bytes,
    mac_from_dotted_decimal,
    oid,
)
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="mac_snmp")

_OID_Q_BRIDGE = oid("Q-BRIDGE-MIB::dot1qTpFdbEntry")       # dot1qTpFdbTable — VLAN-aware, primary
_OID_BRIDGE = oid("BRIDGE-MIB::dot1dTpFdbEntry")            # dot1dTpFdbTable — no VLAN, fallback
_OID_VLAN_CURRENT = oid("Q-BRIDGE-MIB::dot1qVlanCurrentEntry")  # col .3 maps fdb_id → vlan_id

# dot1qTpFdbStatus / dot1dTpFdbStatus integer → string
_ENTRY_STATUS = {
    1: "other",
    2: "invalid",
    3: "learned",
    4: "self",
    5: "mgmt",
}

# Column numbers shared by both tables
_COL_PORT = "2"    # dot1qTpFdbPort / dot1dTpFdbPort
_COL_STATUS = "3"  # dot1qTpFdbStatus / dot1dTpFdbStatus


@dataclass
class MacEntry:
    mac_address: str       # lowercase colon-separated: "aa:bb:cc:dd:ee:ff"
    vlan: int | None       # VLAN ID from primary table; None from fallback
    bridge_port: int       # dot1dBasePort (maps to ifIndex via dot1dBasePortTable)
    entry_status: str      # "other" | "invalid" | "learned" | "self" | "mgmt"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _collect_fdb_vlan_map(
    device_ip: str,
    community: str,
    *,
    version: str,
    timeout_sec: float,
    port: int,
) -> dict[int, int]:
    """Walk dot1qVlanCurrentTable and return {fdb_id: vlan_id}.

    Column .3 (dot1qVlanFdbId) maps each VLAN's FDB ID to its 802.1Q VLAN ID.
    Index suffix: "<TimeMark>.<VlanIndex>" — VlanIndex is the 802.1Q VLAN ID.
    """
    rows = await snmp_collector.walk_table(
        device_ip, community, _OID_VLAN_CURRENT,
        version=version, timeout_sec=timeout_sec, port=port,
    )
    fdb_vlan_map: dict[int, int] = {}
    for row in rows:
        for oid_suffix, val in row.items():
            col, _, index_key = oid_suffix.partition(".")
            if col != "3" or not index_key:
                continue
            idx_parts = index_key.split(".", 1)
            if len(idx_parts) < 2:
                continue
            try:
                vlan_id = int(idx_parts[1])
                fdb_id = int(val)
            except (ValueError, TypeError):
                continue
            fdb_vlan_map[fdb_id] = vlan_id
    return fdb_vlan_map


def _parse_q_bridge_table(
    rows: list[dict[str, Any]],
    fdb_vlan_map: dict[int, int],
) -> tuple[list[MacEntry], int]:
    """Parse walk_table output from dot1qTpFdbTable into MacEntry list.

    Index format: <dot1qFdbId>.<mac_byte0>...<mac_byte5> (7 components).
    Columns: .1=dot1qTpFdbAddress (OctetString MAC), .2=port, .3=status.
    fdb_vlan_map resolves raw FDB IDs to 802.1Q VLAN IDs.

    Returns (entries, skipped_count).
    """
    raw: dict[str, dict[str, Any]] = {}
    for row in rows:
        for oid_suffix, val in row.items():
            col, _, index_key = oid_suffix.partition(".")
            if not index_key:
                continue
            raw.setdefault(index_key, {})[col] = val

    entries: list[MacEntry] = []
    skipped = 0

    for index_key, cols in raw.items():
        # index_key: "<vlan>.<b0>.<b1>.<b2>.<b3>.<b4>.<b5>"
        idx_parts = index_key.split(".", 1)
        if len(idx_parts) < 2:
            log.warning("mac_snmp_skip_row", "Q-bridge row has unparseable index",
                        context={"index_key": index_key, "cols": list(cols.keys())})
            skipped += 1
            continue

        try:
            fdb_id = int(idx_parts[0])
        except ValueError:
            log.warning("mac_snmp_skip_row", "Q-bridge row has non-integer FDB ID in index",
                        context={"index_key": index_key})
            skipped += 1
            continue

        vlan: int | None = fdb_vlan_map.get(fdb_id)

        try:
            mac_addr = mac_from_dotted_decimal(idx_parts[1])
        except ValueError as exc:
            log.warning("mac_snmp_skip_row", "Q-bridge row has malformed MAC in index",
                        context={"index_key": index_key, "error": str(exc)})
            skipped += 1
            continue

        # Cross-verify with column .1 (dot1qTpFdbAddress) if present
        col1_raw = cols.get("1")
        if col1_raw is not None:
            try:
                mac_from_col = mac_from_bytes(col1_raw)
                if mac_from_col != mac_addr:
                    log.warning(
                        "mac_snmp_index_mac_mismatch",
                        "Q-bridge MAC in column .1 differs from index-derived MAC",
                        context={"index_key": index_key,
                                 "index_mac": mac_addr,
                                 "col_mac": mac_from_col},
                    )
                    # Index is authoritative; col1 may be absent on some devices
            except (ValueError, TypeError):
                pass  # ignore unreadable column value, index MAC stands

        port_raw = cols.get(_COL_PORT)
        if port_raw is None:
            log.warning("mac_snmp_skip_row", "Q-bridge row missing port column",
                        context={"index_key": index_key, "mac": mac_addr})
            skipped += 1
            continue
        try:
            bridge_port = int(port_raw)
        except (ValueError, TypeError):
            log.warning("mac_snmp_skip_row", "Q-bridge row has non-integer port",
                        context={"index_key": index_key, "mac": mac_addr,
                                 "port_raw": repr(port_raw)})
            skipped += 1
            continue

        status_int = int(cols.get(_COL_STATUS, 0) or 0)
        if status_int not in _ENTRY_STATUS:
            log.warning("mac_snmp_skip_row", "Q-bridge row has unknown status integer",
                        context={"index_key": index_key, "mac": mac_addr,
                                 "status_int": status_int})
            skipped += 1
            continue

        entries.append(MacEntry(
            mac_address=mac_addr,
            vlan=vlan,
            bridge_port=bridge_port,
            entry_status=_ENTRY_STATUS[status_int],
        ))

    return entries, skipped


def _parse_bridge_table(rows: list[dict[str, Any]]) -> tuple[list[MacEntry], int]:
    """Parse walk_table output from dot1dTpFdbTable into MacEntry list.

    Index format: <b0>.<b1>.<b2>.<b3>.<b4>.<b5> (6 MAC bytes, no VLAN).
    Columns: .1=dot1dTpFdbAddress (OctetString MAC), .2=port, .3=status.
    vlan is always None for this table.

    Returns (entries, skipped_count).
    """
    raw: dict[str, dict[str, Any]] = {}
    for row in rows:
        for oid_suffix, val in row.items():
            col, _, index_key = oid_suffix.partition(".")
            if not index_key:
                continue
            raw.setdefault(index_key, {})[col] = val

    entries: list[MacEntry] = []
    skipped = 0

    for index_key, cols in raw.items():
        # index_key: "<b0>.<b1>.<b2>.<b3>.<b4>.<b5>"
        try:
            mac_addr = mac_from_dotted_decimal(index_key)
        except ValueError as exc:
            log.warning("mac_snmp_skip_row", "Bridge row has malformed MAC in index",
                        context={"index_key": index_key, "error": str(exc)})
            skipped += 1
            continue

        # Cross-verify with column .1 (dot1dTpFdbAddress) if present
        col1_raw = cols.get("1")
        if col1_raw is not None:
            try:
                mac_from_col = mac_from_bytes(col1_raw)
                if mac_from_col != mac_addr:
                    log.warning(
                        "mac_snmp_index_mac_mismatch",
                        "Bridge MAC in column .1 differs from index-derived MAC",
                        context={"index_key": index_key,
                                 "index_mac": mac_addr,
                                 "col_mac": mac_from_col},
                    )
            except (ValueError, TypeError):
                pass

        port_raw = cols.get(_COL_PORT)
        if port_raw is None:
            log.warning("mac_snmp_skip_row", "Bridge row missing port column",
                        context={"index_key": index_key, "mac": mac_addr})
            skipped += 1
            continue
        try:
            bridge_port = int(port_raw)
        except (ValueError, TypeError):
            log.warning("mac_snmp_skip_row", "Bridge row has non-integer port",
                        context={"index_key": index_key, "mac": mac_addr,
                                 "port_raw": repr(port_raw)})
            skipped += 1
            continue

        status_int = int(cols.get(_COL_STATUS, 0) or 0)
        if status_int not in _ENTRY_STATUS:
            log.warning("mac_snmp_skip_row", "Bridge row has unknown status integer",
                        context={"index_key": index_key, "mac": mac_addr,
                                 "status_int": status_int})
            skipped += 1
            continue

        entries.append(MacEntry(
            mac_address=mac_addr,
            vlan=None,
            bridge_port=bridge_port,
            entry_status=_ENTRY_STATUS[status_int],
        ))

    return entries, skipped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def collect_mac(
    device_ip: str,
    community: str,
    *,
    version: str = "2c",
    timeout_sec: float = 10.0,
    port: int = 161,
) -> list[MacEntry]:
    """Collect the MAC/FDB table from a device via SNMP.

    Walks dot1qTpFdbTable (1.3.6.1.2.1.17.7.1.2.2.1) for VLAN-aware
    switches. Falls back to dot1dTpFdbTable (1.3.6.1.2.1.17.4.3.1) if
    the primary table is empty — some simpler switches only implement
    the older BRIDGE-MIB. Devices that do not act as bridges (e.g.
    firewalls, routers) return an empty list from both tables, which is
    a valid result.

    Args:
        device_ip: Target device IP address.
        community: SNMP community string.
        version: SNMP version string; only "2c" is supported.
        timeout_sec: Per-PDU response timeout in seconds.
        port: SNMP UDP port (default 161).

    Returns:
        List of MacEntry. vlan is the resolved 802.1Q VLAN ID from
        dot1qVlanCurrentTable; None when unresolvable or when the
        BRIDGE-MIB fallback was used.

    Raises:
        SnmpTimeoutError: Device did not respond within timeout_sec.
        SnmpAuthError: Community string rejected by device.
        SnmpError: Any other SNMP-level failure.
    """
    start = time.monotonic()
    log.debug("mac_snmp_started", "MAC SNMP collection starting",
              context={"device_ip": device_ip, "version": version})

    try:
        rows = await snmp_collector.walk_table(
            device_ip, community, _OID_Q_BRIDGE,
            version=version, timeout_sec=timeout_sec, port=port,
        )
    except (SnmpTimeoutError, SnmpAuthError):
        raise
    except SnmpError as exc:
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        # TODO: apply dedup wrapper — a flapping device emits many identical
        # errors per polling cycle. See docs/CODING_STANDARDS.md "Deduplication".
        log.error("mac_snmp_failed", "MAC SNMP walk failed",
                  context={"device_ip": device_ip, "error": str(exc),
                           "error_type": type(exc).__name__,
                           "duration_ms": duration_ms})
        raise

    if not rows:
        log.debug("mac_snmp_fallback", "Primary MAC table empty — trying dot1dTpFdbTable",
                  context={"device_ip": device_ip, "reason": "primary_table_empty"})
        try:
            bridge_rows = await snmp_collector.walk_table(
                device_ip, community, _OID_BRIDGE,
                version=version, timeout_sec=timeout_sec, port=port,
            )
        except (SnmpTimeoutError, SnmpAuthError):
            raise
        except SnmpError as exc:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            # TODO: apply dedup wrapper — see above.
            log.error("mac_snmp_failed", "MAC SNMP fallback walk failed",
                      context={"device_ip": device_ip, "error": str(exc),
                               "error_type": type(exc).__name__,
                               "duration_ms": duration_ms})
            raise
        entries, skipped = _parse_bridge_table(bridge_rows)
        vlan_aware = False
    else:
        # Walk VLAN table to resolve raw FDB IDs → 802.1Q VLAN IDs
        try:
            fdb_vlan_map = await _collect_fdb_vlan_map(
                device_ip, community,
                version=version, timeout_sec=timeout_sec, port=port,
            )
        except (SnmpTimeoutError, SnmpAuthError, SnmpError):
            fdb_vlan_map = {}

        if not fdb_vlan_map:
            log.warning("mac_snmp_vlan_map_empty",
                        "dot1qVlanCurrentTable returned no entries — VLAN IDs will be None",
                        context={"device_ip": device_ip})

        entries, skipped = _parse_q_bridge_table(rows, fdb_vlan_map)

        if fdb_vlan_map:
            seen_fdb_ids: set[int] = set()
            for row in rows:
                for oid_suffix in row:
                    parts = oid_suffix.split(".", 2)
                    if len(parts) >= 2:
                        try:
                            seen_fdb_ids.add(int(parts[1]))
                        except ValueError:
                            pass
            orphan_fdb_ids = seen_fdb_ids - set(fdb_vlan_map.keys())
            if orphan_fdb_ids:
                examples = sorted(orphan_fdb_ids)[:5]
                log.warning("mac_snmp_orphan_fdb_ids",
                            "FDB IDs not found in VLAN map — affected entries will have vlan=None",
                            context={"device_ip": device_ip,
                                     "orphan_count": len(orphan_fdb_ids),
                                     "examples": examples})

        vlan_aware = bool(entries)

    duration_ms = round((time.monotonic() - start) * 1000, 1)
    log.debug("mac_snmp_completed", "MAC SNMP collection complete",
              context={"device_ip": device_ip, "entry_count": len(entries),
                       "vlan_aware": vlan_aware, "skipped": skipped,
                       "duration_ms": duration_ms})

    return entries
