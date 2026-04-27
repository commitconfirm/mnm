"""Block C P5 — unit tests for SNMP-based LLDP collection in polling.

Covers the new :func:`app.polling.collect_lldp` (replaced the NAPALM path):

- Happy path: 3 LldpNeighbor across 2 local ifaces → upsert receives
  the {iface: [neighbor_dict, ...]} dict shape with all 5 expansion
  fields populated.
- ifIndex resolution miss: local_port_ifindex not in name_map →
  group key is ``ifindex:N`` sentinel; ``local_port_name`` field falls
  to ``LldpNeighbor.local_port_name`` if set, else the sentinel.
- ``lldp_snmp.collect_lldp`` raises SnmpTimeoutError → row failure;
  no upsert.
- ``collect_ifindex_to_name`` returns {} → all groups get sentinels;
  upsert still runs; row marked SUCCESS.
- Empty neighbor list → upsert called with {}; row marked SUCCESS.
- All 5 expansion fields populated on LldpNeighbor → all 5 reach the
  upsert dict.
- Subtype/description fields = None → None passes through unchanged.
- Multiple neighbors on same local interface → grouped under one key.
- Credential leak guard: snmp_community never appears in any log record.
- Status-gating non-duplication: collect_lldp doesn't read Nautobot
  status (gate lives in poll_loop).

Tests bypass the DB by patching ``polling._mark_attempt`` /
``polling._mark_success`` / ``polling._mark_failure`` to no-ops, and
patch ``endpoint_store.upsert_node_lldp_bulk`` so the suite is
host-runnable (no asyncpg / Postgres dependency).
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
import app.lldp_snmp as _lldp_snmp_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Test fixtures — realistic LldpNeighbor data (Junos-flavored)
# ---------------------------------------------------------------------------

def _make_neighbors():
    """3 LldpNeighbor across 2 local interfaces (ge-0/0/0 has 2 neighbors,
    ge-0/0/1 has 1). Mix of subtypes — mac_address chassis IDs (typical
    for Junos), interface_name port IDs."""
    from app.lldp_snmp import LldpNeighbor
    return [
        LldpNeighbor(
            local_port_ifindex=501,
            local_port_name="ge-0/0/0",
            remote_chassis_id="aa:bb:cc:00:00:01",
            remote_chassis_id_subtype="mac_address",
            remote_port_id="ge-0/0/12",
            remote_port_id_subtype="interface_name",
            remote_system_name="ex2300-24p",
            remote_system_description=("Juniper Networks, Inc. ex2300-24p Ethernet "
                                      "Switch, kernel JUNOS 22.4R3.25"),
            management_ip="192.0.2.10",
        ),
        LldpNeighbor(
            local_port_ifindex=501,
            local_port_name="ge-0/0/0",
            remote_chassis_id="aa:bb:cc:00:00:02",
            remote_chassis_id_subtype="mac_address",
            remote_port_id="ge-0/0/0",
            remote_port_id_subtype="interface_name",
            remote_system_name="ex3300-24p",
            remote_system_description="Juniper Networks, Inc. ex3300-24p Ethernet Switch",
            management_ip="192.0.2.11",
        ),
        LldpNeighbor(
            local_port_ifindex=502,
            local_port_name="ge-0/0/1",
            remote_chassis_id="aa:bb:cc:00:00:03",
            remote_chassis_id_subtype="mac_address",
            remote_port_id="ge-0/0/47",
            remote_port_id_subtype="interface_name",
            remote_system_name="ex4300-48t",
            remote_system_description=None,  # Some neighbors don't advertise sys-desc
            management_ip=None,  # Some neighbors don't advertise mgmt addr
        ),
    ]


def _name_map_full():
    return {501: "ge-0/0/0", 502: "ge-0/0/1"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_collect_lldp_happy_path_grouped_by_interface():
    """3 neighbors across 2 ifaces → upsert receives dict shape with
    grouped entries. All 5 expansion fields populated."""
    from app import polling, endpoint_store

    captured_kwargs = {}

    async def fake_upsert(node_name, lldp_data):
        captured_kwargs["node_name"] = node_name
        captured_kwargs["lldp_data"] = lldp_data
        return sum(len(v) for v in lldp_data.values())

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=_make_neighbors())), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=_name_map_full())), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_lldp("ex2300-24p", "device-uuid",
                                            device_ip="192.0.2.1")

    assert result["success"] is True
    assert result["count"] == 3
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0

    lldp_data = captured_kwargs["lldp_data"]
    assert set(lldp_data.keys()) == {"ge-0/0/0", "ge-0/0/1"}
    assert len(lldp_data["ge-0/0/0"]) == 2  # two neighbors on ge-0/0/0
    assert len(lldp_data["ge-0/0/1"]) == 1

    # Verify the first ge-0/0/0 neighbor carries all 5 expansion fields
    n0 = lldp_data["ge-0/0/0"][0]
    assert n0["local_port_ifindex"] == 501
    assert n0["local_port_name"] == "ge-0/0/0"
    assert n0["remote_chassis_id_subtype"] == "mac_address"
    assert n0["remote_port_id_subtype"] == "interface_name"
    assert "Juniper" in n0["remote_system_description"]

    # And legacy NAPALM-shape fields too
    assert n0["remote_system_name"] == "ex2300-24p"
    assert n0["remote_port"] == "ge-0/0/12"
    assert n0["remote_chassis_id"] == "aa:bb:cc:00:00:01"
    assert n0["remote_management_ip"] == "192.0.2.10"


async def test_collect_lldp_subtype_and_description_none_pass_through():
    """When neighbor's remote_system_description / management_ip are None,
    the upsert dict reflects that (description=None, mgmt_ip empty string
    via `or ''`)."""
    from app import polling, endpoint_store

    captured = {}

    async def fake_upsert(node_name, lldp_data):
        captured["lldp_data"] = lldp_data
        return 1

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=[_make_neighbors()[2]])), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=_name_map_full())), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    n = captured["lldp_data"]["ge-0/0/1"][0]
    # None passes through for nullable expansion fields
    assert n["remote_system_description"] is None
    # management_ip None coerces to empty string for the legacy field
    assert n["remote_management_ip"] == ""


# ---------------------------------------------------------------------------
# ifIndex resolution miss
# ---------------------------------------------------------------------------

async def test_collect_lldp_unresolved_ifindex_uses_sentinel_group_key():
    """Local ifIndex not in name_map → group key is ``ifindex:N``.
    ``local_port_name`` field falls back to ``LldpNeighbor.local_port_name``
    if the collector pre-resolved it; otherwise to the sentinel."""
    from app import polling, endpoint_store
    from app.lldp_snmp import LldpNeighbor

    n = LldpNeighbor(
        local_port_ifindex=999,
        local_port_name=None,  # collector couldn't resolve either
        remote_chassis_id="aa:bb:cc:dd:ee:ff",
        remote_chassis_id_subtype="mac_address",
        remote_port_id="ge-0/0/47",
        remote_port_id_subtype="interface_name",
        remote_system_name="some-switch",
        remote_system_description=None,
        management_ip=None,
    )
    captured = {}

    async def fake_upsert(node_name, lldp_data):
        captured["lldp_data"] = lldp_data
        return 1

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=[n])), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is True
    assert mark_ok.await_count == 1
    # Group key uses sentinel
    assert "ifindex:999" in captured["lldp_data"]
    # local_port_name falls back to the sentinel since LldpNeighbor.local_port_name was None
    assert captured["lldp_data"]["ifindex:999"][0]["local_port_name"] == "ifindex:999"


async def test_collect_lldp_ifindex_unresolved_but_collector_pre_resolved_local_name():
    """Edge case: local_port_ifindex 7777 not in our name_map, but the
    LLDP collector pre-resolved local_port_name (because it ran its own
    ifindex walk). The group key uses the sentinel (it's our resolution
    that informs grouping), but local_port_name preserves the
    collector-supplied value."""
    from app import polling, endpoint_store
    from app.lldp_snmp import LldpNeighbor

    n = LldpNeighbor(
        local_port_ifindex=7777,
        local_port_name="xe-0/1/0",  # collector resolved
        remote_chassis_id="aa:bb:cc:dd:ee:00",
        remote_chassis_id_subtype="mac_address",
        remote_port_id="Ethernet1",
        remote_port_id_subtype="interface_name",
        remote_system_name="arista-leaf-1",
        remote_system_description="Arista vEOS",
        management_ip="198.51.100.5",
    )
    captured = {}

    async def fake_upsert(node_name, lldp_data):
        captured["lldp_data"] = lldp_data
        return 1

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=[n])), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    # Group key: sentinel (the polling-level lookup couldn't resolve)
    assert "ifindex:7777" in captured["lldp_data"]
    # But local_port_name preserved from the collector's own resolution
    assert captured["lldp_data"]["ifindex:7777"][0]["local_port_name"] == "xe-0/1/0"


# ---------------------------------------------------------------------------
# collect_lldp fails (SnmpTimeoutError)
# ---------------------------------------------------------------------------

async def test_collect_lldp_snmp_timeout_marks_failure():
    """SnmpTimeoutError from lldp_snmp.collect_lldp marks the poll row
    failed and returns success=False with the exception class in
    last_error. Resolution + upsert do NOT run after the failure."""
    from app import polling, endpoint_store
    from app.snmp_collector import SnmpTimeoutError

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(side_effect=SnmpTimeoutError("device unreachable"))), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock()) as ifindex_mock, \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock()) as upsert_mock, \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is False
    assert "SnmpTimeoutError" in result["error"]
    assert mark_fail.await_count == 1
    assert mark_ok.await_count == 0
    ifindex_mock.assert_not_awaited()
    upsert_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# collect_ifindex_to_name returns {} → all sentinels, still SUCCESS
# ---------------------------------------------------------------------------

async def test_collect_lldp_empty_name_map_falls_back_to_sentinel_and_succeeds():
    """When collect_ifindex_to_name returns {} (it swallows SnmpError
    internally), every group key is a sentinel; the poll row is still
    SUCCESS — we have LLDP data, just couldn't name local ports."""
    from app import polling, endpoint_store

    captured = {}

    async def fake_upsert(node_name, lldp_data):
        captured["lldp_data"] = lldp_data
        return sum(len(v) for v in lldp_data.values())

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=_make_neighbors())), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is True
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0
    # All group keys are sentinels
    assert all(k.startswith("ifindex:") for k in captured["lldp_data"])


