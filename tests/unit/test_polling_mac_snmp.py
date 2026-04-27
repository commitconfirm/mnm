"""Block C P4 — unit tests for SNMP-based MAC collection in polling.

Covers the new :func:`app.polling.collect_mac` (replaced the NAPALM path):

- Happy path: MacEntry list with all bridge_ports → ifIndex → name resolved.
- Bridge-port resolution miss: bridge_port not in dot1dBasePortTable
  → interface == ``ifindex:<bridge_port>``.
- ifIndex resolution miss: bridge_port resolves to ifIndex, but ifIndex
  not in name_map → interface == ``ifindex:<resolved ifindex>``.
- Both walks fail (empty maps): all entries get sentinels, row still SUCCESS.
- ``mac_snmp.collect_mac`` raises SnmpTimeoutError → row failure.
- VLAN handling: real VLAN passes through; None becomes 0.
- entry_status remap: self/mgmt → static=True; learned → static=False.
- Sentinel classifier behavior: ``_is_access_interface("ifindex:N")`` is
  True; ``_infer_vlan_from_interface("ifindex:N")`` is 0. Pinned to guard
  against future drift in the classifier helpers.
- Empty MAC table: upsert called with []; success.
- Credential hygiene: SNMP_COMMUNITY never appears in any log record.
- Status-gate non-duplication: collect_mac doesn't read Nautobot status.
- Dedup discipline: (mac, interface, vlan) collisions deduped first-wins.
- ``_collect_mac_napalm_deprecated`` is still defined — P6 deletion target.

Tests bypass the DB by patching ``polling._mark_attempt`` /
``polling._mark_success`` / ``polling._mark_failure`` to no-ops, and patch
``endpoint_store.upsert_node_mac_bulk`` so the suite is host-runnable
(no asyncpg / Postgres dependency).
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload to beat sibling sys.modules.setdefault stubs.
import app.polling as _polling_preload  # noqa: E402, F401
import app.mac_snmp as _mac_snmp_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Test fixtures — realistic MacEntry data (Junos-flavored)
# ---------------------------------------------------------------------------

def _make_mac_entries():
    """5 MacEntry rows mixing dynamic + static, access + non-access ports."""
    from app.mac_snmp import MacEntry
    return [
        MacEntry(mac_address="aa:bb:cc:00:00:01", vlan=140,
                 bridge_port=1, entry_status="learned"),
        MacEntry(mac_address="aa:bb:cc:00:00:02", vlan=140,
                 bridge_port=2, entry_status="learned"),
        MacEntry(mac_address="aa:bb:cc:00:00:03", vlan=200,
                 bridge_port=3, entry_status="learned"),
        MacEntry(mac_address="aa:bb:cc:00:00:04", vlan=140,
                 bridge_port=4, entry_status="self"),  # device's own MAC
        MacEntry(mac_address="aa:bb:cc:00:00:05", vlan=300,
                 bridge_port=99, entry_status="learned"),  # bridge_port miss
    ]


def _bridge_map_partial():
    """{bridge_port: ifIndex} missing port 99 → first-stage sentinel."""
    return {
        1: 501,
        2: 502,
        3: 503,
        4: 504,
        # 99 absent → produces ifindex:99 sentinel (uses bridge_port)
    }


def _bridge_map_full():
    """All five bridge ports resolved."""
    return {**_bridge_map_partial(), 99: 599}


def _name_map_partial():
    """ifIndex → name missing 599 → second-stage sentinel for ifIndex 599."""
    return {
        501: "ge-0/0/12",
        502: "ge-0/0/13",
        503: "ae0",
        504: "irb.140",
        # 599 absent → produces ifindex:599 sentinel
    }


def _name_map_full():
    return {**_name_map_partial(), 599: "ge-0/0/47"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_collect_mac_happy_path_all_resolved():
    """5 entries, all bridge_port + ifIndex resolved → 5 upserted with names."""
    from app import polling, endpoint_store

    captured_entries = []

    async def fake_upsert(node_name, entries):
        captured_entries.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=_make_mac_entries())), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value=_bridge_map_full())), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=_name_map_full())), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_mac("ex2300-24p", "device-uuid",
                                           device_ip="192.0.2.1")

    assert result["success"] is True
    assert result["count"] == 5
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0

    assert len(captured_entries) == 5
    by_mac = {e["mac"]: e for e in captured_entries}
    assert by_mac["aa:bb:cc:00:00:01"]["interface"] == "ge-0/0/12"
    assert by_mac["aa:bb:cc:00:00:01"]["vlan"] == 140
    assert by_mac["aa:bb:cc:00:00:01"]["static"] is False  # learned

    assert by_mac["aa:bb:cc:00:00:03"]["interface"] == "ae0"
    assert by_mac["aa:bb:cc:00:00:03"]["vlan"] == 200

    # entry_status="self" → static=True
    assert by_mac["aa:bb:cc:00:00:04"]["static"] is True
    assert by_mac["aa:bb:cc:00:00:04"]["interface"] == "irb.140"

    # bridge_port=99 resolves to ifindex 599 then to ge-0/0/47
    assert by_mac["aa:bb:cc:00:00:05"]["interface"] == "ge-0/0/47"
    assert by_mac["aa:bb:cc:00:00:05"]["vlan"] == 300


# ---------------------------------------------------------------------------
# Bridge-port resolution miss (first stage)
# ---------------------------------------------------------------------------

async def test_collect_mac_unresolved_bridgeport_uses_bridgeport_sentinel():
    """bridge_port not in dot1dBasePortTable → ifindex:<bridge_port>.

    Sentinel must use the bridge_port value (not a missing ifindex), since
    that's what we have. Operators can correlate back to the FDB row by
    matching the suffix to dot1dBasePortTable manually.
    """
    from app import polling, endpoint_store
    from app.mac_snmp import MacEntry

    mac_entry = [MacEntry(mac_address="aa:bb:cc:dd:ee:ff", vlan=42,
                          bridge_port=99, entry_status="learned")]
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=mac_entry)), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=_name_map_full())), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is True
    assert mark_ok.await_count == 1
    assert captured[0]["interface"] == "ifindex:99"


# ---------------------------------------------------------------------------
# ifIndex resolution miss (second stage)
# ---------------------------------------------------------------------------

async def test_collect_mac_unresolved_ifindex_uses_ifindex_sentinel():
    """bridge_port maps to ifIndex, but ifIndex not in name_map.

    Sentinel uses the resolved ifIndex value, NOT the bridge_port — that's
    one step further along the resolution chain.
    """
    from app import polling, endpoint_store
    from app.mac_snmp import MacEntry

    mac_entry = [MacEntry(mac_address="aa:bb:cc:dd:ee:ff", vlan=42,
                          bridge_port=99, entry_status="learned")]
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=mac_entry)), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={99: 7777})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is True
    assert captured[0]["interface"] == "ifindex:7777"


# ---------------------------------------------------------------------------
# Both walks fail (empty maps): every entry is a sentinel; still SUCCESS
# ---------------------------------------------------------------------------

async def test_collect_mac_both_walks_empty_falls_back_and_succeeds():
    """When collect_bridgeport_to_ifindex AND collect_ifindex_to_name both
    return {} (they swallow errors internally), every entry uses the
    bridge_port sentinel and the poll row is still marked SUCCESS — we
    have FDB data, just couldn't resolve interface names.
    """
    from app import polling, endpoint_store

    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=_make_mac_entries())), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is True
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0
    assert all(e["interface"].startswith("ifindex:") for e in captured)


# ---------------------------------------------------------------------------
# SNMP error from collect_mac → row failure
# ---------------------------------------------------------------------------

async def test_collect_mac_snmp_timeout_marks_failure():
    """SnmpTimeoutError from mac_snmp.collect_mac marks the poll row failed
    and returns success=False with the exception class in last_error."""
    from app import polling, endpoint_store
    from app.snmp_collector import SnmpTimeoutError

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(side_effect=SnmpTimeoutError("device unreachable"))), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock()) as bridge_mock, \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock()) as ifindex_mock, \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock()) as upsert_mock, \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is False
    assert "SnmpTimeoutError" in result["error"]
    assert mark_fail.await_count == 1
    assert mark_ok.await_count == 0
    # Resolution + upsert must NOT run after the failure.
    bridge_mock.assert_not_awaited()
    ifindex_mock.assert_not_awaited()
    upsert_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# VLAN handling (None → 0)
# ---------------------------------------------------------------------------

async def test_collect_mac_vlan_none_becomes_zero():
    """MacEntry.vlan can be None (Junos pre-fallback orphan; BRIDGE-MIB
    fallback always returns None). Adapter coerces to int(0) so the upsert
    constraint (NOT NULL vlan) doesn't break."""
    from app import polling, endpoint_store
    from app.mac_snmp import MacEntry

    mac_entry = [MacEntry(mac_address="aa:bb:cc:dd:ee:ff", vlan=None,
                          bridge_port=1, entry_status="learned")]
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=mac_entry)), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={1: 501})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={501: "ge-0/0/12"})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    assert captured[0]["vlan"] == 0
    assert isinstance(captured[0]["vlan"], int)


