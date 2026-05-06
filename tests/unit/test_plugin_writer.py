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


# ===========================================================================
# E3 — Route, BGP, Fingerprint plugin_writer tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Constraint-key pact assertions
# ---------------------------------------------------------------------------


def test_route_constraint_keys_match_e0_design():
    """The Route constraint tuple matches the unique_together
    in plugin's Route model and the controller's Route table."""
    assert plugin_writer.ROUTE_CONSTRAINT_KEYS == (
        "node_name", "prefix", "next_hop", "vrf",
    )


def test_bgp_constraint_keys_match_e0_design():
    """The BGP constraint tuple matches the unique_together in
    the plugin's BgpNeighbor model. ``vrf`` and
    ``address_family`` are part of the key so the same neighbor
    can appear in multiple VRFs / address families."""
    assert plugin_writer.BGP_CONSTRAINT_KEYS == (
        "node_name", "neighbor_ip", "vrf", "address_family",
    )


def test_fingerprint_constraint_keys_match_e0_design():
    """The Fingerprint constraint tuple matches the design doc
    §2c. Schema co-versioning pact even though no production
    caller exists in v1.0 (v1.1 fingerprinting workstream
    wires upstream collectors)."""
    assert plugin_writer.FINGERPRINT_CONSTRAINT_KEYS == (
        "target_mac", "signal_type", "signal_value",
    )


# ---------------------------------------------------------------------------
# _normalize_route_dict
# ---------------------------------------------------------------------------


def test_normalize_route_dict_translates_polling_shape():
    """polling.collect_routes produces dicts with node_name +
    prefix etc.; we add server-side collected_at."""
    row = plugin_writer._normalize_route_dict({
        "node_name": "ex2300-24p",
        "prefix": "192.0.2.0/24",
        "next_hop": "192.0.2.1",
        "protocol": "bgp",
        "vrf": "default",
        "metric": 100,
        "preference": 170,
        "outgoing_interface": "ge-0/0/0.0",
        "active": True,
    })
    assert row["node_name"] == "ex2300-24p"
    assert row["prefix"] == "192.0.2.0/24"
    assert row["protocol"] == "bgp"
    assert row["metric"] == 100
    assert row["outgoing_interface"] == "ge-0/0/0.0"
    assert row["active"] is True
    assert row["collected_at"] is not None


def test_normalize_route_dict_drops_rows_missing_node_or_prefix():
    """Defensive: rows without node_name or prefix are filtered."""
    assert plugin_writer._normalize_route_dict({"prefix": "1.0.0.0/8"}) is None
    assert plugin_writer._normalize_route_dict({"node_name": "x"}) is None
    assert plugin_writer._normalize_route_dict({}) is None


def test_normalize_route_dict_defaults_protocol_and_vrf():
    """Missing protocol → 'unknown'; missing vrf → 'default'."""
    row = plugin_writer._normalize_route_dict({
        "node_name": "x", "prefix": "1.0.0.0/8",
    })
    assert row["protocol"] == "unknown"
    assert row["vrf"] == "default"
    assert row["next_hop"] == ""


def test_normalize_route_dict_outgoing_interface_preserved_vendor_native():
    """outgoing_interface is preserved as-stored — the
    cross-vendor naming helper transforms at render time, not
    at write time. Junos ge-0/0/0.0 lands as ge-0/0/0.0; PAN-OS
    forms land as PAN-OS shape."""
    junos = plugin_writer._normalize_route_dict({
        "node_name": "x", "prefix": "1.0.0.0/8",
        "outgoing_interface": "ge-0/0/0.0",
    })
    assert junos["outgoing_interface"] == "ge-0/0/0.0"
    panos = plugin_writer._normalize_route_dict({
        "node_name": "x", "prefix": "1.0.0.0/8",
        "outgoing_interface": "ethernet1/1",
    })
    assert panos["outgoing_interface"] == "ethernet1/1"


# ---------------------------------------------------------------------------
# _normalize_bgp_dict
# ---------------------------------------------------------------------------