# ---------------------------------------------------------------------------
# Empty neighbor list
# ---------------------------------------------------------------------------

async def test_collect_lldp_empty_neighbors_succeeds():
    """No LLDP neighbors → upsert called with {}, success=True, count=0.
    Common on devices with LLDP disabled or new in the lab."""
    from app import polling, endpoint_store

    upsert_mock = AsyncMock(return_value=0)
    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=[])), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=upsert_mock), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()) as mark_ok, \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    assert result["success"] is True
    assert result["count"] == 0
    assert mark_ok.await_count == 1
    assert mark_fail.await_count == 0
    upsert_mock.assert_awaited_once_with("dev", {})


# ---------------------------------------------------------------------------
# Multiple neighbors on same local interface (LLDP allows this)
# ---------------------------------------------------------------------------

async def test_collect_lldp_multiple_neighbors_same_local_iface_grouped_under_one_key():
    """LLDP allows multiple neighbors per local port (e.g. through a hub
    or a misconfigured shared segment). Both must group under one key."""
    from app import polling, endpoint_store
    from app.lldp_snmp import LldpNeighbor

    neighbors = [
        LldpNeighbor(local_port_ifindex=501, local_port_name="ge-0/0/0",
                     remote_chassis_id="aa:bb:cc:00:00:01",
                     remote_chassis_id_subtype="mac_address",
                     remote_port_id="ge-0/0/12",
                     remote_port_id_subtype="interface_name",
                     remote_system_name="neighbor-a",
                     remote_system_description=None, management_ip=None),
        LldpNeighbor(local_port_ifindex=501, local_port_name="ge-0/0/0",
                     remote_chassis_id="aa:bb:cc:00:00:02",
                     remote_chassis_id_subtype="mac_address",
                     remote_port_id="Eth1/1",
                     remote_port_id_subtype="interface_name",
                     remote_system_name="neighbor-b",
                     remote_system_description=None, management_ip=None),
    ]
    captured = {}

    async def fake_upsert(node_name, lldp_data):
        captured["lldp_data"] = lldp_data
        return 2

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=neighbors)), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={501: "ge-0/0/0"})), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    assert result["count"] == 2
    assert list(captured["lldp_data"].keys()) == ["ge-0/0/0"]
    assert len(captured["lldp_data"]["ge-0/0/0"]) == 2
    sys_names = {n["remote_system_name"] for n in captured["lldp_data"]["ge-0/0/0"]}
    assert sys_names == {"neighbor-a", "neighbor-b"}


