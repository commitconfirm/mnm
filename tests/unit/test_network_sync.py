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
        # phase2-bulk-ip-400-race fix: run_phase2 resolves the Global
        # namespace UUID once per cycle and POSTs IPs with namespace=
        # (not parent=) so Nautobot auto-resolves the parent prefix.
        "get_namespaces": AsyncMock(return_value=[
            {"id": "global-namespace-uuid", "name": "Global"},
        ]),
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


# ---------------------------------------------------------------------------
# phase2-bulk-ip-400-race fix — IPAM-noise filtering + namespace-scoped POST
# ---------------------------------------------------------------------------
# Regression tests for the v1.0.0-blocker race surfaced during G2:
# Phase 2 bulk_create_ips returned 400 from Nautobot when (a) two Junos
# devices hit the same lo0 internal-management addresses (128.0.0.X/2
# duplicates) or (b) a vendor returned prefix_length=0 (PAN-OS) which
# made the orchestrator pass parent=<same-length-prefix-id>, which
# Nautobot rejects.

def test_is_ipam_noise_junos_internal_range():
    """Junos lo0.X internal addresses (128.0.0.0/8 per Juniper docs)
    must be filtered at collection — they collide across every Junos
    chassis and have no operational value."""
    from app.onboarding.network_sync import _is_ipam_noise

    # Inside the noise range — must be filtered.
    assert _is_ipam_noise("128.0.0.1") is True       # observed on lab EX2300
    assert _is_ipam_noise("128.0.0.4") is True
    assert _is_ipam_noise("128.0.0.16") is True
    assert _is_ipam_noise("128.0.0.127") is True
    assert _is_ipam_noise("128.255.255.255") is True  # /8 boundary

    # Outside the noise range — must NOT be filtered.
    assert _is_ipam_noise("127.255.255.255") is False
    assert _is_ipam_noise("129.0.0.1") is False
    assert _is_ipam_noise("172.21.140.6") is False
    assert _is_ipam_noise("10.0.0.1") is False

    # Non-IPv4 input must not raise.
    assert _is_ipam_noise("not-an-ip") is False
    assert _is_ipam_noise("") is False


async def test_collect_ips_filters_junos_lo0_internal_range():
    """End-to-end: SNMP returns Junos lo0 noise IPs alongside real IPs;
    _collect_ips emits only the real ones."""
    from app.onboarding.network_sync import _collect_ips

    # Junos-shape: 5 lo0 internal /2 addresses + 1 real /24 mgmt.
    fixture = _SnmpFixture(
        ifname_rows=[],
        ifdescr_rows=[],
        ipaddr_ifindex_rows=[
            {"1.4.128.0.0.1": 220},
            {"1.4.128.0.0.4": 220},
            {"1.4.128.0.0.16": 220},
            {"1.4.128.0.0.127": 507},
            {"1.4.172.21.140.6": 562},
        ],
        ipaddr_prefix_rows=[
            {"1.4.128.0.0.1": "1.3.6.1.2.1.4.32.1.5.X.X.X.X.X.X.2"},
            {"1.4.128.0.0.4": "1.3.6.1.2.1.4.32.1.5.X.X.X.X.X.X.2"},
            {"1.4.128.0.0.16": "1.3.6.1.2.1.4.32.1.5.X.X.X.X.X.X.2"},
            {"1.4.128.0.0.127": "1.3.6.1.2.1.4.32.1.5.X.X.X.X.X.X.2"},
            {"1.4.172.21.140.6": "1.3.6.1.2.1.4.32.1.5.X.X.X.X.X.X.24"},
        ],
        ipadentifindex_rows=[],
        ipadentnetmask_rows=[],
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)):
        rows, used_fb = await _collect_ips("172.21.140.6", "public")

    # 5 noise rows filtered; only the real /24 mgmt IP survives.
    assert used_fb is False
    assert len(rows) == 1
    assert rows[0][0] == "172.21.140.6"


