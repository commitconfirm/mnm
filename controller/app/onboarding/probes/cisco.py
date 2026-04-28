"""Cisco IOS / IOS-XE device-facts probe for Phase 1 onboarding (Block C.5).

Shape mirrors :mod:`app.onboarding.probes.paloalto` /
:mod:`app.onboarding.probes.fortinet` exactly — same ``DeviceFacts``
shape, same ``probe_device_facts`` signature. Vendor-specific
differences:

  * **sysDescr embeds the IOS / IOS-XE version directly.** c8000v
    reality-check §3 capture:
    ``b"Cisco IOS Software [IOSXE], Virtual XE Software ..."``
    ``b"... Version 17.16.1a, RELEASE SOFTWARE (fc1) ..."``
    Regex extracts the version token after ``Version``.
  * **sysName carries Cisco prompt artifacts.** Cisco devices commonly
    return their CLI prompt (e.g. ``"Router#"``) as sysName when
    `snmp-server contact` / `hostname` haven't been explicitly set.
    The probe strips a trailing ``#`` so the Nautobot Device name
    is clean. After cleaning, also delegate to the FQDN-stripping
    convention shared with Junos/Arista/PAN-OS/FortiGate (split on
    ``.`` and take the first label).
  * **ENTITY-MIB is authoritative for chassis_model + serial on
    modern IOS-XE.** c8000v 17.16.1a populates entPhysicalClass=chassis(3)
    cleanly with model="C8000V" / serial="9AM0AXLEZNB" at the first
    chassis row.
  * **OLD-CISCO-CHASSIS-MIB::chassisType scalar (1.3.6.1.4.1.9.3.6.4.0)**
    is the legacy fallback for older classic IOS where ENTITY-MIB
    isn't populated. **WARNING — IOS-XE returns the ROMMON
    identifier on this OID** ("IOS-XE ROMMON"), which is not a
    chassis model. We use the legacy scalar ONLY when ENTITY-MIB
    returned nothing for chassis_model — never as a tiebreaker
    or override. This means modern IOS-XE always uses ENTITY-MIB;
    the scalar only matters on older classic IOS without ENTITY-MIB.
  * **No enterprise scalar for serial.** OLD-CISCO-CHASSIS-MIB
    targets chassis description, not serial. Serial always comes
    from ENTITY-MIB entPhysicalSerialNum (chassis row).

Hostname cleaning beyond the ``#`` strip and OctetString handling
are identical to Junos / Arista / PAN-OS / FortiGate and imported
from probes/junos.py as shared helpers.

Reality-check citations: §3 (c8000v sysDescr capture, sysObjectID
1.3.6.1.4.1.9.1.2495), §5.1 (3-interface count: GigabitEthernet1,
GigabitEthernet2, Null0), §6 Block C.5 (cisco_iosxe management
interface = ``GigabitEthernet1``; cisco_ios = ``GigabitEthernet0/0``).

Classic IOS path is text-validated only — no real classic-IOS
device in lab. c8000v exercises the IOS-XE classifier discrimination
(sysDescr ``IOSXE`` marker → platform=cisco_iosxe). When a real
classic-IOS device enters the lab, validation is incremental:
sysDescr lacks the IOSXE markers → platform=cisco_ios →
MGMT_INTERFACE_NAME["cisco_ios"]="GigabitEthernet0/0", everything
else identical.

Per-VLAN Catalyst MAC walks via SNMP CSI (community-string
indexing for dot1qTpFdbTable) are NOT implemented. c8000v has no
L2 zones so MAC collection returns empty (legitimate, same as
PA-440). When a real Catalyst enters the lab, MAC table data
will appear empty/partial until CSI handling lands. CLAUDE.md
"Cisco CSI handling" Pending item.
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


# Cisco IOS / IOS-XE sysDescr embeds version after the "Version" token:
#   "... Version 17.16.1a, RELEASE SOFTWARE (fc1) ..."
#   "... Version 15.2(7)E3, RELEASE SOFTWARE (fc3) ..."   (classic IOS)
# Trailing comma / whitespace ends the version token; the regex stops
# at any of comma / semicolon / whitespace.
_CISCO_VERSION_RE = re.compile(rb"Version\s+([^\s,;]+)")


def _parse_cisco_version(sys_descr) -> "str | None":
    """Best-effort regex parse of IOS / IOS-XE version from sysDescr.

    Returns ``None`` when sysDescr lacks a Version token (rare; both
    classic IOS and IOS-XE consistently include it). Caller tolerates
    ``os_version=None``.
    """
    if sys_descr is None:
        return None
    if isinstance(sys_descr, str):
        sys_descr = sys_descr.encode("utf-8", errors="replace")
    if not isinstance(sys_descr, (bytes, bytearray)):
        return None
    match = _CISCO_VERSION_RE.search(sys_descr)
    if not match:
        return None
    return match.group(1).decode("utf-8", errors="replace") or None


def _clean_cisco_hostname(sys_name) -> "str | None":
    """Cisco hostname cleaner — strip trailing CLI prompt ``#`` then
    delegate to the shared FQDN-stripping :func:`_hostname_from_sysname`.

    Cisco devices without explicit ``snmp-server contact`` config
    sometimes return the CLI prompt (e.g. ``"Router#"``) as sysName.
    Strip the trailing ``#`` before further cleaning so the Nautobot
    Device name doesn't carry the prompt artifact. Privileged-mode
    prompt suffix on EXEC mode is also ``#`` (config mode shows
    ``(config)#`` but sysName never includes parens).
    """
    text = _decode(sys_name)
    if not text:
        return None
    # Strip trailing prompt characters. Cisco uses ``#`` (privileged) and
    # ``>`` (user EXEC); both are valid sysName artifacts from
    # underconfigured devices.
    text = text.rstrip("#>").strip()
    if not text:
        return None
    # Reuse the shared FQDN-strip via the helper. It accepts a string
    # (it decodes None/bytes; a stripped str passes through cleanly).
    return _hostname_from_sysname(text)


async def probe_device_facts(ip: str, snmp_community: str) -> DeviceFacts:
    """Collect Phase 1 facts from a Cisco IOS / IOS-XE device.

    Raises :class:`RuntimeError` if sysName cannot be read — the
    orchestrator needs a hostname and has no sensible fallback. All
    other facts degrade to None on SNMP miss.
    """
    sys_name_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysName")
    hostname = _clean_cisco_hostname(sys_name_raw)
    if not hostname:
        raise RuntimeError(
            f"cisco probe: sysName at {ip} returned no hostname; "
            "cannot create Nautobot Device without a name"
        )

    sys_descr_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysDescr")
    os_version = _parse_cisco_version(sys_descr_raw)

    # ENTITY-MIB is primary for both chassis_model and serial. Modern
    # IOS-XE (c8000v) populates entPhysicalClass=chassis(3) cleanly with
    # model + serial at the first chassis row. Serial has no scalar
    # alternative, so this walk is unconditional.
    ent_serial, ent_model = await _walk_entity_chassis(ip, snmp_community)
    serial = (_decode(ent_serial)
              if not isinstance(ent_serial, str) else ent_serial)
    chassis_model = (_decode(ent_model)
                     if not isinstance(ent_model, str) else ent_model)

    # Legacy fallback: OLD-CISCO-CHASSIS-MIB::chassisType only matters on
    # older classic IOS where ENTITY-MIB returned no chassis_model. On
    # IOS-XE this scalar returns "IOS-XE ROMMON" (the ROMMON identifier)
    # which is not a chassis model — that's why we DON'T let it override
    # an ENTITY-MIB value. Only consult it when ENTITY-MIB came back empty.
    if not chassis_model:
        legacy = _decode(
            await _get_scalar_safe(ip, snmp_community,
                                   "OLD-CISCO-CHASSIS-MIB::chassisType")
        )
        if legacy:
            chassis_model = legacy

    return DeviceFacts(
        hostname=hostname,
        serial=serial,
        chassis_model=chassis_model,
        os_version=os_version,
        management_prefix_length=None,
    )
