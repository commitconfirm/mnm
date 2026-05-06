"""Junos device-facts probe for Phase 1 onboarding (Prompt 4).

Collects the four scalar facts the orchestrator needs to build a Nautobot
Device record for a Juniper EX / SRX / MX chassis via SNMP:

  - hostname (from SNMPv2-MIB::sysName, FQDN-stripped)
  - serial (JUNIPER-MIB::jnxBoxSerialNo, with ENTITY-MIB fallback)
  - chassis_model (JUNIPER-MIB::jnxBoxDescr, with ENTITY-MIB fallback)
  - os_version (parsed out of SNMPv2-MIB::sysDescr)

All are best-effort — a ``DeviceFacts`` with ``serial=None`` /
``chassis_model=None`` is still usable; the orchestrator tolerates None
and continues. Only hostname failure is fatal, because the orchestrator
needs a name to key the Nautobot Device record on.

Reality-check §3 supplies the canonical sysDescr shapes (Juniper test
fixtures). Tier-2 vendors in the JUNIPER-MIB enterprise tree deliver
chassis-level scalars directly; the ENTITY-MIB walk is the defensive
backup when we encounter an older Junos build or a vendor-forked image.

OctetString contract: SNMP scalars come back as bytes from
:func:`snmp_collector.get_scalar`. Decode UTF-8 with errors="replace"
before any string operations.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.snmp_collector import (
    SnmpError,
    get_scalar,
    oid,
    walk_table,
)
from app.onboarding.probes._junos_vocab import normalize_chassis_model

log = logging.getLogger(__name__)


# JUNOS version pattern inside sysDescr, e.g.:
#   "... kernel JUNOS 23.4R2-S4.11 ..."
#   "... kernel JUNOS 12.3R12.4 ..."
_JUNOS_VERSION_RE = re.compile(rb"kernel JUNOS\s+([^\s,;]+)")

# entPhysicalClass enum — "chassis(3)" is the top-level chassis row.
_ENT_PHYSICAL_CLASS_CHASSIS = 3


@dataclass
class DeviceFacts:
    """Facts gathered from a Junos device during Phase 1 probing.

    ``hostname`` is always populated on success; all other fields are
    best-effort and may be ``None`` when the corresponding MIB is missing
    or unreadable. ``management_prefix_length`` is currently always
    ``None`` — the orchestrator defaults to a /32 host route if we can't
    derive the actual prefix length from ifTable (a v1.0 follow-up;
    Phase 2 in Prompt 6 walks ifTable authoritatively).
    """

    hostname: str
    serial: "str | None" = None
    chassis_model: "str | None" = None
    os_version: "str | None" = None
    management_prefix_length: "int | None" = None


def _decode(value) -> "str | None":
    """Best-effort text decode for SNMP OctetString responses.

    Accepts bytes / bytearray / str / None. Returns the stripped string,
    or ``None`` when the input is empty after stripping.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = text.strip().strip("\x00")
    return text or None


def _hostname_from_sysname(sys_name) -> "str | None":
    """Clean sysName → Nautobot short-form hostname.

    Junos often returns an FQDN like ``b"ex2300.lab.example.com"``. Nautobot
    convention across the existing lab inventory is the short form
    (``ex2300-24p``, ``SRX320``, etc.), so we split on '.' and take the
    first label. Preserve the case sysName returned — operators have
    historically been loose with casing, and rewriting it here could
    surprise someone who registered a device manually with a specific
    capitalisation.
    """
    text = _decode(sys_name)
    if not text:
        return None
    return text.split(".", 1)[0] or None


def _parse_junos_version(sys_descr) -> "str | None":
    """Extract the Junos version (``23.4R2-S4.11`` et al.) from sysDescr."""
    if sys_descr is None:
        return None
    if isinstance(sys_descr, str):
        sys_descr = sys_descr.encode("utf-8", errors="replace")
    if not isinstance(sys_descr, (bytes, bytearray)):
        return None
    match = _JUNOS_VERSION_RE.search(sys_descr)
    if not match:
        return None
    return match.group(1).decode("utf-8", errors="replace") or None


