"""Arista EOS device-facts probe for Phase 1 onboarding (Prompt 5).

Shape mirrors :mod:`app.onboarding.probes.junos` exactly — same
``DeviceFacts`` shape and same ``probe_device_facts`` signature, so the
orchestrator's ``_probe_vendor`` dispatch is a trivial lookup. The
vendor-specific differences:

  - No widely-documented Arista enterprise scalar equivalent to
    Juniper's ``jnxBoxSerialNo`` / ``jnxBoxDescr`` on EOS. We skip
    directly to ``ENTITY-MIB::entPhysicalTable`` and pick the first
    chassis(3) row for serial and model name.
  - ``sysDescr`` carries the EOS version in the form
    ``"Arista Networks EOS version X.Y.ZL ..."``. Reality-check §3
    captured this verbatim on 172.21.140.16 (2026-04-20):
    ``b"Arista Networks EOS version 4.33.7M running on an Arista
      vEOS-lab"``.

Hostname cleaning (FQDN → short form) and OctetString-as-bytes handling
are identical to the Junos probe.
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


# EOS version pattern inside sysDescr, e.g.:
#   "Arista Networks EOS version 4.33.7M running on an Arista vEOS-lab"
#   "Arista Networks EOS version 4.27.0F-...  running on ..."
_EOS_VERSION_RE = re.compile(rb"EOS version\s+(\S+)")


def _parse_eos_version(sys_descr) -> "str | None":
    """Extract the EOS version (``4.33.7M`` et al.) from sysDescr."""
    if sys_descr is None:
        return None
    if isinstance(sys_descr, str):
        sys_descr = sys_descr.encode("utf-8", errors="replace")
    if not isinstance(sys_descr, (bytes, bytearray)):
        return None
    match = _EOS_VERSION_RE.search(sys_descr)
    if not match:
        return None
    return match.group(1).decode("utf-8", errors="replace") or None


async def probe_device_facts(ip: str, snmp_community: str) -> DeviceFacts:
    """Collect Phase 1 facts from an Arista EOS device.

    Raises :class:`RuntimeError` if sysName cannot be read — the
    orchestrator needs a hostname and has no sensible fallback. All other
    facts degrade to None on SNMP miss.
    """
    sys_name_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysName")
    hostname = _hostname_from_sysname(sys_name_raw)
    if not hostname:
        raise RuntimeError(
            f"arista probe: sysName at {ip} returned no hostname; "
            "cannot create Nautobot Device without a name"
        )

    sys_descr_raw = await _get_scalar_safe(ip, snmp_community, "SNMPv2-MIB::sysDescr")
    os_version = _parse_eos_version(sys_descr_raw)

    # ENTITY-MIB walk: Arista exposes chassis serial + model there. No
    # enterprise scalar shortcut like Junos's jnxBoxSerialNo on EOS.
    serial, chassis_model = await _walk_entity_chassis(ip, snmp_community)

    # Decoded already inside _walk_entity_chassis; keep the signature
    # explicit about bytes/string semantics.
    return DeviceFacts(
        hostname=hostname,
        serial=_decode(serial) if not isinstance(serial, str) else serial,
        chassis_model=_decode(chassis_model) if not isinstance(chassis_model, str) else chassis_model,
        os_version=os_version,
        management_prefix_length=None,
    )
