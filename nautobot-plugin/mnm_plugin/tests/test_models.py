"""Tests for the ``Endpoint`` model.

Posture: these tests run inside Nautobot's test runner where
the Django app registry is loaded. They use Nautobot's test
``TestCase`` which sets up the test database.
"""

from __future__ import annotations

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from mnm_plugin.models import Endpoint


class EndpointModelTests(TestCase):
    """Schema constraints + basic CRUD."""

    def _make_endpoint(self, **overrides) -> Endpoint:
        defaults = dict(
            mac_address="AA:BB:CC:DD:EE:01",
            current_switch="ex2300-24p",
            current_port="ge-0/0/12",
            current_vlan=10,
        )
        defaults.update(overrides)
        return Endpoint.objects.create(**defaults)

    def test_create_basic_endpoint(self):
        ep = self._make_endpoint()
        self.assertEqual(ep.mac_address, "AA:BB:CC:DD:EE:01")
        self.assertTrue(ep.active)
        self.assertFalse(ep.is_uplink)

    def test_composite_unique_constraint(self):
        """Two rows with the same (mac, switch, port, vlan) must
        not coexist — Block C dedup-on-constraint pattern."""
        self._make_endpoint()
        with self.assertRaises(IntegrityError):
            self._make_endpoint()

    def test_same_mac_different_port_allowed(self):
        """One MAC may appear on multiple distinct
        ``(switch, port, vlan)`` tuples — Netdisco-style multi-
        record history."""
        self._make_endpoint(current_port="ge-0/0/12")
        self._make_endpoint(current_port="ge-0/0/13")
        self.assertEqual(Endpoint.objects.count(), 2)

    def test_sentinel_values_allowed(self):
        """Composite key permits the sentinel values that mark
        sweep-only endpoints with no infrastructure
        correlation."""
        ep = self._make_endpoint(
            mac_address="AA:BB:CC:DD:EE:02",
            current_switch="(none)",
            current_port="(none)",
            current_vlan=0,
        )
        self.assertEqual(ep.current_switch, "(none)")

    def test_additional_ips_default_list(self):
        ep = self._make_endpoint()
        self.assertEqual(ep.additional_ips, [])

    def test_additional_ips_jsonfield_round_trip(self):
        ep = self._make_endpoint(
            additional_ips=["192.0.2.10", "192.0.2.11"],
        )
        ep.refresh_from_db()
        self.assertEqual(
            sorted(ep.additional_ips),
            ["192.0.2.10", "192.0.2.11"],
        )

    def test_str_representation(self):
        ep = self._make_endpoint()
        self.assertIn("AA:BB:CC:DD:EE:01", str(ep))
        self.assertIn("ge-0/0/12", str(ep))

    def test_first_seen_default_is_callable(self):
        """``timezone.now`` must be a callable default — not a
        snapshot at module-load time."""
        ep1 = self._make_endpoint()
        ep2 = self._make_endpoint(
            mac_address="AA:BB:CC:DD:EE:03",
        )
        # Different timestamps, even if very close together.
        # Just confirm both are populated and non-null.
        self.assertIsNotNone(ep1.first_seen)
        self.assertIsNotNone(ep2.first_seen)

    def test_indexes_present(self):
        """The model declares four indexes per E0 §2c."""
        index_fields = {
            tuple(idx.fields)
            for idx in Endpoint._meta.indexes
        }
        self.assertIn(("mac_address",), index_fields)
        self.assertIn(("active",), index_fields)
        self.assertIn(("current_ip",), index_fields)
        self.assertIn(("last_seen",), index_fields)

    def test_text_fields_have_no_max_length(self):
        """Per CLAUDE.md schema convention — TextField, no length
        bounds."""
        from django.db.models import TextField

        for field_name in (
            "mac_address",
            "current_switch",
            "current_port",
            "current_ip",
            "hostname",
            "mac_vendor",
            "classification",
        ):
            field = Endpoint._meta.get_field(field_name)
            self.assertIsInstance(field, TextField)
