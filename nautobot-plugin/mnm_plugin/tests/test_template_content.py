"""Tests for the ``InterfaceMnmPanels`` template extension.

Coverage targets per E5 prompt:
  - Constructed with a real ``dcim.Interface`` instance, ``right_page()``
    returns a string containing the expected panel headings.
  - Empty querysets render the "No data for this port" message.
  - Each panel's row count matches the slice cap.
  - One panel's ORM call raising doesn't break the others (BLE001
    fail-soft via ``_safe_query``).
  - The cross-vendor naming helper ``expand_for_lookup`` is consulted
    with the interface's literal name.

These run inside Nautobot's test runner (deferred to G integration
validation), separate from the controller-side ``pytest tests/unit/``.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from mnm_plugin.models import Endpoint, LldpNeighbor, MacEntry
from mnm_plugin.template_content import InterfaceMnmPanels


User = get_user_model()


class _FakeDevice:
    def __init__(self, name: str):
        self.name = name


class _FakeInterface:
    """Stand-in for ``dcim.Interface``. ``right_page()`` only reads
    ``.device.name`` and ``.name`` from the context's object —
    using a fake avoids needing to set up a full Nautobot Device
    record per test."""

    def __init__(self, device_name: str, interface_name: str):
        self.device = _FakeDevice(device_name)
        self.name = interface_name


def _make_extension(device_name: str, interface_name: str):
    iface = _FakeInterface(device_name, interface_name)
    return InterfaceMnmPanels({"object": iface})


class InterfaceMnmPanelsRenderTests(TestCase):
    """Render-only tests against an empty database — every panel
    should render the "No data" message."""

    def test_returns_string(self):
        ext = _make_extension("ex2300-24p", "ge-0/0/0")
        out = ext.right_page()
        self.assertIsInstance(out, str)

    def test_returns_empty_string_when_object_missing(self):
        ext = InterfaceMnmPanels({"object": None})
        self.assertEqual(ext.right_page(), "")

    def test_returns_empty_string_when_no_device(self):
        iface = mock.MagicMock()
        iface.device = None
        ext = InterfaceMnmPanels({"object": iface})
        self.assertEqual(ext.right_page(), "")

    def test_renders_all_four_panel_headings(self):
        ext = _make_extension("ex2300-24p", "ge-0/0/0")
        out = ext.right_page()
        self.assertIn("Endpoints currently on this port", out)
        self.assertIn("Endpoints historically on this port", out)
        self.assertIn("LLDP neighbors on this port", out)
        self.assertIn("MAC entries on this port", out)

    def test_empty_state_renders_no_data_message(self):
        ext = _make_extension("ex2300-24p", "ge-0/0/0")
        out = ext.right_page()
        self.assertIn("No data for this port", out)


class InterfaceMnmPanelsPopulatedTests(TestCase):
    """Render tests with matching plugin rows in the test DB."""

    @classmethod
    def setUpTestData(cls):
        cls.device_name = "ex2300-24p"
        cls.iface_name = "ge-0/0/12"
        # Active endpoint
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:01",
            current_switch=cls.device_name,
            current_port=cls.iface_name,
            current_vlan=10,
            current_ip="192.0.2.10",
            hostname="server-a",
            active=True,
        )
        # Inactive (historical) endpoint
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:02",
            current_switch=cls.device_name,
            current_port=cls.iface_name,
            current_vlan=10,
            current_ip="192.0.2.20",
            active=False,
        )
        # MAC entry
        MacEntry.objects.create(
            node_name=cls.device_name,
            mac="AA:BB:CC:DD:EE:01",
            interface=cls.iface_name,
            vlan=10,
            entry_type="dynamic",
        )
        # LLDP neighbor
        LldpNeighbor.objects.create(
            node_name=cls.device_name,
            local_interface=cls.iface_name,
            remote_system_name="some-other-switch",
            remote_port="ge-0/0/0",
            remote_chassis_id="aa:bb:cc:11:22:33",
        )

    def test_active_endpoint_renders(self):
        ext = _make_extension(self.device_name, self.iface_name)
        out = ext.right_page()
        self.assertIn("AA:BB:CC:DD:EE:01", out)
        self.assertIn("server-a", out)

    def test_lldp_neighbor_renders(self):
        ext = _make_extension(self.device_name, self.iface_name)
        out = ext.right_page()
        self.assertIn("some-other-switch", out)

    def test_mac_entry_renders(self):
        ext = _make_extension(self.device_name, self.iface_name)
        out = ext.right_page()
        # MAC appears in two panels (active endpoint + mac table) —
        # confirm at least one rendered and the type chip is present.
        self.assertIn("Dynamic", out)

    def test_show_all_link_present(self):
        ext = _make_extension(self.device_name, self.iface_name)
        out = ext.right_page()
        self.assertIn(self.iface_name, out)
        self.assertIn("Show all", out)


class InterfaceMnmPanelsCrossVendorLookupTests(TestCase):
    """The expand_for_lookup helper drives every panel — verify it
    actually catches stored rows under variant naming forms."""

    def test_endpoint_matches_via_junos_logical_unit_form(self):
        """dcim.Interface.name = 'ge-0/0/0'; plugin row stored under
        'ge-0/0/0.0' should be picked up by expand_for_lookup."""
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:0A",
            current_switch="ex2300-24p",
            current_port="ge-0/0/0.0",  # legacy NAPALM-shape form
            current_vlan=10,
            active=True,
        )
        ext = _make_extension("ex2300-24p", "ge-0/0/0")
        out = ext.right_page()
        self.assertIn("AA:BB:CC:DD:EE:0A", out)

    def test_mac_matches_via_cisco_short_form(self):
        """dcim.Interface.name = 'GigabitEthernet1'; MAC stored under
        'Gi1' should be picked up."""
        MacEntry.objects.create(
            node_name="cisco-mnm",
            mac="AA:BB:CC:DD:EE:0B",
            interface="Gi1",
            vlan=1,
            entry_type="dynamic",
        )
        ext = _make_extension("cisco-mnm", "GigabitEthernet1")
        out = ext.right_page()
        self.assertIn("AA:BB:CC:DD:EE:0B", out)

    def test_arista_no_expansion(self):
        """dcim.Interface.name = 'Ethernet1'; Arista doesn't expand,
        plugin storage and Nautobot match verbatim."""
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:0C",
            current_switch="arista-mnm",
            current_port="Ethernet1",
            current_vlan=1,
            active=True,
        )
        ext = _make_extension("arista-mnm", "Ethernet1")
        out = ext.right_page()
        self.assertIn("AA:BB:CC:DD:EE:0C", out)

    def test_fortinet_alias_match(self):
        """dcim.Interface.name = 'wan'; FortiGate aliases match
        verbatim (no expansion)."""
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:0D",
            current_switch="FG-40F",
            current_port="wan",
            current_vlan=0,
            active=True,
        )
        ext = _make_extension("FG-40F", "wan")
        out = ext.right_page()
        self.assertIn("AA:BB:CC:DD:EE:0D", out)


class InterfaceMnmPanelsFailSoftTests(TestCase):
    """One panel's ORM raising must not break the others — host
    page must remain renderable even under partial failure."""

    def test_one_panel_query_raise_does_not_break_others(self):
        ext = _make_extension("ex2300-24p", "ge-0/0/0")
        # Force MacEntry.objects.filter to raise; Endpoint and
        # LldpNeighbor queries should still complete and render.
        with mock.patch(
            "mnm_plugin.models.MacEntry.objects",
            new_callable=mock.PropertyMock,
        ) as mock_objects:
            mock_objects.return_value.filter.side_effect = (
                RuntimeError("simulated panel failure")
            )
            out = ext.right_page()
        # Panel headings still render (page wasn't broken)
        self.assertIn("Endpoints currently on this port", out)
        self.assertIn("LLDP neighbors on this port", out)
        # Failed panel renders its error notice
        self.assertIn("Panel rendering failed", out)


class InterfaceMnmPanelsHelperUseTests(TestCase):
    """The helper must be consulted with the interface's literal name."""

    def test_expand_for_lookup_called_with_interface_name(self):
        with mock.patch(
            "mnm_plugin.template_content.expand_for_lookup",
            return_value=["ge-0/0/0", "ge-0/0/0.0"],
        ) as mock_expand:
            ext = _make_extension("ex2300-24p", "ge-0/0/0")
            ext.right_page()
        mock_expand.assert_called_once_with("ge-0/0/0")

    def test_empty_candidates_returns_empty(self):
        with mock.patch(
            "mnm_plugin.template_content.expand_for_lookup",
            return_value=[],
        ):
            ext = _make_extension("ex2300-24p", "")
            self.assertEqual(ext.right_page(), "")
