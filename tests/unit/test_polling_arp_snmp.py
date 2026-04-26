"""Block C P3 — unit tests for SNMP-based ARP collection in polling.

Covers the new :func:`app.polling.collect_arp` (replaced the NAPALM path):

- Happy path: ArpEntry list with all ifIndex resolvable → upsert receives
  resolved interface names.
- ifIndex resolution miss: entries with unknown ifIndex get the
  ``ifindex:N`` sentinel.
- Empty ARP table: no entries collected → upsert called with [].
- ``arp_snmp.collect_arp`` raises ``SnmpTimeoutError`` → row failure.
- ``collect_ifindex_to_name`` returns {} (it swallows errors internally) →
  graceful degradation, all entries get ``ifindex:N`` sentinels, polling
  row marked SUCCESS (we got useful data, just unresolved interfaces).
- Credential hygiene: snmp_community must not appear in any log record.
- ``_collect_arp_napalm_deprecated`` is still defined in the module —
  belt-and-suspenders for the P6 deletion target.
- Status-gating regression check: the gate is enforced in ``poll_loop``,
  not ``collect_arp``; the ARP collector itself doesn't read status.
  Verified by direct call: collect_arp does not consult Nautobot status.

Tests bypass the DB by patching ``polling._mark_attempt`` /
``polling._mark_success`` / ``polling._mark_failure`` to no-ops, since
the upsert path is also patched at ``endpoint_store``. This keeps the
suite host-runnable (no asyncpg / Postgres dependency).
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
import app.arp_snmp as _arp_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Test fixtures — realistic ArpEntry data (Junos-flavored interface names)
# ---------------------------------------------------------------------------

def _make_arp_entries():
    from app.arp_snmp import ArpEntry
    return [
        ArpEntry(ip_address="192.0.2.10", mac_address="aa:bb:cc:00:00:01",
                 interface_index=501, entry_type="dynamic"),
        ArpEntry(ip_address="192.0.2.11", mac_address="aa:bb:cc:00:00:02",
                 interface_index=502, entry_type="dynamic"),
        ArpEntry(ip_address="192.0.2.12", mac_address="aa:bb:cc:00:00:03",
                 interface_index=502, entry_type="dynamic"),
        ArpEntry(ip_address="198.51.100.5", mac_address="aa:bb:cc:00:00:04",
                 interface_index=601, entry_type="static"),
        ArpEntry(ip_address="198.51.100.6", mac_address="aa:bb:cc:00:00:05",
                 interface_index=999, entry_type="dynamic"),  # unresolved
    ]


def _name_map_partial():
    """ifIndex → name map missing ifIndex 999 (the unresolved case)."""
    return {
        501: "ge-0/0/0",
        502: "ge-0/0/1",
        601: "irb.140",
        # 999 absent → produces ifindex:999 sentinel
    }


def _name_map_full():
    return {**_name_map_partial(), 999: "ge-0/0/47"}


# ---------------------------------------------------------------------------
# Mark helpers + DB patcher
# ---------------------------------------------------------------------------

def _patch_polling_db_and_marks():
    """Return a dict of context managers that patch the DB-touching helpers
    so tests don't need a live Postgres."""
    from app import polling
    return [
        patch.object(polling, "_mark_attempt", new=AsyncMock(return_value=None)),
        patch.object(polling, "_mark_success", new=AsyncMock(return_value=None)),
        patch.object(polling, "_mark_failure", new=AsyncMock(return_value=None)),
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_collect_arp_happy_path_all_ifindex_resolved():
    """5 entries, 4 ifIndex resolvable, 1 sentinel — all upserted."""
    from app import polling, endpoint_store

    captured_entries = []

    async def fake_upsert(node_name, entries):
        captured_entries.extend(entries)
        return len(entries)

    arp_entries = _make_arp_entries()
    name_map = _name_map_partial()  # ifindex 999 missing → sentinel

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=arp_entries)), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=name_map)), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_arp("ex2300-24p", "device-uuid",
                                           device_ip="192.0.2.1")
    assert result["success"] is True
    assert result["count"] == 5
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0

    assert len(captured_entries) == 5
    by_ip = {e["ip"]: e for e in captured_entries}
    assert by_ip["192.0.2.10"]["interface"] == "ge-0/0/0"
    assert by_ip["192.0.2.11"]["interface"] == "ge-0/0/1"
    assert by_ip["192.0.2.12"]["interface"] == "ge-0/0/1"
    assert by_ip["198.51.100.5"]["interface"] == "irb.140"
    # The unresolved one falls back to sentinel.
    assert by_ip["198.51.100.6"]["interface"] == "ifindex:999"
    # MAC values pass through as-is.
    assert by_ip["192.0.2.10"]["mac"] == "aa:bb:cc:00:00:01"


async def test_collect_arp_all_ifindex_resolvable_no_sentinels():
    """If every ifIndex resolves, no sentinel strings appear."""
    from app import polling, endpoint_store

    captured_entries = []

    async def fake_upsert(node_name, entries):
        captured_entries.extend(entries)
        return len(entries)

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=_make_arp_entries())), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=_name_map_full())), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_arp("ex2300-24p", "uuid", device_ip="192.0.2.1")
    assert all("ifindex:" not in e["interface"] for e in captured_entries)


