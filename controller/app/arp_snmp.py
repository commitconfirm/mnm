"""SNMP-based ARP collector for MNM Controller.

Walks ipNetToMediaTable (RFC 2011: 1.3.6.1.2.1.4.22.1) and returns
structured ARP entries. Falls back to ipNetToPhysicalTable (RFC 4293:
1.3.6.1.2.1.4.35.1) when the primary table is empty — some newer
devices use the unified IPv4/IPv6 table exclusively.

Uses snmp_collector.walk_table() — no direct pysnmp calls here.
Integration into the polling loop is handled separately.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app import snmp_collector
from app.snmp_collector import SnmpAuthError, SnmpError, SnmpTimeoutError
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="arp_snmp")

# ipNetToMediaTable (RFC 2011)
_OID_ARP_TABLE = "1.3.6.1.2.1.4.22.1"
# ipNetToPhysicalTable (RFC 4293) — fallback for newer devices
_OID_ARP_PHYSICAL = "1.3.6.1.2.1.4.35.1"

# ipNetToMediaType integer → string (RFC 2011)
_ENTRY_TYPE = {1: "other", 2: "invalid", 3: "dynamic", 4: "static"}

# ipNetToPhysicalTable column numbers
# .1=ifIndex, .2=netAddressType, .3=netAddress, .4=physAddress,
# .5=lastUpdated, .6=type, .7=state, .8=rowStatus
_PHYS_COL_PHYS_ADDR = "4"
_PHYS_COL_TYPE = "6"


@dataclass
class ArpEntry:
    ip_address: str
    mac_address: str        # lowercase colon-separated: "aa:bb:cc:dd:ee:ff"
    interface_index: int    # ifIndex — name resolution is caller's responsibility
    entry_type: str         # "other" | "invalid" | "dynamic" | "static"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _mac_from_bytes(raw: Any) -> str | None:
    """Convert a raw MAC value (bytes or hex string) to 'aa:bb:cc:dd:ee:ff'.

    walk_table delivers OctetString values as either a UTF-8 decoded string
    (if the bytes happen to be valid UTF-8) or a hex string (if not). We
    handle both cases plus a plain bytes object for robustness.
    Returns None if the value cannot be parsed as a 6-byte MAC.
    """
    if isinstance(raw, bytes):
        if len(raw) != 6:
            return None
        return ":".join(f"{b:02x}" for b in raw)

    if isinstance(raw, str):
        # hex string from _convert_value fallback: "aabbccddeeff"
        clean = raw.replace(":", "").replace("-", "").lower()
        if len(clean) == 12 and all(c in "0123456789abcdef" for c in clean):
            return ":".join(clean[i:i+2] for i in range(0, 12, 2))
        # _convert_value UTF-8 decodes OctetStrings when all bytes are valid
        # UTF-8. Encoding back with utf-8 recovers the exact original bytes.
        recovered = raw.encode("utf-8")
        if len(recovered) == 6:
            return ":".join(f"{b:02x}" for b in recovered)
        return None

    return None


def _parse_arp_table(rows: list[dict[str, Any]]) -> tuple[list[ArpEntry], int]:
    """Parse walk_table output from ipNetToMediaTable into ArpEntry list.

    Base OID 1.3.6.1.2.1.4.22.1 is ipNetToMediaEntry (already includes
    the entry subidentifier), so walk_table returns suffixes of the form
    "<col>.<ifIndex>.<ip>" — no leading entry subid to strip.

    Returns (entries, skipped_count).
    """
    # Accumulate columns grouped by index key (ifIndex.ip)
    raw: dict[str, dict[str, Any]] = {}
    for row in rows:
        for oid_suffix, val in row.items():
            col, _, index_key = oid_suffix.partition(".")
            if not index_key:
                continue
            raw.setdefault(index_key, {})[col] = val

    entries: list[ArpEntry] = []
    skipped = 0

    for index_key, cols in raw.items():
        # Index key: "<ifIndex>.<a>.<b>.<c>.<d>"
        idx_parts = index_key.split(".", 1)
        if len(idx_parts) < 2:
            log.warning("arp_snmp_skip_row", "Skipping row with unparseable index",
                        context={"index_key": index_key, "cols": list(cols.keys())})
            skipped += 1
            continue

        try:
            ifindex = int(idx_parts[0])
        except ValueError:
            log.warning("arp_snmp_skip_row", "Skipping row with non-integer ifIndex",
                        context={"index_key": index_key})
            skipped += 1
            continue

        ip_str = str(cols.get("3", idx_parts[1]))
        if not ip_str or ip_str == "None":
            # Fall back to IP embedded in the index
            ip_str = idx_parts[1]

        mac_raw = cols.get("2")
        if mac_raw is None:
            log.warning("arp_snmp_skip_row", "Skipping row missing MAC column",
                        context={"index_key": index_key, "ip": ip_str})
            skipped += 1
            continue

        mac = _mac_from_bytes(mac_raw)
        if mac is None:
            log.warning("arp_snmp_skip_row", "Skipping row with malformed MAC",
                        context={"index_key": index_key, "ip": ip_str,
                                 "mac_raw": repr(mac_raw)})
            skipped += 1
            continue

        type_int = int(cols.get("4", 0) or 0)
        entry_type = _ENTRY_TYPE.get(type_int, "other")

        entries.append(ArpEntry(
            ip_address=ip_str,
            mac_address=mac,
            interface_index=ifindex,
            entry_type=entry_type,
        ))

    return entries, skipped


def _parse_phys_table(rows: list[dict[str, Any]]) -> tuple[list[ArpEntry], int]:
    """Parse walk_table output from ipNetToPhysicalTable (RFC 4293 fallback).

    Base OID 1.3.6.1.2.1.4.35.1 is ipNetToPhysicalEntry, so walk_table
    suffixes are "<col>.<ifIndex>.<addrType>.<addrLen>.<ip-octets>".
    Columns: .1=ifIndex, .4=physAddress, .6=type.
    """
    raw: dict[str, dict[str, Any]] = {}
    for row in rows:
        for oid_suffix, val in row.items():
            col, _, index_key = oid_suffix.partition(".")
            if not index_key:
                continue
            raw.setdefault(index_key, {})[col] = val

    entries: list[ArpEntry] = []
    skipped = 0

    for index_key, cols in raw.items():
        idx_parts = index_key.split(".")
        try:
            ifindex = int(idx_parts[0])
        except (ValueError, IndexError):
            skipped += 1
            continue

        mac_raw = cols.get(_PHYS_COL_PHYS_ADDR)
        if mac_raw is None:
            skipped += 1
            continue

        mac = _mac_from_bytes(mac_raw)
        if mac is None:
            log.warning("arp_snmp_skip_row", "Skipping phys table row with malformed MAC",
                        context={"index_key": index_key, "mac_raw": repr(mac_raw)})
            skipped += 1
            continue

        # ipNetToPhysicalType: 1=other, 2=invalid, 3=dynamic, 4=static, 5=local
        type_int = int(cols.get(_PHYS_COL_TYPE, 0) or 0)
        entry_type = _ENTRY_TYPE.get(type_int, "other")

        # IP is embedded in the index as: ifIndex.addrType.addrLen.a.b.c.d
        # addrType 1=IPv4. For IPv4: idx[1]=1 (type), idx[2]=4 (len), idx[3:7]=octets
        ip_str = ""
        if len(idx_parts) >= 7 and idx_parts[1] == "1":
            ip_str = ".".join(idx_parts[3:7])

        entries.append(ArpEntry(
            ip_address=ip_str,
            mac_address=mac,
            interface_index=ifindex,
            entry_type=entry_type,
        ))

    return entries, skipped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def collect_arp(
    device_ip: str,
    community: str,
    *,
    version: str = "2c",
    timeout_sec: float = 10.0,
    port: int = 161,
) -> list[ArpEntry]:
    """Collect the ARP table from a device via SNMP.

    Walks ipNetToMediaTable (1.3.6.1.2.1.4.22.1) and returns structured
    entries. Falls back to ipNetToPhysicalTable (1.3.6.1.2.1.4.35.1) if
    the primary table is empty — some newer devices use the IPv4/IPv6
    unified table exclusively.

    Raises:
        SnmpTimeoutError: Device did not respond within timeout_sec.
        SnmpAuthError: Community string rejected by device.
        SnmpError: Any other SNMP-level failure.
    """
    start = time.monotonic()
    log.debug("arp_snmp_started", "ARP SNMP collection starting",
              context={"device_ip": device_ip, "version": version})

    try:
        rows = await snmp_collector.walk_table(
            device_ip, community, _OID_ARP_TABLE,
            version=version, timeout_sec=timeout_sec, port=port,
        )
    except (SnmpTimeoutError, SnmpAuthError):
        raise
    except SnmpError as exc:
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        # TODO: apply dedup wrapper — a flapping device emits many identical
        # errors per polling cycle. See docs/CODING_STANDARDS.md "Deduplication".
        log.error("arp_snmp_failed", "ARP SNMP walk failed",
                  context={"device_ip": device_ip, "error": str(exc),
                           "error_type": type(exc).__name__,
                           "duration_ms": duration_ms})
        raise

    entries, skipped = _parse_arp_table(rows)

    if not entries:
        log.debug("arp_snmp_fallback", "Primary ARP table empty — trying ipNetToPhysicalTable",
                  context={"device_ip": device_ip, "reason": "primary_table_empty"})
        try:
            phys_rows = await snmp_collector.walk_table(
                device_ip, community, _OID_ARP_PHYSICAL,
                version=version, timeout_sec=timeout_sec, port=port,
            )
        except (SnmpTimeoutError, SnmpAuthError):
            raise
        except SnmpError as exc:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            # TODO: apply dedup wrapper — see above.
            log.error("arp_snmp_failed", "ARP SNMP fallback walk failed",
                      context={"device_ip": device_ip, "error": str(exc),
                               "error_type": type(exc).__name__,
                               "duration_ms": duration_ms})
            raise
        entries, skipped = _parse_phys_table(phys_rows)

    duration_ms = round((time.monotonic() - start) * 1000, 1)
    log.debug("arp_snmp_completed", "ARP SNMP collection complete",
              context={"device_ip": device_ip, "entry_count": len(entries),
                       "skipped": skipped, "duration_ms": duration_ms})

    return entries
