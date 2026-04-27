"""Unit tests for controller/app/onboarding/classifier.py (v1.0 Prompt 3).

Fixture data is the real captured sysDescr / sysObjectID values from the
lab devices as recorded in
.claude/design/nautobot_rest_schema_notes.md Section 3 — those strings are
the canonical contract the classifier is built against.

Mocks at :func:`app.snmp_collector.get_scalar` for the ``classify()`` async
entry point; no real SNMP traffic.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload the real modules so any stubs applied by other test files (via
# sys.modules.setdefault) are no-ops.
import app.onboarding.classifier as _cls_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401

from app.onboarding.classifier import (  # noqa: E402
    CISCO_IOSXE_MARKERS,
    ClassifierResult,
    SYSOBJECTID_TO_VENDOR,
    classify,
    classify_from_signals,
    detect_vendor_platform,
)


# ---------------------------------------------------------------------------
# Reality-check §3 captured signal matrix — canonical test fixtures
# ---------------------------------------------------------------------------

SYSDESCR_EX2300 = (
    b"Juniper Networks, Inc. ex2300-24p Ethernet Switch, "
    b"kernel JUNOS 23.4R2-S4.11, Build date: 2025-03-14 21:17:35 UTC "
    b"Copyright (c) 1996-2025 Juniper Networks, Inc."
)
SYSDESCR_SRX320 = (
    b"Juniper Networks, Inc. srx320 internet router, "
    b"kernel JUNOS 23.4R2-S6.9, Build date: 2025-11-12 15:24:01 UTC "
    b"Copyright (c) 1996-2025 Juniper Networks, Inc."
)
SYSDESCR_EX3300 = (
    b"Juniper Networks, Inc. ex3300-24p Ethernet Switch, "
    b"kernel JUNOS 12.3R12.4, Build date: 2016-01-20 05:01:04 UTC"
)
SYSDESCR_EX4300 = (
    b"Juniper Networks, Inc. ex4300-48t Ethernet Switch, "
    b"kernel JUNOS 21.4R3-S7.6, Build date: 2024-04-20 11:44:44 UTC"
)
SYSDESCR_PA440 = b"Palo Alto Networks PA-400 series firewall"
SYSDESCR_FORTIGATE = b"Fortigate 40F - MNM Test"
SYSDESCR_CISCO_C8000V = (
    b"Cisco IOS Software [IOSXE], Virtual XE Software "
    b"(X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 17.16.1a, "
    b"RELEASE SOFTWARE (fc1)"
)

SYSOBJECTID_EX2300 = "1.3.6.1.4.1.2636.1.1.1.4.132.3"
SYSOBJECTID_SRX320 = "1.3.6.1.4.1.2636.1.1.1.2.134"
SYSOBJECTID_EX3300 = "1.3.6.1.4.1.2636.1.1.1.2.76"
SYSOBJECTID_EX4300 = "1.3.6.1.4.1.2636.1.1.1.2.63"
SYSOBJECTID_PA440 = "1.3.6.1.4.1.25461.2.3.54"
SYSOBJECTID_FORTIGATE = "1.3.6.1.4.1.12356.101.1.443"
SYSOBJECTID_CISCO_C8000V = "1.3.6.1.4.1.9.1.3004"


# ---------------------------------------------------------------------------
# Vendor / platform — sysDescr primary path (one per lab vendor)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sysdescr,sysobjectid,expected_vendor,expected_platform", [
    (SYSDESCR_EX2300,  SYSOBJECTID_EX2300,  "juniper",  "juniper_junos"),
    (SYSDESCR_SRX320,  SYSOBJECTID_SRX320,  "juniper",  "juniper_junos"),
    (SYSDESCR_EX3300,  SYSOBJECTID_EX3300,  "juniper",  "juniper_junos"),
    (SYSDESCR_EX4300,  SYSOBJECTID_EX4300,  "juniper",  "juniper_junos"),
    (SYSDESCR_PA440,   SYSOBJECTID_PA440,   "palo_alto", "paloalto_panos"),
    (SYSDESCR_FORTIGATE, SYSOBJECTID_FORTIGATE, "fortinet", "fortinet_fortios"),
])
def test_detect_vendor_platform_sysdescr_primary(
    sysdescr, sysobjectid, expected_vendor, expected_platform
):
    vendor, platform, signals = detect_vendor_platform(sysdescr, sysobjectid)
    assert vendor == expected_vendor
    assert platform == expected_platform
    assert signals and signals[0].startswith("sysdescr:")


def test_detect_vendor_platform_cisco_iosxe_c8000v():
    vendor, platform, signals = detect_vendor_platform(
        SYSDESCR_CISCO_C8000V, SYSOBJECTID_CISCO_C8000V,
    )
    assert vendor == "cisco"
    assert platform == "cisco_iosxe"
    assert any("iosxe" in s.lower() for s in signals)


# ---------------------------------------------------------------------------
# sysObjectID-only fallback (operator-customized sysDescr)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sysobjectid,expected_vendor,expected_platform", [
    (SYSOBJECTID_EX2300,     "juniper",   "juniper_junos"),
    (SYSOBJECTID_PA440,      "palo_alto", "paloalto_panos"),
    (SYSOBJECTID_FORTIGATE,  "fortinet",  "fortinet_fortios"),
    (SYSOBJECTID_CISCO_C8000V, "cisco",   "cisco_ios"),  # default platform — no sysDescr marker
    ("1.3.6.1.4.1.30065.1.1", "arista",   "arista_eos"),  # validated on vEOS 172.21.140.16
])
def test_detect_vendor_platform_sysobjectid_only(
    sysobjectid, expected_vendor, expected_platform
):
    vendor, platform, signals = detect_vendor_platform(None, sysobjectid)
    assert vendor == expected_vendor
    assert platform == expected_platform
    assert signals == [f"sysobjectid:{_oid_prefix_for(expected_vendor)}"]


def _oid_prefix_for(vendor: str) -> str:
    for prefix, (v, _) in SYSOBJECTID_TO_VENDOR.items():
        if v == vendor:
            return prefix
    raise AssertionError(f"unknown vendor {vendor}")


# ---------------------------------------------------------------------------
# Cisco two-stage IOS vs IOS-XE discrimination
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sysdescr", [
    b"Cisco IOS Software [IOSXE], Virtual XE Software",   # c8000v: [IOSXE]
    b"Cisco IOS-XE Software, Version 17.3.4",             # hyphenated
    b"Cisco IOS XE Software, Version 17.3.4",             # spaced
])
def test_cisco_iosxe_markers_all_variants(sysdescr):
    vendor, platform, _ = detect_vendor_platform(sysdescr, None)
    assert vendor == "cisco"
    assert platform == "cisco_iosxe"


def test_cisco_classic_ios_no_marker():
    vendor, platform, _ = detect_vendor_platform(
        b"Cisco IOS Software, Version 15.2(4)M5", None,
    )
    assert vendor == "cisco"
    assert platform == "cisco_ios"


def test_cisco_iosxe_markers_constant_shape():
    assert b"IOSXE" in CISCO_IOSXE_MARKERS
    assert b"IOS-XE" in CISCO_IOSXE_MARKERS
    assert b"IOS XE" in CISCO_IOSXE_MARKERS


# ---------------------------------------------------------------------------
# FortiGate — case-insensitive match + operator-customized fallback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sysdescr", [
    b"FortiGate-40F",
    b"fortigate",
    b"FORTIGATE",
    b"Fortigate 40F - MNM Test",
])
def test_fortigate_case_insensitive(sysdescr):
    vendor, platform, _ = detect_vendor_platform(sysdescr, None)
    assert vendor == "fortinet"
    assert platform == "fortinet_fortios"


def test_fortigate_operator_customized_sysdescr_falls_back_to_objectid():
    """Reality-check §3: FortiGate admins commonly rewrite sysDescr."""
    vendor, platform, signals = detect_vendor_platform(
        b"Customer-hostname-42", SYSOBJECTID_FORTIGATE,
    )
    assert vendor == "fortinet"
    assert platform == "fortinet_fortios"
    assert signals == ["sysobjectid:1.3.6.1.4.1.12356"]


# ---------------------------------------------------------------------------
# Defensive: non-UTF-8 bytes must not raise
# ---------------------------------------------------------------------------

def test_non_utf8_sysdescr_does_not_raise():
    # \xff is invalid UTF-8 — the classifier must decode with errors=replace
    vendor, platform, _ = detect_vendor_platform(
        b"\xff\xfe Juniper Networks bogon", None,
    )
    assert vendor == "juniper"  # the proper-noun substring still matches bytes
    assert platform == "juniper_junos"


def test_non_utf8_sysdescr_fortigate_path_does_not_raise():
    # No "Juniper" / "Cisco" / etc. — falls through to case-insensitive
    # FortiGate decoder, which must handle invalid bytes gracefully.
    vendor, platform, _ = detect_vendor_platform(
        b"\xff\xfe FortiGate box", None,
    )
    assert vendor == "fortinet"
    assert platform == "fortinet_fortios"


# ---------------------------------------------------------------------------
# Unknown — no vendor signal matches
# ---------------------------------------------------------------------------

def test_unknown_returns_vendor_none():
    vendor, platform, signals = detect_vendor_platform(
        b"Generic-Switch firmware 2.7", "1.3.6.1.4.1.99999.1",
    )
    assert vendor is None
    assert platform is None
    assert signals == []


# ---------------------------------------------------------------------------
# classify_from_signals — role classification preserved across refactor
# ---------------------------------------------------------------------------

def test_classify_from_signals_junos_switch_identifies_as_switch():
    """EX2300's sysDescr contains 'ex2' which maps to switch classification."""
    result = classify_from_signals(sysdescr=SYSDESCR_EX2300)
    assert isinstance(result, ClassifierResult)
    assert result.classification == "switch"
    assert result.vendor == "juniper"
    assert result.platform == "juniper_junos"


