"""Unit tests for controller/app/onboarding/probes/paloalto.py (Prompt 7).

Mocks at snmp_collector.get_scalar / walk_table boundaries. Fixture
sysDescr is the reality-check §3 capture from PA-440 verbatim.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload so sibling-test sys.modules.setdefault stubs are no-ops.
import app.onboarding.probes.paloalto as _probe_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401

from app.onboarding.probes.paloalto import (  # noqa: E402
    DeviceFacts,
    probe_device_facts,
)


# Reality-check §3 PA-440 sysDescr verbatim.
SYSDESCR_PA440 = b"Palo Alto Networks PA-400 series firewall"


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
# Hostname cleaning
# ---------------------------------------------------------------------------

async def test_probe_sysname_fqdn_stripped_to_short_form():
    responses = {
        "SNMPv2-MIB::sysName": b"pa-440.lab.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": b"11.1.2",
        "PAN-COMMON-MIB::panSysSerialNumber": b"013201008421",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.9", "public")
    assert isinstance(facts, DeviceFacts)
    assert facts.hostname == "pa-440"


async def test_probe_sysname_bare_hostname_preserved():
    responses = {
        "SNMPv2-MIB::sysName": b"pa-440",
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": None,
        "PAN-COMMON-MIB::panSysSerialNumber": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.9", "public")
    assert facts.hostname == "pa-440"


async def test_probe_sysname_non_utf8_bytes_handled():
    responses = {
        "SNMPv2-MIB::sysName": b"\xff\xfepa-440.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": None,
        "PAN-COMMON-MIB::panSysSerialNumber": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.9", "public")
    assert "." not in facts.hostname
    assert facts.hostname


async def test_probe_sysname_miss_raises():
    responses = {
        "SNMPv2-MIB::sysName": None,
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": b"11.1.2",
        "PAN-COMMON-MIB::panSysSerialNumber": b"013201008421",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        with pytest.raises(RuntimeError, match="sysName"):
            await probe_device_facts("192.0.2.9", "public")


# ---------------------------------------------------------------------------
# Version parse — PAN-OS sysDescr doesn't carry version
# ---------------------------------------------------------------------------

async def test_probe_bare_sysdescr_no_version_not_fatal():
    """Reality-check §3: PA-440 sysDescr is the bare product string with
    no version. panSysSwVersion=None → os_version=None, no raise."""
    responses = {
        "SNMPv2-MIB::sysName": b"pa-440",
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": None,
        "PAN-COMMON-MIB::panSysSerialNumber": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.9", "public")
    assert facts.os_version is None


async def test_probe_pansyswversion_scalar_populates_os_version():
    responses = {
        "SNMPv2-MIB::sysName": b"pa-440",
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": b"11.1.2",
        "PAN-COMMON-MIB::panSysSerialNumber": b"013201008421",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.9", "public")
    assert facts.os_version == "11.1.2"


# ---------------------------------------------------------------------------
# Serial + chassis model paths
# ---------------------------------------------------------------------------

async def test_probe_pansysserialnumber_primary():
    responses = {
        "SNMPv2-MIB::sysName": b"pa-440",
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": b"11.1.2",
        "PAN-COMMON-MIB::panSysSerialNumber": b"013201008421",
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 3}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"ENT-FALLBACK-SERIAL"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"PA-440"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.9", "public")
    # panSysSerialNumber wins as primary
    assert facts.serial == "013201008421"
    # chassis_model comes from ENTITY-MIB regardless
    assert facts.chassis_model == "PA-440"


async def test_probe_pansysserialnumber_empty_falls_back_to_entity():
    responses = {
        "SNMPv2-MIB::sysName": b"pa-440",
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": b"11.1.2",
        "PAN-COMMON-MIB::panSysSerialNumber": None,
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 3}, {"2": 9}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"ENTITY-SERIAL-1"}, {"2": b"MOD-SERIAL"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"PA-440"}, {"2": b"MOD"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.9", "public")
    assert facts.serial == "ENTITY-SERIAL-1"
    assert facts.chassis_model == "PA-440"


async def test_probe_all_paths_miss_yields_none_fields():
    responses = {
        "SNMPv2-MIB::sysName": b"pa-440",
        "SNMPv2-MIB::sysDescr": SYSDESCR_PA440,
        "PAN-COMMON-MIB::panSysSwVersion": None,
        "PAN-COMMON-MIB::panSysSerialNumber": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.9", "public")
    assert facts.serial is None
    assert facts.chassis_model is None
    assert facts.os_version is None
