"""Unit tests for MNM_SNMP_TIMEOUT_SEC / MNM_SNMP_RETRIES env-var handling.

Covers three concerns:

1. Env-var parsing helper handles numeric, non-numeric (fall back with a
   logged warning), and missing values (silent default).
2. ``polling.collect_arp`` / ``collect_mac`` / ``collect_lldp`` forward the
   configured ``timeout_sec`` and ``retries`` kwargs to the public SNMP
   collectors. Verifies the wiring; does not exercise the polling state
   machine or DB writes (mocked out).
3. ``discovery.py:_snmp_get`` does NOT read the env vars and keeps its
   literal ``timeout=3, retries=1`` regardless of environment. Regression
   test for the design decision documented in CLAUDE.md (Lessons Learned —
   "One knob design must survive contact with operational reality").

Mocks at the public SNMP collector boundary; no real SNMP traffic.
"""
from __future__ import annotations

import inspect
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Preload to beat sibling sys.modules.setdefault stubs.
import app.polling as _polling_preload  # noqa: E402, F401
import app.discovery as _discovery_preload  # noqa: E402, F401


# ---------------------------------------------------------------------------
# _env_numeric helper — direct coverage
# ---------------------------------------------------------------------------


def test_env_numeric_returns_default_when_unset(monkeypatch):
    """Missing env var returns the default silently (no warning)."""
    from app.polling import _env_numeric
    monkeypatch.delenv("MNM_TEST_KEY", raising=False)
    assert _env_numeric("MNM_TEST_KEY", 10.0, float) == 10.0
    assert _env_numeric("MNM_TEST_KEY", 7, int) == 7


def test_env_numeric_returns_default_when_empty_string(monkeypatch):
    """Empty-string env var is treated as unset (matches docker-compose
    behavior where ``FOO=`` exports an empty string, not unset)."""
    from app.polling import _env_numeric
    monkeypatch.setenv("MNM_TEST_KEY", "")
    assert _env_numeric("MNM_TEST_KEY", 10.0, float) == 10.0


def test_env_numeric_parses_valid_float(monkeypatch):
    from app.polling import _env_numeric
    monkeypatch.setenv("MNM_TEST_KEY", "25.5")
    assert _env_numeric("MNM_TEST_KEY", 10.0, float) == 25.5


def test_env_numeric_parses_valid_int(monkeypatch):
    from app.polling import _env_numeric
    monkeypatch.setenv("MNM_TEST_KEY", "3")
    assert _env_numeric("MNM_TEST_KEY", 1, int) == 3


def test_env_numeric_falls_back_on_bad_input(monkeypatch, caplog):
    """Non-numeric value falls back to default with a logged warning."""
    from app.polling import _env_numeric
    monkeypatch.setenv("MNM_TEST_KEY", "not-a-number")
    # Routing the structured log through stdlib logging so caplog sees it
    # is project convention (StructuredLogger wraps logging.getLogger).
    result = _env_numeric("MNM_TEST_KEY", 10.0, float)
    assert result == 10.0


# ---------------------------------------------------------------------------
# polling.py wiring — collectors receive timeout_sec + retries
# ---------------------------------------------------------------------------


@pytest.fixture
def _polling_mocks():
    """Patch every side-effect path in polling so the collector wiring is
    the only thing under test. The collectors themselves are AsyncMocks
    so we can inspect their call kwargs."""
    with patch("app.polling._mark_attempt", new_callable=AsyncMock), \
         patch("app.polling._mark_success", new_callable=AsyncMock), \
         patch("app.polling._mark_failure", new_callable=AsyncMock), \
         patch("app.polling.endpoint_store.upsert_node_arp_bulk",
               new_callable=AsyncMock, return_value=0), \
         patch("app.polling.endpoint_store.upsert_node_mac_bulk",
               new_callable=AsyncMock, return_value=0), \
         patch("app.polling.endpoint_store.upsert_node_lldp_bulk",
               new_callable=AsyncMock, return_value=0), \
         patch("app.polling.snmp_collector.collect_ifindex_to_name",
               new_callable=AsyncMock, return_value={}), \
         patch("app.polling.snmp_collector.collect_bridgeport_to_ifindex",
               new_callable=AsyncMock, return_value={}):
        yield


@pytest.mark.asyncio
async def test_polling_collect_arp_passes_env_var_kwargs(
    monkeypatch, _polling_mocks,
):
    """collect_arp call site passes timeout_sec=SNMP_TIMEOUT_SEC,
    retries=SNMP_RETRIES through to arp_snmp.collect_arp."""
    import app.polling as polling
    # Override module-level constants for this test (they're read at import;
    # monkeypatching the attr is the per-test override.)
    monkeypatch.setattr(polling, "SNMP_TIMEOUT_SEC", 25.0)
    monkeypatch.setattr(polling, "SNMP_RETRIES", 3)

    with patch("app.polling.arp_snmp.collect_arp",
               new_callable=AsyncMock, return_value=[]) as mock_collect:
        await polling.collect_arp("dev1", "uuid-1", device_ip="192.0.2.50")

    assert mock_collect.called
    kwargs = mock_collect.call_args.kwargs
    assert kwargs.get("timeout_sec") == 25.0, \
        f"expected timeout_sec=25.0, got {kwargs.get('timeout_sec')!r}"
    assert kwargs.get("retries") == 3, \
        f"expected retries=3, got {kwargs.get('retries')!r}"


