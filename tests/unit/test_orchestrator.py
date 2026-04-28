"""Unit tests for controller/app/onboarding/orchestrator.py (Prompt 4).

Mocks at the nautobot_client primitive + classifier + probe boundaries.
No real Nautobot or SNMP traffic. Each test covers one step of the 6-step
Phase 1 sequence and its documented failure-mode rollback per
reality-check §4.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload real modules so sibling sys.modules.setdefault stubs are no-ops.
import app.nautobot_client as _nc_preload  # noqa: E402, F401
import app.onboarding.orchestrator as _orch_preload  # noqa: E402, F401
import app.onboarding.classifier as _cls_preload  # noqa: E402, F401
import app.onboarding.probes.junos as _junos_preload  # noqa: E402, F401

from app.onboarding.classifier import ClassifierResult  # noqa: E402
from app.onboarding.orchestrator import (  # noqa: E402
    CLASSIFICATION_TO_ROLE_NAME,
    MGMT_INTERFACE_NAME,
    onboard_device,
)
from app.onboarding.probes.junos import DeviceFacts  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    """Container for the per-test mock set, so tests can assert on each
    primitive call without repeating patch boilerplate."""
    classify: AsyncMock
    probe: AsyncMock
    arista_probe: AsyncMock
    paloalto_probe: AsyncMock
    fortinet_probe: AsyncMock
    cisco_probe: AsyncMock
    find_device_at_location: AsyncMock
    get_role_by_name: AsyncMock
    get_devicetype_by_model: AsyncMock
    get_platform_by_name: AsyncMock
    get_status_by_name: AsyncMock
    create_device: AsyncMock
    ensure_management_interface: AsyncMock
    ensure_prefix: AsyncMock
    create_ip_address: AsyncMock
    link_ip_to_interface: AsyncMock
    set_device_primary_ip4: AsyncMock
    set_device_status: AsyncMock
    ensure_device_polls: AsyncMock
    delete_device: AsyncMock
    delete_ip_address: AsyncMock
    delete_standalone_ip: AsyncMock
    clear_cache: MagicMock


def _make_classifier_result(vendor="juniper", platform="juniper_junos",
                            classification="switch"):
    return ClassifierResult(
        classification=classification,
        vendor=vendor,
        platform=platform,
        confidence="high",
        signals_matched=["sysdescr:juniper_networks"],
    )


def _make_facts(hostname="ex2300", chassis_model="EX2300-24P"):
    return DeviceFacts(
        hostname=hostname,
        serial="JY3622160886",
        chassis_model=chassis_model,
        os_version="23.4R2-S4.11",
        management_prefix_length=None,
    )


@pytest.fixture
def orch_mocks():
    """Patch every primitive the orchestrator touches with an AsyncMock.

    Default returns wire up a happy path that completes all 8 recorded
    steps. Individual tests override fields to simulate failures.
    """
    from app import nautobot_client, polling
    import app.onboarding.orchestrator as orch

    cls_result = _make_classifier_result()
    facts = _make_facts()

    patches = {
        "classify": AsyncMock(return_value=cls_result),
        "probe": AsyncMock(return_value=facts),
        "arista_probe": AsyncMock(return_value=facts),
        "paloalto_probe": AsyncMock(return_value=facts),
        "fortinet_probe": AsyncMock(return_value=facts),
        "cisco_probe": AsyncMock(return_value=facts),
        "find_device_at_location": AsyncMock(return_value=None),
        "get_role_by_name": AsyncMock(return_value={"id": "role-uuid",
                                                     "name": "Switch"}),
        "get_devicetype_by_model": AsyncMock(return_value={
            "id": "dt-uuid", "model": "EX2300-24P",
        }),
        "get_platform_by_name": AsyncMock(return_value={
            "id": "plat-uuid", "name": "juniper_junos",
        }),
        "get_status_by_name": AsyncMock(side_effect=lambda name, **kw: {
            "Active": {"id": "status-active", "name": "Active"},
            "Onboarding Incomplete": {
                "id": "status-incomplete", "name": "Onboarding Incomplete",
            },
        }.get(name)),
        "create_device": AsyncMock(return_value={
            "id": "dev-uuid", "name": facts.hostname,
        }),
        "ensure_management_interface": AsyncMock(return_value={
            "id": "iface-uuid", "name": "me0",
        }),
        "ensure_prefix": AsyncMock(return_value={"id": "prefix-uuid"}),
        "create_ip_address": AsyncMock(return_value={"id": "ip-uuid"}),
        "link_ip_to_interface": AsyncMock(return_value={"id": "link-uuid"}),
        "set_device_primary_ip4": AsyncMock(return_value={"id": "dev-uuid"}),
        "set_device_status": AsyncMock(return_value={"id": "dev-uuid"}),
        "ensure_device_polls": AsyncMock(return_value=None),
        "delete_device": AsyncMock(return_value=None),
        "delete_ip_address": AsyncMock(return_value=None),
        "delete_standalone_ip": AsyncMock(return_value=True),
        "clear_cache": MagicMock(return_value=None),
    }

    # ExitStack avoids Python's "too many statically nested blocks"
    # limit once the patch list grows past ~20 (triggered at Prompt 7
    # when paloalto_probe was added).
    from contextlib import ExitStack
    targets: list[tuple[object, str, object]] = [
        (orch, "classify", patches["classify"]),
        (orch._junos_probe, "probe_device_facts", patches["probe"]),
        (orch._arista_probe, "probe_device_facts", patches["arista_probe"]),
        (orch._paloalto_probe, "probe_device_facts", patches["paloalto_probe"]),
        (orch._fortinet_probe, "probe_device_facts", patches["fortinet_probe"]),
        (orch._cisco_probe, "probe_device_facts", patches["cisco_probe"]),
        (nautobot_client, "find_device_at_location", patches["find_device_at_location"]),
        (nautobot_client, "get_role_by_name", patches["get_role_by_name"]),
        (nautobot_client, "get_devicetype_by_model", patches["get_devicetype_by_model"]),
        (nautobot_client, "get_platform_by_name", patches["get_platform_by_name"]),
        (nautobot_client, "get_status_by_name", patches["get_status_by_name"]),
        (nautobot_client, "create_device", patches["create_device"]),
        (nautobot_client, "ensure_management_interface",
         patches["ensure_management_interface"]),
        (nautobot_client, "ensure_prefix", patches["ensure_prefix"]),
        (nautobot_client, "create_ip_address", patches["create_ip_address"]),
        (nautobot_client, "link_ip_to_interface", patches["link_ip_to_interface"]),
        (nautobot_client, "set_device_primary_ip4", patches["set_device_primary_ip4"]),
        (nautobot_client, "set_device_status", patches["set_device_status"]),
        (polling, "ensure_device_polls", patches["ensure_device_polls"]),
        (nautobot_client, "delete_device", patches["delete_device"]),
        (nautobot_client, "delete_ip_address", patches["delete_ip_address"]),
        (nautobot_client, "delete_standalone_ip", patches["delete_standalone_ip"]),
        (nautobot_client, "clear_cache", patches["clear_cache"]),
    ]
    with ExitStack() as stack:
        for obj, attr, mock in targets:
            stack.enter_context(patch.object(obj, attr, mock))
        yield _Ctx(**patches)


async def _call(classification="switch"):
    """Run onboard_device with the test defaults. Returns the result."""
    return await onboard_device(
        ip="192.0.2.1",
        snmp_community="public",
        secrets_group_id="sg-uuid",
        location_id="loc-uuid",
    )


# ---------------------------------------------------------------------------
# Happy paths (2)
# ---------------------------------------------------------------------------

async def test_happy_path_ex2300_switch(orch_mocks):
    result = await _call()
    assert result.success is True
    assert result.device_id == "dev-uuid"
    assert result.device_name == "ex2300"
    assert result.error is None
    assert result.rollback_performed is False
    for step in ("classify", "probe", "strict_new_check", "create_device",
                 "ensure_mgmt_iface", "ensure_prefix", "create_ip",
                 "link_ip_iface", "set_primary_ip4", "ensure_device_polls",
                 "clear_cache"):
        assert step in result.phase1_steps_completed
    assert orch_mocks.create_device.await_count == 1
    assert orch_mocks.set_device_primary_ip4.await_count == 1
    assert orch_mocks.clear_cache.call_count == 1


async def test_happy_path_srx320_firewall(orch_mocks):
    orch_mocks.classify.return_value = _make_classifier_result(
        classification="firewall",
    )
    orch_mocks.probe.return_value = _make_facts(
        hostname="srx320", chassis_model="SRX320",
    )
    orch_mocks.get_role_by_name.return_value = {"id": "role-fw",
                                                  "name": "Firewall"}
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-srx", "model": "SRX320",
    }
    result = await _call()
    assert result.success is True
    assert result.device_name == "srx320"
    # Role mapped via CLASSIFICATION_TO_ROLE_NAME["firewall"] == "Firewall"
    orch_mocks.get_role_by_name.assert_awaited_with("Firewall")


# ---------------------------------------------------------------------------
# Template-interface reuse (1)
# ---------------------------------------------------------------------------

async def test_template_mgmt_interface_reused_not_created(orch_mocks):
    """ensure_management_interface is the helper that does query-and-reuse;
    the orchestrator simply calls it. Verify it was called exactly once with
    the Junos-platform-specific mgmt name."""
    result = await _call()
    assert result.success
    orch_mocks.ensure_management_interface.assert_awaited_once_with(
        "dev-uuid", MGMT_INTERFACE_NAME["juniper_junos"], "status-active",
    )


# ---------------------------------------------------------------------------
# Strict-new refusal (1)
# ---------------------------------------------------------------------------

async def test_strict_new_refusal_no_writes(orch_mocks):
    orch_mocks.find_device_at_location.return_value = {
        "id": "existing-uuid", "name": "ex2300",
    }
    result = await _call()
    assert result.success is False
    assert "AlreadyOnboardedError" in result.error
    assert result.device_id == "existing-uuid"
    assert orch_mocks.create_device.await_count == 0
    assert orch_mocks.ensure_management_interface.await_count == 0


async def test_strict_new_raises_returns_clean_failure(orch_mocks):
    """Bad input (e.g. bogus location UUID triggering a 400 on the pre-check
    query) must not crash — orchestrator returns a clean OnboardingResult
    pointing at the likely culprit."""
    orch_mocks.find_device_at_location.side_effect = RuntimeError(
        "400 Bad Request from Nautobot"
    )
    result = await _call()
    assert result.success is False
    assert "strict-new pre-check" in result.error
    assert "location_id" in result.error
    assert orch_mocks.create_device.await_count == 0


# ---------------------------------------------------------------------------
# Classification failures (3)
# ---------------------------------------------------------------------------

async def test_vendor_none_classification_failed_error(orch_mocks):
    orch_mocks.classify.return_value = ClassifierResult(
        classification="unknown", vendor=None, platform=None,
        confidence="low", signals_matched=[],
    )
    result = await _call()
    assert result.success is False
    assert "ClassificationFailedError" in result.error
    assert orch_mocks.probe.await_count == 0


async def test_huawei_vendor_raises_unsupported(orch_mocks):
    # Block C.5 added `cisco` to SUPPORTED_VENDORS. Huawei is the honest
    # remaining unsupported vendor (no Huawei in the lab; no probe
    # module). Any vendor outside SUPPORTED_VENDORS still routes through
    # the orchestrator's UnsupportedVendorError branch.
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="huawei", platform="huawei_vrp",
    )
    result = await _call()
    assert result.success is False
    assert "UnsupportedVendorError" in result.error
    assert orch_mocks.probe.await_count == 0


async def test_classifier_raises_wraps_as_classification_failed(orch_mocks):
    orch_mocks.classify.side_effect = RuntimeError("snmp blew up")
    result = await _call()
    assert result.success is False
    assert "ClassificationFailedError" in result.error
    assert "snmp blew up" in result.error


# ---------------------------------------------------------------------------
# Per-step failure + rollback (6)
# ---------------------------------------------------------------------------

async def test_step_A_failure_no_rollback_needed(orch_mocks):
    orch_mocks.create_device.side_effect = RuntimeError("nautobot 400")
    result = await _call()
    assert result.success is False
    assert "Step A" in result.error
    assert orch_mocks.delete_device.await_count == 0
    assert orch_mocks.delete_ip_address.await_count == 0


async def test_step_B_failure_deletes_device_only(orch_mocks):
    orch_mocks.ensure_management_interface.side_effect = RuntimeError("nope")
    result = await _call()
    assert result.success is False
    assert "Step B" in result.error
    assert result.rollback_performed is True
    orch_mocks.delete_device.assert_awaited_once_with("dev-uuid")
    assert orch_mocks.delete_ip_address.await_count == 0
    assert orch_mocks.delete_standalone_ip.await_count == 0


async def test_step_C_failure_deletes_device_only(orch_mocks):
    orch_mocks.ensure_prefix.side_effect = RuntimeError("prefix err")
    result = await _call()
    assert result.success is False
    assert "Step C" in result.error
    orch_mocks.delete_device.assert_awaited_once_with("dev-uuid")
    assert orch_mocks.delete_ip_address.await_count == 0


async def test_step_D_failure_deletes_device_only(orch_mocks):
    orch_mocks.create_ip_address.side_effect = RuntimeError("ip err")
    result = await _call()
    assert result.success is False
    assert "Step D" in result.error
    orch_mocks.delete_device.assert_awaited_once_with("dev-uuid")
    assert orch_mocks.delete_ip_address.await_count == 0


async def test_step_E_failure_deletes_device_AND_explicit_ip(orch_mocks):
    """Reality-check §4.7 asymmetry: device delete cascades interfaces but
    not IPs. Step E failure means Step D already created the IP; orchestrator
    must delete the IP explicitly as a second step."""
    orch_mocks.link_ip_to_interface.side_effect = RuntimeError("link err")
    result = await _call()
    assert result.success is False
    assert "Step E" in result.error
    orch_mocks.delete_device.assert_awaited_once_with("dev-uuid")
    orch_mocks.delete_ip_address.assert_awaited_once_with("ip-uuid")


async def test_step_F_failure_deletes_device_AND_explicit_ip(orch_mocks):
    orch_mocks.set_device_primary_ip4.side_effect = RuntimeError("primary err")
    result = await _call()
    assert result.success is False
    assert "Step F" in result.error
    orch_mocks.delete_device.assert_awaited_once_with("dev-uuid")
    orch_mocks.delete_ip_address.assert_awaited_once_with("ip-uuid")


# ---------------------------------------------------------------------------
# Step G special-case: no rollback, status → Onboarding Incomplete (1)
# ---------------------------------------------------------------------------

async def test_step_G_failure_marks_onboarding_incomplete_no_rollback(orch_mocks):
    orch_mocks.ensure_device_polls.side_effect = RuntimeError("db down")
    result = await _call()
    assert result.success is False
    assert "Step G" in result.error
    # Device was NOT deleted — it's valid; only polling seed failed.
    assert orch_mocks.delete_device.await_count == 0
    assert orch_mocks.delete_ip_address.await_count == 0
    # Status was switched to Onboarding Incomplete.
    orch_mocks.set_device_status.assert_awaited_once_with(
        "dev-uuid", "status-incomplete",
    )
    assert result.rollback_performed is False


# ---------------------------------------------------------------------------
# Step H: cache failure non-fatal (1)
# ---------------------------------------------------------------------------

async def test_step_H_cache_failure_still_returns_success(orch_mocks):
    orch_mocks.clear_cache.side_effect = RuntimeError("cache err")
    result = await _call()
    assert result.success is True
    assert result.device_id == "dev-uuid"


# ---------------------------------------------------------------------------
# Reference-lookup failures (2)
# ---------------------------------------------------------------------------

async def test_missing_role_clear_error_at_step_A(orch_mocks):
    orch_mocks.get_role_by_name.return_value = None
    result = await _call()
    assert result.success is False
    assert "Role 'Switch' not found" in result.error
    assert orch_mocks.create_device.await_count == 0


async def test_missing_devicetype_clear_error_at_step_A(orch_mocks):
    orch_mocks.get_devicetype_by_model.return_value = None
    result = await _call()
    assert result.success is False
    assert "DeviceType" in result.error
    assert orch_mocks.create_device.await_count == 0


# ---------------------------------------------------------------------------
# Probe failure at Step 0 (1)
# ---------------------------------------------------------------------------

async def test_probe_raises_produces_probe_failed_error(orch_mocks):
    orch_mocks.probe.side_effect = RuntimeError("probe oops")
    result = await _call()
    assert result.success is False
    assert "ProbeFailedError" in result.error
    assert orch_mocks.find_device_at_location.await_count == 0


# ---------------------------------------------------------------------------
# Rollback-itself-fails defense (1)
# ---------------------------------------------------------------------------

async def test_rollback_delete_device_failure_still_returns_cleanly(orch_mocks):
    orch_mocks.set_device_primary_ip4.side_effect = RuntimeError("F err")
    orch_mocks.delete_device.side_effect = RuntimeError("cannot delete")
    result = await _call()
    assert result.success is False
    assert "Step F" in result.error
    # rollback_performed=False because rollback itself broke
    assert result.rollback_performed is False
    # We still tried the IP delete even after device delete failed
    orch_mocks.delete_ip_address.assert_awaited_once_with("ip-uuid")


# ---------------------------------------------------------------------------
# Credential hygiene (1)
# ---------------------------------------------------------------------------

async def test_no_snmp_community_in_logs(orch_mocks, caplog):
    """Happy path run must not leak the snmp_community into any log record."""
    caplog.set_level(logging.DEBUG, logger="app.onboarding.orchestrator")
    sentinel = "TEST-COMMUNITY-DO-NOT-LEAK-abc123"
    await onboard_device(
        ip="192.0.2.1",
        snmp_community=sentinel,
        secrets_group_id="sg-uuid",
        location_id="loc-uuid",
    )
    joined = "\n".join(r.getMessage() for r in caplog.records)
    joined += "\n" + "\n".join(str(getattr(r, "mnm_context", ""))
                                for r in caplog.records)
    assert sentinel not in joined


# ---------------------------------------------------------------------------
# CLASSIFICATION_TO_ROLE_NAME invariants
# ---------------------------------------------------------------------------

def test_classification_to_role_unknown_refuses_onboarding():
    assert CLASSIFICATION_TO_ROLE_NAME["unknown"] is None


def test_classification_network_device_maps_to_router():
    assert CLASSIFICATION_TO_ROLE_NAME["network_device"] == "Router"


async def test_unknown_classification_refuses_at_step_A(orch_mocks):
    orch_mocks.classify.return_value = _make_classifier_result(
        classification="unknown",
    )
    # vendor still juniper so probe runs; classification=unknown kills it
    # before create_device.
    result = await _call()
    assert result.success is False
    assert "classification='unknown'" in result.error
    assert orch_mocks.create_device.await_count == 0


# ---------------------------------------------------------------------------
# Prompt 5: Arista dispatch
# ---------------------------------------------------------------------------

async def test_arista_vendor_dispatches_to_arista_probe(orch_mocks):
    """Arista is now in SUPPORTED_VENDORS; _probe_vendor must route to the
    Arista probe module, not the Junos one."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="arista", platform="arista_eos",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-arista", "name": "arista_eos",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-veos", "model": "vEOS",
    }
    result = await _call()
    assert result.success is True
    # Arista probe called once; Junos probe not called.
    assert orch_mocks.arista_probe.await_count == 1
    assert orch_mocks.probe.await_count == 0


