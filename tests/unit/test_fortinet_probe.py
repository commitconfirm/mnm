"""Unit tests for controller/app/onboarding/probes/fortinet.py (Prompt 7.5).

Mocks at snmp_collector.get_scalar / walk_table boundaries. Fixture
sysDescr values include the reality-check §3 lab capture (operator-
customized) and a plausible stock-FortiOS string to exercise the
version-parse branch.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload so sibling-test sys.modules.setdefault stubs are no-ops.
import app.onboarding.probes.fortinet as _probe_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401

from app.onboarding.probes.fortinet import (  # noqa: E402
    DeviceFacts,
    probe_device_facts,
)


# Reality-check §3 FortiGate 40F lab capture (operator-customized).
SYSDESCR_CUSTOMIZED = b"Fortigate 40F - MNM Test"
# Plausible stock FortiOS sysDescr with version string.
SYSDESCR_STOCK = b"FortiGate-40F v7.4.0,build12345,221221 (GA)"


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
        "SNMPv2-MIB::sysName": b"fgt40f.lab.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": b"FGT40FTK1234567",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert isinstance(facts, DeviceFacts)
    assert facts.hostname == "fgt40f"


async def test_probe_sysname_bare_hostname_preserved():
    responses = {
        "SNMPv2-MIB::sysName": b"fgt40f",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert facts.hostname == "fgt40f"


async def test_probe_sysname_non_utf8_bytes_handled():
    responses = {
        "SNMPv2-MIB::sysName": b"\xff\xfefgt40f.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert "." not in facts.hostname
    assert facts.hostname


# ---------------------------------------------------------------------------
# sysDescr version-parse paths (customized + stock)
# ---------------------------------------------------------------------------

async def test_probe_customized_sysdescr_yields_none_os_version():
    """Reality-check §3 lab capture: operator-customized sysDescr with
    no version string. os_version=None, no raise."""
    responses = {
        "SNMPv2-MIB::sysName": b"fgt40f",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert facts.os_version is None


async def test_probe_stock_sysdescr_parses_fortios_version():
    responses = {
        "SNMPv2-MIB::sysName": b"fgt40f",
        "SNMPv2-MIB::sysDescr": SYSDESCR_STOCK,
        "FORTINET-CORE-MIB::fnSysSerial": b"FGT40FTK1234567",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert facts.os_version == "7.4.0"


# ---------------------------------------------------------------------------
# Serial + chassis-model paths
# ---------------------------------------------------------------------------

async def test_probe_fnsysserial_primary():
    responses = {
        "SNMPv2-MIB::sysName": b"fgt40f",
        "SNMPv2-MIB::sysDescr": SYSDESCR_STOCK,
        "FORTINET-CORE-MIB::fnSysSerial": b"FGT40FTK1234567",
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 3}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"ENTITY-FALLBACK-SERIAL"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"FortiGate-40F"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.15", "public")
    # fnSysSerial wins as primary
    assert facts.serial == "FGT40FTK1234567"
    # chassis_model always comes from ENTITY-MIB (FORTINET-CORE-MIB has
    # no product-name scalar).
    assert facts.chassis_model == "FortiGate-40F"


async def test_probe_fnsysserial_empty_falls_back_to_entity():
    responses = {
        "SNMPv2-MIB::sysName": b"fgt40f",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": None,
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 3}, {"2": 9}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"ENTITY-SERIAL-1"}, {"2": b"MOD-SERIAL"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"FortiGate-40F"}, {"2": b"MOD"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert facts.serial == "ENTITY-SERIAL-1"
    assert facts.chassis_model == "FortiGate-40F"


async def test_probe_all_paths_miss_yields_none_fields():
    responses = {
        "SNMPv2-MIB::sysName": b"fgt40f",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": None,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert facts.serial is None
    assert facts.chassis_model is None
    assert facts.os_version is None


async def test_probe_sysname_miss_raises():
    responses = {
        "SNMPv2-MIB::sysName": None,
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": b"FGT40FTK1234567",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        with pytest.raises(RuntimeError, match="sysName"):
            await probe_device_facts("192.0.2.15", "public")