# ---------------------------------------------------------------------------
# No device IP → fail-fast
# ---------------------------------------------------------------------------

async def test_collect_lldp_no_device_ip_marks_failure():
    """Calling without device_ip is a programming error from the
    dispatcher — collector marks failure and returns clean error string,
    never invokes lldp_snmp.collect_lldp."""
    from app import polling

    lldp_mock = AsyncMock()
    with patch.object(polling.lldp_snmp, "collect_lldp", new=lldp_mock), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()) as mark_fail:
        result = await polling.collect_lldp("dev", "uuid", device_ip=None)
    assert result["success"] is False
    assert "no primary_ip4" in result["error"]
    lldp_mock.assert_not_awaited()
    assert mark_fail.await_count == 1


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------

async def test_collect_lldp_community_string_not_logged(caplog, monkeypatch):
    """The configured SNMP_COMMUNITY must never appear in any log record
    raised by the collector."""
    from app import polling, endpoint_store

    secret = "TOTALLY-SECRET-LLDP-COMMUNITY-99999"
    monkeypatch.setenv("SNMP_COMMUNITY", secret)
    caplog.set_level(logging.DEBUG, logger="app.polling")

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=_make_neighbors())), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value=_name_map_full())), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(return_value=3)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    joined = "\n".join(r.getMessage() for r in caplog.records)
    joined += "\n" + "\n".join(
        str(getattr(r, "mnm_context", "")) for r in caplog.records
    )
    assert secret not in joined