async def test_collect_mac_vlan_real_value_passes_through():
    """Real VLAN ID from MacEntry.vlan reaches the upsert dict unchanged."""
    from app import polling, endpoint_store
    from app.mac_snmp import MacEntry

    mac_entry = [MacEntry(mac_address="aa:bb:cc:dd:ee:ff", vlan=4094,
                          bridge_port=1, entry_status="learned")]
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=mac_entry)), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={1: 501})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={501: "ge-0/0/12"})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    assert captured[0]["vlan"] == 4094


# ---------------------------------------------------------------------------
# entry_status remap (static=True for self/mgmt; False for learned)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry_status,expected_static", [
    ("learned", False),
    ("self", True),
    ("mgmt", True),
])
async def test_collect_mac_entry_status_remap(entry_status, expected_static):
    """entry_status mapping → static bool: self/mgmt are administrative,
    everything else (learned/other/invalid) is dynamic."""
    from app import polling, endpoint_store
    from app.mac_snmp import MacEntry

    mac_entry = [MacEntry(mac_address="aa:bb:cc:dd:ee:ff", vlan=10,
                          bridge_port=1, entry_status=entry_status)]
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=mac_entry)), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={1: 501})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={501: "ge-0/0/12"})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    assert captured[0]["static"] is expected_static


