"""Controller read-through client.

E4 introduces the first cross-system query from the plugin to the
controller's REST API: the Endpoint detail page's "Recent Events"
panel pulls from ``/api/endpoints/{mac}/history`` (the existing
controller endpoint that returns ``{"mac": ..., "events": [...]}``
backed by the ``endpoint_events`` table).

Contract per E0 §3e + E4 prompt §C:

  - Fail-soft. The Endpoint detail page MUST render even when the
    controller is unreachable, slow, or returning 5xx. Failure
    surfaces as a graceful-degradation message in the panel; nothing
    else on the page is affected.
  - 30-second response cache keyed on MAC. Repeated views within the
    cache window share the response.
  - 2-second total timeout (1s connect + 1s read). Slower would make
    the page sluggish when the controller is wedged; faster risks
    false-negative under transient load.
  - Deduplicated WARN logging. One log per minute per MAC at most,
    so a controller outage doesn't flood the Nautobot log with one
    line per detail-page hit.

Auth:

The controller's ``require_auth`` dependency expects an
``mnm_token`` cookie — an HMAC-SHA256 of a UNIX timestamp signed
with ``NAUTOBOT_SECRET_KEY``. Both the controller and Nautobot
containers receive ``NAUTOBOT_SECRET_KEY`` from the same ``.env``
entry (verified in ``docker-compose.yml``), so the plugin can
mint a valid token using the identical scheme. No new shared
secret introduced; no controller-side bypass needed.

If ``NAUTOBOT_SECRET_KEY`` is unset (e.g., a developer running
the plugin outside the docker-compose stack), the client logs a
WARN once and returns ``None`` from every call — exactly the
fail-soft posture the calling view expects.

Sync wrapper:

Nautobot 3.x detail views are synchronous CBVs. The async client
is wrapped in a ``asyncio.run``-based sync entry point because
``ObjectView.get_extra_context`` is a regular method, not a
coroutine. The per-request event-loop creation cost is acceptable
for v1.0 — Endpoint detail is operator-driven, not high-frequency.
If profiling shows it as a bottleneck, v1.1 migrates Endpoint
detail to an async Nautobot view.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from typing import Any

import httpx


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONTROLLER_BASE_URL = os.environ.get(
    "MNM_CONTROLLER_URL", "http://mnm-controller:9090",
)
TOKEN_SECRET = os.environ.get("NAUTOBOT_SECRET_KEY", "")
TOKEN_TTL = 86400  # 24h, matches controller-side TOKEN_TTL

CACHE_TTL_SECONDS = 30
HTTP_CONNECT_TIMEOUT = 1.0
HTTP_READ_TIMEOUT = 1.0
HTTP_TOTAL_TIMEOUT = 2.0
WARN_DEDUP_SECONDS = 60


# ---------------------------------------------------------------------------
# Token minting
# ---------------------------------------------------------------------------

def _make_token() -> str | None:
    """Mint an ``mnm_token`` cookie value.

    Mirrors ``controller/app/main.py::_make_token`` exactly so the
    controller's ``_verify_token`` accepts what we produce.

    Returns ``None`` when ``NAUTOBOT_SECRET_KEY`` is unset — the
    caller logs once and falls back to the unavailable rendering.
    """
    if not TOKEN_SECRET:
        return None
    ts = str(int(time.time()))
    sig = hmac.new(
        TOKEN_SECRET.encode(), ts.encode(), hashlib.sha256,
    ).hexdigest()[:32]
    return f"{ts}.{sig}"


# ---------------------------------------------------------------------------
# Caching + log dedup state
# ---------------------------------------------------------------------------

# {mac_upper: (expires_at_epoch, list[dict] | None)}
_event_cache: dict[str, tuple[float, list[dict] | None]] = {}

# {warn_key: last_logged_epoch} — keys are "<mac>:<error_kind>"
_warn_dedup: dict[str, float] = {}

# Lazy-initialized async client. Connection pool sized small —
# Endpoint detail is the only consumer in v1.0.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=CONTROLLER_BASE_URL,
            timeout=httpx.Timeout(
                HTTP_TOTAL_TIMEOUT,
                connect=HTTP_CONNECT_TIMEOUT,
                read=HTTP_READ_TIMEOUT,
            ),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
    return _client


def _warn_once(key: str, msg: str, **context: Any) -> None:
    now = time.time()
    last = _warn_dedup.get(key, 0.0)
    if now - last < WARN_DEDUP_SECONDS:
        return
    _warn_dedup[key] = now
    if context:
        log.warning("%s | %s", msg, context)
    else:
        log.warning(msg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _normalize_event(raw: dict) -> dict:
    """Adapt the controller's ``EndpointEvent.to_dict`` shape to a
    template-friendly dict.

    The controller returns:
      {id, mac_address, event_type, old_value, new_value,
       details, timestamp}

    The template renders ``timestamp``, ``event_type``, a derived
    ``description`` (built from ``old_value``/``new_value``/details)
    and a ``source`` derived from ``details.get('source')`` when
    present.
    """
    et = raw.get("event_type") or "unknown"
    old_val = raw.get("old_value")
    new_val = raw.get("new_value")
    details = raw.get("details") or {}

    if et == "appeared":
        desc = (
            f"First seen on switch {details.get('switch') or '?'} "
            f"port {details.get('port') or '?'} "
            f"(IP {details.get('ip') or '?'})"
        )
    elif et == "moved_port":
        desc = (
            f"Moved from port {old_val} to port {new_val} on switch "
            f"{details.get('switch') or '?'}"
        )
    elif et == "moved_switch":
        desc = f"Moved from switch {old_val} to switch {new_val}"
    elif et == "ip_changed":
        desc = f"IP changed from {old_val} to {new_val}"
    elif et == "hostname_changed":
        desc = f"Hostname changed from '{old_val}' to '{new_val}'"
    elif et == "disappeared":
        desc = "No longer seen on this network"
    else:
        if old_val and new_val:
            desc = f"{old_val} → {new_val}"
        elif new_val:
            desc = str(new_val)
        else:
            desc = ""

    return {
        "timestamp": raw.get("timestamp") or "",
        "event_type": et,
        "source": details.get("source") or details.get("switch") or "",
        "description": desc,
    }


async def get_endpoint_events(
    mac_address: str, limit: int = 25,
) -> list[dict] | None:
    """Fetch recent events for a MAC from the controller. Fail-soft.

    Returns:
      - ``None`` — controller unreachable, timed out, returned non-2xx,
        or the response was malformed JSON. Caller renders the
        "controller unavailable" message.
      - ``[]`` — controller responded successfully, but there are no
        events for this MAC. Caller renders "No recent events".
      - ``list[dict]`` — events newest-first, capped at ``limit``.
        Each dict has ``timestamp``, ``event_type``, ``source``,
        ``description``.
    """
    if not mac_address:
        return None

    mac = mac_address.upper()
    now = time.time()

    cached = _event_cache.get(mac)
    if cached and cached[0] > now:
        return cached[1]

    token = _make_token()
    if token is None:
        _warn_once(
            "no-secret",
            "controller_client: NAUTOBOT_SECRET_KEY unset; cannot mint "
            "controller auth token. Recent Events panel will render the "
            "unavailable message until the secret is configured.",
        )
        _event_cache[mac] = (now + CACHE_TTL_SECONDS, None)
        return None

    url = f"/api/endpoints/{mac}/history"
    cookies = {"mnm_token": token}

    try:
        client = _get_client()
        resp = await client.get(url, cookies=cookies)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        _warn_once(
            f"{mac}:connect",
            "controller_client: connect failed; rendering unavailable",
            mac=mac, error=str(exc),
        )
        _event_cache[mac] = (now + CACHE_TTL_SECONDS, None)
        return None
    except httpx.TimeoutException as exc:
        _warn_once(
            f"{mac}:timeout",
            "controller_client: request timed out",
            mac=mac, error=str(exc),
        )
        _event_cache[mac] = (now + CACHE_TTL_SECONDS, None)
        return None
    except Exception as exc:  # noqa: BLE001
        _warn_once(
            f"{mac}:exception",
            "controller_client: unexpected exception",
            mac=mac, error=str(exc),
        )
        _event_cache[mac] = (now + CACHE_TTL_SECONDS, None)
        return None

    if resp.status_code != 200:
        _warn_once(
            f"{mac}:status-{resp.status_code}",
            "controller_client: non-200 response",
            mac=mac, status=resp.status_code,
        )
        _event_cache[mac] = (now + CACHE_TTL_SECONDS, None)
        return None

    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        _warn_once(
            f"{mac}:json",
            "controller_client: malformed JSON",
            mac=mac, error=str(exc),
        )
        _event_cache[mac] = (now + CACHE_TTL_SECONDS, None)
        return None

    raw_events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(raw_events, list):
        # Controller responded but with an unexpected shape — log as
        # malformed and fail soft.
        _warn_once(
            f"{mac}:shape",
            "controller_client: unexpected response shape",
            mac=mac,
        )
        _event_cache[mac] = (now + CACHE_TTL_SECONDS, None)
        return None

    events = [_normalize_event(e) for e in raw_events[:limit]]
    _event_cache[mac] = (now + CACHE_TTL_SECONDS, events)
    return events


def get_endpoint_events_sync(
    mac_address: str, limit: int = 25,
) -> list[dict] | None:
    """Synchronous wrapper for ``get_endpoint_events``.

    Nautobot ``ObjectView.get_extra_context`` is sync. ``asyncio.run``
    creates and tears down an event loop per call; for v1.0 the cost
    is acceptable because Endpoint detail is operator-driven and the
    30s response cache amortizes repeat hits.
    """
    try:
        return asyncio.run(get_endpoint_events(mac_address, limit=limit))
    except RuntimeError as exc:
        # Defensive: ``asyncio.run`` raises if called from a thread
        # that already has a running loop. This shouldn't happen in
        # normal Django request handling but guard against it so the
        # detail page never crashes.
        _warn_once(
            f"{mac_address}:eventloop",
            "controller_client: asyncio.run failed",
            mac=mac_address, error=str(exc),
        )
        return None
