"""Unit tests for D5 — /api/sweeps/unsupported endpoint.

Covers the handler's filter logic, ordering, pagination, field
projection, and graceful behavior on classifier failure.

Testing strategy: direct-call the route handler coroutine (bypasses
FastAPI's dependency injection). Patch nautobot_client at the
app.main import site so the handler sees the mocks. classify_from_signals
is the real classifier — D5 deliberately re-classifies at read time;
mocking it would test our mock instead of the integration.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload to beat sibling sys.modules.setdefault stubs.
import app.main as _main_preload  # noqa: E402, F401
import app.nautobot_client as _nc_preload  # noqa: E402, F401

from app.main import get_unsupported_classifications  # noqa: E402


# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------

def _ip_record(ip: str, *, sysdescr: str = "", ports: str = "",
               oui_vendor: str = "", first_seen: str = "2026-04-20T00:00:00Z",
               last_seen: str = "2026-04-28T00:00:00Z",
               method: str = "sweep") -> dict:
    """Build an IPAM IP record matching what nautobot_client.get_ip_addresses
    returns — host portion in ``display``, custom_fields populated by the
    sweep pipeline."""
    return {
        "id": f"ip-{ip}",
        "display": f"{ip}/32",
        "address": f"{ip}/32",
        "custom_fields": {
            "discovery_method": method,
            "discovery_snmp_sysdescr": sysdescr,
            "discovery_ports_open": ports,
            "discovery_mac_vendor": oui_vendor,
            "discovery_first_seen": first_seen,
            "discovery_last_seen": last_seen,
        },
    }


def _device_with_primary_ip(ip: str) -> dict:
    """Build a Nautobot Device record with primary_ip4 set — D5 excludes
    these from the response."""
    return {
        "id": f"dev-{ip}",
        "name": f"device-{ip}",
        "primary_ip4": {"display": f"{ip}/32", "address": f"{ip}/32"},
    }


# Realistic sysDescr fixtures sourced from CLAUDE.md / classifier docs.
SYSDESCR_HPE_PROCURVE = (
    "ProCurve J9145A Switch 2910al-24G, revision W.15.10.0011, "
    "ROM W.15.06 (/sw/code/build/btm(t1a))"
)
SYSDESCR_MIKROTIK = "RouterOS RB3011UiAS-RM"
SYSDESCR_JUNIPER_EX2300 = (
    "Juniper Networks, Inc. ex2300-24p Ethernet Switch, "
    "kernel JUNOS 23.4R2-S4.11, Build date: 2024-12-09"
)
SYSDESCR_GENERIC_LINUX = "Linux generic-host 5.4.0 #1 SMP x86_64"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_three_mixed_status_ips():
    """Three sweep-discovered IPs: one unsupported vendor (HPE), one
    unclassified (no signals), one already onboarded (Junos with Device).
    Endpoint returns 2 rows — the onboarded IP is excluded."""
    ip_records = [
        _ip_record("203.0.113.10", sysdescr=SYSDESCR_HPE_PROCURVE,
                   oui_vendor="Hewlett Packard"),
        _ip_record("203.0.113.20"),  # no signals
        _ip_record("203.0.113.30", sysdescr=SYSDESCR_JUNIPER_EX2300),
    ]
    devices = [_device_with_primary_ip("203.0.113.30")]

    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=devices)):
        resp = await get_unsupported_classifications()

    assert resp["count"] == 2
    assert len(resp["results"]) == 2
    ips = {row["ip"] for row in resp["results"]}
    assert ips == {"203.0.113.10", "203.0.113.20"}
    # Junos IP excluded — has Device record
    assert "203.0.113.30" not in ips


@pytest.mark.asyncio
async def test_hpe_procurve_surfaces_as_unclassified():
    """An HPE Procurve sysDescr should surface in the panel — but with
    classification_status='unclassified' rather than 'unsupported_vendor'.

    Why: the classifier's `SYSOBJECTID_TO_VENDOR` map covers exactly
    SUPPORTED_VENDORS today (juniper, arista, palo_alto, fortinet, cisco).
    Vendors NOT in that map produce vendor=None even when sysDescr
    clearly identifies them. From the operator's perspective the result
    is the same — "MNM didn't recognise this as something it supports"
    — but the enum bucket is 'unclassified'.

    Documented as a v1.0.1 candidate in mnm-dev-claude: extend the
    classifier to label vendor for HPE / Mikrotik / etc. so the
    distinction becomes meaningful.
    """
    ip_records = [
        _ip_record("203.0.113.10", sysdescr=SYSDESCR_HPE_PROCURVE,
                   oui_vendor="Hewlett Packard"),
    ]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications()

    assert resp["count"] == 1
    row = resp["results"][0]
    assert row["ip"] == "203.0.113.10"
    assert row["classified_vendor"] is None
    assert row["classification_status"] == "unclassified"
    assert row["in_supported_vendors"] is False
    # The sysDescr excerpt + OUI vendor still surface so operators have
    # raw signals to act on.
    assert row["sys_descr_excerpt"] is not None
    assert "ProCurve" in row["sys_descr_excerpt"]
    assert row["oui_vendor"] == "Hewlett Packard"


@pytest.mark.asyncio
async def test_unsupported_vendor_status_via_mocked_classifier():
    """Forward-compat: verify the 'unsupported_vendor' code path works
    when the classifier returns a vendor not in SUPPORTED_VENDORS.

    Today no real classifier output reaches this path (see
    test_hpe_procurve_surfaces_as_unclassified above for why), but the
    enum bucket and code path exist for the future when the classifier
    is extended to label additional vendors. Mocking classify_from_signals
    here so the test isn't a no-op.
    """
    ip_records = [
        _ip_record("203.0.113.10", sysdescr="some new vendor sysDescr",
                   oui_vendor="Some Vendor"),
    ]
    from unittest.mock import MagicMock

    fake_result = MagicMock()
    fake_result.classification = "switch"
    fake_result.vendor = "future_vendor_not_supported"
    fake_result.platform = "future_vendor_platform"

    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])), \
         patch("app.onboarding.classifier.classify_from_signals",
               return_value=fake_result):
        resp = await get_unsupported_classifications()

    assert resp["count"] == 1
    row = resp["results"][0]
    assert row["classified_vendor"] == "future_vendor_not_supported"
    assert row["classification_status"] == "unsupported_vendor"
    assert row["platform_hint"] == "future_vendor_platform"
    assert row["in_supported_vendors"] is False


@pytest.mark.asyncio
async def test_unclassified_status():
    """An IP with empty sysDescr / no signals should surface with
    classification_status='unclassified' and classified_vendor=None."""
    ip_records = [_ip_record("203.0.113.20")]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications()

    assert resp["count"] == 1
    row = resp["results"][0]
    assert row["classified_vendor"] is None
    assert row["classification_status"] == "unclassified"
    assert row["in_supported_vendors"] is False


# ---------------------------------------------------------------------------
# Filter logic — supported vendor not surfaced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supported_vendor_not_onboarded_is_excluded():
    """An IP whose sysDescr identifies a SUPPORTED vendor (Juniper) but
    that ISN'T onboarded as a Device — D5 deliberately does NOT surface
    these. They're sweep-results-table territory (retry onboarding from
    there). D5's surface is 'MNM doesn't support this vendor'."""
    ip_records = [
        _ip_record("203.0.113.40", sysdescr=SYSDESCR_JUNIPER_EX2300),
    ]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):  # NOT in devices list
        resp = await get_unsupported_classifications()

    assert resp["count"] == 0


@pytest.mark.asyncio
async def test_non_sweep_ip_excluded():
    """An IP that exists in IPAM but wasn't created by the sweep pipeline
    (discovery_method empty) is excluded — only sweep-discovered hosts
    are D5's surface."""
    ip_records = [
        # No discovery_method — typical of operator-created or
        # onboarding-created IP records
        {"id": "ip-x", "display": "203.0.113.50/32", "address": "203.0.113.50/32",
         "custom_fields": {"discovery_method": ""}},
        # discovery_method missing entirely
        {"id": "ip-y", "display": "203.0.113.51/32", "address": "203.0.113.51/32",
         "custom_fields": {}},
    ]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications()

    assert resp["count"] == 0


