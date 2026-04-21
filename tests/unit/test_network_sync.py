"""Unit tests for controller/app/onboarding/network_sync.py (Prompt 6).

Mocks at the :func:`snmp_collector.walk_table` boundary for SNMP collection
and at the :mod:`nautobot_client` primitive boundary for writes. No real
SNMP or Nautobot traffic.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload real modules so sibling-test sys.modules.setdefault stubs are no-ops.
import app.nautobot_client as _nc_preload  # noqa: E402, F401
import app.onboarding.network_sync as _ns_preload  # noqa: E402, F401
import app.snmp_collector as _snmp_preload  # noqa: E402, F401

from app.onboarding.network_sync import Phase2Result, run_phase2  # noqa: E402


# ---------------------------------------------------------------------------
# Fake SNMP walk wiring
# ---------------------------------------------------------------------------

@dataclass
class _SnmpFixture:
    """Per-OID rows returned by the mocked walk_table. Each value is a list
    of single-cell dicts, matching the real walk_table contract."""
    ifname_rows: list
    ifdescr_rows: list
    ipaddr_ifindex_rows: list
    ipaddr_prefix_rows: list
    ipadentifindex_rows: list
    ipadentnetmask_rows: list
    raise_on_ipaddr_ifindex: bool = False


def _make_walk(fixture: _SnmpFixture):
    from app.snmp_collector import OIDS, SnmpError

    async def _fake_walk(ip, community, oid_str, **kw):
        if oid_str == OIDS["IF-MIB::ifName"]:
            return fixture.ifname_rows
        if oid_str == OIDS["IF-MIB::ifDescr"]:
            return fixture.ifdescr_rows
        if oid_str == OIDS["IP-MIB::ipAddressIfIndex"]:
            if fixture.raise_on_ipaddr_ifindex:
                raise SnmpError("simulated walk error")
            return fixture.ipaddr_ifindex_rows
        if oid_str == OIDS["IP-MIB::ipAddressPrefix"]:
            return fixture.ipaddr_prefix_rows
        if oid_str == OIDS["RFC1213-MIB::ipAdEntIfIndex"]:
            return fixture.ipadentifindex_rows
        if oid_str == OIDS["RFC1213-MIB::ipAdEntNetMask"]:
            return fixture.ipadentnetmask_rows
        raise AssertionError(f"unexpected walk OID {oid_str}")

    return _fake_walk


def _veos_shape():
    """5 interfaces (Management1 + Ethernet1..4), 1 IP on Management1."""
    return _SnmpFixture(
        ifname_rows=[
            {"1": b"Management1"},
            {"2": b"Ethernet1"},
            {"3": b"Ethernet2"},
            {"4": b"Ethernet3"},
            {"5": b"Ethernet4"},
        ],
        ifdescr_rows=[
            {"1": b"Management1"},
            {"2": b"Ethernet1"},
            {"3": b"Ethernet2"},
            {"4": b"Ethernet3"},
            {"5": b"Ethernet4"},
        ],
        ipaddr_ifindex_rows=[
            {"1.4.172.21.140.16": 1},
        ],
        ipaddr_prefix_rows=[
            {"1.4.172.21.140.16": "1.3.6.1.2.1.4.32.1.5.1.1.4.172.21.140.0.24"},
        ],
        ipadentifindex_rows=[],
        ipadentnetmask_rows=[],
    )


def _ex3300_shape():
    """ipAddressTable empty; ipAdEntTable populated — triggers fallback."""
    return _SnmpFixture(
        ifname_rows=[{"1": b"me0"}, {"2": b"ge-0/0/0"}],
        ifdescr_rows=[{"1": b"me0"}, {"2": b"ge-0/0/0"}],
        ipaddr_ifindex_rows=[],  # empty — forces fallback
        ipaddr_prefix_rows=[],
        ipadentifindex_rows=[
            {"192.0.2.10": 1},
            {"192.0.2.20": 2},
        ],
        ipadentnetmask_rows=[
            {"192.0.2.10": "255.255.255.0"},
            {"192.0.2.20": "255.255.255.0"},
        ],
    )


def _default_nautobot_mocks(
    primary_ip: str = "172.21.140.16",
    existing_iface_names: list[str] | None = None,
    created_ifaces: list[dict] | None = None,
    created_ips: list[dict] | None = None,
):
    """Build an AsyncMock set covering every nautobot_client primitive
    that run_phase2 uses."""
    existing_iface_names = existing_iface_names if existing_iface_names is not None else ["Management1"]
    existing = [{"id": f"iface-{i}-uuid", "name": n}
                for i, n in enumerate(existing_iface_names)]

    mocks = {
        "get_status_by_name": AsyncMock(return_value={
            "id": "status-active", "name": "Active",
        }),
        "get_interfaces_for_device": AsyncMock(return_value=existing),
        "get_device": AsyncMock(return_value={
            "id": "dev-uuid",
            "primary_ip4": {"address": f"{primary_ip}/32"},
        }),
        "create_interfaces_bulk": AsyncMock(
            return_value=created_ifaces if created_ifaces is not None else [],
        ),
        "create_ip_addresses_bulk": AsyncMock(
            return_value=created_ips if created_ips is not None else [],
        ),
        "link_ips_to_interfaces_bulk": AsyncMock(return_value=[]),
        "delete_standalone_ip": AsyncMock(return_value=True),
        "ensure_prefix": AsyncMock(return_value={"id": "prefix-uuid"}),
    }
    return mocks


def _patch_nautobot(mocks: dict):
    from contextlib import ExitStack
    from app import nautobot_client
    stack = ExitStack()
    for name, mock in mocks.items():
        stack.enter_context(patch.object(nautobot_client, name, mock))
    return stack


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

async def test_happy_path_veos_shape():
    fixture = _veos_shape()
    created_ifaces = [
        {"id": f"new-{n}", "name": n}
        for n in ("Ethernet1", "Ethernet2", "Ethernet3", "Ethernet4")
    ]
    mocks = _default_nautobot_mocks(created_ifaces=created_ifaces)
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="dev-uuid", device_name="arista-mnm",
            device_ip="172.21.140.16", snmp_community="public",
        )
    assert isinstance(result, Phase2Result)
    assert result.success is True
    assert result.interfaces_added == 4      # Ethernet1..4 created
    assert result.interfaces_reused == 1      # Management1 already existed
    assert result.ips_added == 0              # the only IP is primary → reused
    assert result.ips_reused == 1


async def test_happy_path_junos_shape_bulk_not_per_interface():
    """80-interface Junos-style walk: single bulk POST, not 80 individual."""
    ifname_rows = [{str(i): f"ge-0/0/{i}".encode()} for i in range(1, 81)]
    ifdescr_rows = list(ifname_rows)
    fixture = _SnmpFixture(
        ifname_rows=ifname_rows, ifdescr_rows=ifdescr_rows,
        ipaddr_ifindex_rows=[], ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    created = [{"id": f"new-{i}", "name": f"ge-0/0/{i}"} for i in range(1, 81)]
    mocks = _default_nautobot_mocks(
        existing_iface_names=["me0"],
        created_ifaces=created,
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="dev", device_name="junos", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success
    assert result.interfaces_added == 80
    # KEY ASSERTION: exactly one bulk call, not per-interface.
    assert mocks["create_interfaces_bulk"].await_count == 1


async def test_fallback_ipaddrtable_used_when_ipaddresstable_empty():
    fixture = _ex3300_shape()
    created_ifaces = [{"id": "new-ge", "name": "ge-0/0/0"}]
    created_ips = [
        {"id": "ip-1", "address": "192.0.2.10/24"},
        {"id": "ip-2", "address": "192.0.2.20/24"},
    ]
    mocks = _default_nautobot_mocks(
        primary_ip="192.0.2.254",  # primary isn't in the fixture's IPs
        existing_iface_names=["me0"],
        created_ifaces=created_ifaces,
        created_ips=created_ips,
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="dev", device_name="ex3300",
            device_ip="192.0.2.7", snmp_community="public",
        )
    assert result.success is True
    assert result.used_ipaddrtable_fallback is True
    assert result.ips_added == 2


# ---------------------------------------------------------------------------
# Fallback patterns
# ---------------------------------------------------------------------------

async def test_blank_ifname_falls_back_to_ifdescr():
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b""}, {"2": b"Ethernet1"}],   # row 1 ifName blank
        ifdescr_rows=[{"1": b"mgmt"}, {"2": b"Ethernet1"}],  # row 1 ifDescr populated
        ipaddr_ifindex_rows=[], ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    created = [{"id": "new-mgmt", "name": "mgmt"}]
    mocks = _default_nautobot_mocks(
        existing_iface_names=["Ethernet1"],
        created_ifaces=created,
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success
    payload = mocks["create_interfaces_bulk"].call_args.args[0]
    assert any(p["name"] == "mgmt" for p in payload)


async def test_both_ifname_and_ifdescr_blank_skipped():
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b""}, {"2": b"Ethernet1"}],
        ifdescr_rows=[{"1": b""}, {"2": b"Ethernet1"}],
        ipaddr_ifindex_rows=[], ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(existing_iface_names=["Ethernet1"])
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    # Row 1 skipped (both names blank); row 2 reused. No POST.
    assert result.success
    assert result.interfaces_reused == 1
    assert mocks["create_interfaces_bulk"].await_count == 0


async def test_ipaddresstable_raises_falls_back_to_ipaddrtable():
    fixture = _ex3300_shape()
    fixture.raise_on_ipaddr_ifindex = True  # simulate error not empty
    created_ips = [
        {"id": "ip-1", "address": "192.0.2.10/24"},
        {"id": "ip-2", "address": "192.0.2.20/24"},
    ]
    created_ifaces = [{"id": "new-ge", "name": "ge-0/0/0"}]
    mocks = _default_nautobot_mocks(
        primary_ip="192.0.2.254",
        existing_iface_names=["me0"],
        created_ifaces=created_ifaces,
        created_ips=created_ips,
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.7",
            snmp_community="public",
        )
    assert result.success
    assert result.used_ipaddrtable_fallback is True


# ---------------------------------------------------------------------------
# Template reuse
# ---------------------------------------------------------------------------

async def test_all_interfaces_template_reused_no_post():
    names = [f"Ethernet{i}" for i in range(1, 33)]
    fixture = _SnmpFixture(
        ifname_rows=[{str(i + 1): n.encode()} for i, n in enumerate(names)],
        ifdescr_rows=[{str(i + 1): n.encode()} for i, n in enumerate(names)],
        ipaddr_ifindex_rows=[], ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(existing_iface_names=names)
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success
    assert result.interfaces_added == 0
    assert result.interfaces_reused == 32
    assert mocks["create_interfaces_bulk"].await_count == 0


async def test_mixed_template_reuse_and_new_creates():
    template_names = [f"Ethernet{i}" for i in range(1, 33)]
    new_names = [f"Vlan{i}" for i in range(1, 6)]
    all_names = template_names + new_names
    fixture = _SnmpFixture(
        ifname_rows=[{str(i + 1): n.encode()} for i, n in enumerate(all_names)],
        ifdescr_rows=[{str(i + 1): n.encode()} for i, n in enumerate(all_names)],
        ipaddr_ifindex_rows=[], ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    created = [{"id": f"new-{n}", "name": n} for n in new_names]
    mocks = _default_nautobot_mocks(
        existing_iface_names=template_names,
        created_ifaces=created,
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success
    assert result.interfaces_reused == 32
    assert result.interfaces_added == 5


# ---------------------------------------------------------------------------
# IPAM pre-clean
# ---------------------------------------------------------------------------

async def test_delete_standalone_ip_called_for_every_non_primary_ip():
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b"Management1"}, {"2": b"Ethernet1"}],
        ifdescr_rows=[{"1": b"Management1"}, {"2": b"Ethernet1"}],
        ipaddr_ifindex_rows=[
            {"1.4.10.0.0.1": 1},  # will be primary → skipped
            {"1.4.10.0.0.2": 2},
            {"1.4.10.0.0.3": 2},
        ],
        ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    created_ips = [
        {"id": "ip-2", "address": "10.0.0.2/32"},
        {"id": "ip-3", "address": "10.0.0.3/32"},
    ]
    mocks = _default_nautobot_mocks(
        primary_ip="10.0.0.1",
        existing_iface_names=["Management1", "Ethernet1"],
        created_ips=created_ips,
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="10.0.0.1",
            snmp_community="public",
        )
    assert result.success
    # 2 non-primary IPs → 2 pre-clean calls. Primary IP NOT pre-cleaned
    # (skipped before the delete_standalone_ip branch).
    assert mocks["delete_standalone_ip"].await_count == 2


async def test_delete_standalone_ip_failure_is_best_effort():
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b"Management1"}],
        ifdescr_rows=[{"1": b"Management1"}],
        ipaddr_ifindex_rows=[{"1.4.10.0.0.2": 1}],
        ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(
        primary_ip="10.0.0.1", existing_iface_names=["Management1"],
        created_ips=[{"id": "ip-2", "address": "10.0.0.2/32"}],
    )
    mocks["delete_standalone_ip"].side_effect = RuntimeError("404 cleanup")
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="10.0.0.1",
            snmp_community="public",
        )
    # Pre-clean failure does NOT abort Phase 2.
    assert result.success is True


# ---------------------------------------------------------------------------
# Primary IP skip
# ---------------------------------------------------------------------------

async def test_primary_ip_skipped_and_counted_reused():
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b"Management1"}],
        ifdescr_rows=[{"1": b"Management1"}],
        ipaddr_ifindex_rows=[{"1.4.172.21.140.16": 1}],
        ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(
        primary_ip="172.21.140.16",
        existing_iface_names=["Management1"],
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="172.21.140.16",
            snmp_community="public",
        )
    assert result.success
    assert result.ips_added == 0
    assert result.ips_reused == 1
    assert mocks["create_ip_addresses_bulk"].await_count == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

async def test_iftable_walk_fails():
    async def _bad_walk(ip, community, oid_str, **kw):
        raise RuntimeError("snmp timeout")

    mocks = _default_nautobot_mocks()
    with patch("app.onboarding.network_sync.walk_table", side_effect=_bad_walk), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success is False
    assert "phase2_collect_ifaces" in result.error


async def test_create_interfaces_bulk_fails():
    fixture = _veos_shape()
    mocks = _default_nautobot_mocks(existing_iface_names=["Management1"])
    mocks["create_interfaces_bulk"].side_effect = RuntimeError("nautobot 400")
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success is False
    assert "phase2_bulk_create_ifaces" in result.error


async def test_create_ip_addresses_bulk_fails():
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b"Management1"}],
        ifdescr_rows=[{"1": b"Management1"}],
        ipaddr_ifindex_rows=[{"1.4.10.0.0.2": 1}],
        ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(
        primary_ip="10.0.0.1", existing_iface_names=["Management1"],
    )
    mocks["create_ip_addresses_bulk"].side_effect = RuntimeError("ip 400")
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="10.0.0.1",
            snmp_community="public",
        )
    assert result.success is False
    assert "phase2_bulk_create_ips" in result.error


async def test_link_bulk_failure_no_rollback():
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b"Management1"}],
        ifdescr_rows=[{"1": b"Management1"}],
        ipaddr_ifindex_rows=[{"1.4.10.0.0.2": 1}],
        ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(
        primary_ip="10.0.0.1", existing_iface_names=["Management1"],
        created_ips=[{"id": "ip-2", "address": "10.0.0.2/32"}],
    )
    mocks["link_ips_to_interfaces_bulk"].side_effect = RuntimeError("link 400")
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="10.0.0.1",
            snmp_community="public",
        )
    assert result.success is False
    assert "phase2_bulk_link" in result.error
    # Created IPs are NOT rolled back (retry-in-place semantic).
    assert mocks["create_ip_addresses_bulk"].await_count == 1


async def test_ifindex_no_matching_interface_skipped_with_warning():
    """IP references ifIndex 99 but ifTable returned no row for 99."""
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b"Management1"}],
        ifdescr_rows=[{"1": b"Management1"}],
        ipaddr_ifindex_rows=[{"1.4.10.0.0.2": 99}],
        ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(
        primary_ip="10.0.0.1", existing_iface_names=["Management1"],
        created_ips=[{"id": "ip-2", "address": "10.0.0.2/32"}],
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="10.0.0.1",
            snmp_community="public",
        )
    # IP gets created, but link is skipped since ifindex 99 has no match.
    assert result.success
    assert mocks["link_ips_to_interfaces_bulk"].await_count == 0


async def test_empty_iftable_returns_failure():
    fixture = _SnmpFixture(
        ifname_rows=[], ifdescr_rows=[],
        ipaddr_ifindex_rows=[], ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks()
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success is False
    assert "empty" in result.error.lower() or "no rows" in result.error.lower()


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------

async def test_no_community_in_logs(caplog):
    fixture = _veos_shape()
    mocks = _default_nautobot_mocks()
    sentinel = "SECRET-COMMUNITY-do-not-leak"
    caplog.set_level(logging.DEBUG, logger="app.onboarding.network_sync")
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community=sentinel,
        )
    joined = "\n".join(r.getMessage() for r in caplog.records)
    joined += "\n" + "\n".join(
        str(getattr(r, "mnm_context", "")) for r in caplog.records
    )
    assert sentinel not in joined


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

async def test_prefix_length_none_treated_as_host_route():
    fixture = _SnmpFixture(
        ifname_rows=[{"1": b"Management1"}],
        ifdescr_rows=[{"1": b"Management1"}],
        # Fallback path — prefix length absent
        ipaddr_ifindex_rows=[],
        ipaddr_prefix_rows=[],
        ipadentifindex_rows=[{"10.0.0.2": 1}],
        ipadentnetmask_rows=[],  # no mask
    )
    mocks = _default_nautobot_mocks(
        primary_ip="10.0.0.1", existing_iface_names=["Management1"],
        created_ips=[{"id": "ip-2", "address": "10.0.0.2/32"}],
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="10.0.0.1",
            snmp_community="public",
        )
    assert result.success
    # ensure_prefix got a /32 CIDR for the host route
    ensure_prefix_calls = mocks["ensure_prefix"].await_args_list
    assert any("/32" in call.args[0] for call in ensure_prefix_calls)


async def test_ifindex_zero_skipped():
    fixture = _SnmpFixture(
        ifname_rows=[{"0": b"ignored"}, {"1": b"Management1"}],
        ifdescr_rows=[{"0": b"ignored"}, {"1": b"Management1"}],
        ipaddr_ifindex_rows=[], ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(existing_iface_names=["Management1"])
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success
    assert result.interfaces_reused == 1
    assert result.interfaces_added == 0


async def test_long_interface_names_pass_through_untruncated():
    long_name = "xe-0/0/0.16386"
    fixture = _SnmpFixture(
        ifname_rows=[{"1": long_name.encode()}],
        ifdescr_rows=[{"1": long_name.encode()}],
        ipaddr_ifindex_rows=[], ipaddr_prefix_rows=[],
        ipadentifindex_rows=[], ipadentnetmask_rows=[],
    )
    mocks = _default_nautobot_mocks(
        existing_iface_names=[],
        created_ifaces=[{"id": "new", "name": long_name}],
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="192.0.2.1",
            snmp_community="public",
        )
    assert result.success
    payload = mocks["create_interfaces_bulk"].call_args.args[0]
    assert payload[0]["name"] == long_name


async def test_reused_counts_returned_correctly():
    fixture = _veos_shape()
    mocks = _default_nautobot_mocks(
        primary_ip="172.21.140.16",
        existing_iface_names=["Management1", "Ethernet1"],
        created_ifaces=[
            {"id": "e2", "name": "Ethernet2"},
            {"id": "e3", "name": "Ethernet3"},
            {"id": "e4", "name": "Ethernet4"},
        ],
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="d", device_name="x", device_ip="172.21.140.16",
            snmp_community="public",
        )
    assert result.success
    assert result.interfaces_reused == 2
    assert result.interfaces_added == 3
    assert result.ips_reused == 1
    assert result.ips_added == 0
