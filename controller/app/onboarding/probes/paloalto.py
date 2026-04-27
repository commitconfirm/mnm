"""PAN-OS device-facts probe for Phase 1 onboarding (Prompt 7).

Shape mirrors :mod:`app.onboarding.probes.junos` /
:mod:`app.onboarding.probes.arista` exactly — same ``DeviceFacts`` shape
and same ``probe_device_facts`` signature. Vendor-specific differences:

  * **PAN-OS sysDescr does NOT embed the PAN-OS version string.** The
    reality-check §3 capture on PA-440 was the bare
    ``b"Palo Alto Networks PA-400 series firewall"``. Unlike Junos /
    Arista where sysDescr carries the OS version, PAN-OS requires
    reading ``panSysSwVersion`` (PAN-COMMON-MIB, 1.3.6.1.4.1.25461.2.1.2.1.1.0)
    directly. Probe falls back to regex-parse of sysDescr as defense-
    in-depth; if both miss, ``os_version = None`` (not fatal).
  * **PAN-OS has a vendor scalar for serial**:
    ``panSysSerialNumber`` (1.3.6.1.4.1.25461.2.1.2.1.3.0). Used as
    primary; ENTITY-MIB walk is the fallback (same pattern as Arista).
  * **Chassis model** comes from ENTITY-MIB entPhysicalModelName —
    PAN-OS doesn't expose a scalar for the chassis product name, so
    the entity-walk is the only path.

Hostname cleaning and OctetString handling are identical to Junos /
Arista and are imported from probes/junos.py as shared helpers.

Reality-check citations: §3 (PA-440 sysDescr + sysObjectID), §5.1
(PA-440 ifTable count 21, ipAdEntAddr 7, ipAddressIfIndex 9), §6
(PAN-OS management interface is ``mgmt``).
"""
from __future__ import annotations

import logging
import re

from app.onboarding.probes.junos import (
    DeviceFacts,
    _decode,
    _get_scalar_safe,
    _hostname_from_sysname,
    _walk_entity_chassis,
)

log = logging.getLogger(__name__)


# Defense-in-depth regex if panSysSwVersion scalar is unreadable. PAN-OS
# sysDescr typically DOESN'T include the version (reality-check §3 on
# PA-440 captured only `"Palo Alto Networks PA-400 series firewall"`), so
# this is a fallback for vendor-customized sysDescr strings that might
# include it.
_PANOS_VERSION_RE = re.compile(
    rb"(?:PAN-?OS|version)\s+(\d+\.\d+(?:\.\d+)?[^\s,;]*)",
    re.IGNORECASE,
)


def _parse_panos_version(sys_descr) -> "str | None":
    """Best-effort regex parse of PAN-OS version from sysDescr.

    Returns ``None`` for the common case where sysDescr doesn't carry the
    version (PA-440 baseline). Caller should prefer ``panSysSwVersion``
    scalar when available.
    """
    if sys_descr is None:
        return None
    if isinstance(sys_descr, str):
        sys_descr = sys_descr.encode("utf-8", errors="replace")
    if not isinstance(sys_descr, (bytes, bytearray)):
        return None
    match = _PANOS_VERSION_RE.search(sys_descr)
    if not match:
        return None
    return match.group(1).decode("utf-8", errors="replace") or None


async def probe_device_facts(ip: str, snmp_community: str) -> DeviceFacts:
    """Collect Phase 1 facts from a Palo Alto Networks PAN-OS device.

    Raises :class:`RuntimeError` if sysName cannot be read — the
    orchestrator needs a hostname and has no sensible fallback. All other
    facts degrade to None on SNMP miss.
    """
    sys_name_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysName")
    hostname = _hostname_from_sysname(sys_name_raw)
    if not hostname:
        raise RuntimeError(
            f"palo alto probe: sysName at {ip} returned no hostname; "
            "cannot create Nautobot Device without a name"
        )

    # os_version: prefer the PAN-OS-specific scalar; fall back to
    # sysDescr regex if the scalar is unreadable (some older PAN-OS
    # builds don't implement it).
    os_version = _decode(
        await _get_scalar_safe(ip, snmp_community, "PAN-COMMON-MIB::panSysSwVersion")
    )
    if os_version is None:
        sys_descr_raw = await _get_scalar_safe(
            ip, snmp_community, "SNMPv2-MIB::sysDescr",
        )
        os_version = _parse_panos_version(sys_descr_raw)

    # serial: panSysSerialNumber primary, ENTITY-MIB chassis row as fallback.
    serial = _decode(
        await _get_scalar_safe(ip, snmp_community, "PAN-COMMON-MIB::panSysSerialNumber")
    )
    chassis_model = None
    if serial is None or not serial:
        ent_serial, ent_model = await _walk_entity_chassis(ip, snmp_community)
        if serial is None or not serial:
            serial = _decode(ent_serial) if not isinstance(ent_serial, str) else ent_serial
        chassis_model = _decode(ent_model) if not isinstance(ent_model, str) else ent_model
    else:
        # Even with vendor-scalar serial, still pull chassis_model from
        # the entity walk — PAN-OS has no enterprise scalar for the
        # product name.
        _, ent_model = await _walk_entity_chassis(ip, snmp_community)
        chassis_model = _decode(ent_model) if not isinstance(ent_model, str) else ent_model

    return DeviceFacts(
        hostname=hostname,
        serial=serial,
        chassis_model=chassis_model,
        os_version=os_version,
        management_prefix_length=None,
    )
