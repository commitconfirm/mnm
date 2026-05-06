"""Unit tests for controller/app/lldp_snmp.py.

Mocks at snmp_collector.walk_table and snmp_collector.collect_ifindex_to_name
boundaries — no real SNMP traffic. walk_table returns list[dict[str, Any]]
with already-converted native types.

Row index conventions:
  lldpRemTable suffix: "<col>.<time_mark>.<local_port_num>.<rem_index>"
  lldpRemManAddrTable suffix:
    "<col>.<tm>.<lpn>.<ridx>.<addr_subtype>.<addr_len>.<addr_byte0>..."
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

from app.lldp_snmp import (
    LldpNeighbor,
    _decode_lldp_id,
    _parse_lldp_man_addr,
    collect_lldp,
)
from app.snmp_collector import SnmpError, SnmpTimeoutError

DEVICE_IP = "198.51.100.1"
COMMUNITY = "test-ro"

_OID_LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"
_OID_LLDP_MAN_ADDR = "1.0.8802.1.1.2.1.4.2.1"


def _rem_row(col: str, tm: int, lpn: int, ridx: int, val: object) -> dict:
    """Build one walk_table row for lldpRemTable."""
    return {f"{col}.{tm}.{lpn}.{ridx}": val}


def _man_addr_row(
    col: str, tm: int, lpn: int, ridx: int,
    addr_subtype: int, addr_bytes: list[int], val: object = 2,
) -> dict:
    """Build one walk_table row for lldpRemManAddrTable.

    Index: <tm>.<lpn>.<ridx>.<addr_subtype>.<addr_len>.<addr_bytes...>
    """
    addr_len = len(addr_bytes)
    parts = ".".join(str(b) for b in addr_bytes)
    return {f"{col}.{tm}.{lpn}.{ridx}.{addr_subtype}.{addr_len}.{parts}": val}


# ---------------------------------------------------------------------------
# _decode_lldp_id — chassis subtype tests
# ---------------------------------------------------------------------------

def test_decode_chassis_id_mac():
    """Chassis subtype 4 (mac_address) with 6 bytes → colon-hex MAC."""
    raw = b"\xaa\xbb\xcc\xdd\xee\xff"
    value, name = _decode_lldp_id(raw, 4, is_port_id=False)
    assert value == "aa:bb:cc:dd:ee:ff"
    assert name == "mac_address"


def test_decode_chassis_id_mac_wrong_length():
    """MAC subtype with wrong byte count falls back to hex and logs."""
    raw = b"\xaa\xbb\xcc"  # 3 bytes, not 6
    with patch("app.lldp_snmp.log") as mock_log:
        value, name = _decode_lldp_id(raw, 4, is_port_id=False)
    assert value == "aabbcc"
    assert name == "mac_address"
    mock_log.warning.assert_called()
    assert mock_log.warning.call_args[0][0] == "lldp_snmp_malformed_mac_id"


def test_decode_chassis_id_network_address_ipv4():
    """Chassis subtype 5 with family=1 → dotted-quad IPv4."""
    raw = bytes([0x01, 192, 0, 2, 1])  # family 1 + 4 bytes
    value, name = _decode_lldp_id(raw, 5, is_port_id=False)
    assert value == "192.0.2.1"
    assert name == "network_address"


def test_decode_chassis_id_network_address_ipv6():
    """Chassis subtype 5 with family=2 → ipv6: hex placeholder."""
    raw = bytes([0x02] + [0xfe, 0x80] + [0] * 14)
    value, name = _decode_lldp_id(raw, 5, is_port_id=False)
    assert value.startswith("ipv6:")
    assert "fe80" in value
    assert name == "network_address"


def test_decode_chassis_id_interface_name():
    """Chassis subtype 6 (interface_name) → UTF-8 string."""
    raw = b"ge-0/0/12"
    value, name = _decode_lldp_id(raw, 6, is_port_id=False)
    assert value == "ge-0/0/12"
    assert name == "interface_name"


def test_decode_chassis_id_unknown_subtype():
    """Unknown subtype value returns hex + unknown_subtype_N name."""
    raw = b"\x01\x02\x03"
    value, name = _decode_lldp_id(raw, 99, is_port_id=False)
    assert value == "010203"
    assert name == "unknown_subtype_99"


# ---------------------------------------------------------------------------
# _decode_lldp_id — port subtype tests (enum differs from chassis)
# ---------------------------------------------------------------------------

def test_decode_port_id_interface_alias():
    """Port subtype 1 (interface_alias) differs from chassis subtype 1."""
    raw = b"some-alias"
    value, name = _decode_lldp_id(raw, 1, is_port_id=True)
    assert value == "some-alias"
    assert name == "interface_alias"  # NOT "chassis_component"


def test_decode_port_id_agent_circuit_id():
    """Port subtype 6 is agent_circuit_id — chassis subtype 6 is interface_name."""
    raw = b"circuit-X"
    value_port, name_port = _decode_lldp_id(raw, 6, is_port_id=True)
    value_chassis, name_chassis = _decode_lldp_id(raw, 6, is_port_id=False)
    assert name_port == "agent_circuit_id"
    assert name_chassis == "interface_name"
    # Value is identical; only the enum name differs
    assert value_port == value_chassis == "circuit-X"


def test_decode_handles_trailing_nuls():
    """Trailing NUL bytes are stripped from text subtypes."""
    raw = b"foo\x00\x00"
    value, _ = _decode_lldp_id(raw, 7, is_port_id=False)  # subtype 7 = locally_assigned
    assert value == "foo"


# ---------------------------------------------------------------------------
# _parse_lldp_man_addr tests
# ---------------------------------------------------------------------------

def test_parse_man_addr_ipv4():
    """A realistic IPv4 row is extracted into the mapping."""
    rows = [
        # Walking column .3 (IfSubtype=2 = "ifIndex")
        _man_addr_row("3", 795104, 526, 4, 1, [172, 21, 140, 101]),
        # Walking column .4 (IfId=140) — same index, just another column
        _man_addr_row("4", 795104, 526, 4, 1, [172, 21, 140, 101], val=140),
    ]
    result = _parse_lldp_man_addr(rows)
    assert result == {(795104, 526, 4): "172.21.140.101"}


def test_parse_man_addr_prefers_ipv4_over_ipv6():
    """When both IPv4 and IPv6 are advertised for a neighbor, IPv4 wins."""
    rows = [
        _man_addr_row("3", 100, 10, 1, 1, [192, 0, 2, 1]),       # IPv4
        _man_addr_row("3", 100, 10, 1, 2, [0xfe, 0x80] + [0] * 14),  # IPv6
    ]
    result = _parse_lldp_man_addr(rows)
    assert result == {(100, 10, 1): "192.0.2.1"}


def test_parse_man_addr_ipv6_only():
    """IPv6-only advertisement returns ipv6: placeholder."""
    addr = [0xfe, 0x80] + [0] * 14
    rows = [_man_addr_row("3", 100, 10, 1, 2, addr)]
    result = _parse_lldp_man_addr(rows)
    assert list(result.keys()) == [(100, 10, 1)]
    assert result[(100, 10, 1)].startswith("ipv6:")


def test_parse_man_addr_skips_mac_subtype():
    """Subtype 6 (802/MAC) is ignored — a MAC is not a usable management IP."""
    rows = [
        _man_addr_row("3", 99, 536, 1, 6, [204, 225, 148, 34, 132, 96]),  # MAC
    ]
    assert _parse_lldp_man_addr(rows) == {}


def test_parse_man_addr_empty_table_returns_empty_dict():
    """Empty input yields empty output."""
    assert _parse_lldp_man_addr([]) == {}


# ---------------------------------------------------------------------------
# collect_lldp — integration tests
# ---------------------------------------------------------------------------

async def test_collect_lldp_with_neighbors():
    """Happy path: 2 neighbors with mgmt IPs and resolved interface names."""
    rem_rows = (
        # Neighbor 1 — (tm=99, lpn=536, ridx=1): MAC chassis, interface_name port
        [_rem_row("4", 99, 536, 1, 4),                           # chassis subtype 4
         _rem_row("5", 99, 536, 1, b"\xcc\xe1\x94\x22\x84\x60"),  # chassis id MAC
         _rem_row("6", 99, 536, 1, 5),                           # port subtype 5
         _rem_row("7", 99, 536, 1, b"ge-0/0/44"),                # port id
         _rem_row("9", 99, 536, 1, b"ex4300-48t"),               # sys name
         _rem_row("10", 99, 536, 1, b"Juniper")]
        +
        # Neighbor 2 — (tm=795104, lpn=526, ridx=4): MAC chassis, MAC port (AP style)
        [_rem_row("4", 795104, 526, 4, 4),
         _rem_row("5", 795104, 526, 4, b"\xac\x23\x16\x82\x5f\xee"),
         _rem_row("6", 795104, 526, 4, 3),                        # port subtype 3 = MAC
         _rem_row("7", 795104, 526, 4, b"\xac\x23\x16\x82\x5f\xee"),
         _rem_row("9", 795104, 526, 4, b"ap43-garage"),
         _rem_row("10", 795104, 526, 4, b"Mist AP")]
    )
    man_addr_rows = [
        _man_addr_row("3", 795104, 526, 4, 1, [172, 21, 140, 101]),  # AP has IPv4
        # Neighbor 1 has no IPv4 advertised — only MAC (skipped by parser)
        _man_addr_row("3", 99, 536, 1, 6, [204, 225, 148, 34, 132, 96]),
    ]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_LLDP_REM:
            return rem_rows
        if oid == _OID_LLDP_MAN_ADDR:
            return man_addr_rows
        return []

    ifindex_map = {526: "ge-0/0/12", 536: "ge-0/0/22"}

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.snmp_collector.collect_ifindex_to_name",
                   AsyncMock(return_value=ifindex_map)):
            result = await collect_lldp(DEVICE_IP, COMMUNITY)

    assert len(result) == 2
    by_port = {n.local_port_ifindex: n for n in result}

    # Neighbor on ifIndex 536 (ex4300)
    n_switch = by_port[536]
    assert n_switch.remote_chassis_id == "cc:e1:94:22:84:60"
    assert n_switch.remote_chassis_id_subtype == "mac_address"
    assert n_switch.remote_port_id == "ge-0/0/44"
    assert n_switch.remote_port_id_subtype == "interface_name"
    assert n_switch.remote_system_name == "ex4300-48t"
    assert n_switch.local_port_name == "ge-0/0/22"
    assert n_switch.management_ip is None  # only MAC advertised → skipped

    # Neighbor on ifIndex 526 (AP)
    n_ap = by_port[526]
    assert n_ap.remote_chassis_id == "ac:23:16:82:5f:ee"
    assert n_ap.remote_port_id_subtype == "mac_address"
    assert n_ap.remote_system_name == "ap43-garage"
    assert n_ap.management_ip == "172.21.140.101"
    assert n_ap.local_port_name == "ge-0/0/12"


async def test_collect_lldp_no_neighbors():
    """Empty lldpRemTable → returns [] without raising."""
    async def mock_walk(device_ip, community, oid, **kwargs):
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.snmp_collector.collect_ifindex_to_name",
                   AsyncMock(return_value={})):
            result = await collect_lldp(DEVICE_IP, COMMUNITY)

    assert result == []


async def test_collect_lldp_neighbor_without_mgmt_ip():
    """Neighbor not in man_addr map → management_ip is None, no error."""
    rem_rows = [
        _rem_row("4", 10, 20, 1, 4),
        _rem_row("5", 10, 20, 1, b"\xaa\xbb\xcc\xdd\xee\xff"),
        _rem_row("6", 10, 20, 1, 5),
        _rem_row("7", 10, 20, 1, b"Ethernet1"),
        _rem_row("9", 10, 20, 1, b"peer"),
    ]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_LLDP_REM:
            return rem_rows
        return []  # no man_addr entries for this neighbor

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.snmp_collector.collect_ifindex_to_name",
                   AsyncMock(return_value={20: "Ethernet20"})):
            result = await collect_lldp(DEVICE_IP, COMMUNITY)

    assert len(result) == 1
    assert result[0].management_ip is None
    assert result[0].local_port_name == "Ethernet20"


async def test_collect_lldp_unknown_ifindex():
    """Neighbor on ifIndex not in ifName map → local_port_name is None."""
    rem_rows = [
        _rem_row("4", 10, 9999, 1, 4),  # lpn 9999 not in map
        _rem_row("5", 10, 9999, 1, b"\xaa\xbb\xcc\xdd\xee\xff"),
        _rem_row("6", 10, 9999, 1, 5),
        _rem_row("7", 10, 9999, 1, b"whatever"),
    ]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_LLDP_REM:
            return rem_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.snmp_collector.collect_ifindex_to_name",
                   AsyncMock(return_value={1: "eth0"})):  # no 9999
            result = await collect_lldp(DEVICE_IP, COMMUNITY)

    assert len(result) == 1
    assert result[0].local_port_ifindex == 9999
    assert result[0].local_port_name is None


async def test_collect_lldp_mac_chassis_id_normalization():
    """MAC chassis ID bytes are returned as lowercase colon-separated."""
    rem_rows = [
        _rem_row("4", 1, 1, 1, 4),
        _rem_row("5", 1, 1, 1, b"\x00\x11\x22\x33\x44\x55"),
        _rem_row("6", 1, 1, 1, 5),
        _rem_row("7", 1, 1, 1, b"port1"),
    ]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_LLDP_REM:
            return rem_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.snmp_collector.collect_ifindex_to_name",
                   AsyncMock(return_value={})):
            result = await collect_lldp(DEVICE_IP, COMMUNITY)

    assert result[0].remote_chassis_id == "00:11:22:33:44:55"


async def test_collect_lldp_network_address_chassis_id():
    """Network-address chassis ID (IPv4) is parsed to dotted-quad."""
    rem_rows = [
        _rem_row("4", 1, 1, 1, 5),                # subtype 5 = network_address
        _rem_row("5", 1, 1, 1, bytes([0x01, 203, 0, 113, 5])),  # family 1 + IPv4
        _rem_row("6", 1, 1, 1, 5),
        _rem_row("7", 1, 1, 1, b"port1"),
    ]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_LLDP_REM:
            return rem_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.snmp_collector.collect_ifindex_to_name",
                   AsyncMock(return_value={})):
            result = await collect_lldp(DEVICE_IP, COMMUNITY)

    assert result[0].remote_chassis_id == "203.0.113.5"
    assert result[0].remote_chassis_id_subtype == "network_address"


async def test_collect_lldp_rem_walk_timeout_propagates():
    """SnmpTimeoutError from lldpRemTable walk propagates to caller."""
    with patch("app.snmp_collector.walk_table",
               AsyncMock(side_effect=SnmpTimeoutError("timed out"))):
        with pytest.raises(SnmpTimeoutError):
            await collect_lldp(DEVICE_IP, COMMUNITY)


async def test_collect_lldp_man_addr_error_degrades_to_none():
    """Error walking lldpRemManAddrTable doesn't fail the collection."""
    rem_rows = [
        _rem_row("4", 10, 20, 1, 4),
        _rem_row("5", 10, 20, 1, b"\xaa\xbb\xcc\xdd\xee\xff"),
        _rem_row("6", 10, 20, 1, 5),
        _rem_row("7", 10, 20, 1, b"eth0"),
    ]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_LLDP_REM:
            return rem_rows
        if oid == _OID_LLDP_MAN_ADDR:
            raise SnmpError("man addr table not implemented")
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.snmp_collector.collect_ifindex_to_name",
                   AsyncMock(return_value={})):
            result = await collect_lldp(DEVICE_IP, COMMUNITY)

    assert len(result) == 1
    assert result[0].management_ip is None