async def test_arista_happy_path_uses_management1_interface(orch_mocks):
    """Arista's MGMT_INTERFACE_NAME entry must resolve to Management1."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="arista", platform="arista_eos",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-arista", "name": "arista_eos",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-veos", "model": "vEOS",
    }
    result = await _call()
    assert result.success is True
    # ensure_management_interface called with the Arista mgmt name
    orch_mocks.ensure_management_interface.assert_awaited_once_with(
        "dev-uuid", "Management1", "status-active",
    )


# ---------------------------------------------------------------------------
# Prompt 7: PAN-OS dispatch
# ---------------------------------------------------------------------------

async def test_palo_alto_vendor_dispatches_to_paloalto_probe(orch_mocks):
    """PAN-OS is now in SUPPORTED_VENDORS; _probe_vendor must route to the
    PAN-OS probe module, not Junos or Arista."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="palo_alto", platform="paloalto_panos", classification="firewall",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-panos", "name": "paloalto_panos",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-pa440", "model": "PA-440",
    }
    orch_mocks.get_role_by_name.return_value = {"id": "role-fw", "name": "Firewall"}
    result = await _call()
    assert result.success is True
    assert orch_mocks.paloalto_probe.await_count == 1
    assert orch_mocks.probe.await_count == 0        # Junos not called
    assert orch_mocks.arista_probe.await_count == 0  # Arista not called


