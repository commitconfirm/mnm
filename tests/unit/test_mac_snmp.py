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

from app.mac_snmp import MacEntry, _parse_junos_fdb_to_vlan, collect_mac
from app.snmp_collector import SnmpError, SnmpTimeoutError

DEVICE_IP = "198.51.100.1"
COMMUNITY = "test-ro"

_OID_Q_BRIDGE = "1.3.6.1.2.1.17.7.1.2.2.1"
_OID_BRIDGE = "1.3.6.1.2.1.17.4.3.1"
_OID_VLAN_CURRENT = "1.3.6.1.2.1.17.7.1.4.2.1"
_OID_JUNOS_VLAN = "1.3.6.1.4.1.2636.3.48.1.3.1.1"

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


def _vlan_row(vlan_id: int, fdb_id: int) -> dict:
    """Build one walk_table dict for dot1qVlanCurrentTable col .3.

    Suffix: "3.0.<vlan_id>" — TimeMark=0, VlanIndex=vlan_id. Value is fdb_id.
    """
    return {f"3.0.{vlan_id}": fdb_id}


def _junos_vlan_rows(entries: list[tuple[int, str, int, int, int]]) -> list[dict]:
    """Build walk_table rows for jnxL2aldVlanTable.

    Each entry tuple: (idx, name, vlan_tag, vlan_type, fdb_id).
    Columns: .2=name, .3=vlan_tag, .4=type, .5=fdb_id.
    """
    rows: list[dict] = []
    for idx, name, tag, vtype, fdb_id in entries:
        rows.append({f"2.{idx}": name.encode()})
        rows.append({f"3.{idx}": tag})
        rows.append({f"4.{idx}": vtype})
        rows.append({f"5.{idx}": fdb_id})
    return rows


# ---------------------------------------------------------------------------
# collect_mac — dot1qTpFdbTable (primary / VLAN-aware) tests
# ---------------------------------------------------------------------------

async def test_collect_mac_primary_table():
    """Realistic dot1qTpFdbTable data produces MacEntry with resolved vlan."""
    fdb_id = 65536  # Junos-style: not the 802.1Q ID directly
    vlan = 100
    port = 12
    fdb_rows = [
        _q_row("1", fdb_id, MAC_DOT, MAC_BYTES),  # dot1qTpFdbAddress
        _q_row("2", fdb_id, MAC_DOT, port),        # dot1qTpFdbPort
        _q_row("3", fdb_id, MAC_DOT, 3),           # dot1qTpFdbStatus=learned
    ]
    vlan_rows = [_vlan_row(vlan, fdb_id)]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        if oid == _OID_VLAN_CURRENT:
            return vlan_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
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
    """Entries from multiple VLANs are all returned with correct resolved VLAN IDs."""
    fdb_rows = []
    vlan_rows = []
    for vlan in [10, 20, 30]:
        fdb_id = vlan * 65536  # Junos-style encoding
        mac_dot = f"170.187.204.221.238.{vlan}"
        fdb_rows += [
            _q_row("2", fdb_id, mac_dot, vlan),  # port == vlan for easy verification
            _q_row("3", fdb_id, mac_dot, 3),
        ]
        vlan_rows.append(_vlan_row(vlan, fdb_id))

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        if oid == _OID_VLAN_CURRENT:
            return vlan_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
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


# ---------------------------------------------------------------------------
# FDB ID resolution tests
# ---------------------------------------------------------------------------

async def test_collect_mac_resolves_junos_fdb_ids():
    """Junos FDB IDs (multiples of 65536) are resolved to real 802.1Q VLAN IDs."""
    fdb_id = 65536  # Junos internal ID for VLAN 140
    vlan = 140
    fdb_rows = [
        _q_row("2", fdb_id, MAC_DOT, 5),
        _q_row("3", fdb_id, MAC_DOT, 3),
    ]
    vlan_rows = [_vlan_row(vlan, fdb_id)]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        if oid == _OID_VLAN_CURRENT:
            return vlan_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].vlan == vlan


async def test_collect_mac_vlan_matches_fdb_id():
    """When FDB ID equals VLAN ID (e.g. on non-Junos switches), resolution is transparent."""
    fdb_id = 10
    vlan = 10
    fdb_rows = [
        _q_row("2", fdb_id, MAC_DOT, 3),
        _q_row("3", fdb_id, MAC_DOT, 3),
    ]
    vlan_rows = [_vlan_row(vlan, fdb_id)]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        if oid == _OID_VLAN_CURRENT:
            return vlan_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].vlan == vlan


