"""Tests for ``mnm_plugin.utils.controller_client``.

Coverage targets per E4 §C:
  - 200 with populated payload → returns parsed list
  - 200 with empty events list → returns ``[]`` (NOT ``None``)
  - 200 with malformed JSON → returns ``None``
  - 200 with unexpected shape → returns ``None``
  - Non-2xx response → returns ``None``
  - ConnectError / ConnectTimeout → returns ``None``
  - TimeoutException → returns ``None``
  - Cache hit within TTL → no second HTTP call
  - Cache miss after TTL → second HTTP call
  - Missing ``NAUTOBOT_SECRET_KEY`` → returns ``None`` and logs once

Mocks ``httpx.AsyncClient.get`` rather than running a real HTTP
server. The unit under test is the failure-mode envelope, not the
network layer.
"""

from __future__ import annotations

import asyncio
import time
from unittest import mock

import httpx
from django.test import TestCase

from mnm_plugin.utils import controller_client


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _reset_module_state():
    """Wipe cache + warn-dedup state + force a fresh client.

    Called from every test's ``setUp`` so cases don't interfere
    with each other.
    """
    controller_client._event_cache.clear()
    controller_client._warn_dedup.clear()
    controller_client._client = None


def _mock_response(
    *, status_code: int = 200, payload=None, raise_on_json: bool = False,
):
    resp = mock.MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if raise_on_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = payload if payload is not None else {}
    return resp


def _make_secret_env():
    """Patch the module-level TOKEN_SECRET so token minting succeeds."""
    return mock.patch.object(
        controller_client, "TOKEN_SECRET", "test-secret-12345",
    )


