"""Generic SNMP collection primitives for MNM Controller.

Provides walk_table() and get_scalar() — the two building blocks used by
all SNMP-based collection jobs in v1.0+: ARP, MAC, LLDP, routing, and any
future MIB walks.

Wraps pysnmp 7.x async HLAPI. Key notes:
  - bulk_cmd is a coroutine returning one (errI, errS, errIdx, varBinds) tuple
    per call, NOT an async iterator. Table walks require a manual cursor loop.
  - UdpTransportTarget requires async factory: await UdpTransportTarget.create().
  - varBinds from bulk_cmd is a flat tuple[ObjectType, ...].
  - varBinds from get_cmd is a tuple of ObjectType objects.

This module owns SNMP-to-Python type conversion. All callers receive plain
Python native types — no pysnmp objects leak out.

OctetString type contract
-------------------------
OctetString values are returned as raw ``bytes``. Callers that need text
(e.g., sysDescr, interface descriptions) must decode explicitly:
``value.decode("utf-8", errors="replace")``. Callers that need binary
data (MAC addresses, chassis IDs, cryptographic material) receive it
unmodified. Never decode OctetString eagerly in this module — doing so
destroys binary data silently when bytes happen to form valid UTF-8.

OID registry
------------
This module exports an ``OIDS`` dict and ``oid()`` helper mapping symbolic
``"MIB-NAME::objectName"`` strings to numeric OID strings.  Collectors
call ``oid("IP-MIB::ipNetToMediaEntry")`` instead of embedding numeric
strings directly.  A typo in the symbolic name raises ``KeyError`` at
import time rather than silently querying the wrong OID at runtime.
"""

from __future__ import annotations

import time
from typing import Any

from pysnmp.hlapi.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    bulk_cmd,
    get_cmd,
)
from pysnmp.proto import errind, rfc1902
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject
from pyasn1.type import univ as asn1_univ

from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="snmp_collector")

# Maximum rows fetched in a single walk to prevent unbounded memory use on
# very large tables (e.g., a full FDB on a core switch).
_WALK_MAX_ROWS = 10_000


# ---------------------------------------------------------------------------
# OID registry
# ---------------------------------------------------------------------------