async def test_palo_alto_happy_path_uses_mgmt_interface(orch_mocks):
    """PAN-OS MGMT_INTERFACE_NAME entry must resolve to 'mgmt'."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="palo_alto", platform="paloalto_panos", classification="firewall",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-panos", "name": "paloalto_panos",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-pa440", "model": "PA-440",
    }
    orch_mocks.get_role_by_name.return_value = {"id": "role-fw", "name": "Firewall"}
    result = await _call()
    assert result.success is True
    orch_mocks.ensure_management_interface.assert_awaited_once_with(
        "dev-uuid", "mgmt", "status-active",
    )


# ---------------------------------------------------------------------------
# Prompt 7.5: FortiGate dispatch
# ---------------------------------------------------------------------------

async def test_fortinet_vendor_dispatches_to_fortinet_probe(orch_mocks):
    """FortiGate is now in SUPPORTED_VENDORS; _probe_vendor must route to
    the FortiGate probe module, not any other vendor probe."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="fortinet", platform="fortinet_fortios", classification="firewall",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-fortios", "name": "fortinet_fortios",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-fgt40f", "model": "FortiGate-40F",
    }
    orch_mocks.get_role_by_name.return_value = {"id": "role-fw", "name": "Firewall"}
    result = await _call()
    assert result.success is True
    assert orch_mocks.fortinet_probe.await_count == 1
    # No other vendor probe called.
    assert orch_mocks.probe.await_count == 0         # Junos
    assert orch_mocks.arista_probe.await_count == 0
    assert orch_mocks.paloalto_probe.await_count == 0


