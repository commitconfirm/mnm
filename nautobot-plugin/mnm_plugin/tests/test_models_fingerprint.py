"""Tests for the ``Fingerprint`` model.

Schema constraints + index assertions per E0 §2c. v1.0 ships
schema-only; the v1.1 fingerprinting workstream wires upstream
collectors.
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.db import IntegrityError
from django.test import TestCase

from mnm_plugin.models import Fingerprint


def _now():
    return datetime.now(timezone.utc)


class FingerprintModelTests(TestCase):
    def _make(self, **overrides) -> Fingerprint:
        defaults = dict(
            target_mac="AA:BB:CC:DD:EE:01",
            signal_type="mdns",
            signal_value="_workstation._tcp.local.",
        )
        defaults.update(overrides)
        return Fingerprint.objects.create(**defaults)

    def test_create_basic(self):
        row = self._make()
        self.assertEqual(row.signal_type, "mdns")
        self.assertEqual(row.seen_count, 1)

    def test_composite_unique_constraint(self):
        self._make()
        with self.assertRaises(IntegrityError):
            self._make()

    def test_same_mac_different_signal_type_allowed(self):
        """A single MAC may have multiple signals — that's the
        whole point of cross-signal correlation."""
        self._make(signal_type="mdns")
        self._make(signal_type="ssh_hostkey", signal_value="abc")
        self.assertEqual(Fingerprint.objects.count(), 2)

    def test_same_signal_different_macs_allowed(self):
        """Same signal value across MACs is the v1.1
        'same device, moved' detection signal."""
        self._make(target_mac="AA:BB:CC:DD:EE:01")
        self._make(target_mac="AA:BB:CC:DD:EE:02")
        self.assertEqual(Fingerprint.objects.count(), 2)

    def test_signal_metadata_default_dict(self):
        row = self._make()
        self.assertEqual(row.signal_metadata, {})

    def test_signal_metadata_jsonfield_round_trip(self):
        row = self._make(signal_metadata={"keytype": "rsa", "bits": 2048})
        row.refresh_from_db()
        self.assertEqual(
            row.signal_metadata, {"keytype": "rsa", "bits": 2048},
        )

    def test_indexes_present(self):
        index_fields = {
            tuple(idx.fields) for idx in Fingerprint._meta.indexes
        }
        self.assertIn(("target_mac",), index_fields)
        self.assertIn(("signal_type",), index_fields)
        self.assertIn(("signal_value",), index_fields)
        self.assertIn(("last_seen",), index_fields)
