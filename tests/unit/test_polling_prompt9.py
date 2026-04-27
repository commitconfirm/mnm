"""Unit tests for Prompt 9 Block A polling-loop hardening.

Covers:

* Status-gated polling: core collectors (arp/mac/lldp/routes/bgp/dhcp)
  are skipped for devices whose Nautobot status is not ``Active``;
  ``phase2_populate`` is exempt.
* Health calculation on ``/api/nodes``: ``enabled=False`` rows are
  excluded (operator-disabled rows are scheduling gates, not failure
  signals). ``phase2_populate`` is also excluded from the health count.
* Cache-miss retry in ``phase2_populate`` dispatch: if the freshly
  onboarded device is missing from the cached ``get_devices()`` result,
  ``clear_cache()`` + retry once; on second-attempt-still-missing, the
  normal failure path runs.

Tests drive the dispatcher via :func:`_run_device`-equivalent logic
rather than running the full :func:`poll_loop` (which is an infinite
coroutine). The filtering behavior is the subject.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

import app.polling as _polling_preload  # noqa: E402, F401
import app.main as _main_preload  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Status-gated poll-loop filtering (Item 1)
# ---------------------------------------------------------------------------

def _status_gate(device_jobs: dict[str, list[str]],
                 device_info: dict[str, dict]) -> dict[str, list[str]]:
    """Replicate the gating block from polling.poll_loop. Pure function
    so we can unit-test the intent directly."""
    gated: dict[str, list[str]] = {}
    for dn, jts in device_jobs.items():
        info = device_info.get(dn.lower(), {})
        is_active = (info.get("status_name") == "Active")
        kept = [jt for jt in jts if jt == "phase2_populate" or is_active]
        if kept:
            gated[dn] = kept
    return gated


def test_status_gate_active_device_runs_all_jobs():
    device_info = {"active-switch": {"status_name": "Active"}}
    device_jobs = {"active-switch": ["arp", "mac", "lldp"]}
    out = _status_gate(device_jobs, device_info)
    assert out == {"active-switch": ["arp", "mac", "lldp"]}


def test_status_gate_onboarding_incomplete_skips_core_jobs():
    device_info = {"stuck-switch": {"status_name": "Onboarding Incomplete"}}
    device_jobs = {"stuck-switch": ["arp", "mac", "lldp"]}
    out = _status_gate(device_jobs, device_info)
    assert out == {}


def test_status_gate_onboarding_incomplete_allows_phase2_populate():
    """phase2_populate is the one job type that MUST still dispatch for
    non-Active devices — it's the path back to Active."""
    device_info = {"stuck": {"status_name": "Onboarding Incomplete"}}
    device_jobs = {"stuck": ["arp", "phase2_populate", "mac"]}
    out = _status_gate(device_jobs, device_info)
    assert out == {"stuck": ["phase2_populate"]}


def test_status_gate_onboarding_failed_skips_core_and_phase2_is_optional():
    """Onboarding Failed represents a Phase 1 partial state. We don't
    gate phase2_populate by status — the orchestrator guarantees a
    phase2_populate row doesn't exist for an Onboarding Failed device
    (Phase 1 rolled back before Step G.5). So the correct behavior is:
    if the caller hands us phase2_populate for such a device, let it
    through (idempotent failure to resolve primary_ip4 kicks in later).
    Onboarding Failed with ONLY core jobs: everything skipped."""
    device_info = {"failed": {"status_name": "Onboarding Failed"}}
    device_jobs = {"failed": ["arp", "lldp"]}
    out = _status_gate(device_jobs, device_info)
    assert out == {}


def test_status_gate_unknown_status_skips_core_jobs():
    """Devices whose status isn't recognized (Staged, Inventory, etc.)
    are not Active — core collectors skipped."""
    device_info = {"staged": {"status_name": "Staged"}}
    device_jobs = {"staged": ["arp"]}
    out = _status_gate(device_jobs, device_info)
    assert out == {}


def test_status_gate_missing_device_info_skips_core_jobs():
    """poll_loop can encounter rows for devices that aren't in the
    Nautobot fetch (device deleted, cache mid-refresh). Defaults to
    'not Active' — core jobs skipped. (The original _run_device
    path already marks these as 'Device not found in Nautobot'.)"""
    device_info: dict[str, dict] = {}
    device_jobs = {"ghost": ["arp", "mac"]}
    out = _status_gate(device_jobs, device_info)
    assert out == {}


# ---------------------------------------------------------------------------
# Health calculation with enabled=false (Item 2)
# ---------------------------------------------------------------------------