# ---------------------------------------------------------------------------
# Status-gate non-duplication
# ---------------------------------------------------------------------------

async def test_collect_lldp_does_not_consult_nautobot_status():
    """Belt-and-suspenders: the Nautobot status gate is in poll_loop
    (Prompt 9), not collect_lldp. Verify the collector doesn't read or
    call any status-related Nautobot helper."""
    from app import polling, endpoint_store, nautobot_client

    sentinels = {}
    for fn_name in ("get_devices", "get_status_by_name", "set_device_status"):
        if hasattr(nautobot_client, fn_name):
            sentinels[fn_name] = AsyncMock(return_value=None)

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=[])), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={})), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(return_value=0)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        with patch.multiple(nautobot_client, **sentinels):
            await polling.collect_lldp("dev", "uuid", device_ip="192.0.2.1")

    for name, mock_fn in sentinels.items():
        assert not mock_fn.await_count, \
            f"collect_lldp called nautobot_client.{name} — should be gate-free"


# ---------------------------------------------------------------------------
# Dedup discipline — (local_iface, sys_name, remote_port) collisions
# ---------------------------------------------------------------------------

async def test_collect_lldp_deduplicates_unique_constraint_keys():
    """Defensive dedup matching ``uq_lldp_node_iface_remote``
    (node_name, local_interface, remote_system_name, remote_port). Two
    LldpNeighbor rows on the same local port with sys_name=None and the
    same remote_port_id collapse to identical keys after `or ''`
    coercion — observed live on ex4300-48t ge-0/0/24 (two unmanaged
    neighbors sharing a port-MAC identifier but with distinct chassis
    IDs). Without dedup, PostgreSQL ON CONFLICT raises
    CardinalityViolationError and the entire batch is dropped via the
    BLE001 guard; the dedup keeps the first neighbor seen so the row
    lands cleanly."""
    from app import polling, endpoint_store
    from app.lldp_snmp import LldpNeighbor

    # Two neighbors on ge-0/0/24, both sys_name=None, identical port_id,
    # different chassis_id. After `or ''` coercion both keys become
    # ('ge-0/0/24', '', '1a:60:41:42:13:0b').
    neighbors = [
        LldpNeighbor(
            local_port_ifindex=544,
            local_port_name="ge-0/0/24",
            remote_chassis_id="18:60:41:42:13:0b",
            remote_chassis_id_subtype="mac_address",
            remote_port_id="1a:60:41:42:13:0b",
            remote_port_id_subtype="mac_address",
            remote_system_name=None,
            remote_system_description=None,
            management_ip=None,
        ),
        LldpNeighbor(
            local_port_ifindex=544,
            local_port_name="ge-0/0/24",
            remote_chassis_id="18:60:41:42:13:10",  # different chassis
            remote_chassis_id_subtype="mac_address",
            remote_port_id="1a:60:41:42:13:0b",     # SAME port_id
            remote_port_id_subtype="mac_address",
            remote_system_name=None,
            remote_system_description=None,
            management_ip=None,
        ),
    ]
    captured = {}

    async def fake_upsert(node_name, lldp_data):
        captured["lldp_data"] = lldp_data
        return sum(len(v) for v in lldp_data.values())

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=neighbors)), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={544: "ge-0/0/24"})), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_lldp("ex4300-48t", "uuid",
                                            device_ip="172.21.140.5")

    # 2 raw → 1 deduped
    assert result["count"] == 1
    assert len(captured["lldp_data"]["ge-0/0/24"]) == 1
    # First-seen wins — chassis ID 18:60:41:42:13:0b kept.
    kept = captured["lldp_data"]["ge-0/0/24"][0]
    assert kept["remote_chassis_id"] == "18:60:41:42:13:0b"