# ---------------------------------------------------------------------------
# Sentinel classifier behavior (pin downstream stability)
# ---------------------------------------------------------------------------

def test_sentinel_interface_classified_as_access_port():
    """The ``ifindex:N`` sentinel must classify as an access port —
    ``_NON_ACCESS_PREFIXES`` doesn't match "ifindex" and the heuristic
    defaults to True for unmatched names. P1 addendum identified this as
    the expected (acceptable) downstream behavior. If someone tightens
    ``_is_access_interface``, the sentinel-MAC correlation behavior changes
    silently — this test catches it loudly."""
    from app.endpoint_collector import _is_access_interface, _NON_ACCESS_PREFIXES
    assert _is_access_interface("ifindex:42") is True
    assert _is_access_interface("ifindex:7777") is True
    # Sanity-check that the prefix list is what we expect — if "if" or
    # "ifindex" is added later, this test fails first instead of the
    # production behavior changing in mystery.
    for p in _NON_ACCESS_PREFIXES:
        assert not "ifindex:42".startswith(p), \
            f"_NON_ACCESS_PREFIXES gained {p!r} which now matches the sentinel"


def test_sentinel_interface_yields_zero_vlan_inference():
    """``_infer_vlan_from_interface("ifindex:N")`` must return 0 — none
    of the regex patterns match. P1 addendum identified this as the
    expected fallback. Pinned so future regex tweaks don't accidentally
    parse the digits out of the sentinel."""
    from app.endpoint_collector import _infer_vlan_from_interface
    assert _infer_vlan_from_interface("ifindex:42") == 0
    assert _infer_vlan_from_interface("ifindex:7777") == 0
    assert _infer_vlan_from_interface("ifindex:1") == 0


# ---------------------------------------------------------------------------
# Empty MAC table
# ---------------------------------------------------------------------------

async def test_collect_mac_empty_table_succeeds():
    """No MAC entries → upsert called with [], success=True, count=0."""
    from app import polling, endpoint_store

    upsert_mock = AsyncMock(return_value=0)
    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=[])), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=upsert_mock), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is True
    assert result["count"] == 0
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0
    upsert_mock.assert_awaited_once_with("dev", [])


# ---------------------------------------------------------------------------
# No device IP → fail-fast
# ---------------------------------------------------------------------------

async def test_collect_mac_no_device_ip_marks_failure():
    """Calling without a device_ip is a programming error from the
    dispatcher — collector marks failure and returns clean error string,
    never invokes mac_snmp.collect_mac."""
    from app import polling

    mac_mock = AsyncMock()
    with patch.object(polling.mac_snmp, "collect_mac", new=mac_mock), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_mac("dev", "uuid", device_ip=None)
    assert result["success"] is False
    assert "no primary_ip4" in result["error"]
    mac_mock.assert_not_awaited()
    assert mark_fail.await_count == 1


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------