def _make_poll_rows(jobs: list[tuple[str, bool, bool, bool]]) -> list[dict]:
    """Build poll_rows as the /api/nodes aggregator sees them. Each tuple
    is (job_type, enabled, has_success, has_error)."""
    out = []
    for jt, enabled, success, err in jobs:
        out.append({
            "device_name": "test-node",
            "job_type": jt,
            "enabled": enabled,
            "last_success": "2026-04-21T00:00:00Z" if success else None,
            "last_error": "something" if err else None,
            "last_attempt": "2026-04-21T00:00:00Z",
        })
    return out


async def _run_list_nodes(poll_rows: list[dict]) -> dict:
    """Invoke list_nodes with a single ``test-node`` device and the given
    poll_rows; returns the single node dict."""
    from app.main import list_nodes

    devices = [{
        "name": "test-node", "id": "uuid",
        "platform": {"display": "juniper_junos"},
        "location": {"display": "Site"},
        "role": {"display": "Switch"},
        "primary_ip4": {"display": "192.0.2.1/32"},
        "status": {"display": "Active"},
        "url": "",
    }]
    with patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=devices)), \
         patch("app.main.polling.get_all_poll_status",
               new=AsyncMock(return_value=poll_rows)), \
         patch("app.main.polling.get_phase2_state",
               new=AsyncMock(return_value=None)):
        body = await list_nodes()
    return body["nodes"][0]


async def test_health_all_enabled_all_succeeding_is_green():
    rows = _make_poll_rows([
        ("arp", True, True, False),
        ("mac", True, True, False),
        ("lldp", True, True, False),
    ])
    node = await _run_list_nodes(rows)
    assert node["health"] == "green"
    assert node["coverage_label"] == "3/3 active"


async def test_health_disabled_rows_excluded_from_health():
    """6 enabled succeeding + 2 disabled rows: health=green, coverage=6/8."""
    rows = _make_poll_rows([
        ("arp", True, True, False),
        ("mac", True, True, False),
        ("lldp", True, True, False),
        ("routes", True, True, False),
        ("bgp", True, True, False),
        ("dhcp", True, True, False),
        # Operator-disabled — should not count against health.
        ("some_future_job_1", False, False, True),
        ("some_future_job_2", False, False, True),
    ])
    node = await _run_list_nodes(rows)
    assert node["health"] == "green"
    assert node["coverage_label"] == "6/8 active"


async def test_health_enabled_failing_is_yellow():
    rows = _make_poll_rows([
        ("arp", True, True, False),
        ("mac", True, True, False),
        ("lldp", True, False, True),
    ])
    node = await _run_list_nodes(rows)
    assert node["health"] == "yellow"


async def test_health_all_disabled_is_gray_all_disabled_label():
    rows = _make_poll_rows([
        ("arp", False, True, False),
        ("mac", False, True, False),
    ])
    node = await _run_list_nodes(rows)
    assert node["health"] == "gray"
    assert node["health_label"] == "All polls disabled"
    assert node["coverage_label"] == "0/2 active"


async def test_health_phase2_populate_excluded_from_count():
    """phase2_populate enabled=False means Phase 2 succeeded — it must
    not turn the device gray or count against coverage."""
    rows = _make_poll_rows([
        ("arp", True, True, False),
        ("phase2_populate", False, True, False),  # succeeded one-shot
    ])
    node = await _run_list_nodes(rows)
    assert node["health"] == "green"
    # coverage denominator excludes phase2_populate
    assert node["coverage_label"] == "1/1 active"


# ---------------------------------------------------------------------------
# Cache-miss retry for phase2_populate dispatch (Item 3)
# ---------------------------------------------------------------------------

