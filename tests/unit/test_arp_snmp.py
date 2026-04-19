"""Unit tests for controller/app/arp_snmp.py.

Mocks at snmp_collector.walk_table boundary — no real SNMP traffic.
walk_table returns list[dict[str, Any]] with already-converted native
types, matching what the real snmp_collector delivers.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

from app.arp_snmp import ArpEntry, collect_arp
from app.snmp_collector import SnmpTimeoutError

DEVICE_IP = "198.51.100.1"
COMMUNITY = "test-ro"

# Base OIDs mirrored from arp_snmp.py
_OID_ARP_TABLE = "1.3.6.1.2.1.4.22.1"
_OID_ARP_PHYSICAL = "1.3.6.1.2.1.4.35.1"


def _arp_row(col: str, ifindex: int, ip: str, val: object) -> dict:
    """Build one walk_table dict for ipNetToMediaTable.

    Base OID 1.3.6.1.2.1.4.22.1 is ipNetToMediaEntry, so walk_table
    suffixes are "<col>.<ifindex>.<ip>" with no leading entry subid.
    """
    return {f"{col}.{ifindex}.{ip}": val}


# ---------------------------------------------------------------------------
# collect_arp — ipNetToMediaTable tests
# ---------------------------------------------------------------------------

async def test_collect_arp_returns_entries():
    """Realistic ipNetToMediaTable data produces correct ArpEntry list."""
    ifindex = 501
    ip = "192.0.2.1"
    # MAC as hex string (snmp_collector converts non-UTF8 OctetString to hex)
    mac_hex = "aabbccddeeff"

    walk_result = [
        _arp_row("1", ifindex, ip, ifindex),  # ifIndex col
        _arp_row("2", ifindex, ip, mac_hex),  # physAddress col
        _arp_row("3", ifindex, ip, ip),       # netAddress col
        _arp_row("4", ifindex, ip, 3),        # type=3 (dynamic)
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_arp(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    e = entries[0]
    assert e.ip_address == ip
    assert e.mac_address == "aa:bb:cc:dd:ee:ff"
    assert e.interface_index == ifindex
    assert e.entry_type == "dynamic"


async def test_collect_arp_empty_table_triggers_fallback():
    """Empty primary table causes a second walk_table call on the fallback OID."""
    call_args = []

    async def mock_walk(device_ip, community, oid, **kwargs):
        call_args.append(oid)
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        result = await collect_arp(DEVICE_IP, COMMUNITY)

    assert result == []
    assert len(call_args) == 2
    assert call_args[0] == _OID_ARP_TABLE
    assert call_args[1] == _OID_ARP_PHYSICAL


async def test_collect_arp_fallback_also_empty():
    """Both tables empty → returns [] without raising."""
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=[])):
        result = await collect_arp(DEVICE_IP, COMMUNITY)

    assert result == []


async def test_collect_arp_mac_normalization():
    """MAC addresses are normalized to lowercase colon-separated format."""
    # Simulate hex string output from walk_table (binary OctetString path)
    walk_result = [
        _arp_row("2", 1, "192.0.2.10", "0011aabbccdd"),
        _arp_row("4", 1, "192.0.2.10", 3),
    ]
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_arp(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].mac_address == "00:11:aa:bb:cc:dd"


async def test_collect_arp_skips_malformed_rows():
    """Row missing MAC column is skipped; other rows are still returned."""
    ip_good = "192.0.2.1"
    ip_bad = "192.0.2.2"

    walk_result = [
        # Good row — has all columns
        _arp_row("2", 1, ip_good, "aabbccddeeff"),
        _arp_row("4", 1, ip_good, 3),
        # Bad row — missing MAC (col 2)
        _arp_row("4", 1, ip_bad, 3),
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_arp(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].ip_address == ip_good


async def test_collect_arp_entry_types():
    """All four ipNetToMediaType integers map to the correct string values."""
    type_map = {1: "other", 2: "invalid", 3: "dynamic", 4: "static"}

    for type_int, expected_str in type_map.items():
        ip = f"192.0.2.{type_int}"
        walk_result = [
            _arp_row("2", 1, ip, "aabbccddeeff"),
            _arp_row("4", 1, ip, type_int),
        ]
        with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
            entries = await collect_arp(DEVICE_IP, COMMUNITY)

        assert len(entries) == 1, f"type_int={type_int}"
        assert entries[0].entry_type == expected_str, f"type_int={type_int}"


async def test_collect_arp_timeout_propagates():
    """SnmpTimeoutError from walk_table is re-raised unchanged."""
    with patch("app.snmp_collector.walk_table",
               AsyncMock(side_effect=SnmpTimeoutError("timed out"))):
        with pytest.raises(SnmpTimeoutError):
            await collect_arp(DEVICE_IP, COMMUNITY)


async def test_collect_arp_multiple_entries():
    """Multiple ARP rows across two interfaces are all returned."""
    rows = []
    for i in range(1, 4):
        ip = f"192.0.2.{i}"
        ifindex = 100 + i
        rows += [
            _arp_row("2", ifindex, ip, "aabbccddee" + f"{i:02x}"),
            _arp_row("4", ifindex, ip, 3),
        ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=rows)):
        entries = await collect_arp(DEVICE_IP, COMMUNITY)

    assert len(entries) == 3
    ifaces = {e.interface_index for e in entries}
    assert ifaces == {101, 102, 103}


async def test_collect_arp_mac_utf8_decoded_string():
    """MACs where snmp_collector UTF-8 decoded the OctetString are recovered correctly.

    When all 6 MAC bytes are valid UTF-8, _convert_value returns a Python str
    instead of a hex string. _mac_from_bytes must re-encode with utf-8 to
    recover the original bytes. Covers the real-world case found on SRX320:
    MAC 24:2f:d0:b1:35:23 — bytes 0xD0 0xB1 form the UTF-8 sequence for U+0431,
    yielding a 5-char decoded string instead of a 12-char hex string.
    """
    # 24:2f:d0:b1:35:23 → bytes b'\x24\x2f\xd0\xb1\x35\x23'
    # UTF-8 decoded: '$/' + chr(0x0431) + '5#' = 5 chars (0xD0 0xB1 → U+0431)
    mac_bytes = b"\x24\x2f\xd0\xb1\x35\x23"
    mac_as_utf8_str = mac_bytes.decode("utf-8")   # 5-char string
    assert len(mac_as_utf8_str) == 5              # verify the test premise

    walk_result = [
        _arp_row("2", 542, "192.0.2.121", mac_as_utf8_str),
        _arp_row("4", 542, "192.0.2.121", 3),
    ]
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_arp(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].mac_address == "24:2f:d0:b1:35:23"


async def test_collect_arp_unknown_type_maps_to_other():
    """An unrecognised type integer falls back to 'other'."""
    walk_result = [
        _arp_row("2", 1, "192.0.2.1", "aabbccddeeff"),
        _arp_row("4", 1, "192.0.2.1", 99),
    ]
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_arp(DEVICE_IP, COMMUNITY)

    assert entries[0].entry_type == "other"
