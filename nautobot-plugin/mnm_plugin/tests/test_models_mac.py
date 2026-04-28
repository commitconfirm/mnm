"""Tests for the ``MacEntry`` model.

Includes ``entry_type`` round-trip (Block C P4 entry_status
remap) and the orphan-FDB collapse-to-vlan-0 case the unique
constraint must guard against.
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.db import IntegrityError
from django.test import TestCase

from mnm_plugin.models import MacEntry


def _now():
    return datetime.now(timezone.utc)


class MacEntryModelTests(TestCase):
    def _make(self, **overrides) -> MacEntry:
        defaults = dict(
            node_name="ex2300-24p",
            mac="AA:BB:CC:DD:EE:01",
            interface="ge-0/0/12",
            vlan=10,
            entry_type="dynamic",
            collected_at=_now(),
        )
        defaults.update(overrides)
        return MacEntry.objects.create(**defaults)

    def test_create_basic(self):
        row = self._make()
        self.assertEqual(row.entry_type, "dynamic")

    def test_composite_unique_constraint(self):
        self._make()
        with self.assertRaises(IntegrityError):
            self._make()

    def test_static_dynamic_round_trip(self):
        s = self._make(mac="AA:BB:CC:DD:EE:02", entry_type="static")
        d = self._make(mac="AA:BB:CC:DD:EE:03", entry_type="dynamic")
        s.refresh_from_db()
        d.refresh_from_db()
        self.assertEqual(s.entry_type, "static")
        self.assertEqual(d.entry_type, "dynamic")

    def test_same_mac_different_vlan_allowed(self):
        self._make(vlan=10)
        self._make(vlan=20)
        self.assertEqual(MacEntry.objects.count(), 2)

    def test_orphan_fdb_vlan_zero(self):
        """Block C P4 case: FDB rows that all coerce to vlan=0 — the
        unique constraint must distinguish by interface, otherwise
        all orphans collapse to one row."""
        self._make(mac="AA:BB:CC:DD:EE:04", interface="ifindex:7", vlan=0)
        self._make(mac="AA:BB:CC:DD:EE:04", interface="ifindex:8", vlan=0)
        self.assertEqual(
            MacEntry.objects.filter(mac="AA:BB:CC:DD:EE:04").count(), 2,
        )

    def test_indexes_present(self):
        index_fields = {
            tuple(idx.fields) for idx in MacEntry._meta.indexes
        }
        self.assertIn(("node_name",), index_fields)
        self.assertIn(("mac",), index_fields)
        self.assertIn(("vlan",), index_fields)
        self.assertIn(("collected_at",), index_fields)