async def _get_scalar_safe(ip: str, community: str, oid_name: str):
    """get_scalar wrapper that swallows SnmpError and returns None.

    Probe facts are best-effort; an unreachable OID yields None rather
    than an exception the caller must handle per-call.
    """
    try:
        return await get_scalar(ip, community, oid(oid_name))
    except SnmpError as exc:
        log.debug("junos_probe_oid_miss oid=%s err=%s", oid_name, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — defensive probe
        log.debug("junos_probe_oid_err oid=%s err=%s", oid_name, exc)
        return None


async def _walk_entity_chassis(ip: str, community: str) -> tuple["str | None", "str | None"]:
    """ENTITY-MIB fallback: find the first chassis(3) row and read its
    serial + model name.

    Returns (serial, model) — either may be None.
    """
    try:
        class_rows = await walk_table(ip, community, oid("ENTITY-MIB::entPhysicalClass"))
    except Exception as exc:  # noqa: BLE001
        log.debug("junos_probe_entity_walk_failed err=%s", exc)
        return (None, None)

    chassis_index: "str | None" = None
    for row in class_rows:
        for suffix, value in row.items():
            try:
                cls_int = int(str(value))
            except (TypeError, ValueError):
                continue
            if cls_int == _ENT_PHYSICAL_CLASS_CHASSIS:
                chassis_index = suffix
                break
        if chassis_index:
            break

    if chassis_index is None:
        return (None, None)

    serial = None
    model = None
    try:
        serial_rows = await walk_table(ip, community, oid("ENTITY-MIB::entPhysicalSerialNum"))
        for row in serial_rows:
            if chassis_index in row:
                serial = _decode(row[chassis_index])
                break
    except Exception as exc:  # noqa: BLE001
        log.debug("junos_probe_entity_serial_failed err=%s", exc)
    try:
        model_rows = await walk_table(ip, community, oid("ENTITY-MIB::entPhysicalModelName"))
        for row in model_rows:
            if chassis_index in row:
                model = _decode(row[chassis_index])
                break
    except Exception as exc:  # noqa: BLE001
        log.debug("junos_probe_entity_model_failed err=%s", exc)

    return (serial, model)


async def probe_device_facts(ip: str, snmp_community: str) -> DeviceFacts:
    """Collect Phase 1 facts from a Junos device.

    Raises :class:`RuntimeError` if sysName cannot be read — the
    orchestrator needs a hostname and has no sensible fallback. All other
    facts degrade to None on SNMP miss.
    """
    sys_name_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysName")
    hostname = _hostname_from_sysname(sys_name_raw)
    if not hostname:
        raise RuntimeError(
            f"junos probe: sysName at {ip} returned no hostname; "
            "cannot create Nautobot Device without a name"
        )

    sys_descr_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysDescr")
    os_version = _parse_junos_version(sys_descr_raw)

    # Primary path: JUNIPER-MIB scalars.
    serial = _decode(
        await _get_scalar_safe(ip, snmp_community, "JUNIPER-MIB::jnxBoxSerialNo")
    )
    chassis_model = _decode(
        await _get_scalar_safe(ip, snmp_community, "JUNIPER-MIB::jnxBoxDescr")
    )

    # Fallback: ENTITY-MIB entPhysicalTable if either primary path missed.
    if serial is None or chassis_model is None:
        ent_serial, ent_model = await _walk_entity_chassis(ip, snmp_community)
        if serial is None:
            serial = ent_serial
        if chassis_model is None:
            chassis_model = ent_model

    # F1: normalize chassis_model to the netbox-community DeviceType
    # library short form. jnxBoxDescr returns descriptive strings like
    # "Juniper EX2300-24P Switch" while the library indexes by
    # canonical "EX2300-24P". When the vocabulary doesn't match, the
    # original string passes through unchanged and the orchestrator's
    # MissingReferenceError surfaces the gap with operator-actionable
    # text per Rule 5 + D3 discipline.
    normalized = normalize_chassis_model(chassis_model)
    if chassis_model and normalized != chassis_model:
        log.debug(
            "junos_probe_chassis_normalized raw=%r canonical=%r",
            chassis_model, normalized,
        )
    elif chassis_model:
        log.debug(
            "junos_probe_chassis_passthrough value=%r "
            "(no vocabulary match — extend _junos_vocab.py if onboarding fails)",
            chassis_model,
        )
    chassis_model = normalized

    return DeviceFacts(
        hostname=hostname,
        serial=serial,
        chassis_model=chassis_model,
        os_version=os_version,
        management_prefix_length=None,
    )