async def test_collect_mac_community_string_not_logged(caplog, monkeypatch):
    """The configured SNMP_COMMUNITY must never appear in any log record
    raised by the collector."""
    from app import polling, endpoint_store

    secret = "TOTALLY-SECRET-MAC-COMMUNITY-67890"
    monkeypatch.setenv("SNMP_COMMUNITY", secret)
    caplog.set_level(logging.DEBUG, logger="app.polling")

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=_make_mac_entries())), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value=_bridge_map_full())), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=_name_map_full())), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(return_value=5)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    joined = "\n".join(r.getMessage() for r in caplog.records)
    joined += "\n" + "\n".join(
        str(getattr(r, "mnm_context", "")) for r in caplog.records
    )
    assert secret not in joined


# ---------------------------------------------------------------------------
# Status-gate non-duplication (gate lives in poll_loop, not collect_mac)
# ---------------------------------------------------------------------------

async def test_collect_mac_does_not_consult_nautobot_status():
    """Belt-and-suspenders: the Nautobot status gate is in poll_loop
    (Prompt 9), not collect_mac. Verify the collector doesn't read or
    call any status-related Nautobot helper."""
    from app import polling, endpoint_store, nautobot_client

    sentinels = {}
    for fn_name in ("get_devices", "get_status_by_name", "set_device_status"):
        if hasattr(nautobot_client, fn_name):
            sentinels[fn_name] = AsyncMock(return_value=None)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=[])), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(return_value=0)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        with patch.multiple(nautobot_client, **sentinels):
            await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")

    for name, mock_fn in sentinels.items():
        assert not mock_fn.await_count, \
            f"collect_mac called nautobot_client.{name} — should be gate-free"


# ---------------------------------------------------------------------------
# Dedup discipline — (mac, interface, vlan) collisions
# ---------------------------------------------------------------------------

async def test_collect_mac_deduplicates_mac_iface_vlan_keys():
    """Defensive dedup matching ``uq_mac_node_mac_iface_vlan``. If two
    MacEntry rows resolve to the same (mac, interface, vlan) — possible
    with orphan FDB IDs that all coerce to vlan=0 — keep first seen so
    PostgreSQL ON CONFLICT doesn't raise CardinalityViolationError on
    a single-batch repeat."""
    from app import polling, endpoint_store
    from app.mac_snmp import MacEntry

    # Two entries on the same bridge_port (same interface), both vlan=0
    # because their FDB IDs are orphans (not in the VLAN map).
    mac_entries = [
        MacEntry(mac_address="aa:bb:cc:00:00:01", vlan=None,
                 bridge_port=5, entry_status="learned"),
        MacEntry(mac_address="aa:bb:cc:00:00:01", vlan=None,
                 bridge_port=5, entry_status="learned"),
    ]
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=mac_entries)), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={5: 505})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={505: "ge-0/0/4"})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_mac("SRX320", "uuid",
                                           device_ip="192.0.2.1")

    assert result["count"] == 1
    assert len(captured) == 1
    assert captured[0]["interface"] == "ge-0/0/4"
    assert captured[0]["vlan"] == 0


async def test_collect_mac_dedup_case_insensitive_on_mac():
    """MAC casing must not defeat the dedup — upsert uppercases on insert,
    so the adapter's dedup key uppercases first."""
    from app import polling, endpoint_store
    from app.mac_snmp import MacEntry

    mac_entries = [
        MacEntry(mac_address="aa:bb:cc:00:00:01", vlan=140,
                 bridge_port=1, entry_status="learned"),
        MacEntry(mac_address="AA:BB:CC:00:00:01", vlan=140,
                 bridge_port=1, entry_status="learned"),
    ]
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.mac_snmp, "collect_mac",
                      new=AsyncMock(return_value=mac_entries)), \
         patch.object(polling.snmp_collector, "collect_bridgeport_to_ifindex",
                      new=AsyncMock(return_value={1: 501})), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={501: "ge-0/0/12"})), \
         patch.object(endpoint_store, "upsert_node_mac_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_mac("dev", "uuid", device_ip="192.0.2.1")
    assert result["count"] == 1
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Deprecated NAPALM body still defined (P6 deletion target)
# ---------------------------------------------------------------------------

def test_napalm_mac_function_still_defined_for_p6_deletion():
    """``_collect_mac_napalm_deprecated`` is the explicit P6 deletion target.
    Confirm it's still callable from the polling module so the P6 deletion
    has something to remove."""
    from app import polling
    assert hasattr(polling, "_collect_mac_napalm_deprecated"), \
        "P6 deletion target _collect_mac_napalm_deprecated missing"
    assert callable(polling._collect_mac_napalm_deprecated)
