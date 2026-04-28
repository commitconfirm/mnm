"""Tests for the ``ArpEntry`` model.

Schema constraints + index assertions per E0 §2c. Runs inside
Nautobot's test runner (uses ``django.test.TestCase``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.db import IntegrityError
from django.test import TestCase

from mnm_plugin.models import ArpEntry


def _now():
    return datetime.now(timezone.utc)


class ArpEntryModelTests(TestCase):
    def _make(self, **overrides) -> ArpEntry:
        defaults = dict(
            node_name="ex2300-24p",
            ip="192.0.2.10",
            mac="AA:BB:CC:DD:EE:01",
            interface="ge-0/0/12",
            vrf="default",
            collected_at=_now(),
        )
        defaults.update(overrides)
        return ArpEntry.objects.create(**defaults)

    def test_create_basic(self):
        row = self._make()
        self.assertEqual(row.node_name, "ex2300-24p")
        self.assertEqual(row.ip, "192.0.2.10")
        self.assertEqual(row.vrf, "default")

    def test_composite_unique_constraint(self):
        self._make()
        with self.assertRaises(IntegrityError):
            self._make()

    def test_same_ip_different_mac_allowed(self):
        """Operators expect to see two ARP entries when MAC moves —
        controller upserts replace, but we test the schema permits."""
        self._make(mac="AA:BB:CC:DD:EE:01")
        self._make(mac="AA:BB:CC:DD:EE:02")
        self.assertEqual(ArpEntry.objects.count(), 2)

    def test_same_mac_different_vrf_allowed(self):
        """VRF is part of the unique key — same MAC may appear in
        multiple VRFs."""
        self._make(vrf="default")
        self._make(vrf="management")
        self.assertEqual(ArpEntry.objects.count(), 2)

    def test_sentinel_interface_allowed(self):
        """ifindex:N sentinels per Block C P3 are valid stored
        values."""
        row = self._make(interface="ifindex:7")
        self.assertEqual(row.interface, "ifindex:7")

    def test_indexes_present(self):
        index_fields = {
            tuple(idx.fields) for idx in ArpEntry._meta.indexes
        }
        self.assertIn(("node_name",), index_fields)
        self.assertIn(("ip",), index_fields)
        self.assertIn(("mac",), index_fields)
        self.assertIn(("collected_at",), index_fields)

    def test_str_representation(self):
        row = self._make()
        self.assertIn("192.0.2.10", str(row))
        self.assertIn("AA:BB:CC:DD:EE:01", str(row))

    def test_text_fields_have_no_max_length(self):
        from django.db.models import TextField

        for fname in ("node_name", "ip", "mac", "interface", "vrf"):
            field = ArpEntry._meta.get_field(fname)
            self.assertIsInstance(field, TextField)
