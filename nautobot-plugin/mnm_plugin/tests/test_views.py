"""View tests — list view, detail view, and table cross-link
rendering with the cross-vendor naming helper.

E4 extends with detail-view rendering tests for all seven models:
  - Each model's detail view returns 200 for an existing instance
  - Each detail page renders its expected E4 panel headings
  - Endpoint detail's three Recent Events states (events / empty /
    unavailable) all render the correct panel content
  - Cross-row history panel populates when prior observations exist
  - Cross-model identity panels populate when matching MAC rows
    exist in other plugin tables
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from mnm_plugin.models import (
    ArpEntry,
    BgpNeighbor,
    Endpoint,
    Fingerprint,
    LldpNeighbor,
    MacEntry,
    Route,
)


User = get_user_model()


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Endpoint (E1 baseline + E4 enrichment)
# ---------------------------------------------------------------------------

class EndpointViewsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="tester",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:01",
            current_switch="ex2300-24p",
            current_port="ge-0/0/12",
            current_vlan=10,
            current_ip="192.0.2.10",
            hostname="server-a",
        )
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:02",
            current_switch="(none)",
            current_port="(none)",
            current_vlan=0,
            current_ip="192.0.2.20",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_list_view_returns_200(self):
        url = reverse("plugins:mnm_plugin:endpoint_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_list_view_renders_endpoints(self):
        url = reverse("plugins:mnm_plugin:endpoint_list")
        resp = self.client.get(url)
        body = resp.content.decode("utf-8")
        self.assertIn("AA:BB:CC:DD:EE:01", body)
        self.assertIn("ge-0/0/12", body)

    def test_detail_view_returns_200(self):
        ep = Endpoint.objects.get(mac_address="AA:BB:CC:DD:EE:01")
        url = reverse("plugins:mnm_plugin:endpoint", args=[ep.pk])
        with mock.patch(
            "mnm_plugin.views.controller_client.get_endpoint_events_sync",
            return_value=None,
        ):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_detail_view_all_locations_panel(self):
        """If two rows share the same MAC across different
        locations, the detail view's "All locations seen"
        panel must show both rows."""
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:01",
            current_switch="ex2300-24p",
            current_port="ge-0/0/13",
            current_vlan=10,
            active=False,
        )
        ep = Endpoint.objects.filter(
            mac_address="AA:BB:CC:DD:EE:01", active=True,
        ).first()
        url = reverse("plugins:mnm_plugin:endpoint", args=[ep.pk])
        with mock.patch(
            "mnm_plugin.views.controller_client.get_endpoint_events_sync",
            return_value=None,
        ):
            resp = self.client.get(url)
        body = resp.content.decode("utf-8")
        self.assertIn("ge-0/0/12", body)
        self.assertIn("ge-0/0/13", body)

    def test_detail_recent_events_unavailable_state(self):
        """controller_client returning ``None`` renders the
        graceful-degradation message."""
        ep = Endpoint.objects.get(mac_address="AA:BB:CC:DD:EE:01")
        url = reverse("plugins:mnm_plugin:endpoint", args=[ep.pk])
        with mock.patch(
            "mnm_plugin.views.controller_client.get_endpoint_events_sync",
            return_value=None,
        ):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("Controller unavailable", body)

    def test_detail_recent_events_empty_state(self):
        """controller_client returning ``[]`` renders the empty
        message, distinct from the unavailable message."""
        ep = Endpoint.objects.get(mac_address="AA:BB:CC:DD:EE:01")
        url = reverse("plugins:mnm_plugin:endpoint", args=[ep.pk])
        with mock.patch(
            "mnm_plugin.views.controller_client.get_endpoint_events_sync",
            return_value=[],
        ):
            resp = self.client.get(url)
        body = resp.content.decode("utf-8")
        self.assertIn("No recent events", body)
        self.assertNotIn("Controller unavailable", body)

    def test_detail_recent_events_populated_state(self):
        """controller_client returning a list renders the events
        table."""
        ep = Endpoint.objects.get(mac_address="AA:BB:CC:DD:EE:01")
        url = reverse("plugins:mnm_plugin:endpoint", args=[ep.pk])
        with mock.patch(
            "mnm_plugin.views.controller_client.get_endpoint_events_sync",
            return_value=[
                {
                    "timestamp": "2026-05-02T12:00:00+00:00",
                    "event_type": "appeared",
                    "source": "ex2300-24p",
                    "description": "First seen on switch ex2300-24p port ge-0/0/12",
                },
            ],
        ):
            resp = self.client.get(url)
        body = resp.content.decode("utf-8")
        self.assertIn("appeared", body)
        self.assertIn("First seen on switch", body)
        self.assertNotIn("Controller unavailable", body)
        self.assertNotIn("No recent events", body)

    def test_detail_cross_model_mac_table_panel(self):
        """An Endpoint with a matching MacEntry on the same MAC
        renders the MAC table observations panel."""
        ep = Endpoint.objects.get(mac_address="AA:BB:CC:DD:EE:01")
        MacEntry.objects.create(
            node_name="ex2300-24p",
            mac="AA:BB:CC:DD:EE:01",
            interface="ge-0/0/12",
            vlan=10,
            entry_type="dynamic",
        )
        url = reverse("plugins:mnm_plugin:endpoint", args=[ep.pk])
        with mock.patch(
            "mnm_plugin.views.controller_client.get_endpoint_events_sync",
            return_value=None,
        ):
            resp = self.client.get(url)
        body = resp.content.decode("utf-8")
        self.assertIn("MAC table observations", body)
        # The MAC table row must be in the body (the matching entry,
        # rendered via the panel).
        self.assertIn("ex2300-24p", body)

    def test_detail_cross_model_arp_panel(self):
        """An Endpoint with a matching ArpEntry on the same MAC
        renders the ARP observations panel."""
        ep = Endpoint.objects.get(mac_address="AA:BB:CC:DD:EE:01")
        ArpEntry.objects.create(
            node_name="ex2300-24p",
            ip="192.0.2.10",
            mac="AA:BB:CC:DD:EE:01",
            interface="vlan.10",
            vrf="default",
        )
        url = reverse("plugins:mnm_plugin:endpoint", args=[ep.pk])
        with mock.patch(
            "mnm_plugin.views.controller_client.get_endpoint_events_sync",
            return_value=None,
        ):
            resp = self.client.get(url)
        body = resp.content.decode("utf-8")
        self.assertIn("ARP observations", body)


class EndpointApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="apitester",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        Endpoint.objects.create(
            mac_address="AA:BB:CC:DD:EE:0A",
            current_switch="ex3300-48p",
            current_port="ge-0/0/1",
            current_vlan=20,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_api_list_returns_200(self):
        resp = self.client.get("/api/plugins/mnm/endpoints/")
        self.assertEqual(resp.status_code, 200)

    def test_api_list_returns_endpoints(self):
        resp = self.client.get(
            "/api/plugins/mnm/endpoints/?format=json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertGreaterEqual(data["count"], 1)
        macs = [r["mac_address"] for r in data["results"]]
        self.assertIn("AA:BB:CC:DD:EE:0A", macs)


# ---------------------------------------------------------------------------
# E4 — per-model detail-view rendering smoke tests
# ---------------------------------------------------------------------------

class ArpEntryDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="arptester", password="x",
            is_staff=True, is_superuser=True,
        )
        cls.row = ArpEntry.objects.create(
            node_name="ex2300-24p",
            ip="192.0.2.10",
            mac="AA:BB:CC:DD:EE:01",
            interface="vlan.10",
            vrf="default",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_detail_returns_200_and_renders_e4_panels(self):
        url = reverse("plugins:mnm_plugin:arpentry", args=[self.row.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("Prior observations", body)
        self.assertIn("Endpoint records for this MAC", body)
        self.assertIn("MAC table observations", body)


class MacEntryDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="mactester", password="x",
            is_staff=True, is_superuser=True,
        )
        cls.row = MacEntry.objects.create(
            node_name="ex2300-24p",
            mac="AA:BB:CC:DD:EE:01",
            interface="ge-0/0/12",
            vlan=10,
            entry_type="dynamic",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_detail_returns_200_and_renders_e4_panels(self):
        url = reverse("plugins:mnm_plugin:macentry", args=[self.row.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("Prior observations", body)
        self.assertIn("Endpoint records for this MAC", body)
        self.assertIn("ARP observations", body)


class LldpNeighborDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="lldptester", password="x",
            is_staff=True, is_superuser=True,
        )
        cls.row = LldpNeighbor.objects.create(
            node_name="ex2300-24p",
            local_interface="ge-0/0/12",
            remote_system_name="some-other-switch",
            remote_port="ge-0/0/0",
            remote_chassis_id="aa:bb:cc:11:22:33",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_detail_returns_200_and_renders_e4_history_panel(self):
        url = reverse(
            "plugins:mnm_plugin:lldpneighbor", args=[self.row.pk],
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("Prior observations", body)


class RouteDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="routetester", password="x",
            is_staff=True, is_superuser=True,
        )
        cls.row = Route.objects.create(
            node_name="ex2300-24p",
            prefix="192.0.2.0/24",
            next_hop="192.0.2.1",
            protocol="static",
            vrf="default",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_detail_returns_200_and_renders_e4_history_panel(self):
        url = reverse("plugins:mnm_plugin:route", args=[self.row.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("Prior observations", body)
        self.assertIn("Same prefix on other nodes", body)


class BgpNeighborDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="bgptester", password="x",
            is_staff=True, is_superuser=True,
        )
        cls.row = BgpNeighbor.objects.create(
            node_name="ex2300-24p",
            neighbor_ip="192.0.2.2",
            remote_asn=65000,
            local_asn=65001,
            state="Established",
            vrf="default",
            address_family="ipv4-unicast",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_detail_returns_200_and_renders_e4_history_panel(self):
        url = reverse(
            "plugins:mnm_plugin:bgpneighbor", args=[self.row.pk],
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("Prior observations", body)


class FingerprintDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="fptester", password="x",
            is_staff=True, is_superuser=True,
        )
        cls.row = Fingerprint.objects.create(
            target_mac="AA:BB:CC:DD:EE:01",
            signal_type="mdns",
            signal_value="_workstation._tcp.local.",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_detail_returns_200_and_renders_e4_panels(self):
        url = reverse(
            "plugins:mnm_plugin:fingerprint", args=[self.row.pk],
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("Prior observations", body)
        self.assertIn("Endpoint records for this MAC", body)