def test_classify_from_signals_srx_identifies_as_firewall():
    result = classify_from_signals(sysdescr=SYSDESCR_SRX320)
    assert result.classification == "firewall"
    assert result.vendor == "juniper"


def test_classify_from_signals_fortigate_firewall():
    result = classify_from_signals(sysdescr=SYSDESCR_FORTIGATE)
    assert result.classification == "firewall"
    assert result.vendor == "fortinet"


def test_classify_from_signals_empty_snmp_falls_back_to_ports():
    result = classify_from_signals(
        ports_open=["22/tcp", "443/tcp"],
    )
    assert result.classification == "server"
    assert result.vendor is None
    assert result.confidence == "low"


def test_classify_from_signals_no_signals_returns_unknown_or_endpoint():
    # No ports, no snmp, no banner — "endpoint" per the pre-refactor fallback.
    result = classify_from_signals(ports_open=[])
    assert result.classification == "endpoint"
    assert result.vendor is None


def test_classifier_result_to_dict_roundtrip():
    result = classify_from_signals(sysdescr=SYSDESCR_EX2300)
    d = result.to_dict()
    assert d["classification"] == "switch"
    assert d["vendor"] == "juniper"
    assert d["platform"] == "juniper_junos"
    assert "sysdescr:juniper_networks" in d["signals_matched"]