async def test_phase2_bulk_create_uses_namespace_not_parent():
    """Regression for the same-length-prefix-rejection class: bulk-create
    payloads must include namespace=<uuid>, not parent=<prefix-id>.

    Pre-fix: parent=<prefix-id> with same-length prefix → Nautobot 400
    "X/N cannot be assigned as the parent of Y/N".
    Post-fix: namespace=<uuid> → Nautobot auto-resolves the most-specific
    containing prefix; same-length /2 (Junos) and /0 (PAN-OS SNMP-
    degenerate) cases pass.
    """
    fixture = _veos_shape()
    created_ifaces = [
        {"id": f"new-{n}", "name": n}
        for n in ("Ethernet1", "Ethernet2", "Ethernet3", "Ethernet4")
    ]
    # Force a non-primary IP so the bulk_create_ips path actually fires.
    fixture.ipaddr_ifindex_rows = [
        {"1.4.172.21.140.16": 1},
        {"1.4.10.255.0.5": 2},
    ]
    fixture.ipaddr_prefix_rows = [
        {"1.4.172.21.140.16": "1.3.6.1.2.1.4.32.1.5.1.1.4.172.21.140.0.24"},
        {"1.4.10.255.0.5": "1.3.6.1.2.1.4.32.1.5.1.1.4.10.255.0.0.24"},
    ]
    mocks = _default_nautobot_mocks(
        primary_ip="172.21.140.16",
        created_ifaces=created_ifaces,
        created_ips=[{"id": "ip-1", "address": "10.255.0.5/24"}],
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="dev", device_name="arista-mnm",
            device_ip="172.21.140.16", snmp_community="public",
        )
    assert result.success
    assert mocks["create_ip_addresses_bulk"].await_count == 1
    # The single bulk call's payload must use namespace=, not parent=.
    call_args = mocks["create_ip_addresses_bulk"].await_args
    assert call_args is not None
    posted_payloads = call_args.args[0] if call_args.args else call_args.kwargs.get("ip_addresses")
    assert isinstance(posted_payloads, list)
    assert len(posted_payloads) == 1
    payload = posted_payloads[0]
    assert "namespace" in payload, f"expected namespace= in payload, got {payload}"
    assert payload["namespace"] == "global-namespace-uuid"
    assert "parent" not in payload, (
        f"parent= must NOT be in payload (pre-fix shape); got {payload}"
    )


async def test_phase2_resolves_global_namespace_once_per_cycle():
    """run_phase2 calls get_namespaces() exactly once per cycle to
    resolve the Global namespace UUID. Cheap, but a regression guard
    against per-IP get_namespaces() calls (which would scale poorly on
    100-interface devices)."""
    fixture = _veos_shape()
    fixture.ipaddr_ifindex_rows = [
        {"1.4.172.21.140.16": 1},
        {"1.4.10.255.0.5": 2},
        {"1.4.10.255.0.6": 3},
    ]
    fixture.ipaddr_prefix_rows = [
        {"1.4.172.21.140.16": "1.3.6.1.2.1.4.32.1.5.1.1.4.172.21.140.0.24"},
        {"1.4.10.255.0.5": "1.3.6.1.2.1.4.32.1.5.1.1.4.10.255.0.0.24"},
        {"1.4.10.255.0.6": "1.3.6.1.2.1.4.32.1.5.1.1.4.10.255.0.0.24"},
    ]
    created_ifaces = [
        {"id": f"new-{n}", "name": n}
        for n in ("Ethernet1", "Ethernet2", "Ethernet3", "Ethernet4")
    ]
    mocks = _default_nautobot_mocks(
        primary_ip="172.21.140.16",
        created_ifaces=created_ifaces,
        created_ips=[
            {"id": "ip-1", "address": "10.255.0.5/24"},
            {"id": "ip-2", "address": "10.255.0.6/24"},
        ],
    )
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="dev", device_name="x",
            device_ip="172.21.140.16", snmp_community="public",
        )
    assert result.success
    assert mocks["get_namespaces"].await_count == 1


