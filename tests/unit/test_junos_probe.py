"""Unit tests for controller/app/onboarding/probes/junos.py (Prompt 4).

Mocks at the :func:`app.snmp_collector.get_scalar` and
:func:`app.snmp_collector.walk_table` boundaries — no real SNMP traffic.
Fixture data comes from the reality-check §3 captured sysDescr strings.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload the real modules so sibling-test sys.modules.setdefault stubs are
# no-ops (same pattern test_classifier.py and test_nautobot_client.py use).
import app.onboarding.probes.junos as _probe_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401

from app.onboarding.probes.junos import (  # noqa: E402
    DeviceFacts,
    probe_device_facts,
)


# Reality-check §3 EX2300 sysDescr verbatim.
SYSDESCR_EX2300 = (
    b"Juniper Networks, Inc. ex2300-24p Ethernet Switch, "
    b"kernel JUNOS 23.4R2-S4.11, Build date: 2025-03-14 21:17:35 UTC "
    b"Copyright (c) 1996-2025 Juniper Networks, Inc."
)


def _make_get_scalar(responses: dict):
    """Build a fake get_scalar that returns by OID symbolic name.

    ``responses`` maps OID symbolic name (e.g. 'SNMPv2-MIB::sysName') to the
    value (bytes / None / Exception).
    """
    from app.snmp_collector import OIDS
    reverse = {numeric: name for name, numeric in OIDS.items()}

    async def _fake(ip, community, oid_str, **kw):
        name = reverse.get(oid_str, oid_str)
        val = responses.get(name)
        if isinstance(val, Exception):
            raise val
        return val

    return _fake


async def test_probe_sysname_fqdn_stripped_to_short_form():
    responses = {
        "SNMPv2-MIB::sysName": b"ex2300.lab.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_EX2300,
        "JUNIPER-MIB::jnxBoxSerialNo": b"JY3622160886",
        "JUNIPER-MIB::jnxBoxDescr": b"EX2300-24P",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert isinstance(facts, DeviceFacts)
    assert facts.hostname == "ex2300"


async def test_probe_sysname_bare_hostname_preserved():
    responses = {
        "SNMPv2-MIB::sysName": b"SRX320",
        "SNMPv2-MIB::sysDescr": b"Junos",
        "JUNIPER-MIB::jnxBoxSerialNo": None,
        "JUNIPER-MIB::jnxBoxDescr": None,
    }

    async def _empty_walk(*a, **k):
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_empty_walk):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.hostname == "SRX320"


async def test_probe_sysname_non_utf8_bytes_decoded_with_replace():
    responses = {
        # Partially invalid UTF-8; should not raise.
        "SNMPv2-MIB::sysName": b"\xff\xfehost.example.com",
        "SNMPv2-MIB::sysDescr": SYSDESCR_EX2300,
        "JUNIPER-MIB::jnxBoxSerialNo": b"JY1",
        "JUNIPER-MIB::jnxBoxDescr": b"EX2300-24P",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)):
        facts = await probe_device_facts("192.0.2.1", "public")
    # errors="replace" yields a printable form; short-form split still works
    assert "." not in facts.hostname
    assert facts.hostname  # non-empty


async def test_probe_jnxbox_serial_populated_skips_entity_walk():
    responses = {
        "SNMPv2-MIB::sysName": b"ex2300",
        "SNMPv2-MIB::sysDescr": SYSDESCR_EX2300,
        "JUNIPER-MIB::jnxBoxSerialNo": b"JY3622160886",
        "JUNIPER-MIB::jnxBoxDescr": b"EX2300-24P",
    }
    called_walk = False

    async def _walk_spy(*a, **k):
        nonlocal called_walk
        called_walk = True
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.serial == "JY3622160886"
    assert facts.chassis_model == "EX2300-24P"
    assert called_walk is False


async def test_probe_jnxbox_empty_falls_back_to_entity_walk():
    responses = {
        "SNMPv2-MIB::sysName": b"device",
        "SNMPv2-MIB::sysDescr": b"Junos",
        "JUNIPER-MIB::jnxBoxSerialNo": None,
        "JUNIPER-MIB::jnxBoxDescr": None,
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            # Row index "1" has class chassis(3); row "2" has class module(9).
            return [{"1": 3}, {"2": 9}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"ENT-SERIAL-1"}, {"2": b"ENT-SERIAL-2"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"EX-CHASSIS"}, {"2": b"MODULE"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.serial == "ENT-SERIAL-1"
    assert facts.chassis_model == "EX-CHASSIS"


async def test_probe_all_sources_miss_leaves_fields_none():
    responses = {
        "SNMPv2-MIB::sysName": b"device",
        "SNMPv2-MIB::sysDescr": b"generic",
        "JUNIPER-MIB::jnxBoxSerialNo": None,
        "JUNIPER-MIB::jnxBoxDescr": None,
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table", side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.serial is None
    assert facts.chassis_model is None
    assert facts.os_version is None


async def test_probe_parses_junos_version_from_sysdescr():
    responses = {
        "SNMPv2-MIB::sysName": b"ex2300",
        "SNMPv2-MIB::sysDescr": SYSDESCR_EX2300,
        "JUNIPER-MIB::jnxBoxSerialNo": b"JY1",
        "JUNIPER-MIB::jnxBoxDescr": b"EX2300-24P",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.os_version == "23.4R2-S4.11"


async def test_probe_malformed_sysdescr_yields_none_os_version():
    responses = {
        "SNMPv2-MIB::sysName": b"dev",
        "SNMPv2-MIB::sysDescr": b"totally unrelated string",
        "JUNIPER-MIB::jnxBoxSerialNo": b"JY1",
        "JUNIPER-MIB::jnxBoxDescr": b"EX",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.os_version is None


async def test_probe_sysname_miss_raises():
    responses = {
        "SNMPv2-MIB::sysName": None,  # the only fatal miss
        "SNMPv2-MIB::sysDescr": b"Junos",
        "JUNIPER-MIB::jnxBoxSerialNo": b"JY1",
        "JUNIPER-MIB::jnxBoxDescr": b"EX",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)):
        with pytest.raises(RuntimeError, match="sysName"):
            await probe_device_facts("192.0.2.1", "public")


# ---------------------------------------------------------------------------
# F1 — chassis_model normalization (vocabulary-driven; see
# controller/app/onboarding/probes/_junos_vocab.py)
# ---------------------------------------------------------------------------


from app.onboarding.probes._junos_vocab import normalize_chassis_model  # noqa: E402


@pytest.mark.parametrize("raw, expected", [
    # v1.0 lab matrix — actual jnxBoxDescr "marketing" form returned
    # by the device. These are the cases that would have failed onboarding
    # without F1.
    ("Juniper EX2300-24P Switch", "EX2300-24P"),
    ("Juniper EX4300-48T Ethernet Switch", "EX4300-48T"),
    ("Juniper SRX320 Internet Router", "SRX320"),
    # EX3300 — constructed marketing form (E2 lab capture didn't
    # preserve the device's actual string; this matches the "Ethernet
    # Switch" suffix EX3300/EX4300 lab devices share).
    ("Juniper EX3300-24P Ethernet Switch", "EX3300-24P"),
    # Forward-compat — vocabulary-only coverage (no device in v1.0
    # lab) for the EX4600 and an MX router.
    ("Juniper EX4600-40F Ethernet Switch", "EX4600-40F"),
    ("Juniper MX204 Router", "MX204"),
    # SRX with Services Gateway suffix (modern firmware shape).
    ("Juniper SRX340 Services Gateway", "SRX340"),
])
def test_normalize_chassis_model_marketing_forms(raw, expected):
    assert normalize_chassis_model(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    # sysDescr-style "Juniper Networks, Inc. <model> ..." long form,
    # lowercase model name with kernel trailer. The remote_system_
    # description for ex4300-48t captured during E2 validation
    # (§10.2.1) is one of these.
    (
        "Juniper Networks, Inc. ex2300-c-12p Internet Router, "
        "kernel JUNOS 22.4R3-S5.6 #0: ...",
        "EX2300-C-12P",
    ),
    (
        "Juniper Networks, Inc. ex4300-48t internet router, "
        "kernel JUNOS 21.4R3-S7.6, ...",
        "EX4300-48T",
    ),
    (
        "Juniper Networks, Inc. srx320 services gateway, "
        "kernel JUNOS 21.x",
        "SRX320",
    ),
    (
        "Juniper Networks, Inc. mx204 router, kernel JUNOS 22.x",
        "MX204",
    ),
])
def test_normalize_chassis_model_long_form_fallback(raw, expected):
    assert normalize_chassis_model(raw) == expected


def test_normalize_chassis_model_already_canonical_passthrough():
    # The existing controller-side test fixtures (older in this file)
    # use the already-canonical form b"EX2300-24P". F1's vocabulary
    # must NOT rewrite a string that's already canonical.
    assert normalize_chassis_model("EX2300-24P") == "EX2300-24P"
    assert normalize_chassis_model("SRX320") == "SRX320"


def test_normalize_chassis_model_unrecognized_passthrough():
    # Per Rule 5 + D3 discipline: when the vocabulary doesn't match,
    # return the input unchanged so the orchestrator's
    # MissingReferenceError surfaces the gap with operator-actionable
    # text. Never auto-create the DeviceType.
    raw = "Some Future Junos Model We Haven't Seen"
    assert normalize_chassis_model(raw) == raw


def test_normalize_chassis_model_strips_whitespace_before_match():
    # SNMP scalars sometimes return padded strings; whitespace-strip
    # before vocabulary match.
    assert normalize_chassis_model(
        "  Juniper EX4300-48T Ethernet Switch  "
    ) == "EX4300-48T"


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
    assert normalize_chassis_model(
        b"Juniper EX2300-24P Switch"
    ) == "EX2300-24P"


# ---------------------------------------------------------------------------
# F1 — end-to-end through probe_device_facts
# ---------------------------------------------------------------------------


async def test_probe_device_facts_normalizes_jnxboxdescr_long_form():
    """E2-lab string "Juniper EX2300-24P Switch" via the full probe
    pipeline returns canonical short form on facts.chassis_model."""
    responses = {
        "SNMPv2-MIB::sysName": b"ex2300-24p",
        "SNMPv2-MIB::sysDescr": SYSDESCR_EX2300,
        "JUNIPER-MIB::jnxBoxSerialNo": b"JY3622160886",
        "JUNIPER-MIB::jnxBoxDescr": b"Juniper EX2300-24P Switch",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.chassis_model == "EX2300-24P"


async def test_probe_device_facts_normalizes_entity_mib_fallback():
    """When jnxBoxDescr is empty and ENTITY-MIB returns the long
    form, normalization still applies."""
    responses = {
        "SNMPv2-MIB::sysName": b"ex4300-48t",
        "SNMPv2-MIB::sysDescr": b"Junos",
        "JUNIPER-MIB::jnxBoxSerialNo": None,
        "JUNIPER-MIB::jnxBoxDescr": None,
    }

    async def _walk_spy(ip, community, oid_str, **kw):
        from app.snmp_collector import OIDS
        if oid_str == OIDS["ENTITY-MIB::entPhysicalClass"]:
            return [{"1": 3}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalSerialNum"]:
            return [{"1": b"ENT-SERIAL-1"}]
        if oid_str == OIDS["ENTITY-MIB::entPhysicalModelName"]:
            return [{"1": b"Juniper EX4300-48T Ethernet Switch"}]
        return []

    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)), \
         patch("app.onboarding.probes.junos.walk_table",
               side_effect=_walk_spy):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.chassis_model == "EX4300-48T"


async def test_probe_device_facts_unrecognized_chassis_passes_through():
    """Unrecognized model in jnxBoxDescr returns unchanged so the
    orchestrator's MissingReferenceError can surface it."""
    responses = {
        "SNMPv2-MIB::sysName": b"future-device",
        "SNMPv2-MIB::sysDescr": b"Junos",
        "JUNIPER-MIB::jnxBoxSerialNo": b"JY1",
        "JUNIPER-MIB::jnxBoxDescr": b"Juniper Some-Future-Model Switch",
    }
    with patch("app.onboarding.probes.junos.get_scalar",
               side_effect=_make_get_scalar(responses)):
        facts = await probe_device_facts("192.0.2.1", "public")
    assert facts.chassis_model == "Juniper Some-Future-Model Switch"
