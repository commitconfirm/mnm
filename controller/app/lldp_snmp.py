"""SNMP-based LLDP neighbor collector for MNM Controller.

Walks lldpRemTable (LLDP-MIB, 1.0.8802.1.1.2.1.4.1.1) for neighbor data,
joins with lldpRemManAddrTable (1.0.8802.1.1.2.1.4.2.1) for advertised
management IPs, and with ifXTable.ifName (via snmp_collector
collect_ifindex_to_name) for local interface name resolution.

Uses snmp_collector.walk_table() — no direct pysnmp calls here.
MAC normalization uses snmp_collector.mac_from_bytes — shared with
arp_snmp and mac_snmp. Integration into the polling loop is handled
separately.

LLDP-MIB row structure
----------------------
lldpRemTable index: ``<TimeMark>.<LocalPortNum>.<RemIndex>`` — three
integers. Columns on the entry:

    .4 lldpRemChassisIdSubtype  Integer (1-7) — see chassis subtype enum
    .5 lldpRemChassisId         OctetString — opaque per subtype
    .6 lldpRemPortIdSubtype     Integer (1-7) — see port subtype enum
    .7 lldpRemPortId            OctetString — opaque per subtype
    .8 lldpRemPortDesc          OctetString (not exposed by collector;
                                 PortId is the authoritative identity)
    .9 lldpRemSysName           OctetString
    .10 lldpRemSysDesc          OctetString

Chassis / Port ID subtype encoding (RFC 2922 §8)
------------------------------------------------
Chassis ID subtypes (lldpRemChassisIdSubtype):
    1 = chassisComponent     ASCII free string
    2 = interfaceAlias       ASCII free string
    3 = portComponent        ASCII free string
    4 = macAddress           6 raw bytes → colon-hex
    5 = networkAddress       byte 0 = family (1=IPv4, 2=IPv6) + N bytes
    6 = interfaceName        ASCII free string
    7 = local               ASCII free string

Port ID subtypes (lldpRemPortIdSubtype) — same values EXCEPT:
    1 = interfaceAlias
    2 = portComponent
    3 = macAddress
    4 = networkAddress
    5 = interfaceName
    6 = agentCircuitId       ASCII free string (differs from chassis!)
    7 = local

The _decode_lldp_id helper handles this asymmetric enum.

Management IP semantics
-----------------------
lldpRemManAddrTable advertises management addresses per neighbor, but
many devices advertise their MAC (IANA addr-family 6 = "802") rather
than an IP. We prefer IPv4 (family=1), then IPv6 (family=2) as a hex
string placeholder, and ignore MAC/other subtypes.

``LldpNeighbor.management_ip = None`` is a normal outcome — it means
the neighbor did not advertise a usable IP. Cross-referencing for IP
derivation (ARP cache, DNS, Nautobot lookup) is explicitly a later
enrichment pass, not this collector's responsibility.

IPv6 support is deferred: IPv6 chassis/port IDs and IPv6 management
addresses currently return a ``ipv6:<hex>`` placeholder. Full v6
support lands when a v6-primary lab device is available.
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
    oid,
)
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="lldp_snmp")

_OID_LLDP_REM = oid("LLDP-MIB::lldpRemEntry")            # lldpRemTable
_OID_LLDP_MAN_ADDR = oid("LLDP-MIB::lldpRemManAddrEntry")  # lldpRemManAddrTable

# Column numbers under lldpRemEntry
_COL_CHASSIS_SUBTYPE = "4"
_COL_CHASSIS_ID = "5"
_COL_PORT_SUBTYPE = "6"
_COL_PORT_ID = "7"
_COL_SYS_NAME = "9"
_COL_SYS_DESC = "10"

# Chassis ID subtype integer → canonical lowercase string name
_CHASSIS_SUBTYPES: dict[int, str] = {
    1: "chassis_component",
    2: "interface_alias",
    3: "port_component",
    4: "mac_address",
    5: "network_address",
    6: "interface_name",
    7: "locally_assigned",
}

# Port ID subtype integer → canonical string — differs from chassis at 1, 2, 5, 6
_PORT_SUBTYPES: dict[int, str] = {
    1: "interface_alias",
    2: "port_component",
    3: "mac_address",
    4: "network_address",
    5: "interface_name",
    6: "agent_circuit_id",
    7: "locally_assigned",
}

# IANA address family numbers (RFC 3232) subset relevant to LLDP
_IANA_IPV4 = 1
_IANA_IPV6 = 2


@dataclass
class LldpNeighbor:
    local_port_ifindex: int
    local_port_name: str | None        # from ifTable join; None if ifIndex unresolved
    remote_chassis_id: str              # normalized per subtype
    remote_chassis_id_subtype: str      # canonical subtype name
    remote_port_id: str                 # normalized per subtype
    remote_port_id_subtype: str         # canonical subtype name
    remote_system_name: str | None
    remote_system_description: str | None
    management_ip: str | None           # None when neighbor did not advertise a usable IP


# ---------------------------------------------------------------------------
# Internal helpers — subtype decoders
# ---------------------------------------------------------------------------

def _decode_text(raw: bytes) -> str:
    """Decode ASCII-ish bytes to text, stripping trailing NULs."""
    return raw.decode("utf-8", errors="replace").rstrip("\x00")


def _decode_network_address(raw: bytes) -> str:
    """Decode an LLDP network_address octet string.

    Byte 0 is the IANA address family (1=IPv4, 2=IPv6). Remaining bytes
    are the address. IPv6 returns a hex placeholder; full v6 parsing is
    deferred until a v6-primary lab device is available (see module
    docstring).
    """
    if not raw:
        return ""
    family = raw[0]
    addr_bytes = raw[1:]
    if family == _IANA_IPV4 and len(addr_bytes) == 4:
        return ".".join(str(b) for b in addr_bytes)
    if family == _IANA_IPV6:
        # TODO: implement full v6 colon-hex once a v6-primary lab device is available
        return "ipv6:" + addr_bytes.hex()
    # Unknown/unsupported family — preserve as hex for diagnostics
    return f"family{family}:" + addr_bytes.hex()


def _decode_lldp_id(
    raw_bytes: bytes,
    subtype: int,
    *,
    is_port_id: bool,
    device_ip: str | None = None,
) -> tuple[str, str]:
    """Decode an LLDP chassis/port ID octet string per its subtype.

    Returns ``(normalized_value, subtype_name)``.

    Args:
        raw_bytes: The raw OctetString from lldpRemChassisId / lldpRemPortId.
        subtype: Integer subtype from lldpRemChassisIdSubtype / lldpRemPortIdSubtype.
        is_port_id: True for port IDs, False for chassis IDs. Only affects
            which subtype enum is used — byte decoding is identical.
        device_ip: Optional device IP for warning-log context on malformed MACs.

    For mac_address subtype: uses mac_from_bytes when length is 6; falls
    back to hex representation and logs ``lldp_snmp_malformed_mac_id`` if
    the length is wrong. For unknown subtypes, returns hex bytes plus
    ``unknown_subtype_N`` name.
    """
    enum = _PORT_SUBTYPES if is_port_id else _CHASSIS_SUBTYPES
    name = enum.get(subtype)

    if name is None:
        return raw_bytes.hex(), f"unknown_subtype_{subtype}"

    if name == "mac_address":
        try:
            return mac_from_bytes(raw_bytes), name
        except ValueError:
            log.warning(
                "lldp_snmp_malformed_mac_id",
                "LLDP ID declared as MAC but byte length != 6",
                context={"device_ip": device_ip, "subtype": subtype,
                         "is_port_id": is_port_id,
                         "raw_hex": raw_bytes.hex(),
                         "length": len(raw_bytes)},
            )
            return raw_bytes.hex(), name

    if name == "network_address":
        return _decode_network_address(raw_bytes), name

    # All remaining subtypes are free-form text per the spec.
    return _decode_text(raw_bytes), name


# ---------------------------------------------------------------------------
# Internal helpers — lldpRemManAddrTable
# ---------------------------------------------------------------------------

def _parse_lldp_man_addr(
    rows: list[dict[str, Any]],
) -> dict[tuple[int, int, int], str]:
    """Build ``{(time_mark, local_port_num, rem_index): management_ip}``
    from a walk of lldpRemManAddrTable.

    Table index structure:
        ``<TimeMark>.<LocalPortNum>.<RemIndex>.<AddrSubtype>.<AddrLen>.<AddrBytes>``

    AddrSubtype is the IANA address-family number (RFC 3232):
        1 = IPv4 (4 bytes), 2 = IPv6 (16 bytes), 6 = 802/MAC, ...

    Priority: IPv4 over IPv6. MAC and other families are ignored — they
    are not usable as a "management IP" even though the protocol allows
    advertising them here. A neighbor that only advertises a MAC gets no
    entry in the returned map.

    Returns an empty dict when the walk returned nothing.
    """
    # Collect per-neighbor candidates keyed on (tm, lpn, ridx), then pick
    # IPv4 > IPv6 > nothing.
    per_neighbor: dict[tuple[int, int, int], dict[str, str]] = {}

    for row in rows:
        for suffix, _val in row.items():
            parts = suffix.split(".")
            # Expected: <col>.<tm>.<lpn>.<ridx>.<subtype>.<addrlen>.<addrbytes...>
            if len(parts) < 6:
                continue
            try:
                tm = int(parts[1])
                lpn = int(parts[2])
                ridx = int(parts[3])
                addr_subtype = int(parts[4])
                addr_len = int(parts[5])
            except ValueError:
                continue
            addr_parts = parts[6:]
            if len(addr_parts) != addr_len:
                continue  # malformed index — defensive

            key = (tm, lpn, ridx)
            slot = per_neighbor.setdefault(key, {})

            if addr_subtype == _IANA_IPV4 and addr_len == 4:
                # Only set once — same (tm, lpn, ridx, subtype, addr) repeats
                # across every walked column.
                try:
                    ip = ".".join(str(int(b)) for b in addr_parts)
                except ValueError:
                    continue
                slot.setdefault("ipv4", ip)
            elif addr_subtype == _IANA_IPV6 and addr_len == 16:
                try:
                    raw = bytes(int(b) for b in addr_parts)
                except ValueError:
                    continue
                # TODO: colon-hex v6 formatting once v6 device is available
                slot.setdefault("ipv6", "ipv6:" + raw.hex())
            # Other families (6=MAC, etc.) silently skipped.

    result: dict[tuple[int, int, int], str] = {}
    for key, addrs in per_neighbor.items():
        if "ipv4" in addrs:
            result[key] = addrs["ipv4"]
        elif "ipv6" in addrs:
            result[key] = addrs["ipv6"]
    return result


# ---------------------------------------------------------------------------
# Internal helpers — lldpRemTable parsing
# ---------------------------------------------------------------------------

def _parse_lldp_rem_table(
    rows: list[dict[str, Any]],
    man_addr_map: dict[tuple[int, int, int], str],
    ifindex_to_name: dict[int, str],
    *,
    device_ip: str,
) -> tuple[list[LldpNeighbor], int]:
    """Parse walk_table output from lldpRemTable into LldpNeighbor list.

    Groups rows by the three-part index ``<TimeMark>.<LocalPortNum>.<RemIndex>``
    and extracts columns 4, 5, 6, 7, 9, 10. Joins management_ip from
    man_addr_map and local_port_name from ifindex_to_name.

    Returns (neighbors, skipped_count).
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        for suffix, val in row.items():
            col, _, idx = suffix.partition(".")
            if not idx:
                continue
            grouped.setdefault(idx, {})[col] = val

    neighbors: list[LldpNeighbor] = []
    skipped = 0

    for idx_key, cols in grouped.items():
        idx_parts = idx_key.split(".")
        if len(idx_parts) < 3:
            log.warning("lldp_snmp_skip_row", "Row index has fewer than 3 parts",
                        context={"device_ip": device_ip, "index_key": idx_key})
            skipped += 1
            continue
        try:
            time_mark = int(idx_parts[0])
            local_port_num = int(idx_parts[1])
            rem_index = int(idx_parts[2])
        except ValueError:
            log.warning("lldp_snmp_skip_row", "Row index has non-integer component",
                        context={"device_ip": device_ip, "index_key": idx_key})
            skipped += 1
            continue

        chassis_subtype_raw = cols.get(_COL_CHASSIS_SUBTYPE)
        chassis_id_raw = cols.get(_COL_CHASSIS_ID)
        port_subtype_raw = cols.get(_COL_PORT_SUBTYPE)
        port_id_raw = cols.get(_COL_PORT_ID)

        if chassis_subtype_raw is None or chassis_id_raw is None:
            log.warning("lldp_snmp_skip_row", "Row missing chassis subtype/id columns",
                        context={"device_ip": device_ip, "index_key": idx_key,
                                 "cols": list(cols.keys())})
            skipped += 1
            continue
        if port_subtype_raw is None or port_id_raw is None:
            log.warning("lldp_snmp_skip_row", "Row missing port subtype/id columns",
                        context={"device_ip": device_ip, "index_key": idx_key,
                                 "cols": list(cols.keys())})
            skipped += 1
            continue

        try:
            chassis_subtype = int(chassis_subtype_raw)
            port_subtype = int(port_subtype_raw)
        except (TypeError, ValueError):
            log.warning("lldp_snmp_skip_row", "Row has non-integer subtype",
                        context={"device_ip": device_ip, "index_key": idx_key})
            skipped += 1
            continue

        if not isinstance(chassis_id_raw, bytes) or not isinstance(port_id_raw, bytes):
            log.warning("lldp_snmp_skip_row", "Row chassis/port id is not bytes",
                        context={"device_ip": device_ip, "index_key": idx_key,
                                 "chassis_type": type(chassis_id_raw).__name__,
                                 "port_type": type(port_id_raw).__name__})
            skipped += 1
            continue

        chassis_id, chassis_subtype_name = _decode_lldp_id(
            chassis_id_raw, chassis_subtype,
            is_port_id=False, device_ip=device_ip,
        )
        port_id, port_subtype_name = _decode_lldp_id(
            port_id_raw, port_subtype,
            is_port_id=True, device_ip=device_ip,
        )

        sys_name_raw = cols.get(_COL_SYS_NAME)
        sys_desc_raw = cols.get(_COL_SYS_DESC)
        sys_name = _decode_text(sys_name_raw) if isinstance(sys_name_raw, bytes) and sys_name_raw else None
        sys_desc = _decode_text(sys_desc_raw) if isinstance(sys_desc_raw, bytes) and sys_desc_raw else None

        management_ip = man_addr_map.get((time_mark, local_port_num, rem_index))
        local_port_name = ifindex_to_name.get(local_port_num)

        neighbors.append(LldpNeighbor(
            local_port_ifindex=local_port_num,
            local_port_name=local_port_name,
            remote_chassis_id=chassis_id,
            remote_chassis_id_subtype=chassis_subtype_name,
            remote_port_id=port_id,
            remote_port_id_subtype=port_subtype_name,
            remote_system_name=sys_name,
            remote_system_description=sys_desc,
            management_ip=management_ip,
        ))

    return neighbors, skipped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def collect_lldp(
    device_ip: str,
    community: str,
    *,
    version: str = "2c",
    timeout_sec: float = 10.0,
    port: int = 161,
) -> list[LldpNeighbor]:
    """Collect LLDP neighbors from a device via SNMP.

    Walks lldpRemTable (1.0.8802.1.1.2.1.4.1.1) for neighbor data, joins
    with lldpRemManAddrTable (1.0.8802.1.1.2.1.4.2.1) for advertised
    management IPs, and with ifXTable.ifName (via
    ``snmp_collector.collect_ifindex_to_name``) for local interface name
    resolution.

    ``LldpNeighbor.management_ip = None`` is a normal outcome — it means
    the neighbor did not advertise a usable IP. Cross-referencing to
    derive management IPs (via ARP cache, DNS, Nautobot lookup) is
    explicitly a later enrichment pass that runs after all three
    collectors have data.

    Raises:
        SnmpTimeoutError: Device did not respond within timeout_sec.
        SnmpAuthError: Community string rejected by device.
        SnmpError: Any other SNMP-level failure walking lldpRemTable.
    """
    start = time.monotonic()
    log.debug("lldp_snmp_started", "LLDP SNMP collection starting",
              context={"device_ip": device_ip, "version": version})

    try:
        rem_rows = await snmp_collector.walk_table(
            device_ip, community, _OID_LLDP_REM,
            version=version, timeout_sec=timeout_sec, port=port,
        )
    except (SnmpTimeoutError, SnmpAuthError):
        raise
    except SnmpError as exc:
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        # TODO: apply dedup wrapper — see docs/CODING_STANDARDS.md "Deduplication".
        log.error("lldp_snmp_failed", "LLDP SNMP walk failed",
                  context={"device_ip": device_ip, "error": str(exc),
                           "error_type": type(exc).__name__,
                           "duration_ms": duration_ms})
        raise

    if not rem_rows:
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        log.info("lldp_snmp_no_neighbors",
                 "No LLDP neighbors found (table empty or LLDP-MIB unsupported)",
                 context={"device_ip": device_ip, "duration_ms": duration_ms})
        return []

    # Management addresses — degrade to empty on any SNMP error; individual
    # neighbors get management_ip=None rather than failing the collection.
    try:
        man_addr_rows = await snmp_collector.walk_table(
            device_ip, community, _OID_LLDP_MAN_ADDR,
            version=version, timeout_sec=timeout_sec, port=port,
        )
    except (SnmpError, SnmpTimeoutError, SnmpAuthError) as exc:
        log.debug("lldp_snmp_man_addr_walk_failed",
                  "Management address walk failed — neighbors will have management_ip=None",
                  context={"device_ip": device_ip, "error": str(exc)})
        man_addr_rows = []

    man_addr_map = _parse_lldp_man_addr(man_addr_rows)

    # Interface name resolution — shared helper, degrades to empty internally
    ifindex_to_name = await snmp_collector.collect_ifindex_to_name(
        device_ip, community,
        version=version, timeout_sec=timeout_sec, port=port,
    )

    neighbors, skipped = _parse_lldp_rem_table(
        rem_rows, man_addr_map, ifindex_to_name,
        device_ip=device_ip,
    )

    with_mgmt_ip = sum(1 for n in neighbors if n.management_ip is not None)
    duration_ms = round((time.monotonic() - start) * 1000, 1)
    log.debug("lldp_snmp_completed", "LLDP SNMP collection complete",
              context={"device_ip": device_ip,
                       "neighbor_count": len(neighbors),
                       "with_mgmt_ip_count": with_mgmt_ip,
                       "skipped": skipped,
                       "duration_ms": duration_ms})

    return neighbors