# ---------------------------------------------------------------------------
# Async classify() — SNMP collection + classification
# ---------------------------------------------------------------------------

async def test_classify_async_collects_sysdescr_and_sysobjectid():
    async def _fake_get_scalar(ip, community, oid_str, **kw):
        if oid_str.endswith(".1.0"):
            return SYSDESCR_EX2300
        if oid_str.endswith(".2.0"):
            return SYSOBJECTID_EX2300
        raise AssertionError(f"unexpected OID {oid_str}")

    with patch("app.onboarding.classifier.get_scalar", side_effect=_fake_get_scalar):
        result = await classify("192.0.2.1", "public")
    assert result.vendor == "juniper"
    assert result.platform == "juniper_junos"
    assert result.classification == "switch"


async def test_classify_async_snmp_failure_degrades_gracefully():
    from app.snmp_collector import SnmpError

    async def _fake_get_scalar(*args, **kw):
        raise SnmpError("timeout")

    with patch("app.onboarding.classifier.get_scalar", side_effect=_fake_get_scalar):
        result = await classify("192.0.2.1", "public", ports_open=[])
    # No signals at all — falls through to the endpoint/unknown branch.
    assert result.vendor is None
    assert result.classification in ("endpoint", "unknown")


async def test_classify_async_forwards_extra_signals():
    async def _fake_get_scalar(ip, community, oid_str, **kw):
        return None  # both SNMP getters return nothing

    with patch("app.onboarding.classifier.get_scalar", side_effect=_fake_get_scalar):
        result = await classify(
            "192.0.2.1", "public",
            mac_vendor="Axis Communications",
            ports_open=["554/tcp"],
        )
    # mac_vendor + port both vote camera -> two-tier high confidence.
    assert result.classification == "camera"
    assert result.confidence == "high"