async def test_collect_mac_vlan_map_empty():
    """When FDB table has entries but VLAN table is empty, vlan=None and one warning logged."""
    fdb_rows = [
        _q_row("2", 65536, MAC_DOT, 5),
        _q_row("3", 65536, MAC_DOT, 3),
    ]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        return []  # VLAN table empty

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.mac_snmp.log") as mock_log:
            entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].vlan is None

    warning_events = [call[0][0] for call in mock_log.warning.call_args_list]
    assert "mac_snmp_vlan_map_empty" in warning_events
    assert warning_events.count("mac_snmp_vlan_map_empty") == 1


async def test_collect_mac_orphan_fdb_id():
    """FDB ID with no matching VLAN map entry gets vlan=None; one aggregated warning logged."""
    orphan_fdb_id = 99999
    fdb_rows = [
        _q_row("2", orphan_fdb_id, MAC_DOT, 5),
        _q_row("3", orphan_fdb_id, MAC_DOT, 3),
    ]
    # VLAN map contains a different FDB ID — orphan_fdb_id is absent
    vlan_rows = [_vlan_row(10, 10)]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        if oid == _OID_VLAN_CURRENT:
            return vlan_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.mac_snmp.log") as mock_log:
            entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert len(entries) == 1
    assert entries[0].vlan is None

    warning_events = [call[0][0] for call in mock_log.warning.call_args_list]
    assert "mac_snmp_orphan_fdb_ids" in warning_events
    assert warning_events.count("mac_snmp_orphan_fdb_ids") == 1


async def test_collect_mac_fallback_path_skips_vlan_walk():
    """When Q-BRIDGE table is empty, BRIDGE fallback is used and VLAN map is never walked."""
    call_oids = []

    async def mock_walk(device_ip, community, oid, **kwargs):
        call_oids.append(oid)
        if oid == _OID_Q_BRIDGE:
            return []
        if oid == _OID_BRIDGE:
            return [
                _b_row("2", MAC_DOT, 5),
                _b_row("3", MAC_DOT, 3),
            ]
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert _OID_VLAN_CURRENT not in call_oids
    assert len(call_oids) == 2
    assert call_oids[0] == _OID_Q_BRIDGE
    assert call_oids[1] == _OID_BRIDGE
    assert len(entries) == 1
    assert entries[0].vlan is None


# ---------------------------------------------------------------------------
# _parse_junos_fdb_to_vlan (pure-parser tests)
# ---------------------------------------------------------------------------

def test_parse_junos_fdb_to_vlan_valid_rows():
    """Three well-formed rows produce the expected fdb_id → vlan_id mapping."""
    rows = _junos_vlan_rows([
        (2, "borgGrid_120",      120, 1, 131072),
        (3, "default",             1, 1, 196608),
        (4, "ferengiMarket_130", 130, 1, 262144),
    ])
    assert _parse_junos_fdb_to_vlan(rows) == {131072: 120, 196608: 1, 262144: 130}


def test_parse_junos_fdb_to_vlan_skips_vlan_tag_zero():
    """Row with vlanTag=0 (Juniper internal) is skipped."""
    rows = _junos_vlan_rows([
        (1, "____juniper_private1____", 0, 2, 65536),
        (2, "user_vlan",              100, 1, 131072),
    ])
    assert _parse_junos_fdb_to_vlan(rows) == {131072: 100}


def test_parse_junos_fdb_to_vlan_skips_out_of_range_vlan():
    """Rows with vlanTag outside 1..4094 are skipped."""
    rows = _junos_vlan_rows([
        (1, "too_high",    5000, 1, 131072),
        (2, "negative",      -1, 1, 196608),
        (3, "valid",        120, 1, 262144),
        (4, "edge_top",    4095, 1, 327680),  # 4095 is reserved, out of user range
    ])
    assert _parse_junos_fdb_to_vlan(rows) == {262144: 120}


def test_parse_junos_fdb_to_vlan_skips_zero_fdb_id():
    """Row with fdb_id=0 is skipped."""
    rows = _junos_vlan_rows([
        (1, "no_fdb",    100, 1, 0),
        (2, "has_fdb",   120, 1, 131072),
    ])
    assert _parse_junos_fdb_to_vlan(rows) == {131072: 120}


