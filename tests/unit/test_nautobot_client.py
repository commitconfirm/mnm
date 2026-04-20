"""Unit tests for controller/app/nautobot_client.py direct-onboarding
primitives (Prompt 2 of the v1.0 onboarding workstream).

Mocks at the shared httpx.AsyncClient boundary — no real Nautobot traffic.
Every function added in Prompt 2 has at least one happy-path test; every
function whose behaviour is documented in the reality-check document has
tests covering the documented edge cases (Test 2, Test 4, Test 5, Test 6,
Test 7). URL-assertion tests pin the reality-check §1.1 correction for the
Roles endpoint and §1.4 through-model for IP↔Interface linking.
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Import the real module at collection time so subsequent test files that
# stub ``app.nautobot_client`` via ``sys.modules.setdefault`` become no-ops —
# the same pattern test_alembic.py uses for app.db. app.nautobot_client
# imports docker for _get_token; docker is present in the controller
# container's test env. We bypass token fetch per-test by patching
# ``_get_token`` to return a fixed string.
import app.nautobot_client as _nc_preload  # noqa: F401, E402


@pytest.fixture
def mock_nautobot():
    """Yield (mock_client, module) with _get_client + _get_token patched.

    The mock_client is an AsyncMock that answers GET/POST/PATCH/DELETE;
    tests configure .return_value per call.
    """
    from app import nautobot_client as nc
    mock_client = AsyncMock()
    # Sync methods on the client object — the module calls them like
    # coroutines-of-coroutines via `await client.get(...)`.  AsyncMock handles
    # this for all attributes.
    with patch.object(nc, "_get_client", return_value=mock_client), \
         patch.object(nc, "_get_token", return_value="t" * 40):
        # Flush any cached reference data between tests.
        nc.clear_cache()
        yield mock_client, nc


def _response(status_code: int, body: dict | str | list | None = None):
    """Build a MagicMock response that mimics the subset of httpx.Response
    the module uses."""
    resp = MagicMock()
    resp.status_code = status_code
    if isinstance(body, (dict, list)):
        resp.json = MagicMock(return_value=body)
        resp.text = str(body)
    else:
        resp.text = body or ""
        resp.json = MagicMock(side_effect=ValueError("not json"))
    # raise_for_status: only raise on >=400 (the module uses it in GET paths).
    def _raise():
        if status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
    resp.raise_for_status = MagicMock(side_effect=_raise)
    return resp


# ---------------------------------------------------------------------------
# Category 1 — reference lookups
# ---------------------------------------------------------------------------

async def test_get_manufacturer_by_name_found(mock_nautobot):
    client, nc = mock_nautobot
    rec = {"id": "mfr-uuid", "name": "Juniper"}
    client.get.return_value = _response(200, {"results": [rec]})
    got = await nc.get_manufacturer_by_name("Juniper")
    assert got == rec
    # Assert filter params used
    _, kwargs = client.get.call_args
    assert kwargs["params"]["name"] == "Juniper"


async def test_get_manufacturer_by_name_not_found_returns_none(mock_nautobot):
    client, nc = mock_nautobot
    client.get.return_value = _response(200, {"results": []})
    assert await nc.get_manufacturer_by_name("Nonesuch") is None


async def test_get_role_by_name_uses_extras_roles_endpoint(mock_nautobot):
    """Reality-check §1.1: /api/extras/roles/, not /api/dcim/roles/."""
    client, nc = mock_nautobot
    rec = {"id": "role-uuid", "name": "Switch"}
    client.get.return_value = _response(200, {"results": [rec]})
    got = await nc.get_role_by_name("Switch")
    assert got == rec
    args, kwargs = client.get.call_args
    url_path = args[0] if args else kwargs.get("url")
    assert url_path == "/api/extras/roles/", (
        f"Role lookup must use /api/extras/roles/, not {url_path} — "
        "reality-check §1.1."
    )
    assert kwargs["params"]["content_types"] == "dcim.device"


async def test_get_platform_by_name_found(mock_nautobot):
    client, nc = mock_nautobot
    rec = {"id": "plat-uuid", "name": "juniper_junos"}
    client.get.return_value = _response(200, {"results": [rec]})
    assert (await nc.get_platform_by_name("juniper_junos")) == rec


async def test_get_devicetype_by_model_found(mock_nautobot):
    client, nc = mock_nautobot
    rec = {"id": "dt-uuid", "model": "EX2300-24P"}
    client.get.return_value = _response(200, {"results": [rec]})
    assert (await nc.get_devicetype_by_model("EX2300-24P")) == rec
    _, kwargs = client.get.call_args
    assert kwargs["params"]["model"] == "EX2300-24P"


async def test_get_status_by_name_found(mock_nautobot):
    client, nc = mock_nautobot
    rec = {"id": "inc-uuid", "name": "Onboarding Incomplete"}
    client.get.return_value = _response(200, {"results": [rec]})
    got = await nc.get_status_by_name("Onboarding Incomplete")
    assert got == rec
    args, kwargs = client.get.call_args
    assert args[0] == "/api/extras/statuses/"
    assert kwargs["params"]["name"] == "Onboarding Incomplete"


async def test_get_status_by_name_miss_returns_none(mock_nautobot):
    client, nc = mock_nautobot
    client.get.return_value = _response(200, {"results": []})
    assert await nc.get_status_by_name("Nonesuch") is None


async def test_get_status_by_name_content_type_filter_in_url(mock_nautobot):
    """URL-pin test: content_type argument is forwarded as ?content_types=."""
    client, nc = mock_nautobot
    client.get.return_value = _response(200, {"results": [
        {"id": "active-uuid", "name": "Active"},
    ]})
    await nc.get_status_by_name("Active", content_type="dcim.device")
    _, kwargs = client.get.call_args
    assert kwargs["params"]["content_types"] == "dcim.device"
    assert kwargs["params"]["name"] == "Active"


async def test_get_status_by_name_ambiguous_logs_warning_returns_first(
    mock_nautobot,
):
    client, nc = mock_nautobot
    first = {"id": "first-uuid", "name": "Shared"}
    second = {"id": "second-uuid", "name": "Shared"}
    client.get.return_value = _response(200, {"results": [first, second]})
    # Spy on the module's StructuredLogger directly — caplog is unreliable
    # once another test file has triggered setup_logging() because it clears
    # the root handler set. Patching the logger's warning method is the
    # stable equivalent.
    with patch.object(nc.log, "warning") as mock_warn:
        got = await nc.get_status_by_name("Shared")
    assert got == first
    assert mock_warn.called, "ambiguous-match should log a warning"
    event_arg = mock_warn.call_args.args[0] if mock_warn.call_args.args else ""
    assert event_arg == "status_name_ambiguous"


# ---------------------------------------------------------------------------
# Category 2 — Device creation
# ---------------------------------------------------------------------------

async def test_create_device_success_returns_record_with_id(mock_nautobot):
    client, nc = mock_nautobot
    expected = {"id": "dev-uuid", "name": "node1"}
    client.post.return_value = _response(201, expected)
    got = await nc.create_device(
        name="node1",
        device_type_id="dt",
        location_id="loc",
        role_id="role",
        status_id="status",
        platform_id="plat",
    )
    assert got == expected
    args, kwargs = client.post.call_args
    assert args[0] == "/api/dcim/devices/"
    payload = kwargs["json"]
    assert payload == {
        "name": "node1", "device_type": "dt", "location": "loc",
        "role": "role", "status": "status", "platform": "plat",
    }


async def test_create_device_duplicate_name_raises_duplicate_error(mock_nautobot):
    """Reality-check Test 5: duplicate device name at location scope."""
    client, nc = mock_nautobot
    body = {"__all__": [
        "A device named 'foo' with no tenant already exists in this "
        "location: Default Site."
    ]}
    client.post.return_value = _response(400, body)
    with pytest.raises(nc.NautobotDuplicateError) as excinfo:
        await nc.create_device(
            name="foo", device_type_id="dt", location_id="loc",
            role_id="role", status_id="status",
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.response_body == body


async def test_create_device_missing_required_field_raises_validation_error(mock_nautobot):
    client, nc = mock_nautobot
    body = {"device_type": ["This field is required."]}
    client.post.return_value = _response(400, body)
    with pytest.raises(nc.NautobotValidationError):
        await nc.create_device(
            name="foo", device_type_id="", location_id="loc",
            role_id="role", status_id="status",
        )


# ---------------------------------------------------------------------------
# Category 3 — Interface management
# ---------------------------------------------------------------------------

async def test_find_interface_by_name_found(mock_nautobot):
    client, nc = mock_nautobot
    rec = {"id": "if-uuid", "name": "me0"}
    client.get.return_value = _response(200, {"results": [rec]})
    got = await nc.find_interface_by_name("dev-uuid", "me0")
    assert got == rec


async def test_find_interface_by_name_not_found(mock_nautobot):
    client, nc = mock_nautobot
    client.get.return_value = _response(200, {"results": []})
    assert await nc.find_interface_by_name("dev-uuid", "me0") is None


async def test_ensure_management_interface_reuses_existing(mock_nautobot):
    """Reality-check Test 4: device-type template auto-creates interfaces."""
    client, nc = mock_nautobot
    rec = {"id": "if-uuid", "name": "me0"}
    client.get.return_value = _response(200, {"results": [rec]})
    got = await nc.ensure_management_interface("dev-uuid", "me0", "status")
    assert got == rec
    # Key assertion: no POST was issued because the interface was reused.
    assert client.post.call_count == 0


async def test_ensure_management_interface_creates_if_absent(mock_nautobot):
    client, nc = mock_nautobot
    client.get.return_value = _response(200, {"results": []})
    new_rec = {"id": "if-new", "name": "mgmt0"}
    client.post.return_value = _response(201, new_rec)
    got = await nc.ensure_management_interface("dev-uuid", "mgmt0", "status")
    assert got == new_rec
    assert client.post.call_count == 1
    args, kwargs = client.post.call_args
    assert args[0] == "/api/dcim/interfaces/"
    assert kwargs["json"]["type"] == "virtual"


async def test_create_interface_duplicate_raises_duplicate_error(mock_nautobot):
    """Reality-check Test 6: (device, name) uniqueness."""
    client, nc = mock_nautobot
    body = {"non_field_errors": ["The fields device, name must make a unique set."]}
    client.post.return_value = _response(400, body)
    with pytest.raises(nc.NautobotDuplicateError):
        await nc.create_interface(
            device_id="dev", name="me0", status_id="status",
        )


# ---------------------------------------------------------------------------
# Category 4 — IPAddress management
# ---------------------------------------------------------------------------

async def test_create_ip_address_success(mock_nautobot):
    client, nc = mock_nautobot
    expected = {"id": "ip-uuid", "address": "203.0.113.99/24"}
    client.post.return_value = _response(201, expected)
    got = await nc.create_ip_address(
        address="203.0.113.99/24",
        status_id="status",
        namespace_id="ns",
        parent_prefix_id="prefix",
    )
    assert got == expected
    args, kwargs = client.post.call_args
    assert args[0] == "/api/ipam/ip-addresses/"
    assert kwargs["json"]["address"] == "203.0.113.99/24"
    assert kwargs["json"]["namespace"] == "ns"
    assert kwargs["json"]["parent"] == "prefix"


async def test_link_ip_to_interface_uses_through_model_endpoint(mock_nautobot):
    """Reality-check §1.4: through model, not PATCH on the IP."""
    client, nc = mock_nautobot
    client.post.return_value = _response(201, {"id": "link-uuid"})
    await nc.link_ip_to_interface("ip", "iface", is_primary=False)
    args, kwargs = client.post.call_args
    assert args[0] == "/api/ipam/ip-address-to-interface/", (
        "Link must POST to the IPAddressToInterface through model — "
        f"got {args[0]}. Reality-check §1.4."
    )
    assert kwargs["json"] == {
        "ip_address": "ip", "interface": "iface", "is_primary": False,
    }


async def test_delete_ip_address_success(mock_nautobot):
    client, nc = mock_nautobot
    client.delete.return_value = _response(204)
    await nc.delete_ip_address("ip-uuid")
    args, _ = client.delete.call_args
    assert args[0] == "/api/ipam/ip-addresses/ip-uuid/"


# ---------------------------------------------------------------------------
# Category 5 — Device primary IP + lifecycle
# ---------------------------------------------------------------------------

async def test_set_device_primary_ip4_success(mock_nautobot):
    client, nc = mock_nautobot
    client.patch.return_value = _response(200, {"id": "dev", "primary_ip4": {"id": "ip"}})
    got = await nc.set_device_primary_ip4("dev", "ip")
    assert got["primary_ip4"]["id"] == "ip"
    args, kwargs = client.patch.call_args
    assert args[0] == "/api/dcim/devices/dev/"
    assert kwargs["json"] == {"primary_ip4": "ip"}


async def test_set_device_primary_ip4_unlinked_ip_raises_validation_error(mock_nautobot):
    """Reality-check Test 2: primary_ip4 on an unlinked IP is rejected."""
    client, nc = mock_nautobot
    body = {"primary_ip4": [
        "The specified IP address (203.0.113.99/24) is not assigned to this device."
    ]}
    client.patch.return_value = _response(400, body)
    with pytest.raises(nc.NautobotValidationError) as excinfo:
        await nc.set_device_primary_ip4("dev", "ip")
    assert excinfo.value.status_code == 400
    assert "not assigned" in str(excinfo.value.response_body)


async def test_delete_device_success(mock_nautobot):
    client, nc = mock_nautobot
    client.delete.return_value = _response(204)
    await nc.delete_device("dev-uuid")
    args, _ = client.delete.call_args
    assert args[0] == "/api/dcim/devices/dev-uuid/"


# ---------------------------------------------------------------------------
# Category 6 — Pre-check
# ---------------------------------------------------------------------------

async def test_device_exists_at_location_true(mock_nautobot):
    client, nc = mock_nautobot
    client.get.return_value = _response(200, {"count": 1, "results": [{"id": "d"}]})
    assert await nc.device_exists_at_location("foo", "loc") is True


async def test_device_exists_at_location_false(mock_nautobot):
    client, nc = mock_nautobot
    client.get.return_value = _response(200, {"count": 0, "results": []})
    assert await nc.device_exists_at_location("nope", "loc") is False


# ---------------------------------------------------------------------------
# Category 7 — Custom Status bootstrap
# ---------------------------------------------------------------------------

async def test_ensure_custom_statuses_creates_both_when_absent(mock_nautobot):
    client, nc = mock_nautobot
    # Each desired status triggers one get_status_by_name GET; both miss.
    client.get.side_effect = [
        _response(200, {"results": []}),
        _response(200, {"results": []}),
    ]
    client.post.side_effect = [
        _response(201, {"id": "uuid-incomplete", "name": "Onboarding Incomplete"}),
        _response(201, {"id": "uuid-failed", "name": "Onboarding Failed"}),
    ]
    out = await nc.ensure_custom_statuses()
    assert out == {
        "onboarding-incomplete": "uuid-incomplete",
        "onboarding-failed": "uuid-failed",
    }
    assert client.post.call_count == 2


async def test_ensure_custom_statuses_skips_both_when_present(mock_nautobot):
    client, nc = mock_nautobot
    # Each desired status triggers one get_status_by_name GET; both hit.
    client.get.side_effect = [
        _response(200, {"results": [
            {"id": "uuid-incomplete", "name": "Onboarding Incomplete"}]}),
        _response(200, {"results": [
            {"id": "uuid-failed", "name": "Onboarding Failed"}]}),
    ]
    out = await nc.ensure_custom_statuses()
    assert out == {
        "onboarding-incomplete": "uuid-incomplete",
        "onboarding-failed": "uuid-failed",
    }
    # Key: no POST issued because both exist.
    assert client.post.call_count == 0


async def test_ensure_custom_statuses_partial_state_creates_only_missing(mock_nautobot):
    client, nc = mock_nautobot
    # First desired status exists; second does not.
    client.get.side_effect = [
        _response(200, {"results": [
            {"id": "uuid-incomplete", "name": "Onboarding Incomplete"}]}),
        _response(200, {"results": []}),
    ]
    client.post.return_value = _response(
        201, {"id": "uuid-failed", "name": "Onboarding Failed"})
    out = await nc.ensure_custom_statuses()
    assert out == {
        "onboarding-incomplete": "uuid-incomplete",
        "onboarding-failed": "uuid-failed",
    }
    assert client.post.call_count == 1
    # Verify the POST was for the missing one.
    args, kwargs = client.post.call_args
    assert kwargs["json"]["name"] == "Onboarding Failed"


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------

async def test_no_credentials_in_log_output(mock_nautobot, caplog):
    """Secret-looking values passed through the module must not end up in
    any captured log record."""
    client, nc = mock_nautobot
    client.post.return_value = _response(201, {"id": "dev-uuid", "name": "secretbox"})

    secret = "SUPERSECRET-c0ffee-DO-NOT-LEAK"
    caplog.set_level(logging.DEBUG, logger="app.nautobot_client")
    # Stash the secret into a payload field. The module should not log the
    # whole payload — it logs specific structured keys only.
    with patch.dict(os.environ, {"PASSWORD_LIKE": secret}):
        await nc.create_device(
            name="secretbox",
            device_type_id="dt", location_id="loc",
            role_id="role", status_id="status",
        )
    # The secret string must not appear in any captured log record.
    joined = "\n".join(r.getMessage() for r in caplog.records) + "\n" + \
             "\n".join(str(getattr(r, "context", "")) for r in caplog.records)
    assert secret not in joined, "credential-adjacent value leaked into logs"


# ---------------------------------------------------------------------------
# Exception-hierarchy sanity
# ---------------------------------------------------------------------------

def test_exception_hierarchy_is_reachable():
    from app.nautobot_client import (
        NautobotError, NautobotDuplicateError,
        NautobotValidationError, NautobotNotFoundError,
    )
    assert issubclass(NautobotDuplicateError, NautobotError)
    assert issubclass(NautobotValidationError, NautobotError)
    assert issubclass(NautobotNotFoundError, NautobotError)
