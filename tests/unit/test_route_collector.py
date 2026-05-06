"""Unit tests for route collection functions in controller/app/polling.py.

Mocks at the snmp_collector.walk_table boundary — no real SNMP traffic.
walk_table returns list[dict[str, Any]] (already-converted native types),
mirroring what snmp_collector.walk_table() delivers to callers.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Stub modules that require docker/sqlalchemy/etc. before importing polling.
for _mod in ("app.db", "app.nautobot_client", "app.endpoint_store"):
    sys.modules.setdefault(_mod, MagicMock())

import app.snmp_collector  # noqa: F401 — ensure real module is loaded

from app.polling import _snmp_walk_ip_cidr_route, _snmp_walk_ip_route


DEVICE_IP = "198.51.100.1"
COMMUNITY = "test-ro"
DEVICE_NAME = "test-router"


# ---------------------------------------------------------------------------
# _snmp_walk_ip_cidr_route tests
# ---------------------------------------------------------------------------

async def test_cidr_route_returns_route():
    """A single ipCidrRouteTable entry produces a correctly-shaped route dict."""
    # Index: dest(4).mask(4).tos(1).nexthop(4) = 13 octets
    # 10.0.0.0 / 255.255.255.0 / tos=0 / nexthop=198.51.100.254
    index = "10.0.0.0.255.255.255.0.0.198.51.100.254"
    walk_result = [
        {f"1.7.{index}": 3},   # proto=3 (static)
        {f"1.11.{index}": 1},  # metric1=1
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        routes = await _snmp_walk_ip_cidr_route(DEVICE_NAME, DEVICE_IP, COMMUNITY)

    assert len(routes) == 1
    r = routes[0]
    assert r["node_name"] == DEVICE_NAME
    assert r["prefix"] == "10.0.0.0/24"
    assert r["next_hop"] == "198.51.100.254"
    assert r["protocol"] == "static"
    assert r["vrf"] == "default"
    assert r["active"] is True


async def test_cidr_route_empty_walk():
    """walk_table returning [] yields an empty route list."""
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=[])):
        routes = await _snmp_walk_ip_cidr_route(DEVICE_NAME, DEVICE_IP, COMMUNITY)

    assert routes == []


async def test_cidr_route_nexthop_zero_becomes_empty():
    """A next-hop of 0.0.0.0 in the index is normalised to empty string."""
    # nexthop = 0.0.0.0 (directly-connected route)
    index = "192.168.1.0.255.255.255.0.0.0.0.0.0"
    walk_result = [
        {f"1.7.{index}": 2},   # proto=2 (local)
        {f"1.11.{index}": 0},
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        routes = await _snmp_walk_ip_cidr_route(DEVICE_NAME, DEVICE_IP, COMMUNITY)

    assert len(routes) == 1
    assert routes[0]["next_hop"] == ""
    assert routes[0]["protocol"] == "local"


async def test_cidr_route_unknown_protocol():
    """An unrecognised protocol integer maps to 'unknown'."""
    index = "10.1.0.0.255.255.0.0.0.198.51.100.1"
    walk_result = [{f"1.7.{index}": 99}]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        routes = await _snmp_walk_ip_cidr_route(DEVICE_NAME, DEVICE_IP, COMMUNITY)

    assert routes[0]["protocol"] == "unknown"


# ---------------------------------------------------------------------------
# _snmp_walk_ip_route tests
# ---------------------------------------------------------------------------

async def test_ip_route_returns_route():
    """A single ipRouteTable entry produces a correctly-shaped route dict."""
    # index_key is the destination IP
    index = "10.0.0.0"
    walk_result = [
        {f"1.1.{index}": "10.0.0.0"},       # ipRouteDest
        {f"1.7.{index}": "198.51.100.1"},    # ipRouteNextHop
        {f"1.11.{index}": "255.0.0.0"},      # ipRouteMask
        {f"1.3.{index}": 0},                 # ipRouteMetric1
        {f"1.9.{index}": 14},                # ipRouteProto = bgp
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        routes = await _snmp_walk_ip_route(DEVICE_NAME, DEVICE_IP, COMMUNITY)

    assert len(routes) == 1
    r = routes[0]
    assert r["prefix"] == "10.0.0.0/8"
    assert r["next_hop"] == "198.51.100.1"
    assert r["protocol"] == "bgp"
    assert r["node_name"] == DEVICE_NAME


async def test_ip_route_empty_walk():
    """walk_table returning [] yields an empty route list."""
    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=[])):
        routes = await _snmp_walk_ip_route(DEVICE_NAME, DEVICE_IP, COMMUNITY)

    assert routes == []


async def test_ip_route_nexthop_zero_becomes_empty():
    """Next-hop 0.0.0.0 (directly-connected) is normalised to empty string."""
    index = "192.168.0.0"
    walk_result = [
        {f"1.7.{index}": "0.0.0.0"},
        {f"1.11.{index}": "255.255.0.0"},
        {f"1.9.{index}": 2},
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        routes = await _snmp_walk_ip_route(DEVICE_NAME, DEVICE_IP, COMMUNITY)

    assert len(routes) == 1
    assert routes[0]["next_hop"] == ""


async def test_ip_route_falls_back_to_index_for_dest():
    """When column 1 (ipRouteDest) is absent, index_key is used as destination."""
    index = "10.2.0.0"
    # No column "1" — dest must come from index_key
    walk_result = [
        {f"1.7.{index}": "198.51.100.1"},
        {f"1.11.{index}": "255.255.0.0"},
        {f"1.9.{index}": 3},
    ]

    with patch("app.snmp_collector.walk_table", AsyncMock(return_value=walk_result)):
        routes = await _snmp_walk_ip_route(DEVICE_NAME, DEVICE_IP, COMMUNITY)

    assert len(routes) == 1
    assert routes[0]["prefix"].startswith("10.2.0.0/")
