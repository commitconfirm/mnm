"""Tests for the E6 CSV / JSON export endpoints.

Coverage:
  - CSV export streams the filtered queryset; row count matches.
  - JSON export returns a parseable JSON list.
  - Filter params propagate from URL to export queryset.
  - 50,000-row cap returns 413 (mocked via patching EXPORT_LIMIT).
  - Empty queryset returns a valid-but-empty export.
  - Unknown model_key returns 404.

Runs inside Nautobot's test runner (deferred to G validation).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from mnm_plugin import exports, models


User = get_user_model()


def _now():
    return timezone.now()


class ExportEndpointTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="exporter",
            email="exporter@example.com",
            password="x",
        )
        # Three endpoints — two on sw1, one on sw2.
        models.Endpoint.objects.create(
            mac_address="aa:00:00:00:00:01",
            current_switch="sw1", current_port="ge-0/0/1",
            current_vlan=10, current_ip="192.0.2.10",
            last_seen=_now(), active=True,
        )
        models.Endpoint.objects.create(
            mac_address="aa:00:00:00:00:02",
            current_switch="sw1", current_port="ge-0/0/2",
            current_vlan=10, current_ip="192.0.2.11",
            last_seen=_now(), active=True,
        )
        models.Endpoint.objects.create(
            mac_address="aa:00:00:00:00:03",
            current_switch="sw2", current_port="ge-0/0/1",
            current_vlan=20, current_ip="192.0.2.12",
            last_seen=_now(), active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_csv_streams_all_rows_when_unfiltered(self):
        response = self.client.get(
            reverse("plugins:mnm_plugin:endpoint_export_csv"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn(
            'attachment; filename="mnm_endpoints.csv"',
            response["Content-Disposition"],
        )
        body = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.reader(io.StringIO(body))
        rows = list(reader)
        # 1 header + 3 data rows
        self.assertEqual(len(rows), 4)
        # mac_address column should appear in header
        self.assertIn("mac_address", rows[0])

    def test_csv_respects_filter_params(self):
        response = self.client.get(
            reverse("plugins:mnm_plugin:endpoint_export_csv"),
            {"current_switch": "sw2"},
        )
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode("utf-8")
        rows = list(csv.reader(io.StringIO(body)))
        # 1 header + 1 sw2 row
        self.assertEqual(len(rows), 2)

    def test_json_returns_valid_list(self):
        response = self.client.get(
            reverse("plugins:mnm_plugin:endpoint_export_json"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"], "application/json",
        )
        body = b"".join(response.streaming_content).decode("utf-8")
        data = json.loads(body)
        self.assertEqual(len(data), 3)

    def test_json_respects_filter_params(self):
        response = self.client.get(
            reverse("plugins:mnm_plugin:endpoint_export_json"),
            {"current_vlan": 10},
        )
        body = b"".join(response.streaming_content).decode("utf-8")
        data = json.loads(body)
        self.assertEqual(len(data), 2)

    def test_export_with_dsl_query(self):
        response = self.client.get(
            reverse("plugins:mnm_plugin:endpoint_export_json"),
            {"q": "current_switch = sw2"},
        )
        body = b"".join(response.streaming_content).decode("utf-8")
        data = json.loads(body)
        self.assertEqual(len(data), 1)

    def test_50k_cap_returns_413(self):
        # Patch EXPORT_LIMIT down to 1 to trigger the cap.
        with mock.patch.object(exports, "EXPORT_LIMIT", 1):
            response = self.client.get(
                reverse("plugins:mnm_plugin:endpoint_export_csv"),
            )
        self.assertEqual(response.status_code, 413)
        body = json.loads(response.content)
        self.assertIn("error", body)
        self.assertIn("refine your filter", body["error"])

    def test_empty_csv_when_no_rows_match(self):
        response = self.client.get(
            reverse("plugins:mnm_plugin:endpoint_export_csv"),
            {"current_switch": "no-such-switch"},
        )
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode("utf-8")
        rows = list(csv.reader(io.StringIO(body)))
        # Header only — no data rows
        self.assertEqual(len(rows), 1)

    def test_arp_export_csv(self):
        models.ArpEntry.objects.create(
            node_name="sw1", ip="192.0.2.1",
            mac="aa:bb:cc:dd:ee:01", interface="ge-0/0/0",
            vrf="default", collected_at=_now(),
        )
        response = self.client.get(
            reverse("plugins:mnm_plugin:arpentry_export_csv"),
        )
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode("utf-8")
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(len(rows), 2)  # header + 1
        self.assertIn(
            'attachment; filename="mnm_arp_entries.csv"',
            response["Content-Disposition"],
        )


class ExportDirectFunctionTests(TestCase):
    """Direct calls to ``_filtered_queryset`` for behaviour the URL
    routes don't reach (unknown model_key)."""

    def test_unknown_model_key_returns_404(self):
        from django.test import RequestFactory
        rf = RequestFactory()
        request = rf.get("/plugins/mnm/bogus/export.csv")
        response = exports.export_csv(request, "bogus")
        self.assertEqual(response.status_code, 404)