def test_parse_junos_fdb_to_vlan_empty_input():
    """Empty row list yields empty mapping."""
    assert _parse_junos_fdb_to_vlan([]) == {}


def test_parse_junos_fdb_to_vlan_missing_columns():
    """Rows missing vlanTag (.3) or vlanFdbId (.5) are skipped silently."""
    rows = [
        # idx 2: only name column, nothing else
        {"2.2": b"orphan_name_only"},
        # idx 3: has vlan_tag but no fdb_id → treated as fdb_id=0, skipped
        {"3.3": 50},
        # idx 4: has fdb_id but no vlan_tag → treated as vlan=0, skipped
        {"5.4": 131072},
        # idx 5: complete valid row
        {"2.5": b"complete"},
        {"3.5": 130},
        {"5.5": 262144},
    ]
    assert _parse_junos_fdb_to_vlan(rows) == {262144: 130}


# ---------------------------------------------------------------------------
# collect_mac — Junos fallback integration tests
# ---------------------------------------------------------------------------

async def test_collect_mac_junos_fallback_triggered():
    """Standard VLAN map empty and FDB has unresolved IDs → Junos walk used."""
    fdb_id = 131072
    vlan = 120
    fdb_rows = [
        _q_row("2", fdb_id, MAC_DOT, 7),
        _q_row("3", fdb_id, MAC_DOT, 3),
    ]
    junos_rows = _junos_vlan_rows([(2, "borgGrid_120", vlan, 1, fdb_id)])
    called_oids: list[str] = []

    async def mock_walk(device_ip, community, oid, **kwargs):
        called_oids.append(oid)
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        if oid == _OID_VLAN_CURRENT:
            return []            # standard path empty → triggers Junos
        if oid == _OID_JUNOS_VLAN:
            return junos_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert _OID_JUNOS_VLAN in called_oids
    assert len(entries) == 1
    assert entries[0].vlan == vlan


async def test_collect_mac_junos_fallback_not_triggered_when_standard_works():
    """Standard VLAN map has data → Junos walk is NOT attempted."""
    fdb_id = 10
    vlan = 10
    fdb_rows = [
        _q_row("2", fdb_id, MAC_DOT, 3),
        _q_row("3", fdb_id, MAC_DOT, 3),
    ]
    vlan_rows = [_vlan_row(vlan, fdb_id)]
    called_oids: list[str] = []

    async def mock_walk(device_ip, community, oid, **kwargs):
        called_oids.append(oid)
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        if oid == _OID_VLAN_CURRENT:
            return vlan_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert _OID_JUNOS_VLAN not in called_oids
    assert len(entries) == 1
    assert entries[0].vlan == vlan


async def test_collect_mac_junos_fallback_empty_returns_vlan_none():
    """Standard empty and Junos empty → entries have vlan=None (today's behavior)."""
    fdb_id = 131072
    fdb_rows = [
        _q_row("2", fdb_id, MAC_DOT, 5),
        _q_row("3", fdb_id, MAC_DOT, 3),
    ]
    called_oids: list[str] = []

    async def mock_walk(device_ip, community, oid, **kwargs):
        called_oids.append(oid)
        return fdb_rows if oid == _OID_Q_BRIDGE else []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        with patch("app.mac_snmp.log") as mock_log:
            entries = await collect_mac(DEVICE_IP, COMMUNITY)

    assert _OID_JUNOS_VLAN in called_oids        # fallback was attempted
    assert len(entries) == 1
    assert entries[0].vlan is None               # but no mapping found

    warning_events = [c[0][0] for c in mock_log.warning.call_args_list]
    assert "mac_snmp_vlan_map_empty" in warning_events


async def test_collect_mac_junos_fallback_handles_snmp_error():
    """Junos walk raising SnmpError is caught; entries get vlan=None."""
    fdb_id = 131072
    fdb_rows = [
        _q_row("2", fdb_id, MAC_DOT, 5),
        _q_row("3", fdb_id, MAC_DOT, 3),
    ]

    async def mock_walk(device_ip, community, oid, **kwargs):
        if oid == _OID_Q_BRIDGE:
            return fdb_rows
        if oid == _OID_VLAN_CURRENT:
            return []
        if oid == _OID_JUNOS_VLAN:
            raise SnmpError("enterprise MIB not implemented")
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        entries = await collect_mac(DEVICE_IP, COMMUNITY)

    # No exception propagates; entry returned with vlan=None
    assert len(entries) == 1
    assert entries[0].vlan is None
