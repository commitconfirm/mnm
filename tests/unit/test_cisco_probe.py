"""Unit tests for controller/app/onboarding/probes/cisco.py (Block C.5).

Mocks at snmp_collector.get_scalar / walk_table boundaries. Fixture
sysDescr is the c8000v reality-check §3 lab capture verbatim — that's
the IOS-XE classifier discrimination target (sysDescr contains
``IOSXE`` marker).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload so sibling-test sys.modules.setdefault stubs are no-ops.
import app.onboarding.probes.cisco as _probe_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401

from app.onboarding.probes.cisco import (  # noqa: E402
    DeviceFacts,
    probe_device_facts,
)


# c8000v reality-check §3 sysDescr capture (IOS-XE 17.16.1a).
SYSDESCR_C8000V = (
    b"Cisco IOS Software [IOSXE], Virtual XE Software "
    b"(X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 17.16.1a, "
    b"RELEASE SOFTWARE (fc1)..."
)
# Plausible classic IOS sysDescr (text-validated only, no lab device).
SYSDESCR_CLASSIC_IOS = (
    b"Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M), "
    b"Version 15.2(7)E3, RELEASE SOFTWARE (fc3)"
)
# Operator-customized / malformed sysDescr — version parse misses.
SYSDESCR_MALFORMED = b"operator-customised description, no software-rev marker"


def _make_get_scalar(responses: dict):
    """Build a fake get_scalar keyed by OID symbolic name."""
    from app.snmp_collector import OIDS
    reverse = {numeric: name for name, numeric in OIDS.items()}

    async def _fake(ip, community, oid_str, **kw):
        name = reverse.get(oid_str, oid_str)
        val = responses.get(name)
        if isinstance(val, Exception):
            raise val
        return val

    return _fake


async def _empty_walk(*a, **k):
    return []


# ---------------------------------------------------------------------------
# Hostname cleaning — Cisco prompt artifact (#) + FQDN strip
# ---------------------------------------------------------------------------

async def test_probe_sysname_with_prompt_hash_stripped():
    """Cisco devices without explicit hostname config return the CLI
    prompt (e.g. ``"Router#"``) as sysName — strip the trailing ``#``."""
    responses = {
        "SNMPv2-MIB::sysName": b"Router#",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert isinstance(facts, DeviceFacts)
    assert facts.hostname == "Router"


async def test_probe_sysname_fqdn_stripped_after_prompt_strip():
    """sysName with both prompt and FQDN — both cleanups apply."""
    responses = {
        "SNMPv2-MIB::sysName": b"c8000v.lab.example.com#",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert facts.hostname == "c8000v"


async def test_probe_sysname_clean_hostname_unchanged():
    """Properly configured sysName passes through cleanly."""
    responses = {
        "SNMPv2-MIB::sysName": b"c8000v",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert facts.hostname == "c8000v"


async def test_probe_sysname_user_exec_prompt_stripped():
    """User EXEC prompt suffix ``>`` also stripped (operator may have
    SNMP poll from a non-privileged context where sysName reflects
    the user EXEC prompt)."""
    responses = {
        "SNMPv2-MIB::sysName": b"Router>",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert facts.hostname == "Router"


async def test_probe_sysname_non_utf8_bytes_handled():
    responses = {
        "SNMPv2-MIB::sysName": b"\xff\xfec8000v.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert "." not in facts.hostname
    assert facts.hostname  # non-empty after replacement-decode


async def test_probe_sysname_empty_raises():
    responses = {
        "SNMPv2-MIB::sysName": None,
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        with pytest.raises(RuntimeError, match="sysName"):
            await probe_device_facts("192.0.2.17", "public")


async def test_probe_sysname_prompt_only_raises():
    """sysName=``"#"`` strips to empty → no usable hostname → raise."""
    responses = {
        "SNMPv2-MIB::sysName": b"#",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        with pytest.raises(RuntimeError, match="sysName"):
            await probe_device_facts("192.0.2.17", "public")


# ---------------------------------------------------------------------------
# sysDescr version parse paths
# ---------------------------------------------------------------------------

async def test_probe_iosxe_sysdescr_parses_version():
    """c8000v IOS-XE sysDescr → os_version=17.16.1a (real lab capture)."""
    responses = {
        "SNMPv2-MIB::sysName": b"c8000v",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert facts.os_version == "17.16.1a"


async def test_probe_classic_ios_sysdescr_parses_version():
    """Classic IOS sysDescr (text-validated only) → version with parens."""
    responses = {
        "SNMPv2-MIB::sysName": b"sw01",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CLASSIC_IOS,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert facts.os_version == "15.2(7)E3"


async def test_probe_malformed_sysdescr_yields_none_os_version():
    responses = {
        "SNMPv2-MIB::sysName": b"sw01",
        "SNMPv2-MIB::sysDescr": SYSDESCR_MALFORMED,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert facts.os_version is None


# ---------------------------------------------------------------------------
# Chassis model + serial paths
# ---------------------------------------------------------------------------

async def test_probe_entity_mib_primary_wins_over_legacy_scalar():
    """ENTITY-MIB is authoritative on modern IOS-XE — the legacy
    OLD-CISCO-CHASSIS-MIB scalar must NOT override the entity walk
    even if the scalar is populated. On c8000v, the legacy scalar
    returns "IOS-XE ROMMON" (the ROMMON identifier, not a chassis
    model); using it would mis-name the chassis. Pinned so any
    future fall-back-priority swap fails this test loudly."""
    responses = {
        "SNMPv2-MIB::sysName": b"c8000v",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        # Real c8000v capture: legacy scalar returns ROMMON identifier
        "OLD-CISCO-CHASSIS-MIB::chassisType": b"\r\nIOS-XE ROMMON\r\n",
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 3}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"9AM0AXLEZNB"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"C8000V"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.17", "public")

    # ENTITY-MIB wins — the ROMMON noise from the legacy scalar must
    # NOT make it into chassis_model.
    assert facts.chassis_model == "C8000V"
    assert facts.serial == "9AM0AXLEZNB"


async def test_probe_entity_mib_empty_falls_back_to_legacy_scalar():
    """Older classic IOS where ENTITY-MIB isn't populated — fall back
    to the legacy scalar for chassis_model. Serial stays None (no
    serial scalar in OLD-CISCO-CHASSIS-MIB). Text-validated only —
    no real classic-IOS device in lab."""
    responses = {
        "SNMPv2-MIB::sysName": b"sw01",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CLASSIC_IOS,
        "OLD-CISCO-CHASSIS-MIB::chassisType": b"cisco2960-24TT-L",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    # ENTITY-MIB empty → fall back to legacy scalar for chassis_model.
    assert facts.chassis_model == "cisco2960-24TT-L"
    # No serial (entity walk empty + no serial scalar).
    assert facts.serial is None


async def test_probe_all_chassis_paths_miss_yields_none():
    """Both ENTITY-MIB and legacy scalar return nothing — serial +
    chassis_model both None, no raise."""
    responses = {
        "SNMPv2-MIB::sysName": b"c8000v",
        "SNMPv2-MIB::sysDescr": SYSDESCR_C8000V,
        "OLD-CISCO-CHASSIS-MIB::chassisType": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.17", "public")
    assert facts.serial is None
    assert facts.chassis_model is None