#: Symbolic OID name → numeric OID string for every MIB object MNM queries.
#:
#: Convention: keys are ``"MIB-NAME::objectName"``; values are the dotted-
#: decimal numeric OID string.  Collectors use ``oid("MIB-NAME::object")``
#: rather than embedding numeric strings.  A typo raises KeyError at import
#: time rather than silently walking the wrong OID at runtime.
#:
#: Scope: only OIDs used by MNM collectors.  Add entries here when building
#: new collectors — this is not a general MIB database.
OIDS: dict[str, str] = {
    # ARP / neighbour tables
    "IP-MIB::ipNetToMediaEntry":          "1.3.6.1.2.1.4.22.1",
    "IP-MIB::ipNetToPhysicalEntry":       "1.3.6.1.2.1.4.35.1",
    # MAC / FDB tables (Q-BRIDGE-MIB and BRIDGE-MIB)
    "Q-BRIDGE-MIB::dot1qTpFdbEntry":      "1.3.6.1.2.1.17.7.1.2.2.1",
    "BRIDGE-MIB::dot1dTpFdbEntry":        "1.3.6.1.2.1.17.4.3.1",
    "Q-BRIDGE-MIB::dot1qVlanCurrentEntry":"1.3.6.1.2.1.17.7.1.4.2.1",
    # Junos enterprise fallback for FDB-ID → 802.1Q VLAN mapping.  Used when
    # dot1qVlanCurrentTable returns No Such Object — Junos does not implement
    # it.  Table jnxL2aldVlanTable: col .3 = vlanTag (802.1Q ID),
    # col .5 = vlanFdbId (matches dot1qFdbId from dot1qTpFdbTable index).
    "JUNIPER-L2ALD-MIB::jnxL2aldVlanEntry":"1.3.6.1.4.1.2636.3.48.1.3.1.1",
    # Routing tables
    "IP-FORWARD-MIB::ipCidrRouteEntry":   "1.3.6.1.2.1.4.24.4.1",
    "RFC1213-MIB::ipRouteEntry":          "1.3.6.1.2.1.4.21.1",
    # LLDP neighbor tables (RFC 2922)
    "LLDP-MIB::lldpRemEntry":             "1.0.8802.1.1.2.1.4.1.1",
    "LLDP-MIB::lldpRemManAddrEntry":      "1.0.8802.1.1.2.1.4.2.1",
    # Interface name tables — ifXTable.ifName preferred, ifTable.ifDescr fallback
    "IF-MIB::ifIndex":                    "1.3.6.1.2.1.2.2.1.1",
    "IF-MIB::ifDescr":                    "1.3.6.1.2.1.2.2.1.2",
    "IF-MIB::ifName":                     "1.3.6.1.2.1.31.1.1.1.1",
    # Bridge port → ifIndex mapping (BRIDGE-MIB::dot1dBasePortIfIndex column).
    # Walking this column directly returns one row per bridge port; suffix is
    # the bridge port number (matches MacEntry.bridge_port), value is ifIndex.
    # Used by MAC collector to bridge from FDB rows to interface names.
    "BRIDGE-MIB::dot1dBasePortIfIndex":   "1.3.6.1.2.1.17.1.4.1.2",
    # IP address tables — ipAddressTable (modern, v4+v6) preferred with
    # legacy ipAddrTable fallback for older firmware (e.g. EX3300 12.3
    # returns empty for the modern table).
    "IP-MIB::ipAddressIfIndex":           "1.3.6.1.2.1.4.34.1.3",
    "IP-MIB::ipAddressPrefix":            "1.3.6.1.2.1.4.34.1.5",
    "RFC1213-MIB::ipAdEntIfIndex":        "1.3.6.1.2.1.4.20.1.2",
    "RFC1213-MIB::ipAdEntNetMask":        "1.3.6.1.2.1.4.20.1.3",
    # System MIB scalars — used by onboarding classifier (sysDescr substring
    # match + sysObjectID prefix fallback). See
    # .claude/design/nautobot_rest_schema_notes.md §3 for the captured
    # signal matrix across the lab vendor set.
    "SNMPv2-MIB::sysDescr":               "1.3.6.1.2.1.1.1.0",
    "SNMPv2-MIB::sysObjectID":            "1.3.6.1.2.1.1.2.0",
    "SNMPv2-MIB::sysName":                "1.3.6.1.2.1.1.5.0",
    # Juniper enterprise chassis scalars (JUNIPER-MIB at 2636.3.1) —
    # authoritative on Junos EX/SRX/MX; primary source of serial + chassis
    # model for the onboarding probe.
    "JUNIPER-MIB::jnxBoxDescr":           "1.3.6.1.4.1.2636.3.1.2.0",
    "JUNIPER-MIB::jnxBoxSerialNo":        "1.3.6.1.4.1.2636.3.1.3.0",
    # ENTITY-MIB fallbacks for serial / chassis model on vendors that don't
    # implement the Juniper scalars. Table columns under entPhysicalTable
    # (1.3.6.1.2.1.47.1.1.1); the onboarding probe walks for the first row
    # where entPhysicalClass = chassis(3).
    "ENTITY-MIB::entPhysicalClass":       "1.3.6.1.2.1.47.1.1.1.1.5",
    "ENTITY-MIB::entPhysicalSerialNum":   "1.3.6.1.2.1.47.1.1.1.1.11",
    "ENTITY-MIB::entPhysicalModelName":   "1.3.6.1.2.1.47.1.1.1.1.13",
    # PAN-OS enterprise scalars (PAN-COMMON-MIB at 25461.2.1.2.1) — analogue
    # to Juniper's jnxBoxSerialNo. PAN-OS sysDescr doesn't embed the PAN-OS
    # version string, so we read panSysSwVersion directly.
    "PAN-COMMON-MIB::panSysSwVersion":    "1.3.6.1.4.1.25461.2.1.2.1.1.0",
    "PAN-COMMON-MIB::panSysSerialNumber": "1.3.6.1.4.1.25461.2.1.2.1.3.0",
    # FortiGate enterprise scalar (FORTINET-CORE-MIB) — chassis serial.
    # No enterprise scalar for chassis model name; ENTITY-MIB walk is the
    # fallback for product-name discovery.
    "FORTINET-CORE-MIB::fnSysSerial":     "1.3.6.1.4.1.12356.1.2.0",
    # Cisco OLD-CISCO-CHASSIS-MIB::chassisType — pre-ENTITY-MIB chassis
    # description scalar. Real classic-IOS devices on older trains may
    # only populate this; modern IOS-XE populates ENTITY-MIB cleanly.
    # Probe tries this first and falls back to entPhysicalTable.
    "OLD-CISCO-CHASSIS-MIB::chassisType": "1.3.6.1.4.1.9.3.6.4.0",
}