async def test_phase2_fails_cleanly_when_global_namespace_missing():
    """If Nautobot's namespaces list doesn't contain Global (operator
    misconfiguration or a different namespace name), Phase 2 fails
    with an operator-actionable error rather than ploughing on with
    namespace=None."""
    fixture = _veos_shape()
    fixture.ipaddr_ifindex_rows = [
        {"1.4.172.21.140.16": 1},
        {"1.4.10.255.0.5": 2},
    ]
    fixture.ipaddr_prefix_rows = [
        {"1.4.172.21.140.16": "1.3.6.1.2.1.4.32.1.5.1.1.4.172.21.140.0.24"},
        {"1.4.10.255.0.5": "1.3.6.1.2.1.4.32.1.5.1.1.4.10.255.0.0.24"},
    ]
    mocks = _default_nautobot_mocks(
        primary_ip="172.21.140.16",
        created_ifaces=[
            {"id": f"new-{n}", "name": n}
            for n in ("Ethernet1", "Ethernet2", "Ethernet3", "Ethernet4")
        ],
    )
    # Override get_namespaces to omit Global.
    mocks["get_namespaces"] = AsyncMock(return_value=[
        {"id": "other-uuid", "name": "Tenant-A"},
    ])
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await run_phase2(
            device_id="dev", device_name="x",
            device_ip="172.21.140.16", snmp_community="public",
        )
    assert result.success is False
    assert "Global namespace not found" in (result.error or "")
    # ensure_prefix should NOT have been called — we bail before that.
    assert mocks["ensure_prefix"].await_count == 0
    assert mocks["create_ip_addresses_bulk"].await_count == 0


async def test_phase2_per_ip_fallback_on_bulk_400_with_duplicate():
    """When bulk_create_ips returns a 400 whose body indicates 'already
    exists' on at least one record, the orchestrator falls back to per-IP
    create with reuse-on-duplicate. The per-IP path looks up the existing
    IPAddress by address+namespace and reuses its UUID.

    Cross-device IP collision (e.g., 10.0.0.1 on both cisco-mnm Loopback
    and pa-440 mgmt) is the canonical case.
    """
    from app.onboarding import network_sync
    # Simulate the bulk create raising a duplicate-error 400.
    class _DupError(Exception):
        def __init__(self, body):
            super().__init__("rejected by Nautobot (400)")
            self.response_body = body

    fixture = _veos_shape()
    fixture.ipaddr_ifindex_rows = [
        {"1.4.172.21.140.16": 1},
        {"1.4.10.0.0.1": 2},  # this one will be flagged as duplicate
    ]
    fixture.ipaddr_prefix_rows = [
        {"1.4.172.21.140.16": "1.3.6.1.2.1.4.32.1.5.1.1.4.172.21.140.0.24"},
        {"1.4.10.0.0.1": "1.3.6.1.2.1.4.32.1.5.1.1.4.10.0.0.0.32"},
    ]

    mocks = _default_nautobot_mocks(
        primary_ip="172.21.140.16",
        created_ifaces=[
            {"id": f"new-{n}", "name": n}
            for n in ("Ethernet1", "Ethernet2", "Ethernet3", "Ethernet4")
        ],
    )
    # Bulk raises duplicate; per-IP fallback then creates one + reuses one.
    duplicate_body = [
        {"__all__": ["IP address with this Parent and Host already exists."]},
    ]
    mocks["create_ip_addresses_bulk"] = AsyncMock(side_effect=[
        _DupError(duplicate_body),  # bulk call (1 IP, the duplicate)
        # Per-IP retry of the only payload — also raises (Nautobot still
        # sees it as duplicate) → triggers the lookup path.
        _DupError(duplicate_body),
    ])
    # Patch _lookup_ip_address to return the "existing" record.
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         patch("app.onboarding.network_sync._lookup_ip_address",
               AsyncMock(return_value={"id": "existing-ip-uuid",
                                       "address": "10.0.0.1/32"})), \
         _patch_nautobot(mocks):
        result = await network_sync.run_phase2(
            device_id="dev", device_name="x",
            device_ip="172.21.140.16", snmp_community="public",
        )
    # Phase 2 succeeds with the IP marked as reused (cross-device shared).
    assert result.success is True
    assert result.ips_added == 0
    assert result.ips_reused == 2  # 1 for primary, 1 for duplicate-reused


