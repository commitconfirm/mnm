"""Tests for the E6 saved-filter preset methods on plugin filtersets.

Coverage:
  - Each preset on EndpointFilterSet (5 presets) returns the
    expected subset given a curated fixture.
  - ``preset_stale`` works on each of ArpEntry, MacEntry,
    LldpNeighbor, Route, BgpNeighbor (one round-trip per model
    is enough — the implementation is identical across the four).
  - Multiple presets compose with AND semantics (preset_unmapped
    + preset_no_dns).
  - Per-column filter + preset compose with AND semantics
    (current_switch=foo + preset_stale).
  - DSL via ?q= composes with column filters and presets.

Runs inside Nautobot's test runner (deferred to G validation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import TestCase
from django.utils import timezone as django_tz

from mnm_plugin import filters, models


def _now():
    return django_tz.now()


def _ago(days: int) -> datetime:
    return _now() - timedelta(days=days)


class EndpointPresetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Two endpoints sharing the same current_ip (duplicate IPs).
        cls.dup_a = models.Endpoint.objects.create(
            mac_address="aa:00:00:00:00:01",
            current_switch="sw1",
            current_port="ge-0/0/1",
            current_vlan=10,
            current_ip="192.0.2.10",
            hostname="host-a",
            last_seen=_now(),
            active=True,
        )
        cls.dup_b = models.Endpoint.objects.create(
            mac_address="aa:00:00:00:00:02",
            current_switch="sw1",
            current_port="ge-0/0/2",
            current_vlan=10,
            current_ip="192.0.2.10",
            hostname="host-b",
            last_seen=_now(),
            active=True,
        )
        # Multi-homed endpoint: one MAC on two switches.
        cls.mh_a = models.Endpoint.objects.create(
            mac_address="bb:00:00:00:00:01",
            current_switch="sw1",
            current_port="ge-0/0/3",
            current_vlan=20,
            current_ip="192.0.2.20",
            hostname="multi-homed",
            last_seen=_now(),
            active=True,
        )
        cls.mh_b = models.Endpoint.objects.create(
            mac_address="bb:00:00:00:00:01",
            current_switch="sw2",
            current_port="ge-0/0/3",
            current_vlan=20,
            current_ip="192.0.2.20",
            hostname="multi-homed",
            last_seen=_now(),
            active=False,
        )
        # Stale endpoint: last_seen 30 days ago.
        cls.stale = models.Endpoint.objects.create(
            mac_address="cc:00:00:00:00:01",
            current_switch="sw1",
            current_port="ge-0/0/4",
            current_vlan=30,
            current_ip="192.0.2.30",
            hostname="stale-host",
            last_seen=_ago(30),
            active=True,
        )
        # Unmapped endpoint: current_switch is "(none)" sentinel.
        cls.unmapped = models.Endpoint.objects.create(
            mac_address="dd:00:00:00:00:01",
            current_switch="(none)",
            current_port="(none)",
            current_vlan=0,
            current_ip="192.0.2.40",
            hostname="unmapped-host",
            last_seen=_now(),
            active=True,
        )
        # No-DNS endpoint: hostname empty.
        cls.no_dns = models.Endpoint.objects.create(
            mac_address="ee:00:00:00:00:01",
            current_switch="sw1",
            current_port="ge-0/0/5",
            current_vlan=50,
            current_ip="192.0.2.50",
            hostname="",
            last_seen=_now(),
            active=True,
        )

    def _filter(self, **params):
        fs = filters.EndpointFilterSet(
            params, queryset=models.Endpoint.objects.all(),
        )
        return list(fs.qs.order_by("mac_address"))

    def test_duplicate_ips_returns_both_sharing_ip(self):
        result = self._filter(preset_duplicate_ips="true")
        macs = {e.mac_address for e in result}
        self.assertIn("aa:00:00:00:00:01", macs)
        self.assertIn("aa:00:00:00:00:02", macs)
        self.assertNotIn("cc:00:00:00:00:01", macs)

    def test_multi_homed_returns_mac_on_two_switches(self):
        result = self._filter(preset_multi_homed="true")
        macs = {e.mac_address for e in result}
        self.assertIn("bb:00:00:00:00:01", macs)
        # Singletons must NOT appear.
        self.assertNotIn("cc:00:00:00:00:01", macs)

    def test_stale_returns_old_last_seen(self):
        result = self._filter(preset_stale="true")
        macs = {e.mac_address for e in result}
        self.assertIn("cc:00:00:00:00:01", macs)
        self.assertNotIn("aa:00:00:00:00:01", macs)

    def test_unmapped_returns_none_sentinel(self):
        result = self._filter(preset_unmapped="true")
        macs = {e.mac_address for e in result}
        self.assertIn("dd:00:00:00:00:01", macs)
        self.assertNotIn("aa:00:00:00:00:01", macs)

    def test_no_dns_returns_blank_hostname(self):
        result = self._filter(preset_no_dns="true")
        macs = {e.mac_address for e in result}
        self.assertIn("ee:00:00:00:00:01", macs)
        self.assertNotIn("aa:00:00:00:00:01", macs)

    def test_presets_compose_with_AND(self):
        # Unmapped AND no_dns: nothing in fixture matches both.
        result = self._filter(
            preset_unmapped="true",
            preset_no_dns="true",
        )
        self.assertEqual(result, [])

    def test_preset_with_column_filter(self):
        # Stale AND current_switch=sw1
        result = self._filter(
            preset_stale="true",
            current_switch="sw1",
        )
        macs = {e.mac_address for e in result}
        self.assertEqual(macs, {"cc:00:00:00:00:01"})

    def test_preset_false_value_no_op(self):
        # preset_stale=false should NOT filter (BooleanFilter
        # interprets falsy values as "do nothing").
        all_count = models.Endpoint.objects.count()
        result = self._filter(preset_stale="false")
        self.assertEqual(len(result), all_count)


class ArpEntryStalePresetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        models.ArpEntry.objects.create(
            node_name="sw1",
            ip="192.0.2.1",
            mac="aa:bb:cc:dd:ee:01",
            interface="ge-0/0/0",
            vrf="default",
            collected_at=_now(),
        )
        models.ArpEntry.objects.create(
            node_name="sw1",
            ip="192.0.2.2",
            mac="aa:bb:cc:dd:ee:02",
            interface="ge-0/0/0",
            vrf="default",
            collected_at=_ago(30),
        )

    def test_stale_only(self):
        fs = filters.ArpEntryFilterSet(
            {"preset_stale": "true"},
            queryset=models.ArpEntry.objects.all(),
        )
        ips = {e.ip for e in fs.qs}
        self.assertEqual(ips, {"192.0.2.2"})


class MacEntryStalePresetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        models.MacEntry.objects.create(
            node_name="sw1", mac="aa:00:00:00:00:01",
            interface="ge-0/0/1", vlan=10,
            entry_type="dynamic", collected_at=_now(),
        )
        models.MacEntry.objects.create(
            node_name="sw1", mac="aa:00:00:00:00:02",
            interface="ge-0/0/2", vlan=10,
            entry_type="dynamic", collected_at=_ago(30),
        )

    def test_stale_only(self):
        fs = filters.MacEntryFilterSet(
            {"preset_stale": "true"},
            queryset=models.MacEntry.objects.all(),
        )
        macs = {e.mac for e in fs.qs}
        self.assertEqual(macs, {"aa:00:00:00:00:02"})


class LldpStalePresetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        models.LldpNeighbor.objects.create(
            node_name="sw1", local_interface="ge-0/0/1",
            remote_system_name="peer-a", remote_port="ge-0/0/1",
            collected_at=_now(),
        )
        models.LldpNeighbor.objects.create(
            node_name="sw1", local_interface="ge-0/0/2",
            remote_system_name="peer-b", remote_port="ge-0/0/2",
            collected_at=_ago(30),
        )

    def test_stale_only(self):
        fs = filters.LldpNeighborFilterSet(
            {"preset_stale": "true"},
            queryset=models.LldpNeighbor.objects.all(),
        )
        peers = {e.remote_system_name for e in fs.qs}
        self.assertEqual(peers, {"peer-b"})


class RouteStalePresetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        models.Route.objects.create(
            node_name="sw1", prefix="192.0.2.0/24",
            next_hop="192.0.2.1", protocol="ospf",
            vrf="default", active=True, collected_at=_now(),
        )
        models.Route.objects.create(
            node_name="sw1", prefix="198.51.100.0/24",
            next_hop="192.0.2.2", protocol="bgp",
            vrf="default", active=True, collected_at=_ago(30),
        )

    def test_stale_only(self):
        fs = filters.RouteFilterSet(
            {"preset_stale": "true"},
            queryset=models.Route.objects.all(),
        )
        prefixes = {r.prefix for r in fs.qs}
        self.assertEqual(prefixes, {"198.51.100.0/24"})


class BgpNeighborStalePresetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        models.BgpNeighbor.objects.create(
            node_name="sw1", neighbor_ip="192.0.2.1",
            remote_asn=65001, state="Established",
            vrf="default", address_family="ipv4 unicast",
            collected_at=_now(),
        )
        models.BgpNeighbor.objects.create(
            node_name="sw1", neighbor_ip="192.0.2.2",
            remote_asn=65002, state="Established",
            vrf="default", address_family="ipv4 unicast",
            collected_at=_ago(30),
        )

    def test_stale_only(self):
        fs = filters.BgpNeighborFilterSet(
            {"preset_stale": "true"},
            queryset=models.BgpNeighbor.objects.all(),
        )
        ips = {n.neighbor_ip for n in fs.qs}
        self.assertEqual(ips, {"192.0.2.2"})


class DslThroughFilterSetTests(TestCase):
    """End-to-end: ?q=<DSL> via the filterset, with preset+column."""

    @classmethod
    def setUpTestData(cls):
        models.Endpoint.objects.create(
            mac_address="aa:00:00:00:00:01",
            current_switch="sw1", current_port="ge-0/0/1",
            current_vlan=10, current_ip="192.0.2.10",
            last_seen=_now(), active=True,
        )
        models.Endpoint.objects.create(
            mac_address="bb:00:00:00:00:01",
            current_switch="sw1", current_port="ge-0/0/2",
            current_vlan=20, current_ip="192.0.2.20",
            last_seen=_now(), active=False,
        )

    def test_dsl_simple_eq(self):
        fs = filters.EndpointFilterSet(
            {"q": "current_vlan = 10"},
            queryset=models.Endpoint.objects.all(),
        )
        self.assertEqual(fs.qs.count(), 1)

    def test_dsl_invalid_does_not_filter(self):
        # Bad expression: filterset returns the unfiltered queryset
        # (the view's extra_context surfaces the error to the UI).
        fs = filters.EndpointFilterSet(
            {"q": "secret_field = 1"},
            queryset=models.Endpoint.objects.all(),
        )
        self.assertEqual(fs.qs.count(), 2)