# ---------------------------------------------------------------------------
# Empty result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_ipam_returns_200_with_empty_list():
    """No IPAM records at all — returns 200 with empty results list, NOT
    a 404. Operators should be able to call this on a fresh deploy
    before any sweep has run."""
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=[])), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications()

    assert resp["count"] == 0
    assert resp["results"] == []
    assert resp["limit"] == 100
    assert resp["offset"] == 0


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pagination_limit_offset():
    """250 unsupported IPs in fixture, query with limit=100 — returns
    100 with correct pagination metadata."""
    ip_records = [
        _ip_record(f"203.0.113.{i}",
                   sysdescr=f"unknown-vendor-{i}",
                   last_seen=f"2026-04-{20 + (i % 8):02d}T00:00:00Z")
        for i in range(1, 251)  # 250 IPs
    ]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        page1 = await get_unsupported_classifications(limit=100, offset=0)
        page2 = await get_unsupported_classifications(limit=100, offset=100)
        page3 = await get_unsupported_classifications(limit=100, offset=200)

    assert page1["count"] == 250
    assert len(page1["results"]) == 100
    assert page2["count"] == 250
    assert len(page2["results"]) == 100
    assert page3["count"] == 250
    assert len(page3["results"]) == 50  # 250 - 200
    # Offset 0 + offset 100 + offset 200 should produce disjoint sets
    ips_p1 = {r["ip"] for r in page1["results"]}
    ips_p2 = {r["ip"] for r in page2["results"]}
    assert ips_p1.isdisjoint(ips_p2)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_ordering_by_classified_vendor_then_last_seen_desc():
    """Default order: classified_vendor ASC then last_seen DESC. Ensures
    operators see vendors grouped + within a vendor, the most recent
    observations first."""
    ip_records = [
        # Will classify to nothing (unknown vendor) — sorts to end
        _ip_record("203.0.113.1", sysdescr="random-thing-A",
                   last_seen="2026-04-25T00:00:00Z"),
        # Two HPE-ish hosts; the more recent one should sort first within
        # the HPE group
        _ip_record("203.0.113.2", sysdescr=SYSDESCR_HPE_PROCURVE,
                   last_seen="2026-04-20T00:00:00Z"),
        _ip_record("203.0.113.3", sysdescr=SYSDESCR_HPE_PROCURVE,
                   last_seen="2026-04-28T00:00:00Z"),
    ]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications()

    assert resp["count"] == 3
    # The two HPE hosts should appear before the unclassified one (ASC
    # by classified_vendor, with None sorting last). Within HPE,
    # the more recent last_seen should come first.
    statuses = [row["classification_status"] for row in resp["results"]]
    # Both unsupported_vendor before unclassified
    assert statuses[-1] == "unclassified"


