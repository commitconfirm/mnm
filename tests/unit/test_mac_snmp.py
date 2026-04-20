"""Unit tests for controller/app/mac_snmp.py.

Mocks at snmp_collector.walk_table boundary — no real SNMP traffic.
walk_table returns list[dict[str, Any]] with already-converted native
types, matching what the real snmp_collector delivers.

OID index conventions:
  dot1qTpFdbTable suffix: "<col>.<vlan>.<b0>.<b1>.<b2>.<b3>.<b4>.<b5>"
  dot1dTpFdbTable suffix: "<col>.<b0>.<b1>.<b2>.<b3>.<b4>.<b5>"

MAC bytes in the OID index are dotted-decimal (decimal value of each byte).
physAddress column values are bytes per the snmp_collector OctetString contract.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

from app.mac_snmp import MacEntry, collect_mac
from app.snmp_collector import SnmpTimeoutError

DEVICE_IP = "198.51.100.1"
COMMUNITY = "test-ro"

_OID_Q_BRIDGE = "1.3.6.1.2.1.17.7.1.2.2.1"
_OID_BRIDGE = "1.3.6.1.2.1.17.4.3.1"

# Dotted-decimal for aa:bb:cc:dd:ee:ff = 170.187.204.221.238.255
MAC_HEX = "aa:bb:cc:dd:ee:ff"
MAC_DOT = "170.187.204.221.238.255"
MAC_BYTES = b"\xaa\xbb\xcc\xdd\xee\xff"

MAC2_HEX = "bc:24:11:69:50:19"
MAC2_DOT = "188.36.17.105.80.25"
MAC2_BYTES = b"\xbc\x24\x11\x69\x50\x19"


def _q_row(col: str, vlan: int, mac_dot: str, val: object) -> dict:
    """Build one walk_table dict for dot1qTpFdbTable.

    Suffix: "<col>.<vlan>.<mac_dot>" — index is VLAN then 6 MAC bytes.
    """
    return {f"{col}.{vlan}.{mac_dot}": val}


def _b_row(col: str, mac_dot: str, val: object) -> dict:
    """Build one walk_table dict for dot1dTpFdbTable.

    Suffix: "<col>.<mac_dot>" — index is 6 MAC bytes only, no VLAN.
    """
    return {f"{col}.{mac_dot}": val}


# ---------------------------------------------------------------------------
# collect_mac — dot1qTpFdbTable (primary / VLAN-aware) tests
# ---------------------------------------------------------------------------

async def test_collect_mac_primary_table():
    """Realistic dot1qTpFdbTable data produces MacEntry with vlan populated."""
    vlan = 100
    port = 12
    walk_result = [
        _q_row("1", vlan, MAC_DOT, MAC_BYTES),  # dot1qTpFdbAddress
        _q_row("2", vlan, MAC_DOT, port),        # dot1qTpFdbPort
        _q_row("3", vlan, MAC_DOT, 3),           # dot1qTpFdbStatus=learned
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    e = entries[0]
    assert e.mac_address == MAC_HEX
    assert e.vlan == vlan
    assert e.bridge_port == port
    assert e.entry_status == "learned"


async def test_collect_mac_fallback_table():
    """Empty primary table triggers fallback; entries have vlan=None."""
    port = 5
    call_args = []

    async def mock_walk(device_ip, community, oid, **kwargs):
        call_args.append(oid)
        if oid == _OID_Q_BRIDGE:
            return []
        # fallback returns one bridge entry
        return [
            _b_row("2", MAC_DOT, port),
            _b_row("3", MAC_DOT, 3),
        ]

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(call_args) == 2
    assert call_args[0] == _OID_Q_BRIDGE
    assert call_args[1] == _OID_BRIDGE

    assert len(entries) == 1
    e = entries[0]
    assert e.mac_address == MAC_HEX
    assert e.vlan is None
    assert e.bridge_port == port
    assert e.entry_status == "learned"


async def test_collect_mac_both_empty():
    """Both tables empty returns [] without raising."""
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=[])):
        result = await collect_mac(DEVICE_IP, COMMUNITY)

    assert result == []


async def test_collect_mac_index_mac_mismatch_logged():
    """Row where index MAC differs from col-1 MAC: warning logged, entry
    returned using the index-derived MAC."""
    # Index says MAC_HEX but col.1 says MAC2_HEX
    walk_result = [
        _q_row("1", 100, MAC_DOT, MAC2_BYTES),  # col.1 disagrees with index
        _q_row("2", 100, MAC_DOT, 7),
        _q_row("3", 100, MAC_DOT, 3),
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        with patch("app.mac_snmp.log") as mock_log:
            entries = await collect_mac(DEVICE_IP, COMMUNITY)

    # Warning must have been logged
    mock_log.warning.assert_called()
    warning_event = mock_log.warning.call_args[0][0]
    assert warning_event == "mac_snmp_index_mac_mismatch"

    # Entry still returned, using index-derived MAC
    assert len(entries) == 1
    assert entries[0].mac_address == MAC_HEX


async def test_collect_mac_skips_malformed():
    """Row with a malformed MAC in the index is skipped; others are returned."""
    # "999.0.0.0.0.0" — byte value 999 is out of range
    bad_mac_dot = "999.0.0.0.0.0"
    walk_result = [
        # Bad row
        _q_row("2", 100, bad_mac_dot, 7),
        _q_row("3", 100, bad_mac_dot, 3),
        # Good row
        _q_row("2", 100, MAC_DOT, 5),
        _q_row("3", 100, MAC_DOT, 3),
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].mac_address == MAC_HEX


async def test_collect_mac_status_enum():
    """All five status integer values map to the correct string."""
    status_map = {1: "other", 2: "invalid", 3: "learned", 4: "self", 5: "mgmt"}

    for status_int, expected_str in status_map.items():
        # Use a unique MAC per status to get distinct index_keys
        mac_dot = f"170.187.204.221.238.{status_int}"
        walk_result = [
            _q_row("2", 100, mac_dot, 8),
            _q_row("3", 100, mac_dot, status_int),
        ]
        with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
            entries = await collect_mac(DEVICE_IP, COMMUNITY)

        assert len(entries) == 1, f"status_int={status_int}"
        assert entries[0].entry_status == expected_str, f"status_int={status_int}"


async def test_collect_mac_normalization():
    """MAC is always returned as lowercase colon-separated format."""
    walk_result = [
        _q_row("2", 200, MAC2_DOT, 3),
        _q_row("3", 200, MAC2_DOT, 3),
    ]
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].mac_address == MAC2_HEX


async def test_collect_mac_multiple_vlans():
    """Entries from multiple VLANs are all returned with correct VLAN IDs."""
    rows = []
    for vlan in [10, 20, 30]:
        mac_dot = f"170.187.204.221.238.{vlan}"
        rows += [
            _q_row("2", vlan, mac_dot, vlan),  # port == vlan for easy verification
            _q_row("3", vlan, mac_dot, 3),
        ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=rows)):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(entries) == 3
    vlans = {e.vlan for e in entries}
    assert vlans == {10, 20, 30}
    for e in entries:
        assert e.bridge_port == e.vlan  # port was set to vlan value


async def test_collect_mac_timeout_propagates():
    """SnmpTimeoutError from walk_table is re-raised unchanged."""
    with patch("app.snmp_collector.walk_table",
               AsyncMock(side_effect=SnmpTimeoutError("timed out"))):
        with pytest.raises(SnmpTimeoutError):
            await collect_mac(DEVICE_IP, COMMUNITY)


async def test_collect_mac_unknown_status_skipped():
    """A row with a status integer outside 1-5 is skipped."""
    walk_result = [
        _q_row("2", 100, MAC_DOT, 5),
        _q_row("3", 100, MAC_DOT, 99),  # unknown status
    ]
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert entries == []


async def test_collect_mac_missing_port_skipped():
    """A row missing the port column (.2) is skipped."""
    walk_result = [
        # Only status column present, no port column
        _q_row("3", 100, MAC_DOT, 3),
    ]
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert entries == []
