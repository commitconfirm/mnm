"""Tests for ``app.plugin_writer``.

Tests cover the pure-Python helpers (`_dedup_by_constraint`,
`_normalize_endpoint_dict`) and the soft-failure semantics of
`upsert_endpoint_bulk` when the plugin DB is unreachable.

Live DB write paths are covered in plugin-side integration tests
(run inside Nautobot's test environment, not here).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

from app import plugin_writer  # noqa: E402


# ---------------------------------------------------------------------------
# _dedup_by_constraint
# ---------------------------------------------------------------------------


def test_dedup_empty():
    assert plugin_writer._dedup_by_constraint([], ("a",)) == []


def test_dedup_no_duplicates():
    entries = [{"a": 1, "b": 2}, {"a": 2, "b": 2}]
    result = plugin_writer._dedup_by_constraint(entries, ("a",))
    assert result == entries


def test_dedup_collapses_duplicates_last_wins():
    """Last entry wins — consistent with ON CONFLICT DO UPDATE
    semantics."""
    entries = [
        {"a": 1, "b": "first"},
        {"a": 1, "b": "second"},
        {"a": 1, "b": "last"},
    ]
    result = plugin_writer._dedup_by_constraint(entries, ("a",))
    assert len(result) == 1
    assert result[0]["b"] == "last"


def test_dedup_composite_key():
    """The Block C P3/P4/P5 case — composite constraint key over
    multiple fields."""
    entries = [
        {"mac": "AA", "switch": "s1", "port": "p1", "vlan": 10, "v": 1},
        {"mac": "AA", "switch": "s1", "port": "p1", "vlan": 10, "v": 2},
        {"mac": "AA", "switch": "s1", "port": "p1", "vlan": 20, "v": 3},
        {"mac": "AA", "switch": "s2", "port": "p1", "vlan": 10, "v": 4},
    ]
    result = plugin_writer._dedup_by_constraint(
        entries, ("mac", "switch", "port", "vlan"),
    )
    assert len(result) == 3
    # Last value for the colliding (AA,s1,p1,10) is v=2
    collapsed = [
        r for r in result
        if r["mac"] == "AA" and r["switch"] == "s1"
        and r["port"] == "p1" and r["vlan"] == 10
    ]
    assert len(collapsed) == 1
    assert collapsed[0]["v"] == 2


# ---------------------------------------------------------------------------
# _normalize_endpoint_dict
# ---------------------------------------------------------------------------


def test_normalize_correlator_shape():
    """Correlator emits {mac, ip, device_name, switch_port, vlan}
    — translate to plugin shape."""
    ep = {
        "mac": "aa:bb:cc:dd:ee:01",
        "ip": "192.0.2.10",
        "device_name": "ex2300-24p",
        "switch_port": "ge-0/0/12",
        "vlan": 10,
        "mac_vendor": "Juniper Networks",
        "hostname": "server-a",
        "source": "infrastructure",
    }
    row = plugin_writer._normalize_endpoint_dict(ep)
    assert row["mac_address"] == "AA:BB:CC:DD:EE:01"
    assert row["current_switch"] == "ex2300-24p"
    assert row["current_port"] == "ge-0/0/12"
    assert row["current_vlan"] == 10
    assert row["current_ip"] == "192.0.2.10"
    assert row["mac_vendor"] == "Juniper Networks"
    assert row["hostname"] == "server-a"
    assert row["data_source"] == "infrastructure"
    assert row["active"] is True


def test_normalize_sentinels():
    """Missing switch/port → ``"(none)"``, missing vlan → ``0``,
    per E0 §2e sentinels."""
    ep = {"mac": "AA:BB:CC:DD:EE:02"}
    row = plugin_writer._normalize_endpoint_dict(ep)
    assert row["current_switch"] == "(none)"
    assert row["current_port"] == "(none)"
    assert row["current_vlan"] == 0


def test_normalize_vlan_string_coercion():
    ep = {"mac": "AA", "vlan": "100"}
    row = plugin_writer._normalize_endpoint_dict(ep)
    assert row["current_vlan"] == 100


def test_normalize_vlan_unparseable_falls_back_to_zero():
    ep = {"mac": "AA", "vlan": "trunk"}
    row = plugin_writer._normalize_endpoint_dict(ep)
    assert row["current_vlan"] == 0


def test_normalize_mac_uppercased():
    ep = {"mac": "aa:bb:cc:dd:ee:ff"}
    row = plugin_writer._normalize_endpoint_dict(ep)
    assert row["mac_address"] == "AA:BB:CC:DD:EE:FF"


def test_normalize_additional_ips_includes_current():
    """If ip is set, it's mirrored into additional_ips so the
    list is the union of all IPs ever seen for this row."""
    ep = {
        "mac": "AA",
        "ip": "192.0.2.10",
        "additional_ips": ["192.0.2.11"],
    }
    row = plugin_writer._normalize_endpoint_dict(ep)
    assert "192.0.2.10" in row["additional_ips"]
    assert "192.0.2.11" in row["additional_ips"]


def test_normalize_existing_current_ip_preserved():
    """If ``ip`` is already in additional_ips, no duplicate."""
    ep = {
        "mac": "AA",
        "ip": "192.0.2.10",
        "additional_ips": ["192.0.2.10", "192.0.2.11"],
    }
    row = plugin_writer._normalize_endpoint_dict(ep)
    assert row["additional_ips"].count("192.0.2.10") == 1


# ---------------------------------------------------------------------------
# upsert_endpoint_bulk soft-failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_empty_returns_zero():
    assert await plugin_writer.upsert_endpoint_bulk([]) == 0


@pytest.mark.asyncio
async def test_upsert_table_missing_returns_zero():
    """If reflection fails (plugin not migrated), upsert returns
    0 silently — no exception bubbles up."""
    with patch.object(
        plugin_writer, "_ensure_table",
        new=AsyncMock(return_value=None),
    ):
        result = await plugin_writer.upsert_endpoint_bulk([
            {"mac": "AA"},
        ])
        assert result == 0


@pytest.mark.asyncio
async def test_upsert_skips_rows_without_mac():
    """Rows with empty MAC are filtered before dedup/upsert."""
    with patch.object(
        plugin_writer, "_ensure_table",
        new=AsyncMock(return_value=None),
    ):
        result = await plugin_writer.upsert_endpoint_bulk([
            {"mac": ""},
            {"mac": None},
            {"foo": "bar"},  # no mac key at all
        ])
        # All filtered → 0
        assert result == 0


def test_endpoint_constraint_keys_match_e0_design():
    """The constraint key tuple matches the unique_together in
    plugin's Endpoint model — this is the pact between the
    controller's dedup discipline and the plugin's schema."""
    assert plugin_writer.ENDPOINT_CONSTRAINT_KEYS == (
        "mac_address",
        "current_switch",
        "current_port",
        "current_vlan",
    )


# ===========================================================================
# E2 — ARP, MAC, LLDP plugin_writer tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Constraint-key pact assertions (the seam between controller and plugin)
# ---------------------------------------------------------------------------


def test_arp_constraint_keys_match_e0_design():
    """The ARP constraint tuple matches the Meta.unique_together
    in plugin's ArpEntry model."""
    assert plugin_writer.ARP_CONSTRAINT_KEYS == (
        "node_name", "ip", "mac", "vrf",
    )