async def test_collect_lldp_distinct_remote_port_not_deduped():
    """Two neighbors on the same local port with sys_name=None but
    DIFFERENT remote_port_id stay as separate rows. Belt-and-suspenders
    against an over-aggressive dedup (e.g. accidentally keying on
    local_iface alone)."""
    from app import polling, endpoint_store
    from app.lldp_snmp import LldpNeighbor

    neighbors = [
        LldpNeighbor(
            local_port_ifindex=544, local_port_name="ge-0/0/24",
            remote_chassis_id="18:60:41:42:13:0b",
            remote_chassis_id_subtype="mac_address",
            remote_port_id="1a:60:41:42:13:0b",
            remote_port_id_subtype="mac_address",
            remote_system_name=None, remote_system_description=None,
            management_ip=None,
        ),
        LldpNeighbor(
            local_port_ifindex=544, local_port_name="ge-0/0/24",
            remote_chassis_id="18:60:41:42:13:10",
            remote_chassis_id_subtype="mac_address",
            remote_port_id="1a:60:41:42:13:11",  # DIFFERENT port_id
            remote_port_id_subtype="mac_address",
            remote_system_name=None, remote_system_description=None,
            management_ip=None,
        ),
    ]
    captured = {}

    async def fake_upsert(node_name, lldp_data):
        captured["lldp_data"] = lldp_data
        return sum(len(v) for v in lldp_data.values())

    with patch.object(polling.lldp_snmp, "collect_lldp",
                      new=AsyncMock(return_value=neighbors)), \
         patch.object(polling.snmp_collector, "collect_ifindex_to_name",
                      new=AsyncMock(return_value={544: "ge-0/0/24"})), \
         patch.object(endpoint_store, "upsert_node_lldp_bulk",
                      new=AsyncMock(side_effect=fake_upsert)), \
         patch.object(polling, "_mark_attempt", new=AsyncMock()), \
         patch.object(polling, "_mark_success", new=AsyncMock()), \
         patch.object(polling, "_mark_failure", new=AsyncMock()):
        result = await polling.collect_lldp("dev", "uuid",
                                            device_ip="192.0.2.1")

    assert result["count"] == 2
    assert len(captured["lldp_data"]["ge-0/0/24"]) == 2