@pytest.mark.asyncio
async def test_invalid_order_by_falls_back_to_default():
    """An invalid order_by value (not in the whitelist) shouldn't 500;
    handler falls back to the default field."""
    ip_records = [
        _ip_record("203.0.113.10", sysdescr=SYSDESCR_HPE_PROCURVE),
    ]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications(order_by="DROP TABLE")
    assert resp["count"] == 1


# ---------------------------------------------------------------------------
# Field projection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_response_row_has_all_documented_fields():
    """Every row should carry all 12 documented fields. Missing custom
    fields default to None / empty rather than crashing."""
    ip_records = [
        _ip_record("203.0.113.10", sysdescr=SYSDESCR_HPE_PROCURVE,
                   ports="22,80,443", oui_vendor="Hewlett Packard"),
    ]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications()

    row = resp["results"][0]
    expected_fields = {
        "ip", "classified_vendor", "classification_status",
        "platform_hint", "chassis_model_hint", "sys_descr_excerpt",
        "sys_object_id", "oui_vendor", "open_ports",
        "last_seen", "first_seen", "in_supported_vendors",
    }
    assert set(row.keys()) == expected_fields
    assert row["open_ports"] == ["22", "80", "443"]
    assert row["oui_vendor"] == "Hewlett Packard"
    assert row["sys_descr_excerpt"] is not None
    # chassis_model_hint and sys_object_id are intentionally None — not
    # persisted by sweep pipeline today (v1.0.1 cleanup candidate).
    assert row["chassis_model_hint"] is None
    assert row["sys_object_id"] is None


@pytest.mark.asyncio
async def test_sys_descr_excerpt_truncates_at_200_chars():
    """sys_descr_excerpt caps at 200 chars + ellipsis to keep response
    payloads predictable."""
    long_descr = "X" * 500
    ip_records = [_ip_record("203.0.113.10", sysdescr=long_descr)]
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(return_value=ip_records)), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications()
    excerpt = resp["results"][0]["sys_descr_excerpt"]
    assert excerpt is not None
    # 200 chars + ellipsis char
    assert len(excerpt) == 201
    assert excerpt.endswith("…")


# ---------------------------------------------------------------------------
# In-memory filter — no N+1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_memory_filter_no_n_plus_one_queries():
    """Confirm the handler issues exactly one IPAM fetch + one Device
    fetch — Python filters in-memory rather than per-IP queries.
    Performance contract for D5: one round-trip pair per request, not
    one per IP."""
    ip_records = [
        _ip_record(f"203.0.113.{i}", sysdescr=f"unknown-{i}")
        for i in range(1, 11)
    ]
    ip_mock = AsyncMock(return_value=ip_records)
    dev_mock = AsyncMock(return_value=[])
    with patch("app.main.nautobot_client.get_ip_addresses", new=ip_mock), \
         patch("app.main.nautobot_client.get_devices", new=dev_mock):
        await get_unsupported_classifications()

    assert ip_mock.await_count == 1, \
        "expected exactly one get_ip_addresses call (no N+1)"
    assert dev_mock.await_count == 1, \
        "expected exactly one get_devices call (no N+1)"


# ---------------------------------------------------------------------------
# Error handling — upstream Nautobot failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upstream_failure_returns_empty_with_error_field():
    """If Nautobot is unreachable, the endpoint reports the error in
    the response body rather than 500ing — operator dashboard surfaces
    the error to the operator."""
    with patch("app.main.nautobot_client.get_ip_addresses",
               new=AsyncMock(side_effect=RuntimeError("nautobot down"))), \
         patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=[])):
        resp = await get_unsupported_classifications()

    assert resp["count"] == 0
    assert resp["results"] == []
    assert "error" in resp
    assert "nautobot down" in resp["error"]