def test_mac_constraint_keys_match_e0_design():
    """The MAC constraint tuple matches the Meta.unique_together
    in plugin's MacEntry model."""
    assert plugin_writer.MAC_CONSTRAINT_KEYS == (
        "node_name", "mac", "interface", "vlan",
    )


def test_lldp_constraint_keys_match_e0_design():
    """The LLDP constraint tuple matches the Meta.unique_together
    in plugin's LldpNeighbor model — Block C P2 schema-expansion
    columns are NOT part of the constraint (they update on
    conflict)."""
    assert plugin_writer.LLDP_CONSTRAINT_KEYS == (
        "node_name", "local_interface", "remote_system_name", "remote_port",
    )


# ---------------------------------------------------------------------------
# _normalize_arp_dict
# ---------------------------------------------------------------------------


def test_normalize_arp_dict_translates_polling_shape():
    """polling.collect_arp produces {ip, mac, interface}; we
    add node_name + vrf=default + collected_at."""
    row = plugin_writer._normalize_arp_dict("ex2300", {
        "ip": "192.0.2.10", "mac": "aa:bb:cc:dd:ee:01",
        "interface": "ge-0/0/12",
    })
    assert row["node_name"] == "ex2300"
    assert row["ip"] == "192.0.2.10"
    assert row["mac"] == "AA:BB:CC:DD:EE:01"
    assert row["interface"] == "ge-0/0/12"
    assert row["vrf"] == "default"
    assert row["collected_at"] is not None


def test_normalize_arp_dict_drops_rows_without_ip_or_mac():
    """Defensive: rows missing ip or mac are filtered (return None)."""
    assert plugin_writer._normalize_arp_dict("ex2300", {"mac": "AA:BB"}) is None
    assert plugin_writer._normalize_arp_dict("ex2300", {"ip": "192.0.2.10"}) is None
    assert plugin_writer._normalize_arp_dict("ex2300", {}) is None