# ---------------------------------------------------------------------------
# ifIndex resolution miss → sentinel
# ---------------------------------------------------------------------------

async def test_collect_arp_unresolved_ifindex_uses_sentinel():
    """Entries whose ifIndex isn't in the name_map land as ifindex:N."""
    from app import polling, endpoint_store
    from app.arp_snmp import ArpEntry

    arp_entry = [ArpEntry(ip_address="192.0.2.99", mac_address="aa:bb:cc:dd:ee:ff",
                          interface_index=12345, entry_type="dynamic")]
    captured = []
    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=arp_entry)), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_arp("dev", "uuid", device_ip="192.0.2.1")
    assert result["success"] is True
    assert captured[0]["interface"] == "ifindex:12345"


# ---------------------------------------------------------------------------
# Empty ARP table
# ---------------------------------------------------------------------------

async def test_collect_arp_empty_table_succeeds():
    """No ARP entries → upsert called with [], success=True, count=0."""
    from app import polling, endpoint_store

    upsert_mock = AsyncMock(return_value=0)
    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=[])), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=upsert_mock), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_arp("dev", "uuid", device_ip="192.0.2.1")
    assert result["success"] is True
    assert result["count"] == 0
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0
    upsert_mock.assert_awaited_once_with("dev", [])


# ---------------------------------------------------------------------------
# SNMP error from collect_arp → row failure
# ---------------------------------------------------------------------------

async def test_collect_arp_snmp_timeout_marks_failure():
    """SnmpTimeoutError from arp_snmp.collect_arp marks the poll row failed
    and returns success=False with the exception class in last_error."""
    from app import polling, endpoint_store
    from app.snmp_collector import SnmpTimeoutError

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(side_effect=SnmpTimeoutError("device unreachable"))), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock()) as ifindex_mock, \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock()) as upsert_mock, \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_arp("dev", "uuid", device_ip="192.0.2.1")
    assert result["success"] is False
    assert "SnmpTimeoutError" in result["error"]
    assert mark_fail.await_count == 1
    assert mark_ok.await_count == 0
    # ifindex resolution + upsert must NOT have run after the failure.
    ifindex_mock.assert_not_awaited()
    upsert_mock.assert_not_awaited()


async def test_collect_arp_snmp_auth_error_marks_failure():
    """Same path for SnmpAuthError — community rejected by device."""
    from app import polling, endpoint_store
    from app.snmp_collector import SnmpAuthError

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(side_effect=SnmpAuthError("bad community"))), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock()), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_arp("dev", "uuid", device_ip="192.0.2.1")
    assert result["success"] is False
    assert "SnmpAuthError" in result["error"]
    assert mark_fail.await_count == 1


# ---------------------------------------------------------------------------
# ifindex map empty (collect_ifindex_to_name swallows errors → returns {})
# ---------------------------------------------------------------------------

async def test_collect_arp_empty_name_map_falls_back_to_sentinel_and_succeeds():
    """When collect_ifindex_to_name returns {} (it swallows SnmpError
    internally), every entry uses the ifindex:N sentinel and the poll
    row is still marked SUCCESS — we got the ARP data, just couldn't
    name the ports."""
    from app import polling, endpoint_store

    captured = []
    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=_make_arp_entries())), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_arp("dev", "uuid", device_ip="192.0.2.1")
    assert result["success"] is True
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0
    assert all(e["interface"].startswith("ifindex:") for e in captured)


# ---------------------------------------------------------------------------
# No device IP → fail-fast
# ---------------------------------------------------------------------------

async def test_collect_arp_no_device_ip_marks_failure():
    """Calling without a device_ip is a programming error from the
    dispatcher — collector marks failure and returns clean error string,
    never invokes arp_snmp.collect_arp."""
    from app import polling

    arp_mock = AsyncMock()
    with patch.object(polling.arp_snmp, "collect_arp", new=arp_mock), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_arp("dev", "uuid", device_ip=None)
    assert result["success"] is False
    assert "no primary_ip4" in result["error"]
    arp_mock.assert_not_awaited()
    assert mark_fail.await_count == 1


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------

async def test_collect_arp_community_string_not_logged(caplog, monkeypatch):
    """The configured SNMP_COMMUNITY must never appear in any log record
    raised by the collector."""
    from app import polling, endpoint_store

    secret = "TOTALLY-SECRET-COMMUNITY-12345"
    monkeypatch.setenv("SNMP_COMMUNITY", secret)
    caplog.set_level(logging.DEBUG, logger="app.polling")

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=_make_arp_entries())), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=_name_map_full())), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock(return_value=5)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_arp("dev", "uuid", device_ip="192.0.2.1")

    joined = "\n".join(r.getMessage() for r in caplog.records)
    joined += "\n" + "\n".join(
        str(getattr(r, "mnm_context", "")) for r in caplog.records
    )
    assert secret not in joined


# ---------------------------------------------------------------------------
# (ip, mac) dedup — Junos lo0.X multi-subinterface gotcha
# ---------------------------------------------------------------------------

