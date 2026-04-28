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
        plugin_writer, "_ensure_endpoint_table",
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
        plugin_writer, "_ensure_endpoint_table",
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
