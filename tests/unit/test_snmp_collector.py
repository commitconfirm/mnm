"""Unit tests for controller/app/snmp_collector.py.

Mocks at the pysnmp boundary: bulk_cmd and get_cmd are patched directly.
No real SNMP traffic is generated. Test per CODING_STANDARDS.md section
"Testing" — mock at the boundary, not internally.

v1.0 test infrastructure introduction: pytest + pytest-asyncio are added
to requirements.txt alongside these tests.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, patch

import pytest

# Ensure controller/app is importable without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning, module="pysnmp")

from pyasn1.type.univ import ObjectIdentifier
from pysnmp.proto import rfc1902, errind
from pysnmp.proto.rfc1905 import NoSuchInstance

import re

from app.snmp_collector import (
    OIDS,
    SnmpAuthError,
    SnmpError,
    SnmpTimeoutError,
    collect_ifindex_to_name,
    get_scalar,
    mac_from_bytes,
    mac_from_dotted_decimal,
    oid,
    walk_table,
)


# ---------------------------------------------------------------------------
# Helpers — build realistic pysnmp varBind structures
# ---------------------------------------------------------------------------

def _oid(oid_str: str) -> ObjectIdentifier:
    """Build a pyasn1 ObjectIdentifier from a dotted-decimal string."""
    return ObjectIdentifier().clone(oid_str)


def _bulk_varbinds(*oid_value_pairs: tuple[str, object]) -> list[list]:
    """Build a varBinds list-of-lists for testing the defensive unwrap path.

    Each pair is (oid_string, pysnmp_value). The inner list wraps a
    minimal two-tuple (oid, value) to match the list-of-lists shape that
    snmp_collector handles via ``ot = item[0] if isinstance(item, list) else item``.
    """
    return [[(oid(s), v)] for s, v in oid_value_pairs
            if (oid := lambda x: _oid(x)) or True]


def _make_bulk_varbind(oid_str: str, val: object) -> list:
    """Single varBind list entry for bulk_cmd output."""
    return [(_oid(oid_str), val)]


BASE_OID = "1.3.6.1.2.1.4.22.1"
DEVICE_IP = "198.51.100.1"
COMMUNITY = "test-ro"


# ---------------------------------------------------------------------------
# walk_table tests
# ---------------------------------------------------------------------------

async def test_walk_table_returns_rows():
    """bulk_cmd returning a two-entry ARP table yields two result dicts."""
    varbinds_batch1 = [
        _make_bulk_varbind(BASE_OID + ".2.1.10.0.0.1",
                           rfc1902.OctetString(b"\xaa\xbb\xcc\xdd\xee\xff")),
        _make_bulk_varbind(BASE_OID + ".3.1.10.0.0.1",
                           rfc1902.IpAddress("10.0.0.1")),
    ]
    # Second call returns an out-of-scope OID, signalling end of table.
    out_of_scope_oid = "1.3.6.1.2.1.99.1.1.10.0.0.2"
    varbinds_batch2 = [_make_bulk_varbind(out_of_scope_oid, rfc1902.Integer32(0))]

    mock_bulk = AsyncMock(side_effect=[
        (None, 0, 0, varbinds_batch1),
        (None, 0, 0, varbinds_batch2),
    ])

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert len(result) == 2
    # OctetString → bytes (raw, uninterpreted)
    assert {"2.1.10.0.0.1": b"\xaa\xbb\xcc\xdd\xee\xff"} in result
    assert {"3.1.10.0.0.1": "10.0.0.1"} in result


async def test_walk_table_empty():
    """bulk_cmd returning no varBinds yields an empty list."""
    mock_bulk = AsyncMock(return_value=(None, 0, 0, []))

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert result == []


async def test_walk_table_timeout():
    """bulk_cmd returning requestTimedOut raises SnmpTimeoutError."""
    mock_bulk = AsyncMock(
        return_value=(errind.requestTimedOut, 0, 0, [])
    )

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        with pytest.raises(SnmpTimeoutError):
            await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)


async def test_walk_table_auth_failure():
    """bulk_cmd returning authenticationFailure raises SnmpAuthError."""
    mock_bulk = AsyncMock(
        return_value=(errind.authenticationFailure, 0, 0, [])
    )

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        with pytest.raises(SnmpAuthError):
            await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)


async def test_walk_table_generic_error():
    """bulk_cmd returning an unrecognised error indication raises SnmpError."""
    mock_bulk = AsyncMock(
        return_value=(errind.otherError, 0, 0, [])
    )

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        with pytest.raises(SnmpError):
            await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)


async def test_walk_table_stops_at_table_boundary():
    """Walk stops when a varBind OID falls outside the base OID prefix."""
    in_scope = _make_bulk_varbind(
        BASE_OID + ".2.1.10.0.0.1", rfc1902.Integer32(1)
    )
    out_of_scope = _make_bulk_varbind(
        "1.3.6.1.2.1.99.0.1", rfc1902.Integer32(99)
    )

    mock_bulk = AsyncMock(return_value=(None, 0, 0, [in_scope, out_of_scope]))

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    # Only the in-scope row should be returned
    assert len(result) == 1
    assert "2.1.10.0.0.1" in result[0]


# ---------------------------------------------------------------------------
# get_scalar tests
# ---------------------------------------------------------------------------

SCALAR_OID = "1.3.6.1.2.1.1.1.0"  # sysDescr.0


def _get_varbinds(oid_str: str, val: object) -> tuple:
    """Build the tuple-of-ObjectType that get_cmd returns."""
    # get_cmd returns varBinds as a tuple of (oid, value) two-tuples,
    # not wrapped in lists like bulk_cmd.
    return ((_oid(oid_str), val),)


async def test_get_scalar_returns_value():
    """get_cmd returning an OctetString (e.g. sysDescr) yields raw bytes.

    Callers that want text must decode explicitly:
    ``result.decode("utf-8", errors="replace")``.
    """
    description = "Linux mnm-device 5.15.0 #1 SMP x86_64"
    varbinds = _get_varbinds(
        SCALAR_OID, rfc1902.OctetString(description.encode())
    )
    mock_get = AsyncMock(return_value=(None, 0, 0, varbinds))

    with patch("app.snmp_collector.get_cmd", mock_get):
        result = await get_scalar(DEVICE_IP, COMMUNITY, SCALAR_OID)

    assert result == description.encode()
    assert isinstance(result, bytes)
    # Caller is responsible for text decoding:
    assert result.decode("utf-8") == description


async def test_get_scalar_missing_oid():
    """get_cmd returning NoSuchInstance yields None from get_scalar."""
    varbinds = _get_varbinds(SCALAR_OID, NoSuchInstance())
    mock_get = AsyncMock(return_value=(None, 0, 0, varbinds))

    with patch("app.snmp_collector.get_cmd", mock_get):
        result = await get_scalar(DEVICE_IP, COMMUNITY, SCALAR_OID)

    assert result is None


async def test_get_scalar_timeout():
    """get_cmd returning requestTimedOut raises SnmpTimeoutError."""
    mock_get = AsyncMock(return_value=(errind.requestTimedOut, 0, 0, ()))

    with patch("app.snmp_collector.get_cmd", mock_get):
        with pytest.raises(SnmpTimeoutError):
            await get_scalar(DEVICE_IP, COMMUNITY, SCALAR_OID)


async def test_get_scalar_integer_value():
    """get_cmd returning an Integer32 yields a Python int."""
    varbinds = _get_varbinds("1.3.6.1.2.1.1.7.0", rfc1902.Integer32(72))
    mock_get = AsyncMock(return_value=(None, 0, 0, varbinds))

    with patch("app.snmp_collector.get_cmd", mock_get):
        result = await get_scalar(DEVICE_IP, COMMUNITY, "1.3.6.1.2.1.1.7.0")

    assert result == 72
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# _convert_value tests (indirectly via walk_table / get_scalar)
# ---------------------------------------------------------------------------

async def test_walk_table_converts_timeticks_to_int():
    """TimeTicks values are returned as int (centiseconds)."""
    varbinds = [_make_bulk_varbind(BASE_OID + ".1.0", rfc1902.TimeTicks(360000))]
    mock_bulk = AsyncMock(side_effect=[
        (None, 0, 0, varbinds),
        (None, 0, 0, []),  # end
    ])

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert len(result) == 1
    val = result[0]["1.0"]
    assert val == 360000
    assert isinstance(val, int)


async def test_walk_table_octetstring_returns_raw_bytes():
    """OctetString values are returned as bytes regardless of content."""
    # Bytes that are invalid UTF-8 — previously went through hex fallback.
    binary_mac = b"\xff\xfe\xaa\xbb\xcc\xdd"
    varbinds = [_make_bulk_varbind(BASE_OID + ".1.0", rfc1902.OctetString(binary_mac))]
    mock_bulk = AsyncMock(side_effect=[
        (None, 0, 0, varbinds),
        (None, 0, 0, []),
    ])

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert result[0]["1.0"] == binary_mac
    assert isinstance(result[0]["1.0"], bytes)


async def test_walk_table_octetstring_text_returns_bytes():
    """OctetString containing ASCII text also returns bytes (caller decodes)."""
    text = b"Juniper SRX320"
    varbinds = [_make_bulk_varbind(BASE_OID + ".1.0", rfc1902.OctetString(text))]
    mock_bulk = AsyncMock(side_effect=[
        (None, 0, 0, varbinds),
        (None, 0, 0, []),
    ])

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert result[0]["1.0"] == text
    assert isinstance(result[0]["1.0"], bytes)


async def test_walk_table_octetstring_utf8_multibyte_bytes_unchanged():
    """OctetString whose bytes form valid UTF-8 multibyte sequences (e.g. MAC bytes
    24:2f:d0:b1:35:23 where 0xd0 0xb1 = U+0431) returns the original bytes unchanged.

    This was the root-cause failure: the old eager UTF-8 decode returned a 5-char
    Python str instead of 6 bytes, breaking MAC parsing downstream.
    """
    mac_bytes = b"\x24\x2f\xd0\xb1\x35\x23"
    varbinds = [_make_bulk_varbind(BASE_OID + ".2.542.192.0.2.121",
                                   rfc1902.OctetString(mac_bytes))]
    mock_bulk = AsyncMock(side_effect=[
        (None, 0, 0, varbinds),
        (None, 0, 0, []),
    ])

    with patch("app.snmp_collector.bulk_cmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert result[0]["2.542.192.0.2.121"] == mac_bytes
    assert len(result[0]["2.542.192.0.2.121"]) == 6


# ---------------------------------------------------------------------------
# mac_from_bytes tests
# ---------------------------------------------------------------------------

def test_mac_from_bytes_valid():
    """Exactly 6 bytes are converted to lowercase colon-separated form."""
    assert mac_from_bytes(b"\xaa\xbb\xcc\xdd\xee\xff") == "aa:bb:cc:dd:ee:ff"
    assert mac_from_bytes(b"\x00\x00\x00\x00\x00\x00") == "00:00:00:00:00:00"
    assert mac_from_bytes(b"\x24\x2f\xd0\xb1\x35\x23") == "24:2f:d0:b1:35:23"


def test_mac_from_bytes_wrong_length():
    """Input shorter or longer than 6 bytes raises ValueError."""
    with pytest.raises(ValueError):
        mac_from_bytes(b"\xaa\xbb\xcc\xdd\xee")       # 5 bytes
    with pytest.raises(ValueError):
        mac_from_bytes(b"\xaa\xbb\xcc\xdd\xee\xff\x00")  # 7 bytes
    with pytest.raises(ValueError):
        mac_from_bytes(b"")


# ---------------------------------------------------------------------------
# mac_from_dotted_decimal tests
# ---------------------------------------------------------------------------

def test_mac_from_dotted_decimal_valid():
    """Six dot-separated decimal byte values convert to colon-separated hex."""
    assert mac_from_dotted_decimal("170.187.204.221.238.255") == "aa:bb:cc:dd:ee:ff"
    assert mac_from_dotted_decimal("0.0.0.0.0.0") == "00:00:00:00:00:00"
    assert mac_from_dotted_decimal("36.47.208.177.53.35") == "24:2f:d0:b1:35:23"


def test_mac_from_dotted_decimal_wrong_count():
    """Input with wrong number of dot-separated parts raises ValueError."""
    with pytest.raises(ValueError):
        mac_from_dotted_decimal("170.187.204.221.238")       # 5 parts
    with pytest.raises(ValueError):
        mac_from_dotted_decimal("170.187.204.221.238.255.0")  # 7 parts
    with pytest.raises(ValueError):
        mac_from_dotted_decimal("")


def test_mac_from_dotted_decimal_out_of_range():
    """A byte value outside 0–255 raises ValueError."""
    with pytest.raises(ValueError):
        mac_from_dotted_decimal("170.187.256.221.238.255")  # 256 > 255
    with pytest.raises(ValueError):
        mac_from_dotted_decimal("170.187.abc.221.238.255")  # non-integer


# ---------------------------------------------------------------------------
# OID registry tests
# ---------------------------------------------------------------------------

_NUMERIC_OID_RE = re.compile(r"^\d+(\.\d+)+$")

_EXPECTED_OID_NAMES = [
    "IP-MIB::ipNetToMediaEntry",
    "IP-MIB::ipNetToPhysicalEntry",
    "Q-BRIDGE-MIB::dot1qTpFdbEntry",
    "BRIDGE-MIB::dot1dTpFdbEntry",
    "Q-BRIDGE-MIB::dot1qVlanCurrentEntry",
    "JUNIPER-L2ALD-MIB::jnxL2aldVlanEntry",
    "IP-FORWARD-MIB::ipCidrRouteEntry",
    "RFC1213-MIB::ipRouteEntry",
    "LLDP-MIB::lldpRemEntry",
    "LLDP-MIB::lldpRemManAddrEntry",
    "IF-MIB::ifDescr",
    "IF-MIB::ifName",
]


def test_oid_registry_resolves_known_name():
    """oid() returns the correct numeric string for a registered name."""
    assert oid("IP-MIB::ipNetToMediaEntry") == "1.3.6.1.2.1.4.22.1"


def test_oid_registry_raises_on_unknown_name():
    """oid() raises KeyError for a name not in the registry."""
    with pytest.raises(KeyError):
        oid("NONEXISTENT::fake")


def test_oid_registry_entries_are_valid_oids():
    """Every value in OIDS looks like a valid dotted-decimal numeric OID."""
    for name, numeric in OIDS.items():
        assert _NUMERIC_OID_RE.match(numeric), (
            f"OIDS[{name!r}] = {numeric!r} does not look like a numeric OID"
        )


def test_oid_registry_has_expected_entries():
    """All expected symbolic names are present in the registry."""
    for name in _EXPECTED_OID_NAMES:
        assert name in OIDS, f"Expected OID name {name!r} missing from registry"


# ---------------------------------------------------------------------------
# collect_ifindex_to_name tests
# ---------------------------------------------------------------------------

_OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
_OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"


async def test_collect_ifindex_to_name_prefers_ifname():
    """When ifName is populated, ifDescr is not consulted."""
    ifname_rows = [
        {"526": b"ge-0/0/12"},
        {"536": b"ge-0/0/22"},
    ]
    called = []

    async def mock_walk(device_ip, community, oid, **kwargs):
        called.append(oid)
        if oid == _OID_IF_NAME:
            return ifname_rows
        return []

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        result = await collect_ifindex_to_name("198.51.100.1", "test")

    assert result == {526: "ge-0/0/12", 536: "ge-0/0/22"}
    assert _OID_IF_NAME in called
    assert _OID_IF_DESCR not in called


async def test_collect_ifindex_to_name_falls_back_to_ifdescr():
    """Empty ifName walk triggers ifDescr fallback."""
    ifdescr_rows = [
        {"1": b"eth0 Ethernet interface"},
        {"2": b"lo loopback"},
    ]
    called = []

    async def mock_walk(device_ip, community, oid, **kwargs):
        called.append(oid)
        if oid == _OID_IF_DESCR:
            return ifdescr_rows
        return []  # ifName empty

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        result = await collect_ifindex_to_name("198.51.100.1", "test")

    assert result == {1: "eth0 Ethernet interface", 2: "lo loopback"}
    assert _OID_IF_NAME in called
    assert _OID_IF_DESCR in called


async def test_collect_ifindex_to_name_empty_on_error():
    """SnmpError during walk yields empty dict, not exception."""
    async def mock_walk(device_ip, community, oid, **kwargs):
        raise SnmpError("oid not implemented")

    with patch("app.snmp_collector.walk_table", side_effect=mock_walk):
        result = await collect_ifindex_to_name("198.51.100.1", "test")

    assert result == {}