async def test_collect_arp_deduplicates_ip_mac_keys():
    """Junos exposes loopback IPs on multiple lo0.X subinterfaces, producing
    SNMP rows with the same (ip, mac) pair but different ifIndex values.
    The unique constraint on node_arp_entries is (node_name, ip, mac, vrf) —
    PostgreSQL's ON CONFLICT DO UPDATE raises CardinalityViolationError when
    a single batch contains the same constrained tuple twice. The adapter
    must dedupe by (ip, mac) before handing to upsert. Keep first interface
    seen — matches NAPALM's implicit behavior."""
    from app import polling, endpoint_store
    from app.arp_snmp import ArpEntry

    # 4 raw entries, 2 distinct (ip, mac) keys (zero-MAC + 10.0.0.1 collide
    # across lo0.16385 / lo0.16386; 128.0.0.1 collides similarly).
    arp_entries = [
        ArpEntry(ip_address="10.0.0.1", mac_address="00:00:00:00:00:00",
                 interface_index=22, entry_type="static"),
        ArpEntry(ip_address="10.0.0.1", mac_address="00:00:00:00:00:00",
                 interface_index=23, entry_type="static"),
        ArpEntry(ip_address="128.0.0.1", mac_address="00:00:00:00:00:00",
                 interface_index=22, entry_type="static"),
        ArpEntry(ip_address="128.0.0.1", mac_address="00:00:00:00:00:00",
                 interface_index=24, entry_type="static"),
    ]
    name_map = {22: "lo0.16384", 23: "lo0.16385", 24: "lo0.16386"}
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=arp_entries)), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=name_map)), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_arp("SRX320", "uuid",
                                           device_ip="172.21.140.1")

    # 4 raw → 2 deduped
    assert result["count"] == 2
    assert len(captured) == 2
    keys = {(e["ip"], e["mac"]) for e in captured}
    assert keys == {("10.0.0.1", "00:00:00:00:00:00"),
                    ("128.0.0.1", "00:00:00:00:00:00")}
    # First-seen wins — 10.0.0.1 keeps lo0.16384, 128.0.0.1 keeps lo0.16384
    by_ip = {e["ip"]: e for e in captured}
    assert by_ip["10.0.0.1"]["interface"] == "lo0.16384"
    assert by_ip["128.0.0.1"]["interface"] == "lo0.16384"


async def test_collect_arp_dedup_case_insensitive_on_mac():
    """MAC casing must not defeat the dedup — upsert uppercases on insert,
    so the adapter must too. Otherwise (ip, 'aa:bb...') and
    (ip, 'AA:BB...') would slip through the dedup but conflict in the DB."""
    from app import polling, endpoint_store
    from app.arp_snmp import ArpEntry

    arp_entries = [
        ArpEntry(ip_address="192.0.2.1", mac_address="aa:bb:cc:00:00:01",
                 interface_index=10, entry_type="dynamic"),
        ArpEntry(ip_address="192.0.2.1", mac_address="AA:BB:CC:00:00:01",
                 interface_index=11, entry_type="dynamic"),
    ]
    captured = []

    async def fake_upsert(name, entries):
        captured.extend(entries)
        return len(entries)

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=arp_entries)), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={10: "ge-0/0/0", 11: "ge-0/0/1"})), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_arp("dev", "uuid", device_ip="192.0.2.1")
    assert result["count"] == 1
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Deprecated NAPALM body still defined (P6 deletion target)
# ---------------------------------------------------------------------------

def test_napalm_arp_function_still_defined_for_p6_deletion():
    """``_collect_arp_napalm_deprecated`` is the explicit P6 deletion
    target. Confirm it's still callable from the polling module so the
    P6 deletion has something to remove."""
    from app import polling
    assert hasattr(polling, "_collect_arp_napalm_deprecated"), \
        "P6 deletion target _collect_arp_napalm_deprecated missing"
    assert callable(polling._collect_arp_napalm_deprecated)


# ---------------------------------------------------------------------------
# Status-gating regression: collector itself doesn't read status
# ---------------------------------------------------------------------------

async def test_collect_arp_does_not_consult_nautobot_status():
    """The Nautobot status gate lives in poll_loop (Prompt 9), not in
    collect_arp. Verify the collector doesn't read or call any
    status-related Nautobot helper. Belt-and-suspenders against
    accidental gate duplication."""
    from app import polling, endpoint_store, nautobot_client

    # If collect_arp were to call any of these, the test would notice.
    sentinels = {}
    for fn_name in ("get_devices", "get_status_by_name", "set_device_status"):
        if hasattr(nautobot_client, fn_name):
            sentinels[fn_name] = AsyncMock(return_value=None)

    with patch.object(polling.arp_snmp, "collect_arp",
                      new=AsyncMock(return_value=[])), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_arp_bulk",
                      new=AsyncMock(return_value=0)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        with patch.multiple(nautobot_client, **sentinels):
            await polling.collect_arp("dev", "uuid", device_ip="192.0.2.1")

    for name, mock_fn in sentinels.items():
        assert not mock_fn.await_count, \
            f"collect_arp called nautobot_client.{name} — should be gate-free"
