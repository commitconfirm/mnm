"""FortiGate (FortiOS) device-facts probe for Phase 1 onboarding (Prompt 7.5).

Shape mirrors :mod:`app.onboarding.probes.paloalto` /
:mod:`app.onboarding.probes.arista` exactly — same ``DeviceFacts`` shape
and same ``probe_device_facts`` signature. Vendor-specific differences:

  * **Operator-customized sysDescr is common on FortiGate.** The
    reality-check §3 lab capture on FortiGate 40F was
    ``b"Fortigate 40F - MNM Test"`` — no version, no standard prefix.
    FortiGate admins frequently rewrite sysDescr. Classifier falls back
    to sysObjectID prefix (``1.3.6.1.4.1.12356``) per reality-check §3;
    the probe's ``os_version`` parse is best-effort and degrades to None.
  * **FortiGate has a vendor scalar for serial**:
    ``fnSysSerial`` (FORTINET-CORE-MIB, 1.3.6.1.4.1.12356.1.2.0). Used
    as primary; ENTITY-MIB entPhysicalTable walk is the fallback.
  * **Chassis model** comes from ENTITY-MIB entPhysicalModelName —
    FORTINET-CORE-MIB doesn't expose a scalar for the product name, so
    the entity-walk is the only chassis_model source (same pattern as
    Arista and PAN-OS).
  * **Phase 2 blank-ifDescr handling is already generic.** Reality-check
    §5.1 documented FortiGate's blank ``ifDescr`` with populated
    ``ifName``; Phase 2's ifName-first fallback (shipped in Prompt 6)
    covers this. FortiGate is the first live exercise of that path.

Hostname cleaning and OctetString handling are identical to
Junos/Arista/PAN-OS and are imported as shared helpers.

Reality-check citations: §3 (sysDescr capture + operator-customized
caveat), §5.1 (12-interface ifName/blank-ifDescr shape), §6 Prompt 7.5
(management interface = ``mgmt``).

VDOM handling:
v1.0 treats FortiGate as a single logical device per the SNMP agent's
view. Multi-VDOM FortiGates will surface all interfaces across all
VDOMs as peers of one Nautobot Device without VDOM attribution. This
parallels the v1.0 treatment of Juniper VC/VCF and Cisco stacks.

VDOM-awareness is v1.1+ scope:
  - Collect FORTINET-FORTIGATE-MIB::fgVirtualDomain
    (1.3.6.1.4.1.12356.101.3.2) — per-VDOM name + index
  - Map interfaces to VDOMs via snmp-index correlation
  - Surface in mnm-plugin as per-interface VDOM badge +
    per-device VDOM topology tab
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


# Stock FortiOS sysDescr format when not operator-customized:
#   "FortiGate-40F v7.4.0,build12345,221221 (GA)"
# Regex tries v<ver> pattern; if sysDescr is customized (common), no
# match → os_version=None.
_FORTIOS_VERSION_RE = re.compile(rb"v(\d+\.\d+(?:\.\d+)?[^\s,;]*)")


def _parse_fortios_version(sys_descr) -> "str | None":
    """Best-effort regex parse of FortiOS version from sysDescr.

    Returns ``None`` when sysDescr is operator-customized (common on
    FortiGate per reality-check §3) — not fatal; the orchestrator
    tolerates ``os_version=None``.
    """
    if sys_descr is None:
        return None
    if isinstance(sys_descr, str):
        sys_descr = sys_descr.encode("utf-8", errors="replace")
    if not isinstance(sys_descr, (bytes, bytearray)):
        return None
    match = _FORTIOS_VERSION_RE.search(sys_descr)
    if not match:
        return None
    return match.group(1).decode("utf-8", errors="replace") or None


async def probe_device_facts(ip: str, snmp_community: str) -> DeviceFacts:
    """Collect Phase 1 facts from a FortiGate (FortiOS) device.

    Raises :class:`RuntimeError` if sysName cannot be read — the
    orchestrator needs a hostname and has no sensible fallback. All other
    facts degrade to None on SNMP miss.
    """
    sys_name_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysName")
    hostname = _hostname_from_sysname(sys_name_raw)
    if not hostname:
        raise RuntimeError(
            f"fortinet probe: sysName at {ip} returned no hostname; "
            "cannot create Nautobot Device without a name"
        )

    sys_descr_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysDescr")
    os_version = _parse_fortios_version(sys_descr_raw)

    # serial: fnSysSerial primary, ENTITY-MIB chassis row as fallback.
    serial = _decode(
        await _get_scalar_safe(ip, snmp_community, "FORTINET-CORE-MIB::fnSysSerial")
    )
    chassis_model = None
    if serial is None or not serial:
        ent_serial, ent_model = await _walk_entity_chassis(ip, snmp_community)
        if serial is None or not serial:
            serial = _decode(ent_serial) if not isinstance(ent_serial, str) else ent_serial
        chassis_model = _decode(ent_model) if not isinstance(ent_model, str) else ent_model
    else:
        # Vendor-scalar serial hit; still pull chassis_model via entity
        # walk — FORTINET-CORE-MIB has no enterprise product-name scalar.
        _, ent_model = await _walk_entity_chassis(ip, snmp_community)
        chassis_model = _decode(ent_model) if not isinstance(ent_model, str) else ent_model

    return DeviceFacts(
        hostname=hostname,
        serial=serial,
        chassis_model=chassis_model,
        os_version=os_version,
        management_prefix_length=None,
    )