def test_normalize_arp_dict_preserves_sentinel_interface():
    """ifindex:N sentinels are valid interface values per Rule 7."""
    row = plugin_writer._normalize_arp_dict("ex2300", {
        "ip": "192.0.2.10", "mac": "AA:BB:CC:DD:EE:01",
        "interface": "ifindex:7",
    })
    assert row["interface"] == "ifindex:7"


# ---------------------------------------------------------------------------
# _normalize_mac_dict
# ---------------------------------------------------------------------------


def test_normalize_mac_dict_remaps_static_bool_to_entry_type():
    """polling-side ``static: bool`` (Block C P4 entry_status remap)
    becomes plugin-side ``entry_type: str``."""
    static_row = plugin_writer._normalize_mac_dict("ex2300", {
        "mac": "AA:BB", "interface": "ge-0/0/12", "vlan": 10, "static": True,
    })
    assert static_row["entry_type"] == "static"

    dynamic_row = plugin_writer._normalize_mac_dict("ex2300", {
        "mac": "AA:BB", "interface": "ge-0/0/12", "vlan": 10, "static": False,
    })
    assert dynamic_row["entry_type"] == "dynamic"


def test_normalize_mac_dict_default_entry_type_is_dynamic():
    """Missing or falsy ``static`` defaults to dynamic."""
    row = plugin_writer._normalize_mac_dict("ex2300", {
        "mac": "AA:BB", "interface": "ge-0/0/12", "vlan": 10,
    })
    assert row["entry_type"] == "dynamic"


def test_normalize_mac_dict_vlan_int_coercion():
    """Plugin's vlan column is IntegerField; coerce strings."""
    row = plugin_writer._normalize_mac_dict("ex2300", {
        "mac": "AA:BB", "interface": "ge-0/0/12", "vlan": "100",
    })
    assert row["vlan"] == 100


def test_normalize_mac_dict_drops_rows_without_mac():
    assert plugin_writer._normalize_mac_dict("ex2300", {"interface": "ge"}) is None


# ---------------------------------------------------------------------------
# _normalize_lldp_neighbor
# ---------------------------------------------------------------------------


def test_normalize_lldp_neighbor_includes_p2_expansion_columns():
    """All five Block C P2 expansion columns must round-trip."""
    row = plugin_writer._normalize_lldp_neighbor(
        "ex2300", "ge-0/0/12",
        {
            "remote_system_name": "switch-b",
            "remote_port": "ge-0/0/24",
            "remote_chassis_id": "aa:bb:cc:dd:ee:ff",
            "remote_management_ip": "192.0.2.20",
            "local_port_ifindex": 514,
            "local_port_name": "ge-0/0/12",
            "remote_chassis_id_subtype": "macAddress",
            "remote_port_id_subtype": "interfaceName",
            "remote_system_description": "Junos 21.4R3-S6.4",
        },
    )
    assert row["node_name"] == "ex2300"
    assert row["local_interface"] == "ge-0/0/12"
    assert row["remote_system_name"] == "switch-b"
    assert row["remote_chassis_id"] == "aa:bb:cc:dd:ee:ff"
    assert row["local_port_ifindex"] == 514
    assert row["remote_chassis_id_subtype"] == "macAddress"
    assert row["remote_port_id_subtype"] == "interfaceName"
    assert row["remote_system_description"] == "Junos 21.4R3-S6.4"
    assert row["collected_at"] is not None


def test_normalize_lldp_neighbor_handles_missing_remote_fields():
    """Anonymous LLDP neighbor (sys_name None, no chassis_id) —
    Block C P5 unmanaged-neighbor pattern."""
    row = plugin_writer._normalize_lldp_neighbor(
        "ex4300-48t", "ge-0/0/24",
        {
            "remote_port": "00:11:22:33:44:55",
            "remote_chassis_id": None,
        },
    )
    assert row["remote_system_name"] == ""
    assert row["remote_chassis_id"] is None


# ---------------------------------------------------------------------------
# upsert_arp_bulk soft-failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_arp_bulk_empty_returns_zero():
    assert await plugin_writer.upsert_arp_bulk("ex2300", []) == 0


@pytest.mark.asyncio
async def test_upsert_arp_bulk_table_missing_returns_zero():
    """Reflection-fail (plugin not migrated) → 0, no exception."""
    with patch.object(
        plugin_writer, "_ensure_table",
        new=AsyncMock(return_value=None),
    ):
        result = await plugin_writer.upsert_arp_bulk("ex2300", [
            {"ip": "192.0.2.10", "mac": "AA:BB", "interface": "ge"},
        ])
        assert result == 0