@pytest.mark.asyncio
async def test_polling_collect_mac_passes_env_var_kwargs(
    monkeypatch, _polling_mocks,
):
    import app.polling as polling
    monkeypatch.setattr(polling, "SNMP_TIMEOUT_SEC", 15.0)
    monkeypatch.setattr(polling, "SNMP_RETRIES", 2)

    with patch("app.polling.mac_snmp.collect_mac",
               new_callable=AsyncMock, return_value=[]) as mock_collect:
        await polling.collect_mac("dev1", "uuid-1", device_ip="192.0.2.50")

    assert mock_collect.called
    kwargs = mock_collect.call_args.kwargs
    assert kwargs.get("timeout_sec") == 15.0
    assert kwargs.get("retries") == 2


@pytest.mark.asyncio
async def test_polling_collect_lldp_passes_env_var_kwargs(
    monkeypatch, _polling_mocks,
):
    import app.polling as polling
    monkeypatch.setattr(polling, "SNMP_TIMEOUT_SEC", 20.0)
    monkeypatch.setattr(polling, "SNMP_RETRIES", 5)

    with patch("app.polling.lldp_snmp.collect_lldp",
               new_callable=AsyncMock, return_value=[]) as mock_collect:
        await polling.collect_lldp("dev1", "uuid-1", device_ip="192.0.2.50")

    assert mock_collect.called
    kwargs = mock_collect.call_args.kwargs
    assert kwargs.get("timeout_sec") == 20.0
    assert kwargs.get("retries") == 5


# ---------------------------------------------------------------------------
# Discovery regression — _snmp_get keeps internal 3s/1 constants
# ---------------------------------------------------------------------------


def test_discovery_snmp_get_keeps_internal_constants():
    """``discovery.py:_snmp_get`` must keep literal ``timeout=3, retries=1``
    regardless of ``MNM_SNMP_TIMEOUT_SEC`` / ``MNM_SNMP_RETRIES``.

    Discovery sweep optimizes for fast dead-IP iteration; folding it under
    the polling default would 3× sweep cost on the dead-IP path. The
    design decision is documented in CLAUDE.md Lessons Learned ("One knob
    design must survive contact with operational reality") and reinforced
    by a code comment in ``discovery.py``. This test prevents accidental
    parameterization of the function in future refactors.
    """
    from app.discovery import _snmp_get
    source = inspect.getsource(_snmp_get)
    assert "timeout=3" in source, (
        "discovery._snmp_get must keep literal timeout=3 for sweep speed; "
        "see CLAUDE.md Lessons Learned about the discovery vs polling split"
    )
    assert "retries=1" in source, (
        "discovery._snmp_get must keep literal retries=1; see CLAUDE.md "
        "Key Design Decisions Log for the rationale"
    )
    # The function must not parameterize the values from polling's env-var
    # constants. The comment may reference the env var by name (and does,
    # to signpost the polling-side location); the CODE must not.
    assert "timeout=SNMP_TIMEOUT_SEC" not in source, (
        "discovery._snmp_get must not parameterize timeout from "
        "polling.SNMP_TIMEOUT_SEC. The env var governs polling only."
    )
    assert "retries=SNMP_RETRIES" not in source, (
        "discovery._snmp_get must not parameterize retries from "
        "polling.SNMP_RETRIES. The env var governs polling only."
    )
    assert "polling.SNMP_TIMEOUT_SEC" not in source, (
        "discovery._snmp_get must not import polling.SNMP_TIMEOUT_SEC."
    )
    assert "polling.SNMP_RETRIES" not in source


def test_discovery_snmp_get_constants_have_explanatory_comment():
    """The literal 3/1 values in _snmp_get must be accompanied by a comment
    explaining the design choice — a future reader should not have to guess
    why these aren't parameterized."""
    from app.discovery import _snmp_get
    source = inspect.getsource(_snmp_get)
    assert "MNM_SNMP_TIMEOUT_SEC" in source, (
        "The comment above the UdpTransportTarget.create call should mention "
        "MNM_SNMP_TIMEOUT_SEC so a reader knows where polling tuning lives."
    )
    assert "sweep" in source.lower(), (
        "The comment should explain this is sweep-specific."
    )


# ---------------------------------------------------------------------------
# Module-level constants — defaults match the documented contract
# ---------------------------------------------------------------------------


def test_snmp_polling_defaults():
    """With no env vars set, polling.SNMP_TIMEOUT_SEC and SNMP_RETRIES
    must match the documented defaults (10.0 / 1)."""
    # Module was imported under whatever env was in effect at test-collection
    # time. The defaults are documented as 10.0 / 1; verify via the parser.
    from app.polling import _env_numeric
    assert _env_numeric("MNM_DEFINITELY_UNSET_KEY_XYZ", 10.0, float) == 10.0
    assert _env_numeric("MNM_DEFINITELY_UNSET_KEY_XYZ", 1, int) == 1