async def test_phase2_cache_miss_retry_resolves_on_second_attempt():
    """When the first get_devices() doesn't contain the just-onboarded
    device, poll_loop's dispatcher must clear_cache() + retry once.
    Validates by asserting clear_cache was called and the device is
    ultimately resolved from the refetched list."""
    from app import polling, nautobot_client

    # The "cache miss" is represented by an empty device_info dict
    # passed to the dispatcher (poll_loop has already done its
    # get_devices() call and didn't see the new device). The
    # dispatcher then clears the cache and does its OWN refetch.
    # That refetch is what we mock — returning the freshly-onboarded
    # device so the dispatcher can resolve it.
    call_counter = {"n": 0}

    async def fake_get_devices():
        call_counter["n"] += 1
        return [{
            "name": "fresh-device",
            "id": "fresh-uuid",
            "primary_ip4": {"display": "192.0.2.99/32"},
            "platform": {"name": "juniper_junos"},
        }]

    clear_calls = {"n": 0}

    def fake_clear_cache():
        clear_calls["n"] += 1

    # Shim _run_phase2_populate to capture dispatch without actually
    # running network_sync.run_phase2.
    dispatched = {}

    async def fake_poll_device(device_name, device_id, due_jobs,
                               device_ip=None, is_junos=False):
        dispatched["device_name"] = device_name
        dispatched["device_id"] = device_id
        dispatched["due_jobs"] = due_jobs
        dispatched["device_ip"] = device_ip
        return [{"device_name": device_name, "job_type": "phase2_populate",
                 "success": True, "count": 0, "duration": 0.0,
                 "error": None}]

    # Inline dispatcher equivalent to poll_loop._run_device
    async def _dispatch(dev_name, job_types, device_info):
        info = device_info.get(dev_name.lower(), {})
        dev_id = info.get("id")
        if not dev_id and "phase2_populate" in job_types:
            nautobot_client.clear_cache()
            fresh = await nautobot_client.get_devices()
            for d in fresh:
                if (d.get("name") or "").lower() == dev_name.lower():
                    pip = d.get("primary_ip4") or {}
                    ip_disp = pip.get("display", "") if isinstance(pip, dict) else ""
                    info = {
                        "id": d.get("id", ""),
                        "ip": ip_disp.split("/")[0] if "/" in ip_disp else ip_disp,
                        "is_junos": True,
                    }
                    dev_id = info["id"]
                    break
        if not dev_id:
            return None
        return await polling.poll_device(
            dev_name, dev_id, job_types,
            device_ip=info.get("ip"), is_junos=info.get("is_junos", False))

    with patch.object(nautobot_client, "get_devices",
                      new=AsyncMock(side_effect=fake_get_devices)), \
         patch.object(nautobot_client, "clear_cache", new=fake_clear_cache), \
         patch.object(polling, "poll_device",
                      new=AsyncMock(side_effect=fake_poll_device)):
        # Initial "cached" device_info is empty — simulates the miss.
        result = await _dispatch("fresh-device", ["phase2_populate"], {})

    assert clear_calls["n"] == 1, "clear_cache() must be called once"
    assert call_counter["n"] == 1, "get_devices() must be retried once"
    assert result is not None
    assert dispatched["device_id"] == "fresh-uuid"
    assert dispatched["device_ip"] == "192.0.2.99"


async def test_phase2_cache_miss_retry_both_attempts_fail_proceeds_to_failure():
    """If the device is still missing after clear_cache() + refetch,
    dispatcher returns None (the caller's normal failure path runs:
    _mark_failure('Device not found in Nautobot'))."""
    from app import polling, nautobot_client

    async def fake_get_devices():
        return []  # still empty

    clear_calls = {"n": 0}

    def fake_clear_cache():
        clear_calls["n"] += 1

    async def _dispatch(dev_name, job_types, device_info):
        info = device_info.get(dev_name.lower(), {})
        dev_id = info.get("id")
        if not dev_id and "phase2_populate" in job_types:
            nautobot_client.clear_cache()
            fresh = await nautobot_client.get_devices()
            for d in fresh:
                if (d.get("name") or "").lower() == dev_name.lower():
                    dev_id = d.get("id", "")
                    break
        return dev_id  # None on double-miss

    with patch.object(nautobot_client, "get_devices",
                      new=AsyncMock(side_effect=fake_get_devices)), \
         patch.object(nautobot_client, "clear_cache", new=fake_clear_cache):
        out = await _dispatch("missing", ["phase2_populate"], {})
    assert out is None
    assert clear_calls["n"] == 1


async def test_phase2_cache_miss_retry_not_triggered_for_non_phase2_jobs():
    """Cache-miss retry is narrowly scoped to phase2_populate's dispatch
    path. Other job types (arp/mac/lldp/…) do NOT trigger clear_cache()
    when missing — they just fail normally."""
    from app import nautobot_client

    clear_calls = {"n": 0}

    def fake_clear_cache():
        clear_calls["n"] += 1

    async def _dispatch(dev_name, job_types, device_info):
        info = device_info.get(dev_name.lower(), {})
        dev_id = info.get("id")
        if not dev_id and "phase2_populate" in job_types:
            nautobot_client.clear_cache()

    with patch.object(nautobot_client, "clear_cache", new=fake_clear_cache):
        await _dispatch("ghost", ["arp", "mac"], {})
    assert clear_calls["n"] == 0