async def test_fortinet_happy_path_uses_mgmt_interface(orch_mocks):
    """FortiGate MGMT_INTERFACE_NAME entry must resolve to 'mgmt'."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="fortinet", platform="fortinet_fortios", classification="firewall",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-fortios", "name": "fortinet_fortios",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-fgt40f", "model": "FortiGate-40F",
    }
    orch_mocks.get_role_by_name.return_value = {"id": "role-fw", "name": "Firewall"}
    result = await _call()
    assert result.success is True
    orch_mocks.ensure_management_interface.assert_awaited_once_with(
        "dev-uuid", "mgmt", "status-active",
    )


async def test_cisco_vendor_dispatches_to_cisco_probe(orch_mocks):
    """Block C.5: cisco is in SUPPORTED_VENDORS; _probe_vendor must
    route to the Cisco probe module, not any other vendor probe."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="cisco", platform="cisco_iosxe", classification="router",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-cisco-iosxe", "name": "cisco_iosxe",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-c8000v", "model": "C8000V",
    }
    orch_mocks.get_role_by_name.return_value = {"id": "role-router", "name": "Router"}
    result = await _call()
    assert result.success is True
    assert orch_mocks.cisco_probe.await_count == 1
    # No other vendor probe called.
    assert orch_mocks.probe.await_count == 0          # Junos
    assert orch_mocks.arista_probe.await_count == 0
    assert orch_mocks.paloalto_probe.await_count == 0
    assert orch_mocks.fortinet_probe.await_count == 0


