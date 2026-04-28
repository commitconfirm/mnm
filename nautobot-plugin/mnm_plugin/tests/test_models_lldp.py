"""Tests for the ``LldpNeighbor`` model.

Includes the Block C P2 schema-expansion columns
(``local_port_ifindex``, ``local_port_name``,
``remote_chassis_id_subtype``, ``remote_port_id_subtype``,
``remote_system_description``) and the unmanaged-neighbor
collision case from Block C P5.
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.db import IntegrityError
from django.test import TestCase

from mnm_plugin.models import LldpNeighbor


def _now():
    return datetime.now(timezone.utc)


class LldpNeighborModelTests(TestCase):
    def _make(self, **overrides) -> LldpNeighbor:
        defaults = dict(
            node_name="ex2300-24p",
            local_interface="ge-0/0/12",
            remote_system_name="switch-b",
            remote_port="Eth1",
            collected_at=_now(),
        )
        defaults.update(overrides)
        return LldpNeighbor.objects.create(**defaults)

    def test_create_basic(self):
        row = self._make()
        self.assertEqual(row.remote_system_name, "switch-b")

    def test_composite_unique_constraint(self):
        self._make()
        with self.assertRaises(IntegrityError):
            self._make()

    def test_p2_expansion_columns_round_trip(self):
        row = self._make(
            remote_chassis_id="aa:bb:cc:dd:ee:ff",
            remote_management_ip="192.0.2.20",
            local_port_ifindex=514,
            local_port_name="ge-0/0/12",
            remote_chassis_id_subtype="macAddress",
            remote_port_id_subtype="interfaceName",
            remote_system_description="Junos 21.4R3-S6.4",
        )
        row.refresh_from_db()
        self.assertEqual(row.local_port_ifindex, 514)
        self.assertEqual(row.remote_chassis_id_subtype, "macAddress")
        self.assertEqual(row.remote_port_id_subtype, "interfaceName")
        self.assertEqual(
            row.remote_system_description, "Junos 21.4R3-S6.4",
        )

    def test_unmanaged_neighbor_with_empty_remote_system_name(self):
        """Block C P5 case: anonymous LLDP neighbor with sys_name
        empty — the schema must permit ``""`` as a default."""
        row = self._make(remote_system_name="", remote_port="AA:BB")
        self.assertEqual(row.remote_system_name, "")

    def test_indexes_present(self):
        index_fields = {
            tuple(idx.fields) for idx in LldpNeighbor._meta.indexes
        }
        self.assertIn(("node_name",), index_fields)
        self.assertIn(("local_interface",), index_fields)
        self.assertIn(("remote_system_name",), index_fields)
        self.assertIn(("collected_at",), index_fields)

    def test_p2_expansion_columns_nullable(self):
        """All five expansion columns must be nullable per Block
        C P2 — NAPALM-path rows from before the SNMP collector
        landed have NULL in these fields."""
        row = self._make(
            remote_chassis_id=None,
            remote_management_ip=None,
            local_port_ifindex=None,
            local_port_name=None,
            remote_chassis_id_subtype=None,
            remote_port_id_subtype=None,
            remote_system_description=None,
        )
        row.refresh_from_db()
        self.assertIsNone(row.local_port_ifindex)
        self.assertIsNone(row.remote_chassis_id_subtype)