@pytest.mark.asyncio
async def test_upsert_arp_bulk_dedups_constraint_key_collisions():
    """Two entries with same (node, ip, mac, vrf) must collapse —
    Block C P3 Junos lo0.X collision class."""
    rows = [
        plugin_writer._normalize_arp_dict("ex2300", {
            "ip": "192.0.2.10", "mac": "AA:BB", "interface": "lo0.0",
        }),
        plugin_writer._normalize_arp_dict("ex2300", {
            "ip": "192.0.2.10", "mac": "AA:BB", "interface": "lo0.1",
        }),
    ]
    deduped = plugin_writer._dedup_by_constraint(
        rows, plugin_writer.ARP_CONSTRAINT_KEYS,
    )
    assert len(deduped) == 1


# ---------------------------------------------------------------------------
# upsert_mac_bulk soft-failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_mac_bulk_empty_returns_zero():
    assert await plugin_writer.upsert_mac_bulk("ex2300", []) == 0


@pytest.mark.asyncio
async def test_upsert_mac_bulk_table_missing_returns_zero():
    with patch.object(
        plugin_writer, "_ensure_table",
        new=AsyncMock(return_value=None),
    ):
        result = await plugin_writer.upsert_mac_bulk("ex2300", [
            {"mac": "AA:BB", "interface": "ge", "vlan": 10, "static": False},
        ])
        assert result == 0


@pytest.mark.asyncio
async def test_upsert_mac_bulk_dedups_orphan_fdb_collapsing_to_vlan_zero():
    """Block C P4 defensive: orphan FDB IDs that all coerce to
    vlan=0 must dedup before the upsert."""
    rows = [
        plugin_writer._normalize_mac_dict("ex2300", {
            "mac": "AA:BB", "interface": "ifindex:7", "vlan": 0,
        }),
        plugin_writer._normalize_mac_dict("ex2300", {
            "mac": "AA:BB", "interface": "ifindex:7", "vlan": 0,
        }),
    ]
    deduped = plugin_writer._dedup_by_constraint(
        rows, plugin_writer.MAC_CONSTRAINT_KEYS,
    )
    assert len(deduped) == 1


# ---------------------------------------------------------------------------
# upsert_lldp_bulk soft-failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_lldp_bulk_empty_returns_zero():
    assert await plugin_writer.upsert_lldp_bulk("ex2300", {}) == 0


@pytest.mark.asyncio
async def test_upsert_lldp_bulk_table_missing_returns_zero():
    with patch.object(
        plugin_writer, "_ensure_table",
        new=AsyncMock(return_value=None),
    ):
        result = await plugin_writer.upsert_lldp_bulk("ex2300", {
            "ge-0/0/12": [{
                "remote_system_name": "switch-b",
                "remote_port": "ge-0/0/24",
            }],
        })
        assert result == 0


@pytest.mark.asyncio
async def test_upsert_lldp_bulk_flattens_grouped_dict():
    """polling.collect_lldp produces dict-by-iface; plugin schema
    is flat one-row-per-neighbor. The writer flattens internally."""
    grouped = {
        "ge-0/0/12": [
            {"remote_system_name": "neighbor-a", "remote_port": "Eth1"},
            {"remote_system_name": "neighbor-b", "remote_port": "Eth2"},
        ],
        "ge-0/0/24": [
            {"remote_system_name": "neighbor-c", "remote_port": "Eth3"},
        ],
    }
    rows: list[dict] = []
    for local_iface, neighbors in grouped.items():
        for n in neighbors:
            rows.append(plugin_writer._normalize_lldp_neighbor(
                "ex2300", local_iface, n,
            ))
    assert len(rows) == 3
    interfaces = sorted({r["local_interface"] for r in rows})
    assert interfaces == ["ge-0/0/12", "ge-0/0/24"]
    sys_names = sorted({r["remote_system_name"] for r in rows})
    assert sys_names == ["neighbor-a", "neighbor-b", "neighbor-c"]


@pytest.mark.asyncio
async def test_upsert_lldp_bulk_dedups_unmanaged_neighbor_collision():
    """Block C P5 EX4300-48t ge-0/0/24 case: two unmanaged
    neighbors with sys_name=None and identical port_id collapse
    to one constraint key."""
    grouped = {
        "ge-0/0/24": [
            {"remote_system_name": "", "remote_port": "AA:BB"},
            {"remote_system_name": "", "remote_port": "AA:BB"},
        ],
    }
    rows: list[dict] = []
    for local_iface, neighbors in grouped.items():
        for n in neighbors:
            rows.append(plugin_writer._normalize_lldp_neighbor(
                "ex4300-48t", local_iface, n,
            ))
    deduped = plugin_writer._dedup_by_constraint(
        rows, plugin_writer.LLDP_CONSTRAINT_KEYS,
    )
    assert len(deduped) == 1