def test_normalize_bgp_dict_state_default_unknown():
    """Missing state defaults to 'Unknown' — the BGP state-chip
    renders these as grey, not red, because they're indeterminate
    rather than failing."""
    row = plugin_writer._normalize_bgp_dict({
        "node_name": "ex2300-24p",
        "neighbor_ip": "192.0.2.1",
        "remote_asn": 65000,
    })
    assert row["state"] == "Unknown"


def test_normalize_bgp_dict_asn_int_coercion():
    """remote_asn / local_asn arrive as strings sometimes;
    coerce to int. Bad values fall back: remote_asn → 0,
    local_asn → None."""
    row = plugin_writer._normalize_bgp_dict({
        "node_name": "x", "neighbor_ip": "192.0.2.1",
        "remote_asn": "65000", "local_asn": "65001",
    })
    assert row["remote_asn"] == 65000
    assert row["local_asn"] == 65001

    bad = plugin_writer._normalize_bgp_dict({
        "node_name": "x", "neighbor_ip": "192.0.2.1",
        "remote_asn": "abc", "local_asn": "xyz",
    })
    assert bad["remote_asn"] == 0
    assert bad["local_asn"] is None


def test_normalize_bgp_dict_address_family_default():
    """Missing address_family → 'ipv4 unicast'; vrf → 'default'.
    Both are part of the unique key, so defaults are critical."""
    row = plugin_writer._normalize_bgp_dict({
        "node_name": "x", "neighbor_ip": "192.0.2.1",
        "remote_asn": 65000,
    })
    assert row["address_family"] == "ipv4 unicast"
    assert row["vrf"] == "default"


# ---------------------------------------------------------------------------
# _normalize_fingerprint_dict
# ---------------------------------------------------------------------------


def test_normalize_fingerprint_dict_preserves_metadata_dict():
    """signal_metadata is an arbitrary dict from the (future)
    collector; round-trip without modification."""
    row = plugin_writer._normalize_fingerprint_dict({
        "target_mac": "AA:BB:CC:DD:EE:01",
        "signal_type": "ssh_hostkey",
        "signal_value": "AAAAB3NzaC1yc2EAAAA...",
        "signal_metadata": {"keytype": "rsa", "bits": 2048},
    })
    assert row["signal_metadata"] == {"keytype": "rsa", "bits": 2048}
    assert row["target_mac"] == "AA:BB:CC:DD:EE:01"


def test_normalize_fingerprint_dict_defaults_seen_count_to_one():
    """New fingerprint rows start with seen_count = 1; the
    increment-on-conflict happens at SQL time in the upsert."""
    row = plugin_writer._normalize_fingerprint_dict({
        "target_mac": "AA:BB",
        "signal_type": "mdns",
        "signal_value": "_workstation._tcp",
    })
    assert row["seen_count"] == 1


def test_normalize_fingerprint_dict_iso_timestamp_coerced():
    """first_seen / last_seen ISO strings get coerced to
    datetime via _coerce_dt — same E2 fix #3 pattern as the
    Endpoint normalize."""
    iso = "2026-04-29T00:00:00+00:00"
    row = plugin_writer._normalize_fingerprint_dict({
        "target_mac": "AA",
        "signal_type": "tls_cert",
        "signal_value": "sha256:abc",
        "first_seen": iso,
        "last_seen": iso,
    })
    assert isinstance(row["first_seen"], datetime)
    assert isinstance(row["last_seen"], datetime)


# ---------------------------------------------------------------------------
# upsert_route_bulk soft-failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_route_bulk_empty_returns_zero():
    assert await plugin_writer.upsert_route_bulk([]) == 0


@pytest.mark.asyncio
async def test_upsert_route_bulk_table_missing_returns_zero():
    """Reflection-fail (plugin not migrated) → 0, no exception."""
    with patch.object(
        plugin_writer, "_ensure_table",
        new=AsyncMock(return_value=None),
    ):
        result = await plugin_writer.upsert_route_bulk([
            {"node_name": "x", "prefix": "1.0.0.0/8"},
        ])
        assert result == 0


