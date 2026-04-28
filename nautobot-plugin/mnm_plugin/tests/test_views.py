"""View tests — list view, detail view, and table cross-link
rendering with the cross-vendor naming helper."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from mnm_plugin.models import Endpoint


User = get_user_model()


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
        resp = self.client.get(url)
        body = resp.content.decode("utf-8")
        self.assertIn("ge-0/0/12", body)
        self.assertIn("ge-0/0/13", body)


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
