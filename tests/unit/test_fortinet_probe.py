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


# ---------------------------------------------------------------------------
# F2 — chassis_model normalization (vocabulary-driven; see
# controller/app/onboarding/probes/_fortinet_vocab.py)
# ---------------------------------------------------------------------------


from app.onboarding.probes._fortinet_vocab import normalize_chassis_model  # noqa: E402


@pytest.mark.parametrize("raw, expected", [
    # v1.0 lab matrix — actual entPhysicalModelName form returned by
    # FG-40F. This is the case that would have failed onboarding
    # without F2.
    ("FGT_40F_3G4G", "FortiGate 40F-3G4G"),
    # Forward-compat with submodel suffix (synthetic; not in v1.0 lab).
    ("FGT_100F_POE", "FortiGate 100F-POE"),
    ("FGT_80F_BDSL", "FortiGate 80F-BDSL"),
    # Forward-compat without submodel suffix (synthetic; not in v1.0 lab).
    ("FGT_60F", "FortiGate 60F"),
    ("FGT_200F", "FortiGate 200F"),
    ("FGT_600F", "FortiGate 600F"),
])
def test_normalize_chassis_model_underscore_slug(raw, expected):
    assert normalize_chassis_model(raw) == expected


def test_normalize_chassis_model_already_canonical_passthrough():
    # Forward-compat: a future FortiOS firmware that returns the
    # library-canonical marketing form via entPhysicalModelName must
    # not be rewritten.
    assert normalize_chassis_model("FortiGate 40F-3G4G") == "FortiGate 40F-3G4G"
    assert normalize_chassis_model("FortiGate 60F") == "FortiGate 60F"
    assert normalize_chassis_model("FortiGate 80F-DSL") == "FortiGate 80F-DSL"


def test_normalize_chassis_model_unrecognized_passthrough():
    # Per Rule 5 + D3 discipline: when the vocabulary doesn't match,
    # return the input unchanged so the orchestrator's
    # MissingReferenceError surfaces the gap with operator-actionable
    # text. Never auto-create the DeviceType.
    raw = "Some Future FortiGate Format"
    assert normalize_chassis_model(raw) == raw
    # Also: hyphen-instead-of-underscore form (not produced by the
    # known FortiOS shapes) passes through unchanged.
    assert normalize_chassis_model("FortiGate-40F") == "FortiGate-40F"


def test_normalize_chassis_model_strips_whitespace_before_match():
    # SNMP scalars sometimes return padded strings; whitespace-strip
    # before vocabulary match.
    assert normalize_chassis_model("  FGT_40F_3G4G  ") == "FortiGate 40F-3G4G"


def test_normalize_chassis_model_none_passthrough():
    # The orchestrator handles chassis_model=None gracefully (the
    # MissingReferenceError surface). Don't crash on a missing
    # primary-and-fallback case.
    assert normalize_chassis_model(None) is None


def test_normalize_chassis_model_empty_string_passthrough():
    assert normalize_chassis_model("") == ""


def test_normalize_chassis_model_bytes_input_decoded():
    # The probe pipes _decode'd strings into normalize, but accept
    # bytes too — defensively, in case any caller bypasses _decode.
    assert normalize_chassis_model(b"FGT_40F_3G4G") == "FortiGate 40F-3G4G"


# ---------------------------------------------------------------------------
# F2 — end-to-end through probe_device_facts
# ---------------------------------------------------------------------------


async def test_probe_device_facts_normalizes_entity_mib_underscore_slug():
    """The actual FG-40F lab string "FGT_40F_3G4G" via the full probe
    pipeline returns canonical "FortiGate 40F-3G4G" on
    facts.chassis_model — the F2-target case."""
    responses = {
        "SNMPv2-MIB::sysName": b"fgt40f",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": b"FGT40FTK1234567",
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 3}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"FGT40FTK1234567"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"FGT_40F_3G4G"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert facts.chassis_model == "FortiGate 40F-3G4G"


async def test_probe_device_facts_unrecognized_chassis_passes_through():
    """Unrecognized model in entPhysicalModelName returns unchanged so
    the orchestrator's MissingReferenceError can surface it."""
    responses = {
        "SNMPv2-MIB::sysName": b"future-fgt",
        "SNMPv2-MIB::sysDescr": SYSDESCR_CUSTOMIZED,
        "FORTINET-CORE-MIB::fnSysSerial": b"FGT-FUTURE-1",
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 3}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"FGT-FUTURE-1"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"FortiGate-Future-Model-X"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.15", "public")
    assert facts.chassis_model == "FortiGate-Future-Model-X"