def oid(name: str) -> str:
    """Resolve a symbolic OID name to its numeric string.

    Raises KeyError if the name is not registered in OIDS.
    """
    return OIDS[name]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SnmpError(Exception):
    """Base exception for SNMP collection failures."""


class SnmpTimeoutError(SnmpError):
    """Device did not respond within timeout."""


class SnmpAuthError(SnmpError):
    """Authentication failed (wrong community string or credentials)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _convert_value(val: Any) -> int | str | bytes | None:
    """Convert a pysnmp value object to a Python native type.

    Type mapping:
      - Integer family (Integer32, Counter32, Counter64, Gauge32, Unsigned32,
        TimeTicks) → int
      - IpAddress → str (dotted-quad)
      - ObjectIdentifier → str (dotted OID)
      - OctetString → bytes (raw, uninterpreted — see module docstring)
      - NoSuchInstance / NoSuchObject / EndOfMibView → None
      - Unknown types → str()

    OctetString is always returned as raw bytes. Callers decide whether to
    decode as text or treat as binary. Eagerly decoding as UTF-8 destroys
    binary data (MACs, chassis IDs) when bytes happen to be valid UTF-8.
    """
    # NoSuchInstance / NoSuchObject / EndOfMibView → signal absence
    if isinstance(val, (NoSuchInstance, NoSuchObject, EndOfMibView)):
        return None

    # Integer family: Integer32, Counter32, Counter64, Gauge32, Unsigned32, TimeTicks
    if isinstance(val, (rfc1902.Integer32, rfc1902.Counter32, rfc1902.Counter64,
                        rfc1902.Gauge32, rfc1902.Unsigned32, rfc1902.TimeTicks,
                        asn1_univ.Integer)):
        return int(val)

    # IP address: str() gives garbled bytes; prettyPrint() gives dotted-quad
    if isinstance(val, rfc1902.IpAddress):
        return val.prettyPrint()

    # Object identifier
    if isinstance(val, asn1_univ.ObjectIdentifier):
        return str(val)

    # OctetString: return raw bytes — caller decodes for text, uses directly for binary
    if isinstance(val, (rfc1902.OctetString, asn1_univ.OctetString)):
        return bytes(val)

    # Fallback
    return str(val)


# ---------------------------------------------------------------------------
# MAC address utilities (public helpers used by ARP, MAC, and LLDP collectors)
# ---------------------------------------------------------------------------

def mac_from_bytes(value: bytes) -> str:
    """Normalize 6 raw bytes into a lowercase colon-separated MAC string.

    Args:
        value: Exactly 6 bytes representing a MAC address.

    Returns:
        Lowercase colon-separated string, e.g. ``"aa:bb:cc:dd:ee:ff"``.

    Raises:
        ValueError: If ``value`` is not exactly 6 bytes.
    """
    if len(value) != 6:
        raise ValueError(f"MAC address must be 6 bytes, got {len(value)}")
    return ":".join(f"{b:02x}" for b in value)


def mac_from_dotted_decimal(value: str) -> str:
    """Parse a dotted-decimal MAC from an OID index into colon-separated form.

    OID indices encode MAC addresses as six decimal byte values separated by
    dots, e.g. ``"170.187.204.221.238.255"`` for ``aa:bb:cc:dd:ee:ff``.

    Args:
        value: Six dot-separated decimal integers, each 0–255.

    Returns:
        Lowercase colon-separated string, e.g. ``"aa:bb:cc:dd:ee:ff"``.

    Raises:
        ValueError: If ``value`` does not contain exactly 6 parts, or any
            part is not an integer in the range 0–255.
    """
    parts = value.split(".")
    if len(parts) != 6:
        raise ValueError(
            f"Dotted-decimal MAC must have 6 parts, got {len(parts)}: {value!r}"
        )
    octets: list[int] = []
    for part in parts:
        try:
            octet = int(part)
        except ValueError:
            raise ValueError(f"Non-integer byte in dotted-decimal MAC: {part!r}")
        if not 0 <= octet <= 255:
            raise ValueError(f"Byte out of range in dotted-decimal MAC: {octet}")
        octets.append(octet)
    return ":".join(f"{b:02x}" for b in octets)


def _make_error(error_indication: Any, error_status: Any) -> SnmpError:
    """Map a pysnmp error indication to one of our three exception types."""
    if isinstance(error_indication, errind.RequestTimedOut):
        return SnmpTimeoutError(str(error_indication))

    auth_types = (
        errind.AuthenticationFailure,
        errind.AuthenticationError,
    )
    if isinstance(error_indication, auth_types):
        return SnmpAuthError(str(error_indication))

    # error_status is the SNMP PDU-level error (integer); non-zero means
    # the agent returned an application-level error.
    msg = str(error_indication or error_status)
    return SnmpError(msg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def walk_table(
    device_ip: str,
    community: str,
    oid: str,
    *,
    version: str = "2c",
    timeout_sec: float = 10.0,
    max_repetitions: int = 25,
    port: int = 161,
) -> list[dict[str, Any]]:
    """Walk an SNMP table starting at the given OID.

    Returns a list of dicts, one per table row, with keys being OID leaves
    (relative to the base OID) and values being the SNMP-typed values
    converted to Python native types.

    The OID prefix is stripped from each result key: if base OID is
    ``1.3.6.1.2.1.4.22.1`` and a varBind has OID
    ``1.3.6.1.2.1.4.22.1.2.1.192.168.1.1``, the key in the result dict
    is ``2.1.192.168.1.1`` (everything after the base OID + ".").

    Rows are grouped by the sub-identifier that follows the column number —
    i.e., the table index. Each unique index produces one dict.

    Args:
        device_ip: Target device IP address.
        community: SNMP community string.
        oid: Base OID of the table (e.g. ``"1.3.6.1.2.1.4.22.1"``).
        version: SNMP version string; only ``"2c"`` is supported.
        timeout_sec: Per-PDU response timeout in seconds.
        max_repetitions: BulkCmd max-repetitions value.
        port: SNMP UDP port (default 161).

    Returns:
        List of dicts. Each dict maps OID suffix → native Python value.
        The suffix includes the column number and the row index, e.g.
        ``{"2.1.192.168.1.1": "aa:bb:cc:dd:ee:ff", ...}``.

    Raises:
        SnmpTimeoutError: Device did not respond within timeout_sec.
        SnmpAuthError: Community string rejected by device.
        SnmpError: Any other SNMP-level failure.
    """
    start = time.monotonic()
    log.debug("snmp_walk_started", "SNMP table walk starting",
              context={"device_ip": device_ip, "oid": oid, "version": version})

    engine = SnmpEngine()
    target = await UdpTransportTarget.create(
        (device_ip, port),
        timeout=timeout_sec,
        retries=1,
    )
    auth = CommunityData(community, mpModel=1)  # mpModel=1 → SNMPv2c
    ctx = ContextData()
    base_prefix = oid + "."

    raw: list[tuple[str, Any]] = []
    oid_cursor = ObjectType(ObjectIdentity(oid))

    for _ in range(_WALK_MAX_ROWS // max_repetitions + 1):
        try:
            errI, errS, errIdx, varBinds = await bulk_cmd(
                engine, auth, target, ctx,
                0, max_repetitions, oid_cursor,
            )
        except Exception as exc:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            # TODO: apply dedup wrapper here — a flapping device can emit
            # thousands of identical errors per polling cycle. See
            # docs/CODING_STANDARDS.md "Deduplication and Rate Limiting".
            log.error("snmp_walk_failed", "SNMP walk raised unexpected exception",
                      context={"device_ip": device_ip, "oid": oid,
                               "duration_ms": duration_ms, "error": str(exc)})
            raise SnmpError(str(exc)) from exc

        if errI:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            err = _make_error(errI, errS)
            # TODO: apply dedup wrapper here — see above.
            log.error("snmp_walk_failed", "SNMP walk error from device",
                      context={"device_ip": device_ip, "oid": oid,
                               "duration_ms": duration_ms,
                               "error": str(errI)})
            raise err

        if not varBinds:
            break

        out_of_scope = False
        last_ot = None

        for item in varBinds:
            # pysnmp 6.x returned [[ObjectType], ...]; 7.x returns a flat tuple. Handle both.
            ot = item[0] if isinstance(item, list) else item
            oid_str = str(ot[0])

            if not oid_str.startswith(base_prefix):
                out_of_scope = True
                break

            raw.append((oid_str[len(base_prefix):], _convert_value(ot[1])))
            last_ot = ot

            if len(raw) >= _WALK_MAX_ROWS:
                out_of_scope = True  # treat as done
                break

        if out_of_scope or last_ot is None:
            break

        # Advance cursor to the last received OID. Rebuild via string to avoid
        # requiring an ObjectIdentity instance — raw ObjectIdentifier from mock
        # or resolved varBinds both have a usable str() representation.
        oid_cursor = ObjectType(ObjectIdentity(str(last_ot[0])))

    duration_ms = round((time.monotonic() - start) * 1000, 1)

    # Each entry in the result is a single-key dict: {oid_suffix: native_value}.
    # The oid_suffix is everything after base_oid + ".", e.g. "2.1.10.0.0.1"
    # for a varBind at "1.3.6.1.2.1.4.22.1.2.1.10.0.0.1" with base
    # "1.3.6.1.2.1.4.22.1". Callers (ARP, MAC, LLDP collectors) split the
    # suffix on "." to extract column number and row index.
    result: list[dict[str, Any]] = [{suffix: value} for suffix, value in raw]

    log.debug("snmp_walk_completed", "SNMP table walk completed",
              context={"device_ip": device_ip, "oid": oid,
                       "row_count": len(result), "duration_ms": duration_ms})

    return result


async def get_scalar(
    device_ip: str,
    community: str,
    oid: str,
    *,
    version: str = "2c",
    timeout_sec: float = 10.0,
    port: int = 161,
) -> Any:
    """Fetch a single SNMP scalar value.

    Returns the Python-native value, or None if the OID is not present
    on the device (NoSuchInstance / NoSuchObject).

    Args:
        device_ip: Target device IP address.
        community: SNMP community string.
        oid: Scalar OID, e.g. ``"1.3.6.1.2.1.1.1.0"`` (sysDescr.0).
        version: SNMP version string; only ``"2c"`` is supported.
        timeout_sec: Per-PDU response timeout in seconds.
        port: SNMP UDP port (default 161).

    Returns:
        Python-native value (int, str, etc.), or None if OID absent.

    Raises:
        SnmpTimeoutError: Device did not respond within timeout_sec.
        SnmpAuthError: Community string rejected by device.
        SnmpError: Any other SNMP-level failure.
    """
    start = time.monotonic()
    log.debug("snmp_get_started", "SNMP scalar get starting",
              context={"device_ip": device_ip, "oid": oid, "version": version})

    engine = SnmpEngine()
    target = await UdpTransportTarget.create(
        (device_ip, port),
        timeout=timeout_sec,
        retries=1,
    )
    auth = CommunityData(community, mpModel=1)
    ctx = ContextData()

    try:
        errI, errS, errIdx, varBinds = await get_cmd(
            engine, auth, target, ctx,
            ObjectType(ObjectIdentity(oid)),
        )
    except Exception as exc:
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        # TODO: apply dedup wrapper here — see docs/CODING_STANDARDS.md
        # "Deduplication and Rate Limiting".
        log.error("snmp_get_failed", "SNMP get raised unexpected exception",
                  context={"device_ip": device_ip, "oid": oid,
                           "duration_ms": duration_ms, "error": str(exc)})
        raise SnmpError(str(exc)) from exc

    duration_ms = round((time.monotonic() - start) * 1000, 1)

    if errI:
        err = _make_error(errI, errS)
        # TODO: apply dedup wrapper here — see docs/CODING_STANDARDS.md
        # "Deduplication and Rate Limiting".
        log.error("snmp_get_failed", "SNMP get error from device",
                  context={"device_ip": device_ip, "oid": oid,
                           "duration_ms": duration_ms, "error": str(errI)})
        raise err

    if not varBinds:
        log.debug("snmp_get_completed", "SNMP get returned no varBinds",
                  context={"device_ip": device_ip, "oid": oid,
                           "duration_ms": duration_ms})
        return None

    # get_cmd varBinds is a tuple of ObjectType objects
    ot = varBinds[0]
    value = _convert_value(ot[1])

    log.debug("snmp_get_completed", "SNMP scalar get completed",
              context={"device_ip": device_ip, "oid": oid,
                       "duration_ms": duration_ms})

    return value


async def collect_ifindex_to_name(
    device_ip: str,
    community: str,
    *,
    version: str = "2c",
    timeout_sec: float = 10.0,
    port: int = 161,
) -> dict[int, str]:
    """Build an {ifIndex: interface_name} mapping for a device.

    Walks ifXTable.ifName (1.3.6.1.2.1.31.1.1.1.1) — the short-form name
    matching operator vocabulary ("ge-0/0/12"). Falls back to
    ifTable.ifDescr (1.3.6.1.2.1.2.2.1.2) when ifName is empty, which
    happens on older agents that don't implement ifXTable.

    Used by LLDP collector for local port resolution and reused by any
    other collector that receives bare ifIndex values.

    Returns:
        {ifIndex: name} dict. Empty on any SNMP error — callers treat
        a missing ifIndex as "unresolved" rather than a hard failure.
    """
    ifindex_to_name: dict[int, str] = {}

    try:
        ifname_rows = await walk_table(
            device_ip, community, OIDS["IF-MIB::ifName"],
            version=version, timeout_sec=timeout_sec, port=port,
        )
    except SnmpError:
        ifname_rows = []

    for row in ifname_rows:
        for suffix, val in row.items():
            try:
                ifindex = int(suffix)
            except ValueError:
                continue
            if isinstance(val, bytes) and val:
                ifindex_to_name[ifindex] = val.decode("utf-8", errors="replace").rstrip("\x00")

    # Any ifIndex without an ifName entry — try ifDescr fallback
    missing = not ifindex_to_name
    if missing:
        try:
            ifdescr_rows = await walk_table(
                device_ip, community, OIDS["IF-MIB::ifDescr"],
                version=version, timeout_sec=timeout_sec, port=port,
            )
        except SnmpError:
            return ifindex_to_name

        for row in ifdescr_rows:
            for suffix, val in row.items():
                try:
                    ifindex = int(suffix)
                except ValueError:
                    continue
                if isinstance(val, bytes) and val:
                    ifindex_to_name[ifindex] = val.decode("utf-8", errors="replace").rstrip("\x00")

    return ifindex_to_name


async def collect_bridgeport_to_ifindex(
    device_ip: str,
    community: str,
    *,
    version: str = "2c",
    timeout_sec: float = 10.0,
    port: int = 161,
) -> dict[int, int]:
    """Build a {bridge_port: ifIndex} map for a device.

    Walks BRIDGE-MIB::dot1dBasePortIfIndex (1.3.6.1.2.1.17.1.4.1.2) — one row
    per bridge port; suffix is the bridge port number (matches
    ``MacEntry.bridge_port`` from ``mac_snmp.collect_mac``), value is the
    ifIndex of the interface that bridge port maps to.

    Used by the MAC collector to bridge from FDB rows to interface names
    (bridge_port → ifIndex → ifName via ``collect_ifindex_to_name``).

    Returns:
        ``{bridge_port: ifIndex}`` dict. Empty on any SNMP error or on
        devices that don't act as bridges (e.g. firewalls without an L2
        domain) — callers treat a missing bridge port as "unresolved"
        rather than a hard failure.
    """
    bridge_to_ifindex: dict[int, int] = {}

    try:
        rows = await walk_table(
            device_ip, community, OIDS["BRIDGE-MIB::dot1dBasePortIfIndex"],
            version=version, timeout_sec=timeout_sec, port=port,
        )
    except SnmpError:
        return bridge_to_ifindex

    for row in rows:
        for suffix, val in row.items():
            try:
                bridge_port = int(suffix)
                ifindex = int(val)
            except (ValueError, TypeError):
                continue
            if bridge_port > 0 and ifindex > 0:
                bridge_to_ifindex[bridge_port] = ifindex

    return bridge_to_ifindex