async def test_cisco_iosxe_happy_path_uses_gi1_interface(orch_mocks):
    """cisco_iosxe MGMT_INTERFACE_NAME entry must resolve to ``Gi1``
    (the short form Cisco SNMP returns via ifName, NOT the long
    ``GigabitEthernet1`` form shown in the CLI). Pinned so a rename
    in the orchestrator map fails the test loudly — using the long
    form would cause Phase 2's SNMP walk to create a second interface
    record for the same physical port (caught live on c8000v 17.16.1a)."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="cisco", platform="cisco_iosxe", classification="router",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-cisco-iosxe", "name": "cisco_iosxe",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-c8000v", "model": "C8000V",
    }
    orch_mocks.get_role_by_name.return_value = {"id": "role-router", "name": "Router"}
    result = await _call()
    assert result.success is True
    orch_mocks.ensure_management_interface.assert_awaited_once_with(
        "dev-uuid", "Gi1", "status-active",
    )


async def test_cisco_ios_classic_happy_path_uses_gi00_interface(orch_mocks):
    """Classic IOS MGMT_INTERFACE_NAME entry must resolve to ``Gi0/0``
    (short form, matching the SNMP-agent ifName convention shared
    with IOS-XE). Different from IOS-XE's ``Gi1``. Pinned so the
    two-stage classifier discrimination's downstream effect is
    regression-tested even without a classic-IOS lab device."""
    orch_mocks.classify.return_value = _make_classifier_result(
        vendor="cisco", platform="cisco_ios", classification="router",
    )
    orch_mocks.get_platform_by_name.return_value = {
        "id": "plat-cisco-ios", "name": "cisco_ios",
    }
    orch_mocks.get_devicetype_by_model.return_value = {
        "id": "dt-2960", "model": "WS-C2960-24TT-L",
    }
    orch_mocks.get_role_by_name.return_value = {"id": "role-router", "name": "Router"}
    result = await _call()
    assert result.success is True
    orch_mocks.ensure_management_interface.assert_awaited_once_with(
        "dev-uuid", "Gi0/0", "status-active",
    )