def _run(coro):
    """Run a coroutine to completion in a fresh event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 200 — happy paths
# ---------------------------------------------------------------------------

class HappyPathTests(TestCase):
    def setUp(self):
        _reset_module_state()

    def test_200_populated_returns_list(self):
        payload = {
            "mac": "AA:BB:CC:DD:EE:01",
            "events": [
                {
                    "id": "1", "mac_address": "AA:BB:CC:DD:EE:01",
                    "event_type": "appeared",
                    "old_value": None, "new_value": None,
                    "details": {"switch": "ex2300", "port": "ge-0/0/12", "ip": "192.0.2.10"},
                    "timestamp": "2026-05-02T12:00:00+00:00",
                },
                {
                    "id": "2", "mac_address": "AA:BB:CC:DD:EE:01",
                    "event_type": "moved_port",
                    "old_value": "ge-0/0/12", "new_value": "ge-0/0/13",
                    "details": {"switch": "ex2300"},
                    "timestamp": "2026-05-02T13:00:00+00:00",
                },
            ],
        }
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(return_value=_mock_response(payload=payload)),
        ):
            result = _run(
                controller_client.get_endpoint_events("aa:bb:cc:dd:ee:01"),
            )
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["event_type"], "appeared")
        self.assertIn("First seen on switch ex2300", result[0]["description"])
        self.assertEqual(result[1]["event_type"], "moved_port")
        self.assertIn("ge-0/0/12", result[1]["description"])
        self.assertIn("ge-0/0/13", result[1]["description"])

    def test_200_empty_events_returns_empty_list(self):
        payload = {"mac": "AA:BB:CC:DD:EE:02", "events": []}
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(return_value=_mock_response(payload=payload)),
        ):
            result = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:02"),
            )
        self.assertEqual(result, [])
        self.assertIsNotNone(result)  # Must be [] not None — distinct semantic

    def test_limit_parameter_caps_response(self):
        payload = {
            "mac": "AA:BB:CC:DD:EE:03",
            "events": [
                {
                    "id": str(i), "event_type": "ip_changed",
                    "old_value": "192.0.2.1", "new_value": "192.0.2.2",
                    "details": {}, "timestamp": f"2026-05-02T{i:02d}:00:00+00:00",
                }
                for i in range(50)
            ],
        }
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(return_value=_mock_response(payload=payload)),
        ):
            result = _run(
                controller_client.get_endpoint_events(
                    "AA:BB:CC:DD:EE:03", limit=10,
                ),
            )
        self.assertEqual(len(result), 10)


# ---------------------------------------------------------------------------
# Failure modes — all return None
# ---------------------------------------------------------------------------

class FailureModeTests(TestCase):
    def setUp(self):
        _reset_module_state()

    def test_connect_error_returns_none(self):
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(side_effect=httpx.ConnectError("conn refused")),
        ):
            result = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:04"),
            )
        self.assertIsNone(result)

    def test_connect_timeout_returns_none(self):
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(side_effect=httpx.ConnectTimeout("connect timeout")),
        ):
            result = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:05"),
            )
        self.assertIsNone(result)

    def test_timeout_returns_none(self):
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(side_effect=httpx.ReadTimeout("read timeout")),
        ):
            result = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:06"),
            )
        self.assertIsNone(result)

    def test_500_returns_none(self):
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(return_value=_mock_response(status_code=500)),
        ):
            result = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:07"),
            )
        self.assertIsNone(result)

    def test_401_returns_none(self):
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(return_value=_mock_response(status_code=401)),
        ):
            result = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:08"),
            )
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(
                return_value=_mock_response(raise_on_json=True),
            ),
        ):
            result = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:09"),
            )
        self.assertIsNone(result)

    def test_unexpected_shape_returns_none(self):
        # Returns a 200 with a list at top-level instead of dict — wrong shape
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(return_value=_mock_response(payload=[1, 2, 3])),
        ):
            result = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:0A"),
            )
        self.assertIsNone(result)

    def test_missing_secret_returns_none(self):
        # No TOKEN_SECRET → cannot mint a token → fail soft
        with mock.patch.object(controller_client, "TOKEN_SECRET", ""):
            getter = mock.AsyncMock(
                return_value=_mock_response(payload={"events": []}),
            )
            with mock.patch.object(httpx.AsyncClient, "get", getter):
                result = _run(
                    controller_client.get_endpoint_events("AA:BB:CC:DD:EE:0B"),
                )
        self.assertIsNone(result)
        getter.assert_not_called()  # Must not even attempt the call


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------

class CacheTests(TestCase):
    def setUp(self):
        _reset_module_state()

    def test_cache_hit_within_ttl_no_second_call(self):
        payload = {"mac": "AA:BB:CC:DD:EE:0C", "events": []}
        getter = mock.AsyncMock(
            return_value=_mock_response(payload=payload),
        )
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get", getter,
        ):
            r1 = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:0C"),
            )
            r2 = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:0C"),
            )
        self.assertEqual(r1, [])
        self.assertEqual(r2, [])
        self.assertEqual(getter.await_count, 1)

    def test_cache_miss_after_ttl_triggers_second_call(self):
        payload = {"mac": "AA:BB:CC:DD:EE:0D", "events": []}
        getter = mock.AsyncMock(
            return_value=_mock_response(payload=payload),
        )
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get", getter,
        ):
            _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:0D"),
            )
            # Manually expire the cache entry
            mac = "AA:BB:CC:DD:EE:0D"
            _, value = controller_client._event_cache[mac]
            controller_client._event_cache[mac] = (
                time.time() - 1, value,
            )
            _run(
                controller_client.get_endpoint_events(mac),
            )
        self.assertEqual(getter.await_count, 2)

    def test_failure_is_cached_too(self):
        # A failure response should also be cached so a controller
        # outage doesn't generate one HTTP call per detail-page hit.
        getter = mock.AsyncMock(side_effect=httpx.ConnectError("down"))
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get", getter,
        ):
            r1 = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:0E"),
            )
            r2 = _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:0E"),
            )
        self.assertIsNone(r1)
        self.assertIsNone(r2)
        self.assertEqual(getter.await_count, 1)


# ---------------------------------------------------------------------------
# MAC normalization — case-insensitive cache key
# ---------------------------------------------------------------------------

class MacNormalizationTests(TestCase):
    def setUp(self):
        _reset_module_state()

    def test_lowercase_and_uppercase_share_cache(self):
        payload = {"mac": "AA:BB:CC:DD:EE:0F", "events": []}
        getter = mock.AsyncMock(
            return_value=_mock_response(payload=payload),
        )
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get", getter,
        ):
            _run(
                controller_client.get_endpoint_events("aa:bb:cc:dd:ee:0f"),
            )
            _run(
                controller_client.get_endpoint_events("AA:BB:CC:DD:EE:0F"),
            )
        self.assertEqual(getter.await_count, 1)


# ---------------------------------------------------------------------------
# Sync wrapper
# ---------------------------------------------------------------------------

class SyncWrapperTests(TestCase):
    def setUp(self):
        _reset_module_state()

    def test_sync_wrapper_returns_async_result(self):
        payload = {
            "mac": "AA:BB:CC:DD:EE:10",
            "events": [
                {
                    "id": "1", "event_type": "appeared",
                    "old_value": None, "new_value": None,
                    "details": {"switch": "s1", "port": "p1", "ip": "192.0.2.1"},
                    "timestamp": "2026-05-02T12:00:00+00:00",
                },
            ],
        }
        with _make_secret_env(), mock.patch.object(
            httpx.AsyncClient, "get",
            mock.AsyncMock(return_value=_mock_response(payload=payload)),
        ):
            result = controller_client.get_endpoint_events_sync(
                "AA:BB:CC:DD:EE:10",
            )
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# Event normalization — exercise the description-builder branches
# ---------------------------------------------------------------------------

class EventNormalizationTests(TestCase):
    def test_appeared_description(self):
        out = controller_client._normalize_event({
            "event_type": "appeared",
            "details": {"switch": "s1", "port": "p1", "ip": "192.0.2.1"},
            "timestamp": "ts",
        })
        self.assertIn("First seen on switch s1", out["description"])

    def test_moved_port_description(self):
        out = controller_client._normalize_event({
            "event_type": "moved_port",
            "old_value": "p1", "new_value": "p2",
            "details": {"switch": "s1"},
            "timestamp": "ts",
        })
        self.assertIn("Moved from port p1 to port p2", out["description"])

    def test_moved_switch_description(self):
        out = controller_client._normalize_event({
            "event_type": "moved_switch",
            "old_value": "s1", "new_value": "s2",
            "details": {},
            "timestamp": "ts",
        })
        self.assertIn("Moved from switch s1 to switch s2", out["description"])

    def test_ip_changed_description(self):
        out = controller_client._normalize_event({
            "event_type": "ip_changed",
            "old_value": "192.0.2.1", "new_value": "192.0.2.2",
            "details": {},
            "timestamp": "ts",
        })
        self.assertIn("IP changed from 192.0.2.1 to 192.0.2.2", out["description"])

    def test_disappeared_description(self):
        out = controller_client._normalize_event({
            "event_type": "disappeared",
            "details": {},
            "timestamp": "ts",
        })
        self.assertEqual(out["description"], "No longer seen on this network")

    def test_unknown_event_falls_through(self):
        out = controller_client._normalize_event({
            "event_type": "wat",
            "old_value": "a", "new_value": "b",
            "details": {},
            "timestamp": "ts",
        })
        self.assertEqual(out["description"], "a → b")
        self.assertEqual(out["event_type"], "wat")