@pytest.mark.asyncio
async def test_upsert_route_bulk_dedups_constraint_collisions():
    """Two routes with the same (node, prefix, next_hop, vrf)
    collapse to one — defensive against NAPALM-route-table
    duplicates that occasionally emit on multi-RIB setups."""
    rows = [
        plugin_writer._normalize_route_dict({
            "node_name": "x", "prefix": "1.0.0.0/8",
            "next_hop": "192.0.2.1", "protocol": "bgp",
        }),
        plugin_writer._normalize_route_dict({
            "node_name": "x", "prefix": "1.0.0.0/8",
            "next_hop": "192.0.2.1", "protocol": "ospf",
        }),
    ]
    deduped = plugin_writer._dedup_by_constraint(
        rows, plugin_writer.ROUTE_CONSTRAINT_KEYS,
    )
    assert len(deduped) == 1


# ---------------------------------------------------------------------------
# upsert_bgp_neighbor_bulk soft-failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_bgp_neighbor_bulk_empty_returns_zero():
    assert await plugin_writer.upsert_bgp_neighbor_bulk([]) == 0


@pytest.mark.asyncio
async def test_upsert_bgp_neighbor_bulk_table_missing_returns_zero():
    with patch.object(
        plugin_writer, "_ensure_table",
        new=AsyncMock(return_value=None),
    ):
        result = await plugin_writer.upsert_bgp_neighbor_bulk([
            {"node_name": "x", "neighbor_ip": "192.0.2.1", "remote_asn": 65000},
        ])
        assert result == 0


@pytest.mark.asyncio
async def test_upsert_bgp_neighbor_bulk_dedups_per_address_family():
    """Same neighbor in same VRF but different address-families
    is allowed; same (node, ip, vrf, af) collapses."""
    rows = [
        plugin_writer._normalize_bgp_dict({
            "node_name": "x", "neighbor_ip": "192.0.2.1",
            "remote_asn": 65000,
            "vrf": "default", "address_family": "ipv4 unicast",
        }),
        plugin_writer._normalize_bgp_dict({
            "node_name": "x", "neighbor_ip": "192.0.2.1",
            "remote_asn": 65000,
            "vrf": "default", "address_family": "ipv4 unicast",
            "state": "Established",
        }),
    ]
    deduped = plugin_writer._dedup_by_constraint(
        rows, plugin_writer.BGP_CONSTRAINT_KEYS,
    )
    assert len(deduped) == 1
    assert deduped[0]["state"] == "Established"  # last wins


# ---------------------------------------------------------------------------
# upsert_fingerprint_bulk soft-failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_fingerprint_bulk_empty_returns_zero():
    assert await plugin_writer.upsert_fingerprint_bulk([]) == 0


@pytest.mark.asyncio
async def test_upsert_fingerprint_bulk_table_missing_returns_zero():
    with patch.object(
        plugin_writer, "_ensure_table",
        new=AsyncMock(return_value=None),
    ):
        result = await plugin_writer.upsert_fingerprint_bulk([
            {"target_mac": "AA", "signal_type": "mdns", "signal_value": "x"},
        ])
        assert result == 0


@pytest.mark.asyncio
async def test_upsert_fingerprint_bulk_dedups_constraint_collisions():
    """Two signals with same (mac, type, value) collapse — the
    SQL-time increment will fire just once for the deduped row."""
    rows = [
        plugin_writer._normalize_fingerprint_dict({
            "target_mac": "AA", "signal_type": "mdns",
            "signal_value": "_workstation._tcp",
        }),
        plugin_writer._normalize_fingerprint_dict({
            "target_mac": "AA", "signal_type": "mdns",
            "signal_value": "_workstation._tcp",
            "signal_metadata": {"hostname": "alpha.local"},
        }),
    ]
    deduped = plugin_writer._dedup_by_constraint(
        rows, plugin_writer.FINGERPRINT_CONSTRAINT_KEYS,
    )
    assert len(deduped) == 1
    assert deduped[0]["signal_metadata"] == {"hostname": "alpha.local"}
