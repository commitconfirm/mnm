"""Tests for the ``Route`` model.

Schema constraints + index assertions per E0 §2c. Runs inside
Nautobot's test runner.
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.db import IntegrityError
from django.test import TestCase

from mnm_plugin.models import Route


def _now():
    return datetime.now(timezone.utc)


class RouteModelTests(TestCase):
    def _make(self, **overrides) -> Route:
        defaults = dict(
            node_name="ex2300-24p",
            prefix="192.0.2.0/24",
            next_hop="192.0.2.1",
            protocol="bgp",
            vrf="default",
            collected_at=_now(),
        )
        defaults.update(overrides)
        return Route.objects.create(**defaults)

    def test_create_basic(self):
        row = self._make()
        self.assertEqual(row.protocol, "bgp")
        self.assertTrue(row.active)

    def test_composite_unique_constraint(self):
        self._make()
        with self.assertRaises(IntegrityError):
            self._make()

    def test_same_prefix_different_vrf_allowed(self):
        """VRF is part of the unique key — same prefix in
        multiple VRFs is normal in MPLS L3VPN."""
        self._make(vrf="default")
        self._make(vrf="customer-a")
        self.assertEqual(Route.objects.count(), 2)

    def test_same_prefix_different_next_hop_allowed(self):
        """ECMP — same prefix with different next-hops is
        normal."""
        self._make(next_hop="192.0.2.1")
        self._make(next_hop="192.0.2.2")
        self.assertEqual(Route.objects.count(), 2)

    def test_metric_and_preference_nullable(self):
        row = self._make(metric=None, preference=None)
        self.assertIsNone(row.metric)
        self.assertIsNone(row.preference)

    def test_outgoing_interface_preserved_vendor_native(self):
        """The cross-vendor naming helper transforms at render
        time, not write time. Junos ``ge-0/0/0.0`` lands as
        ``ge-0/0/0.0`` on disk."""
        row = self._make(outgoing_interface="ge-0/0/0.0")
        row.refresh_from_db()
        self.assertEqual(row.outgoing_interface, "ge-0/0/0.0")

    def test_indexes_present(self):
        index_fields = {
            tuple(idx.fields) for idx in Route._meta.indexes
        }
        self.assertIn(("node_name",), index_fields)
        self.assertIn(("prefix",), index_fields)
        self.assertIn(("protocol",), index_fields)
        self.assertIn(("collected_at",), index_fields)
