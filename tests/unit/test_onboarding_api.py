"""Unit tests for Prompt 8 onboarding API endpoints.

Covers:
  - ``polling.get_phase2_state`` helper (shared by
    ``/api/onboarding/phase2-status/{device_name}`` and ``/api/nodes``).
  - ``POST /api/onboarding/direct-rest`` — thin wrapper around the
    orchestrator; pass-through of success / failure / error_type.
  - ``POST /api/onboarding/retry-phase2/{device_name}`` — idempotent
    re-enable of the phase2_populate row.

Testing strategy: direct-call the route handler coroutines (bypassing
FastAPI's dep injection). Patch orchestrator / polling at the app.main
import site so the handlers see the mocks.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload to beat sibling sys.modules.setdefault stubs.
import app.main as _main_preload  # noqa: E402, F401
import app.polling as _polling_preload  # noqa: E402, F401
import app.onboarding.orchestrator as _orch_preload  # noqa: E402, F401


# ---------------------------------------------------------------------------
# polling.get_phase2_state helper
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db_session():
    """Patch polling.db.is_ready + polling.db.SessionLocal to return a
    mocked session that yields whatever ``scalar_one_or_none`` you set.

    Yields (set_row, session) — set_row(row) injects what the session
    execute returns; ``row`` may be None or any object with attributes
    enabled/last_attempt/last_success/next_due/last_error.
    """
    from unittest.mock import MagicMock, AsyncMock
    from app import polling as _polling
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    execute_result = MagicMock()
    session.execute = AsyncMock(return_value=execute_result)

    def _set_row(row):
        execute_result.scalar_one_or_none.return_value = row

    with patch.object(_polling.db, "is_ready", return_value=True), \
         patch.object(_polling.db, "SessionLocal", return_value=session):
        yield _set_row, session


async def test_get_phase2_state_none_when_db_not_ready():
    from app import polling as _polling
    with patch.object(_polling.db, "is_ready", return_value=False):
        got = await _polling.get_phase2_state("anything")
    assert got is None


async def test_get_phase2_state_none_when_no_row(mock_db_session):
    from app import polling
    set_row, _ = mock_db_session
    set_row(None)
    got = await polling.get_phase2_state("legacy-device")
    assert got is None


async def test_get_phase2_state_completed(mock_db_session):
    from datetime import datetime, timezone
    from app import polling
    set_row, _ = mock_db_session
    now = datetime.now(timezone.utc)
    set_row(type("Row", (), {
        "enabled": False, "last_attempt": now, "last_success": now,
        "next_due": now, "last_error": None,
    }))
    got = await polling.get_phase2_state("done-device")
    assert got is not None
    assert got["state"] == "completed"
    assert got["enabled"] is False


async def test_get_phase2_state_pending(mock_db_session):
    from datetime import datetime, timezone
    from app import polling
    set_row, _ = mock_db_session
    set_row(type("Row", (), {
        "enabled": True, "last_attempt": None, "last_success": None,
        "next_due": datetime.now(timezone.utc), "last_error": None,
    }))
    got = await polling.get_phase2_state("new-device")
    assert got["state"] == "pending"


async def test_get_phase2_state_failed(mock_db_session):
    from datetime import datetime, timezone, timedelta
    from app import polling
    set_row, _ = mock_db_session
    now = datetime.now(timezone.utc)
    set_row(type("Row", (), {
        "enabled": True,
        "last_attempt": now - timedelta(minutes=10),  # outside running window
        "last_success": None,
        "next_due": now + timedelta(minutes=5),
        "last_error": "SNMP timeout",
    }))
    got = await polling.get_phase2_state("stuck-device")
    assert got["state"] == "failed"
    assert got["last_error"] == "SNMP timeout"


async def test_get_phase2_state_running(mock_db_session):
    from datetime import datetime, timezone, timedelta
    from app import polling
    set_row, _ = mock_db_session
    now = datetime.now(timezone.utc)
    set_row(type("Row", (), {
        "enabled": True,
        "last_attempt": now - timedelta(seconds=10),
        "last_success": None,
        "next_due": now,
        "last_error": None,
    }))
    got = await polling.get_phase2_state("running-device")
    assert got["state"] == "running"


# ---------------------------------------------------------------------------
# POST /api/onboarding/direct-rest
# ---------------------------------------------------------------------------

async def test_direct_rest_happy_path():
    """Pass-through of orchestrator OnboardingResult to API response shape."""
    from app.main import onboarding_direct_rest, DirectRESTOnboardRequest
    from app.onboarding.orchestrator import OnboardingResult

    fake_result = OnboardingResult(
        success=True,
        device_id="dev-uuid",
        device_name="onboarded",
        phase1_steps_completed=["classify", "probe", "create_device", "..."],
        error=None,
        rollback_performed=False,
    )
    with patch("app.onboarding.orchestrator.onboard_device",
               new=AsyncMock(return_value=fake_result)):
        resp = await onboarding_direct_rest(DirectRESTOnboardRequest(
            ip="192.0.2.1", snmp_community="s3kret",
            secrets_group_id="sg", location_id="loc",
        ))
    assert resp["success"] is True
    assert resp["device_id"] == "dev-uuid"
    assert resp["device_name"] == "onboarded"
    assert resp["error"] is None
    assert resp["error_type"] is None


async def test_direct_rest_already_onboarded_exposes_error_type():
    from app.main import onboarding_direct_rest, DirectRESTOnboardRequest
    from app.onboarding.orchestrator import OnboardingResult

    fake_result = OnboardingResult(
        success=False,
        device_id="existing-uuid",
        device_name="pa-440",
        phase1_steps_completed=["classify", "probe"],
        error="AlreadyOnboardedError: device 'pa-440' already exists at location …",
        rollback_performed=False,
    )
    with patch("app.onboarding.orchestrator.onboard_device",
               new=AsyncMock(return_value=fake_result)):
        resp = await onboarding_direct_rest(DirectRESTOnboardRequest(
            ip="172.21.140.9", snmp_community="public",
            secrets_group_id="sg", location_id="loc",
        ))
    assert resp["success"] is False
    # UI-side dispatch expects the exception-type prefix.
    assert resp["error_type"] == "AlreadyOnboardedError"
    assert resp["device_id"] == "existing-uuid"


async def test_direct_rest_orchestrator_exception_raises_500():
    from fastapi import HTTPException
    from app.main import onboarding_direct_rest, DirectRESTOnboardRequest
    with patch("app.onboarding.orchestrator.onboard_device",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(HTTPException) as excinfo:
            await onboarding_direct_rest(DirectRESTOnboardRequest(
                ip="192.0.2.1", snmp_community="x",
                secrets_group_id="sg", location_id="loc",
            ))
    assert excinfo.value.status_code == 500


async def test_direct_rest_community_not_logged(caplog):
    """Credential hygiene: the snmp_community string must never appear in
    any log record raised by the endpoint."""
    import logging
    from app.main import onboarding_direct_rest, DirectRESTOnboardRequest
    from app.onboarding.orchestrator import OnboardingResult

    secret = "SUPER-SECRET-community-TEST"
    fake_result = OnboardingResult(
        success=True, device_id="d", device_name="n",
        phase1_steps_completed=[], error=None, rollback_performed=False,
    )
    caplog.set_level(logging.DEBUG, logger="app.main")
    with patch("app.onboarding.orchestrator.onboard_device",
               new=AsyncMock(return_value=fake_result)):
        await onboarding_direct_rest(DirectRESTOnboardRequest(
            ip="192.0.2.1", snmp_community=secret,
            secrets_group_id="sg", location_id="loc",
        ))
    joined = "\n".join(r.getMessage() for r in caplog.records)
    joined += "\n" + "\n".join(
        str(getattr(r, "mnm_context", "")) for r in caplog.records
    )
    assert secret not in joined


# ---------------------------------------------------------------------------
# POST /api/onboarding/retry-phase2/{device_name}
# ---------------------------------------------------------------------------

async def test_retry_phase2_happy_path():
    from app.main import onboarding_retry_phase2, db as main_db, polling as main_polling
    with patch.object(main_polling, "ensure_phase2_populate_row",
                      new=AsyncMock(return_value=None)), \
         patch.object(main_db, "is_ready", return_value=True):
        resp = await onboarding_retry_phase2("some-device")
    assert resp["success"] is True
    assert resp["device_name"] == "some-device"
    assert resp["error"] is None


async def test_retry_phase2_db_not_ready_503():
    from fastapi import HTTPException
    from app.main import onboarding_retry_phase2, db as main_db
    with patch.object(main_db, "is_ready", return_value=False):
        with pytest.raises(HTTPException) as excinfo:
            await onboarding_retry_phase2("some-device")
    assert excinfo.value.status_code == 503


async def test_retry_phase2_ensure_helper_failure_500():
    from fastapi import HTTPException
    from app.main import onboarding_retry_phase2, db as main_db, polling as main_polling
    with patch.object(main_polling, "ensure_phase2_populate_row",
                      new=AsyncMock(side_effect=RuntimeError("db write failed"))), \
         patch.object(main_db, "is_ready", return_value=True):
        with pytest.raises(HTTPException) as excinfo:
            await onboarding_retry_phase2("some-device")
    assert excinfo.value.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/nodes now carries status_name + phase2_state
# ---------------------------------------------------------------------------

async def test_list_nodes_exposes_status_name_and_phase2_state():
    """The /api/nodes response MUST include ``status_name`` and
    ``phase2_state`` per-node so the UI can render badges without
    per-device polling."""
    from app.main import list_nodes

    devices = [{
        "name": "node-a", "id": "dev-a-uuid",
        "platform": {"display": "juniper_junos"},
        "location": {"display": "Default Site"},
        "role": {"display": "Switch"},
        "primary_ip4": {"display": "192.0.2.1/32"},
        "status": {"display": "Active"},
        "url": "http://nautobot/api/.../dev-a-uuid/",
    }]
    poll_rows = [
        {"device_name": "node-a", "job_type": "arp",
         "last_success": "2026-04-21T00:00:00Z", "last_error": None,
         "last_attempt": "2026-04-21T00:00:00Z"},
    ]

    async def _fake_get_phase2_state(name):
        if name == "node-a":
            return {"device_name": "node-a", "state": "completed",
                    "enabled": False, "last_attempt": "x", "last_success": "x",
                    "next_due": "x", "last_error": None}
        return None

    with patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=devices)), \
         patch("app.main.polling.get_all_poll_status",
               new=AsyncMock(return_value=poll_rows)), \
         patch("app.main.polling.get_phase2_state",
               new=AsyncMock(side_effect=_fake_get_phase2_state)):
        body = await list_nodes()
    assert "nodes" in body
    assert len(body["nodes"]) == 1
    node = body["nodes"][0]
    assert node["status_name"] == "Active"
    assert node["phase2_state"] == "completed"


async def test_list_nodes_legacy_device_phase2_state_none():
    """A legacy plugin-onboarded device has no phase2_populate row;
    phase2_state must be None, not a failure state."""
    from app.main import list_nodes

    devices = [{
        "name": "legacy", "id": "legacy-uuid",
        "platform": {"display": "juniper_junos"},
        "location": {"display": "Default Site"},
        "role": {"display": "Switch"},
        "primary_ip4": {"display": "192.0.2.2/32"},
        "status": {"display": "Active"},
        "url": "",
    }]
    poll_rows = [
        {"device_name": "legacy", "job_type": "arp",
         "last_success": "2026-04-21T00:00:00Z", "last_error": None,
         "last_attempt": "2026-04-21T00:00:00Z"},
    ]
    with patch("app.main.nautobot_client.get_devices",
               new=AsyncMock(return_value=devices)), \
         patch("app.main.polling.get_all_poll_status",
               new=AsyncMock(return_value=poll_rows)), \
         patch("app.main.polling.get_phase2_state",
               new=AsyncMock(return_value=None)):
        body = await list_nodes()
    node = body["nodes"][0]
    assert node["phase2_state"] is None
    assert node["status_name"] == "Active"
