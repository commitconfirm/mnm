"""Unit tests for controller/app/onboarding/probes/arista.py (Prompt 5).

Mocks at the :func:`app.snmp_collector.get_scalar` and
:func:`app.snmp_collector.walk_table` boundaries — no real SNMP traffic.
Fixture sysDescr is the reality-check §3 captured value from live vEOS
at 172.21.140.16 (2026-04-20).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload so sibling-test sys.modules.setdefault stubs are no-ops.
import app.onboarding.probes.arista as _probe_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401

from app.onboarding.probes.arista import (  # noqa: E402
    DeviceFacts,
    probe_device_facts,
)


# Reality-check §3 verbatim (2026-04-20 capture).
SYSDESCR_VEOS = b"Arista Networks EOS version 4.33.7M running on an Arista vEOS-lab"


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
        "SNMPv2-MIB::sysName": b"veos.lab.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_VEOS,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.16", "public")
    assert isinstance(facts, DeviceFacts)
    assert facts.hostname == "veos"


async def test_probe_sysname_bare_hostname_preserved():
    responses = {
        "SNMPv2-MIB::sysName": b"veos-lab",
        "SNMPv2-MIB::sysDescr": SYSDESCR_VEOS,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.16", "public")
    assert facts.hostname == "veos-lab"


async def test_probe_sysname_non_utf8_bytes_handled():
    responses = {
        "SNMPv2-MIB::sysName": b"\xff\xfehost.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_VEOS,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.16", "public")
    assert "." not in facts.hostname
    assert facts.hostname


async def test_probe_sysname_miss_raises():
    responses = {
        "SNMPv2-MIB::sysName": None,
        "SNMPv2-MIB::sysDescr": SYSDESCR_VEOS,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        with pytest.raises(RuntimeError, match="sysName"):
            await probe_device_facts("192.0.2.16", "public")


# ---------------------------------------------------------------------------
# EOS version parse (uses the verbatim captured sysDescr)
# ---------------------------------------------------------------------------

async def test_probe_parses_eos_version_from_real_sysdescr():
    responses = {
        "SNMPv2-MIB::sysName": b"veos",
        "SNMPv2-MIB::sysDescr": SYSDESCR_VEOS,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.16", "public")
    assert facts.os_version == "4.33.7M"


async def test_probe_malformed_sysdescr_yields_none_os_version():
    responses = {
        "SNMPv2-MIB::sysName": b"veos",
        "SNMPv2-MIB::sysDescr": b"something unrelated",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.16", "public")
    assert facts.os_version is None


# ---------------------------------------------------------------------------
# ENTITY-MIB walk — serial + chassis model
# ---------------------------------------------------------------------------

async def test_probe_entity_walk_populates_serial_and_model():
    responses = {
        "SNMPv2-MIB::sysName": b"veos",
        "SNMPv2-MIB::sysDescr": SYSDESCR_VEOS,
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            # Row "1" is chassis(3); row "2" is module(9).
            return [{"1": 3}, {"2": 9}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"ARISTA-SERIAL-1"}, {"2": b"MOD-SERIAL"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"DCS-7050SX-64"}, {"2": b"MODULE"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.16", "public")
    assert facts.serial == "ARISTA-SERIAL-1"
    assert facts.chassis_model == "DCS-7050SX-64"


async def test_probe_entity_walk_empty_yields_none_fields():
    responses = {
        "SNMPv2-MIB::sysName": b"veos",
        "SNMPv2-MIB::sysDescr": SYSDESCR_VEOS,
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.16", "public")
    assert facts.serial is None
    assert facts.chassis_model is None


async def test_probe_entity_walk_no_chassis_row_yields_none():
    """Only non-chassis rows (modules, ports, power supplies) — no
    entPhysicalClass=chassis(3). Probe must not pick up a random row."""
    responses = {
        "SNMPv2-MIB::sysName": b"veos",
        "SNMPv2-MIB::sysDescr": SYSDESCR_VEOS,
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 9}, {"2": 10}, {"3": 6}]  # module, port, powerSupply
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"MOD-S"}, {"2": b"PORT-S"}, {"3": b"PSU-S"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"MOD-M"}, {"2": b"PORT-M"}, {"3": b"PSU-M"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.16", "public")
    assert facts.serial is None
    assert facts.chassis_model is None
