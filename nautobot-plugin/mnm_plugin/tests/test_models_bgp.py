"""Tests for the ``BgpNeighbor`` model.

Schema constraints + index assertions per E0 §2c.
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.db import IntegrityError
from django.test import TestCase

from mnm_plugin.models import BgpNeighbor


def _now():
    return datetime.now(timezone.utc)


class BgpNeighborModelTests(TestCase):
    def _make(self, **overrides) -> BgpNeighbor:
        defaults = dict(
            node_name="ex2300-24p",
            neighbor_ip="192.0.2.1",
            remote_asn=65001,
            local_asn=65000,
            state="Established",
            vrf="default",
            address_family="ipv4 unicast",
            collected_at=_now(),
        )
        defaults.update(overrides)
        return BgpNeighbor.objects.create(**defaults)

    def test_create_basic(self):
        row = self._make()
        self.assertEqual(row.state, "Established")
        self.assertEqual(row.remote_asn, 65001)

    def test_composite_unique_constraint(self):
        self._make()
        with self.assertRaises(IntegrityError):
            self._make()

    def test_same_neighbor_different_address_family_allowed(self):
        """ipv4 unicast and ipv6 unicast in the same VRF for the
        same neighbor is normal multi-AFI BGP."""
        self._make(address_family="ipv4 unicast")
        self._make(address_family="ipv6 unicast")
        self.assertEqual(BgpNeighbor.objects.count(), 2)

    def test_same_neighbor_different_vrf_allowed(self):
        """Same neighbor IP in multiple VRFs is normal in
        multi-tenant networks."""
        self._make(vrf="default")
        self._make(vrf="customer-a")
        self.assertEqual(BgpNeighbor.objects.count(), 2)

    def test_state_default_unknown(self):
        """Missing state defaults to 'Unknown' per E0 §2c."""
        defaults = dict(
            node_name="x", neighbor_ip="192.0.2.99", remote_asn=65000,
            collected_at=_now(),
        )
        row = BgpNeighbor.objects.create(**defaults)
        self.assertEqual(row.state, "Unknown")

    def test_indexes_present(self):
        index_fields = {
            tuple(idx.fields) for idx in BgpNeighbor._meta.indexes
        }
        self.assertIn(("node_name",), index_fields)
        self.assertIn(("neighbor_ip",), index_fields)
        self.assertIn(("state",), index_fields)
        self.assertIn(("collected_at",), index_fields)