async def test_phase2_per_link_fallback_on_bulk_link_400_with_duplicate():
    """When link_ips_to_interfaces_bulk fails with 'must make a unique
    set' (idempotent re-run case), per-link fallback skips already-linked
    records silently. Phase 2 still succeeds."""
    from app.onboarding import network_sync

    class _LinkDupError(Exception):
        def __init__(self, body):
            super().__init__("rejected by Nautobot (400)")
            self.response_body = body

    fixture = _veos_shape()
    fixture.ipaddr_ifindex_rows = [
        {"1.4.172.21.140.16": 1},
        {"1.4.10.255.0.5": 2},
    ]
    fixture.ipaddr_prefix_rows = [
        {"1.4.172.21.140.16": "1.3.6.1.2.1.4.32.1.5.1.1.4.172.21.140.0.24"},
        {"1.4.10.255.0.5": "1.3.6.1.2.1.4.32.1.5.1.1.4.10.255.0.0.24"},
    ]
    created_ifaces = [
        {"id": f"new-{n}", "name": n}
        for n in ("Ethernet1", "Ethernet2", "Ethernet3", "Ethernet4")
    ]
    mocks = _default_nautobot_mocks(
        primary_ip="172.21.140.16",
        created_ifaces=created_ifaces,
        created_ips=[{"id": "ip-1", "address": "10.255.0.5/24"}],
    )
    # Bulk link raises with unique-set duplicate. Per-link path retries
    # individually and accepts the duplicate as already-linked.
    duplicate_link_body = [
        {"non_field_errors":
            ["The fields interface, ip_address must make a unique set."]},
    ]
    mocks["link_ips_to_interfaces_bulk"] = AsyncMock(side_effect=[
        _LinkDupError(duplicate_link_body),  # bulk fails
        _LinkDupError(duplicate_link_body),  # per-link retry also "fails" duplicate-style
    ])
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await network_sync.run_phase2(
            device_id="dev", device_name="x",
            device_ip="172.21.140.16", snmp_community="public",
        )
    # Per-link fallback treats already-linked as success → Phase 2 success.
    assert result.success is True
    assert result.error is None


async def test_phase2_per_ip_fallback_returns_none_on_non_duplicate_error():
    """Per-IP fallback only swallows duplicate-class errors; any other
    400 (e.g., validation rejection on address format) is hard failure."""
    from app.onboarding import network_sync

    class _OtherError(Exception):
        def __init__(self, body):
            super().__init__("rejected by Nautobot (400)")
            self.response_body = body

    fixture = _veos_shape()
    fixture.ipaddr_ifindex_rows = [
        {"1.4.172.21.140.16": 1},
        {"1.4.10.0.0.1": 2},
    ]
    fixture.ipaddr_prefix_rows = [
        {"1.4.172.21.140.16": "1.3.6.1.2.1.4.32.1.5.1.1.4.172.21.140.0.24"},
        {"1.4.10.0.0.1": "1.3.6.1.2.1.4.32.1.5.1.1.4.10.0.0.0.32"},
    ]
    mocks = _default_nautobot_mocks(
        primary_ip="172.21.140.16",
        created_ifaces=[{"id": "i", "name": "Ethernet1"}],
    )
    other_body = [{"address": ["Invalid format"]}]
    mocks["create_ip_addresses_bulk"] = AsyncMock(side_effect=[
        _OtherError(other_body),  # bulk
        _OtherError(other_body),  # per-IP retry also non-duplicate
    ])
    with patch("app.onboarding.network_sync.walk_table",
               side_effect=_make_walk(fixture)), \
         _patch_nautobot(mocks):
        result = await network_sync.run_phase2(
            device_id="dev", device_name="x",
            device_ip="172.21.140.16", snmp_community="public",
        )
    assert result.success is False
    assert "phase2_bulk_create_ips failed" in (result.error or "")
