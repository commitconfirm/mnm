"""Unit tests for controller/app/snmp_collector.py.

Mocks at the pysnmp boundary: bulkCmd and getCmd are patched directly.
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

# Suppress pysnmp-lextudio deprecation warning in test output.
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning, module="pysnmp")

from pyasn1.type.univ import ObjectIdentifier
from pysnmp.proto import rfc1902, errind
from pysnmp.proto.rfc1905 import NoSuchInstance

from app.snmp_collector import (
    SnmpAuthError,
    SnmpError,
    SnmpTimeoutError,
    get_scalar,
    walk_table,
)


# ---------------------------------------------------------------------------
# Helpers — build realistic pysnmp varBind structures
# ---------------------------------------------------------------------------

def _oid(oid_str: str) -> ObjectIdentifier:
    """Build a pyasn1 ObjectIdentifier from a dotted-decimal string."""
    return ObjectIdentifier().clone(oid_str)


def _bulk_varbinds(*oid_value_pairs: tuple[str, object]) -> list[list]:
    """Build the [[ObjectType], ...] structure that bulkCmd 6.x returns.

    Each pair is (oid_string, pysnmp_value). The inner list wraps a
    minimal two-tuple (oid, value) to match the structure that
    snmp_collector unpacks with ``ot = item[0] if isinstance(item, list) else item``.
    """
    return [[(oid(s), v)] for s, v in oid_value_pairs
            if (oid := lambda x: _oid(x)) or True]


def _make_bulk_varbind(oid_str: str, val: object) -> list:
    """Single varBind list entry for bulkCmd output."""
    return [(_oid(oid_str), val)]


BASE_OID = "1.3.6.1.2.1.4.22.1"
DEVICE_IP = "198.51.100.1"
COMMUNITY = "test-ro"


# ---------------------------------------------------------------------------
# walk_table tests
# ---------------------------------------------------------------------------

async def test_walk_table_returns_rows():
    """bulkCmd returning a two-entry ARP table yields two result dicts."""
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

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert len(result) == 2
    # OctetString → bytes (raw, uninterpreted)
    assert {"2.1.10.0.0.1": b"\xaa\xbb\xcc\xdd\xee\xff"} in result
    assert {"3.1.10.0.0.1": "10.0.0.1"} in result


async def test_walk_table_empty():
    """bulkCmd returning no varBinds yields an empty list."""
    mock_bulk = AsyncMock(return_value=(None, 0, 0, []))

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert result == []


async def test_walk_table_timeout():
    """bulkCmd returning requestTimedOut raises SnmpTimeoutError."""
    mock_bulk = AsyncMock(
        return_value=(errind.requestTimedOut, 0, 0, [])
    )

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
        with pytest.raises(SnmpTimeoutError):
            await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)


async def test_walk_table_auth_failure():
    """bulkCmd returning authenticationFailure raises SnmpAuthError."""
    mock_bulk = AsyncMock(
        return_value=(errind.authenticationFailure, 0, 0, [])
    )

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
        with pytest.raises(SnmpAuthError):
            await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)


async def test_walk_table_generic_error():
    """bulkCmd returning an unrecognised error indication raises SnmpError."""
    mock_bulk = AsyncMock(
        return_value=(errind.otherError, 0, 0, [])
    )

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
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

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    # Only the in-scope row should be returned
    assert len(result) == 1
    assert "2.1.10.0.0.1" in result[0]


# ---------------------------------------------------------------------------
# get_scalar tests
# ---------------------------------------------------------------------------

SCALAR_OID = "1.3.6.1.2.1.1.1.0"  # sysDescr.0


def _get_varbinds(oid_str: str, val: object) -> tuple:
    """Build the tuple-of-ObjectType that getCmd 6.x returns."""
    # getCmd returns varBinds as a tuple of (oid, value) two-tuples,
    # not wrapped in lists like bulkCmd.
    return ((_oid(oid_str), val),)


async def test_get_scalar_returns_value():
    """getCmd returning an OctetString (e.g. sysDescr) yields raw bytes.

    Callers that want text must decode explicitly:
    ``result.decode("utf-8", errors="replace")``.
    """
    description = "Linux mnm-device 5.15.0 #1 SMP x86_64"
    varbinds = _get_varbinds(
        SCALAR_OID, rfc1902.OctetString(description.encode())
    )
    mock_get = AsyncMock(return_value=(None, 0, 0, varbinds))

    with patch("app.snmp_collector.getCmd", mock_get):
        result = await get_scalar(DEVICE_IP, COMMUNITY, SCALAR_OID)

    assert result == description.encode()
    assert isinstance(result, bytes)
    # Caller is responsible for text decoding:
    assert result.decode("utf-8") == description


async def test_get_scalar_missing_oid():
    """getCmd returning NoSuchInstance yields None from get_scalar."""
    varbinds = _get_varbinds(SCALAR_OID, NoSuchInstance())
    mock_get = AsyncMock(return_value=(None, 0, 0, varbinds))

    with patch("app.snmp_collector.getCmd", mock_get):
        result = await get_scalar(DEVICE_IP, COMMUNITY, SCALAR_OID)

    assert result is None


async def test_get_scalar_timeout():
    """getCmd returning requestTimedOut raises SnmpTimeoutError."""
    mock_get = AsyncMock(return_value=(errind.requestTimedOut, 0, 0, ()))

    with patch("app.snmp_collector.getCmd", mock_get):
        with pytest.raises(SnmpTimeoutError):
            await get_scalar(DEVICE_IP, COMMUNITY, SCALAR_OID)


async def test_get_scalar_integer_value():
    """getCmd returning an Integer32 yields a Python int."""
    varbinds = _get_varbinds("1.3.6.1.2.1.1.7.0", rfc1902.Integer32(72))
    mock_get = AsyncMock(return_value=(None, 0, 0, varbinds))

    with patch("app.snmp_collector.getCmd", mock_get):
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

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
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

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
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

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
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

    with patch("app.snmp_collector.bulkCmd", mock_bulk):
        result = await walk_table(DEVICE_IP, COMMUNITY, BASE_OID)

    assert result[0]["2.542.192.0.2.121"] == mac_bytes
    assert len(result[0]["2.542.192.0.2.121"]) == 6
