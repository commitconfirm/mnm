"""MNM Controller — FastAPI application."""

import asyncio
import hashlib
import hmac
import os
import platform
import secrets
import time

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.logging_config import StructuredLogger, setup_logging, get_recent_logs

# Initialize structured logging before anything else
setup_logging()

from app import auto_discover, db, discovery, docker_manager, endpoint_store, nautobot_client, polling, probes
from app.connectors import proxmox as proxmox_connector
from app.config import (
    DATA_DIR, load_config, load_config_async, save_config, save_config_async,
)

log = StructuredLogger(__name__, module="controller")

MNM_VERSION = "0.2.5"
_start_time = time.time()

app = FastAPI(title="MNM Controller", docs_url=None, redoc_url=None)

# Auth config
ADMIN_PASSWORD = os.environ.get("MNM_ADMIN_PASSWORD", "")
TOKEN_SECRET = os.environ.get("NAUTOBOT_SECRET_KEY", secrets.token_hex(32))
TOKEN_TTL = 86400  # 24 hours


def _make_token() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(TOKEN_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def _verify_token(token: str) -> bool:
    try:
        ts, sig = token.split(".", 1)
        expected = hmac.new(TOKEN_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return False
        if time.time() - int(ts) > TOKEN_TTL:
            return False
        return True
    except (ValueError, TypeError):
        return False


def require_auth(request: Request):
    token = request.cookies.get("mnm_token")
    if not token or not _verify_token(token):
        raise HTTPException(status_code=401, detail="Not authenticated")


# -------------------------------------------------------------------------
# Request logging middleware
# -------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration_ms = round((time.time() - start) * 1000)
    # Skip noisy health checks and static files at INFO level
    path = request.url.path
    if path in ("/api/health",) or path.startswith("/static/"):
        if duration_ms > 1000:  # Only log slow static/health
            log.warning("slow_request", f"{request.method} {path}", context={
                "method": request.method, "path": path,
                "status": response.status_code, "duration_ms": duration_ms,
            })
    elif path.startswith("/api/"):
        log.debug("api_request", f"{request.method} {path} -> {response.status_code}", context={
            "method": request.method, "path": path,
            "status": response.status_code, "duration_ms": duration_ms,
            "client": request.client.host if request.client else "unknown",
        })
    return response


# -------------------------------------------------------------------------
# Startup — launch background tasks and log diagnostics
# -------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    # Startup diagnostics
    nautobot_url = os.environ.get("NAUTOBOT_URL", "http://nautobot:8080")
    log.info("startup", "MNM Controller starting", context={
        "version": MNM_VERSION,
        "python": platform.python_version(),
        "log_level": os.environ.get("MNM_LOG_LEVEL", "INFO"),
        "log_format": os.environ.get("MNM_LOG_FORMAT", "json"),
        "nautobot_url": nautobot_url,
        "env_vars_set": [k for k in ("MNM_ADMIN_USER", "MNM_ADMIN_PASSWORD",
                                      "NAUTOBOT_NAPALM_USERNAME", "SNMP_COMMUNITY",
                                      "GNMI_USERNAME") if os.environ.get(k)],
    })

    # Initialize controller database (Phase 2.7)
    db_ok = await db.init_db()
    if db_ok:
        # Run one-shot JSON -> Postgres migration if applicable
        try:
            mig = await endpoint_store.migrate_from_json(DATA_DIR)
            if mig.get("endpoints_imported") or mig.get("config_imported"):
                log.info("migration_done", "JSON -> Postgres migration applied", context=mig)
        except Exception as e:
            log.warning("migration_failed", "Migration check failed", context={"error": str(e)})
        # Prime config cache
        try:
            await load_config_async()
        except Exception:
            pass

    # Check Docker connectivity
    try:
        containers = docker_manager.get_containers()
        log.info("docker_connected", f"Docker connected, {len(containers)} containers", context={
            "container_count": len(containers),
        })
    except Exception as e:
        log.error("docker_failed", f"Docker connection failed: {e}", exc_info=True)

    log.info("startup_storage", "Storage backend status", context={
        "db_ready": db.is_ready(),
        "endpoints_in_db": (await endpoint_store.count_endpoints()) if db.is_ready() else 0,
    })

    # Repair missing primary IPs on onboarded devices (Phase 2.8 fix)
    try:
        repair = await discovery.repair_missing_primary_ips()
        if repair.get("fixed"):
            log.info("primary_ip_repair_done", "Repaired missing primary IPs",
                     context=repair)
    except Exception as e:
        log.warning("primary_ip_repair_error", "Primary IP repair failed at startup",
                    context={"error": str(e)})

    # Ensure custom onboarding Statuses exist in Nautobot (v1.0 onboarding
    # workstream, Prompt 2; reality-check §2 / operator Q5 decision).
    # Must tolerate Nautobot briefly unavailable at boot — log a warning and
    # continue so the controller still comes up.
    try:
        slugs = await nautobot_client.ensure_custom_statuses()
        log.info("custom_statuses_ready",
                 "Nautobot custom onboarding statuses ready",
                 context={"slugs": list(slugs.keys())})
    except Exception as e:
        log.warning("custom_statuses_unavailable",
                    "Could not bootstrap custom Statuses — Nautobot not ready",
                    context={"error": str(e)})

    # Seed any missing poll job types for existing devices (e.g. routes, bgp added after initial onboarding)
    try:
        devices = await nautobot_client.get_devices()
        for dev in devices:
            name = dev.get("name")
            if name:
                await polling.ensure_device_polls(name)
    except Exception as e:
        log.warning("poll_seed_startup", "Failed to seed missing poll types",
                    context={"error": str(e)})

    # Launch background tasks
    asyncio.create_task(discovery.scheduled_sweep_loop())
    asyncio.create_task(polling.poll_loop())
    asyncio.create_task(proxmox_connector.scheduled_loop())
    asyncio.create_task(_scheduled_prune_loop())
    asyncio.create_task(_node_info_warmup_loop())
    log.info("background_tasks",
             "Background tasks launched: sweep scheduler, modular poller, proxmox connector, prune loop, node info warmup",
             context={"proxmox_configured": proxmox_connector.is_configured(),
                      "retention_days": _retention_days(),
                      "prune_interval_hours": _prune_interval_hours()})


@app.on_event("shutdown")
async def on_shutdown():
    await nautobot_client.close_client()
    log.info("shutdown", "MNM Controller shutting down")


# -------------------------------------------------------------------------
# Health
# -------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    uptime = round(time.time() - _start_time)
    containers = []
    try:
        containers = docker_manager.get_containers()
    except Exception:
        pass
    healthy = sum(1 for c in containers if c.get("health") == "healthy")

    sweep_state = discovery.get_sweep_state()
    poll_state = polling.get_poll_state()
    ep_count = 0
    if db.is_ready():
        try:
            ep_count = await endpoint_store.count_endpoints()
        except Exception:
            pass

    return {
        "status": "ok",
        "version": MNM_VERSION,
        "uptime_seconds": uptime,
        "docker_connected": len(containers) > 0,
        "containers_total": len(containers),
        "containers_healthy": healthy,
        "db_connected": db.is_ready(),
        "last_sweep": sweep_state.get("finished_at"),
        "last_poll": poll_state.get("last_check"),
        "endpoints_tracked": ep_count,
        "log_level": os.environ.get("MNM_LOG_LEVEL", "INFO"),
    }


@app.get("/api/dashboard/interface-errors", dependencies=[Depends(require_auth)])
async def dashboard_interface_errors():
    """Query Prometheus for interfaces with non-zero error/discard counters.

    Returns a list of {node_name, interface, errors_in, errors_out, discards_in,
    discards_out} for interfaces with any non-zero counter. Degrades gracefully
    if Prometheus is unreachable.
    """
    import httpx as _httpx

    prom_url = os.environ.get("PROMETHEUS_URL", "http://mnm-prometheus:9090")
    metrics = {
        "errors_in": "ifInErrors",
        "errors_out": "ifOutErrors",
        "discards_in": "ifInDiscards",
        "discards_out": "ifOutDiscards",
    }
    # Collect all non-zero counters keyed by (instance, ifName)
    iface_data: dict[tuple[str, str], dict] = {}

    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            for field, metric in metrics.items():
                resp = await client.get(
                    f"{prom_url}/api/v1/query",
                    params={"query": f"{metric} > 0"},
                )
                if resp.status_code != 200:
                    continue
                results = resp.json().get("data", {}).get("result", [])
                for r in results:
                    labels = r.get("metric", {})
                    instance = labels.get("instance", "")
                    ifname = labels.get("ifName", labels.get("ifDescr", ""))
                    device = labels.get("device_name", instance.split(":")[0])
                    val = float(r.get("value", [0, 0])[1])
                    if val <= 0:
                        continue
                    key = (device, ifname)
                    if key not in iface_data:
                        iface_data[key] = {
                            "node_name": device,
                            "interface": ifname,
                            "errors_in": 0, "errors_out": 0,
                            "discards_in": 0, "discards_out": 0,
                        }
                    iface_data[key][field] = int(val)
    except Exception as e:
        return {"interfaces": [], "error": str(e)}

    return {"interfaces": sorted(iface_data.values(), key=lambda x: x["node_name"])}


@app.get("/api/service-urls")
async def service_urls(request: Request):
    """Return external URLs for linked services, derived from the request's
    Host header so they work regardless of how the operator accesses MNM
    (LAN IP, Tailscale, SSH tunnel, etc.). Ports are configurable via env vars.

    This is the single source of truth for service URLs — all frontend pages
    should use this instead of hardcoding ports.
    """
    host = request.headers.get("host", "localhost:9090").split(":")[0]
    proto = "https" if request.url.scheme == "https" else "http"
    nautobot_port = os.environ.get("MNM_NAUTOBOT_EXT_PORT", "8443")
    traefik_port = os.environ.get("MNM_TRAEFIK_EXT_PORT", "8080")
    return {
        "nautobot": f"{proto}://{host}:{nautobot_port}",
        "grafana": f"{proto}://{host}:{traefik_port}/grafana/",
        "prometheus": f"{proto}://{host}:{traefik_port}/prometheus/",
    }


# -------------------------------------------------------------------------
# Auth
# -------------------------------------------------------------------------
class LoginRequest(BaseModel):
    password: str


@app.post("/api/auth/login")
async def login(body: LoginRequest, response: Response):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="MNM_ADMIN_PASSWORD not configured")
    if not hmac.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Invalid password")
    token = _make_token()
    response.set_cookie("mnm_token", token, httponly=True, samesite="lax", max_age=TOKEN_TTL)
    return {"status": "ok"}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("mnm_token")
    return {"status": "ok"}


@app.get("/api/auth/check")
async def auth_check(request: Request):
    token = request.cookies.get("mnm_token")
    if token and _verify_token(token):
        return {"authenticated": True}
    return {"authenticated": False}


# -------------------------------------------------------------------------
# Status
# -------------------------------------------------------------------------
@app.get("/api/status", dependencies=[Depends(require_auth)])
async def status():
    containers = docker_manager.get_containers()
    return {"containers": containers}


# -------------------------------------------------------------------------
# Discovery
# -------------------------------------------------------------------------
class SweepRequest(BaseModel):
    cidr_ranges: list[str]
    location_id: str
    secrets_group_id: str
    snmp_community: str = ""
    auto_discover_hops: int = 0


@app.post("/api/discover/sweep", dependencies=[Depends(require_auth)])
async def start_sweep(body: SweepRequest):
    state = discovery.get_sweep_state()
    if state["running"]:
        raise HTTPException(status_code=409, detail="Sweep already running")

    asyncio.create_task(
        discovery.sweep(
            body.cidr_ranges,
            body.location_id,
            body.secrets_group_id,
            snmp_community=body.snmp_community,
            auto_discover_hops=body.auto_discover_hops,
        )
    )
    return {"status": "started"}


class OnboardRequest(BaseModel):
    ip: str


@app.post("/api/discover/onboard", dependencies=[Depends(require_auth)])
async def onboard_single(body: OnboardRequest):
    """Re-submit onboarding for a single IP. Uses the saved sweep schedule's
    location and credentials. For retrying failed onboarding attempts."""
    config = await load_config_async()
    schedules = config.get("sweep_schedules", [])
    if not schedules:
        raise HTTPException(status_code=400, detail="No sweep schedule configured — need location and credentials")
    s = schedules[0]
    asyncio.create_task(
        discovery._onboard_host(
            body.ip,
            s["location_id"],
            s["secrets_group_id"],
        )
    )
    return {"status": "submitted", "ip": body.ip}


@app.post("/api/discover/stop", dependencies=[Depends(require_auth)])
async def stop_sweep():
    """Stop the currently running sweep."""
    discovery.stop_sweep()
    return {"status": "stopped"}


@app.get("/api/discover/status", dependencies=[Depends(require_auth)])
async def sweep_status():
    return discovery.get_sweep_state()


@app.get("/api/discover/onboarding", dependencies=[Depends(require_auth)])
async def onboarding_states():
    """All onboarding job states tracked by the controller."""
    return {"onboarding": discovery.get_onboarding_state()}


@app.get("/api/discover/onboarding/{ip}", dependencies=[Depends(require_auth)])
async def onboarding_state_for(ip: str):
    """Detailed onboarding progress / error for a single host.

    Used by the host Details panel to surface stage transitions and
    actionable error messages.
    """
    state = discovery.get_onboarding_state(ip)
    if not state:
        return {"ip": ip, "stage": "none", "message": "No onboarding job tracked for this IP"}
    return state


@app.get("/api/discover/history", dependencies=[Depends(require_auth)])
async def sweep_history():
    """Return history of past sweep runs."""
    state = discovery.get_sweep_state()
    return {"history": state.get("history", [])}


@app.get("/api/onboarding/phase2-status/{device_name}",
         dependencies=[Depends(require_auth)])
async def phase2_status(device_name: str):
    """Return the Phase 2 onboarding status for a device.

    State-derivation logic is shared with the ``/api/nodes`` list via
    :func:`app.polling.get_phase2_state`. Returns 404 when no
    ``phase2_populate`` row exists (legacy plugin-onboarded devices).
    """
    from app import db as app_db
    if not app_db.is_ready():
        raise HTTPException(status_code=503, detail="controller DB not ready")
    state = await polling.get_phase2_state(device_name)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=f"no phase2_populate row for device {device_name!r}",
        )
    return state


# ---------------------------------------------------------------------------
# Direct-REST onboarding (v1.0 Prompt 8).
#
# ``/api/onboarding/direct-rest`` is now the default onboarding path from
# the Discover UI and the Nodes "Add Node" form. The legacy plugin-based
# path at ``/api/nautobot/onboard`` + ``/api/nautobot/sync-network-data``
# is retained per operator Q1 (plugin stays installed during v1.0) but
# is no longer reachable from the UI. Prompt 10 (deferred) removes it.
# ---------------------------------------------------------------------------


class DirectRESTOnboardRequest(BaseModel):
    ip: str
    snmp_community: str
    secrets_group_id: str
    location_id: str


@app.post("/api/onboarding/direct-rest",
          dependencies=[Depends(require_auth)])
async def onboarding_direct_rest(body: DirectRESTOnboardRequest):
    """Invoke the direct-REST Phase 1 orchestrator synchronously.

    Returns after Phase 1 completes (seconds). Phase 2 is scheduled by
    Step G.5 as a one-shot polling-job row; the frontend polls
    ``/api/onboarding/phase2-status/{device_name}`` to observe progress.

    Credential hygiene: ``snmp_community`` is sensitive. Never logged;
    only its presence (``snmp_community_set: bool``) is recorded.
    """
    from app.onboarding import orchestrator

    log.info("onboarding_api_called",
             "Direct-REST onboarding invoked via API",
             context={"ip": body.ip,
                      "location_id": body.location_id,
                      "secrets_group_id": body.secrets_group_id,
                      "snmp_community_set": bool(body.snmp_community)})

    try:
        result = await orchestrator.onboard_device(
            ip=body.ip,
            snmp_community=body.snmp_community,
            secrets_group_id=body.secrets_group_id,
            location_id=body.location_id,
        )
    except Exception as exc:
        log.error("onboarding_api_exception",
                  "Direct-REST orchestrator raised unexpectedly",
                  context={"ip": body.ip, "error": str(exc)}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    # Derive error_type from the orchestrator's error string prefix for
    # UI-side dispatch (AlreadyOnboardedError vs ClassificationFailedError
    # vs UnsupportedVendorError vs NautobotWriteError vs ProbeFailedError).
    error_type: "str | None" = None
    if result.error:
        prefix = result.error.split(":", 1)[0].strip()
        error_type = prefix or None

    return {
        "success": result.success,
        "device_id": result.device_id,
        "device_name": result.device_name,
        "phase1_steps_completed": result.phase1_steps_completed,
        "error": result.error,
        "error_type": error_type,
        "rollback_performed": result.rollback_performed,
    }


@app.post("/api/onboarding/retry-phase2/{device_name}",
          dependencies=[Depends(require_auth)])
async def onboarding_retry_phase2(device_name: str):
    """Re-enable the ``phase2_populate`` row for a device.

    Operator-triggered retry for devices stuck in Onboarding Incomplete.
    Uses :func:`polling.ensure_phase2_populate_row`, which is idempotent —
    re-enables a disabled row and resets ``next_due`` to now so the next
    polling-loop tick picks it up.
    """
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="controller DB not ready")

    try:
        await polling.ensure_phase2_populate_row(device_name)
    except Exception as exc:
        log.error("phase2_retry_failed",
                  "ensure_phase2_populate_row raised",
                  context={"device_name": device_name, "error": str(exc)},
                  exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return {"success": True, "device_name": device_name, "error": None}


# In-memory state for the sync-incomplete progress tracker (Rule 9 — every
# operator action gets a visible progress indicator). Single-run state since
# multiple concurrent runs are not allowed.
_sync_incomplete_state: dict = {
    "running": False,
    "total": 0,
    "completed": 0,
    "succeeded": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "current_device": "",
    "devices": [],   # [{name, id, status: pending|running|ok|failed, error}]
}


async def _run_sync_incomplete():
    """Background task: iterate the current incomplete-device list and call
    Nautobot's Sync Network Data job once per device. Updates progress state
    so the dashboard can poll it."""
    try:
        devices = await nautobot_client.get_devices()
        ip_records = await nautobot_client.get_ip_addresses()
    except Exception as e:
        _sync_incomplete_state["running"] = False
        _sync_incomplete_state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log.error("sync_incomplete_fetch_failed", "Could not fetch device list",
                  context={"error": str(e)}, exc_info=True)
        return

    dev_has_iface_ip: set[str] = set()
    for rec in ip_records:
        ao = rec.get("assigned_object") or {}
        dev_ref = ao.get("device") if isinstance(ao, dict) else None
        if isinstance(dev_ref, dict) and dev_ref.get("id"):
            dev_has_iface_ip.add(dev_ref["id"])

    targets = [
        {"id": d.get("id"), "name": d.get("name", "(unnamed)"), "status": "pending", "error": ""}
        for d in devices
        if not (d.get("primary_ip4") or d.get("primary_ip6") or d.get("id") in dev_has_iface_ip)
    ]

    _sync_incomplete_state["devices"] = targets
    _sync_incomplete_state["total"] = len(targets)
    _sync_incomplete_state["completed"] = 0
    _sync_incomplete_state["succeeded"] = 0
    _sync_incomplete_state["failed"] = 0

    log.info("sync_incomplete_start", "Starting sync of incomplete devices",
             context={"count": len(targets)})

    for entry in targets:
        entry["status"] = "running"
        _sync_incomplete_state["current_device"] = entry["name"]
        try:
            await nautobot_client.submit_sync_network_data(
                device_ids=[entry["id"]],
                sync_cables=True,
                sync_vlans=False,
                sync_vrfs=False,
                dryrun=False,
            )
            entry["status"] = "ok"
            _sync_incomplete_state["succeeded"] += 1
        except Exception as e:
            entry["status"] = "failed"
            entry["error"] = str(e)[:200]
            _sync_incomplete_state["failed"] += 1
            log.warning("sync_incomplete_device_failed", "Sync failed for device",
                        context={"device": entry["name"], "error": str(e)})
        _sync_incomplete_state["completed"] += 1

    _sync_incomplete_state["running"] = False
    _sync_incomplete_state["current_device"] = ""
    _sync_incomplete_state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log.info("sync_incomplete_done", "Sync of incomplete devices complete",
             context={"total": len(targets),
                      "succeeded": _sync_incomplete_state["succeeded"],
                      "failed": _sync_incomplete_state["failed"]})


@app.post("/api/discover/sync-incomplete", dependencies=[Depends(require_auth)])
async def sync_incomplete_devices():
    """Trigger a Nautobot 'Sync Network Data From Network' run for every
    device that the incomplete-devices advisory currently lists. The run
    happens as a background task; poll GET on the same path for progress.
    """
    if _sync_incomplete_state["running"]:
        raise HTTPException(status_code=409, detail="Sync already running")
    _sync_incomplete_state["running"] = True
    _sync_incomplete_state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _sync_incomplete_state["finished_at"] = None
    asyncio.create_task(_run_sync_incomplete())
    return {"status": "started"}


@app.get("/api/discover/sync-incomplete", dependencies=[Depends(require_auth)])
async def sync_incomplete_status():
    """Return progress for the in-flight (or last completed) sync run."""
    return dict(_sync_incomplete_state)


# -------------------------------------------------------------------------
# Discovery exclusion list (Rule 6 — operator-controlled scope)
# -------------------------------------------------------------------------
class ExcludeRequest(BaseModel):
    identifier: str
    type: str  # "ip" or "device_name"
    reason: str = ""


@app.get("/api/discover/excludes", dependencies=[Depends(require_auth)])
async def list_discover_excludes():
    if not db.is_ready():
        return {"excludes": []}
    return {"excludes": await endpoint_store.list_excludes()}


@app.post("/api/discover/excludes", dependencies=[Depends(require_auth)])
async def add_discover_exclude(body: ExcludeRequest):
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    ident = body.identifier.strip()
    if not ident:
        raise HTTPException(status_code=400, detail="identifier is required")
    if body.type not in ("ip", "device_name"):
        raise HTTPException(status_code=400, detail="type must be 'ip' or 'device_name'")
    user = os.environ.get("MNM_ADMIN_USER", "admin")
    try:
        return await endpoint_store.add_exclude(
            identifier=ident,
            type=body.type,
            reason=body.reason,
            created_by=user,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/discover/excludes/{identifier}", dependencies=[Depends(require_auth)])
async def delete_discover_exclude(identifier: str):
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    removed = await endpoint_store.remove_exclude(identifier)
    if not removed:
        raise HTTPException(status_code=404, detail="Exclusion not found")
    return {"status": "removed"}


@app.get("/api/discover/incomplete-devices", dependencies=[Depends(require_auth)])
async def discover_incomplete_devices():
    """Surface onboarded devices whose record exists in Nautobot but has no
    primary IP set and no interface IPs assigned.

    These devices typically come from a partial onboarding run — the device
    object got created but NAPALM either failed to pull interface data or
    the IP assignment step never completed. Without an IP they cannot be
    SNMP-polled, ARP-collected from, or shown in dashboards.

    The advisory is informational — MNM never auto-acts. The operator should
    re-run "Sync Device From Network" against these devices in Nautobot.
    """
    incomplete: list[dict] = []
    try:
        devices = await nautobot_client.get_devices()
        ip_records = await nautobot_client.get_ip_addresses()
    except Exception as e:
        return {"devices": [], "error": str(e)}

    # Operator exclusion list — devices whose primary IP, any interface IP,
    # OR device name is in this set are filtered out of the advisory.
    # Device-name filtering is the primary path for this advisory because
    # the devices it surfaces by definition have no IPs to match against.
    excluded_ips: set[str] = set()
    excluded_names: set[str] = set()
    if db.is_ready():
        try:
            excluded_ips = await endpoint_store.get_excluded_ips()
            excluded_names = await endpoint_store.get_excluded_device_names()
        except Exception:
            pass

    # Build a per-device set of IP strings (primary + interface) so we can
    # cheaply check device-vs-exclusion membership below.
    dev_has_iface_ip: set[str] = set()
    dev_ips: dict[str, set[str]] = {}
    ip_by_id: dict[str, dict] = {}
    for rec in ip_records:
        ip_by_id[rec.get("id")] = rec
        ao = rec.get("assigned_object") or {}
        dev_ref = ao.get("device") if isinstance(ao, dict) else None
        if isinstance(dev_ref, dict) and dev_ref.get("id"):
            dev_id_ref = dev_ref["id"]
            dev_has_iface_ip.add(dev_id_ref)
            disp = (rec.get("display") or rec.get("address") or "").split("/")[0]
            if disp:
                dev_ips.setdefault(dev_id_ref, set()).add(disp)

    def _device_has_excluded_ip(dev: dict) -> bool:
        if not excluded_ips:
            return False
        dev_id = dev.get("id")
        # Primary IP
        primary = dev.get("primary_ip4") or dev.get("primary_ip6")
        if isinstance(primary, dict):
            disp = (primary.get("display") or primary.get("address") or "").split("/")[0]
            if disp and disp in excluded_ips:
                return True
            pid = primary.get("id")
            if pid and pid in ip_by_id:
                disp = (ip_by_id[pid].get("display") or ip_by_id[pid].get("address") or "").split("/")[0]
                if disp and disp in excluded_ips:
                    return True
        # Any interface IP
        return bool(dev_ips.get(dev_id, set()) & excluded_ips)

    for dev in devices:
        dev_id = dev.get("id")
        primary = dev.get("primary_ip4") or dev.get("primary_ip6")
        has_primary = bool(primary)
        has_iface_ip = dev_id in dev_has_iface_ip
        if has_primary or has_iface_ip:
            continue
        if _device_has_excluded_ip(dev):
            continue
        if dev.get("name") in excluded_names:
            continue
        platform = dev.get("platform") or {}
        location = dev.get("location") or {}
        incomplete.append({
            "id": dev_id,
            "name": dev.get("name", "(unnamed)"),
            "status": (dev.get("status") or {}).get("display", ""),
            "platform": platform.get("display", "") if isinstance(platform, dict) else "",
            "location": location.get("display", "") if isinstance(location, dict) else "",
            "device_type": (dev.get("device_type") or {}).get("display", ""),
        })

    return {"devices": incomplete, "count": len(incomplete)}


@app.get("/api/discover/subnets", dependencies=[Depends(require_auth)])
async def discover_subnets():
    """Surface subnets seen on onboarded devices that the operator hasn't
    added to any sweep schedule yet.

    Inviolable Rule 6 — MNM never autonomously expands sweep scope. This
    endpoint is purely advisory: it lists candidate CIDRs and lets the
    operator click "Add to sweep" in the UI to bring them into scope.

    Sources of candidate subnets:
      1. Every IP recorded in Nautobot IPAM is reduced to its /24 (or
         /64 for v6) parent and counted as a candidate.
      2. Existing sweep schedules from the controller config define the
         "already in scope" set.
    """
    import ipaddress as _ipaddress

    config = await load_config_async()
    schedules = config.get("sweep_schedules", []) or []
    in_scope: set = set()
    for sched in schedules:
        for cidr in sched.get("cidr_ranges", []) or []:
            try:
                in_scope.add(_ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                continue

    # Also exclude subnets that contain IPs on onboarded device interfaces
    # — those are auto-expanded into the sweep schedule and shouldn't
    # clutter the advisory.
    try:
        device_subnets = await nautobot_client.get_device_interface_subnets()
        for ds in device_subnets:
            try:
                in_scope.add(_ipaddress.ip_network(ds, strict=False))
            except ValueError:
                pass
    except Exception:
        pass

    candidates: dict[str, dict] = {}
    try:
        ip_records = await nautobot_client.get_ip_addresses()
    except Exception as e:
        return {"subnets": [], "error": str(e)}

    for rec in ip_records:
        display = rec.get("display") or rec.get("address") or ""
        ip_part = display.split("/")[0] if "/" in display else display
        if not ip_part:
            continue
        try:
            addr = _ipaddress.ip_address(ip_part)
        except ValueError:
            continue
        # Reduce to /24 for v4, /64 for v6
        prefix_len = 24 if addr.version == 4 else 64
        try:
            net = _ipaddress.ip_network(f"{ip_part}/{prefix_len}", strict=False)
        except ValueError:
            continue
        # Skip if already in any operator-defined sweep range
        if any(net.subnet_of(s) or net == s for s in in_scope if s.version == net.version):
            continue
        key = str(net)
        if key not in candidates:
            candidates[key] = {
                "cidr": key,
                "version": addr.version,
                "ip_count": 0,
                "sample_ips": [],
                "in_scope": False,
            }
        candidates[key]["ip_count"] += 1
        if len(candidates[key]["sample_ips"]) < 5:
            candidates[key]["sample_ips"].append(ip_part)

    out = sorted(candidates.values(), key=lambda c: c["ip_count"], reverse=True)
    return {"subnets": out, "in_scope_count": len(in_scope)}


@app.get("/api/discover/neighbors", dependencies=[Depends(require_auth)])
async def discover_neighbors():
    """Return LLDP neighbors that don't match known devices."""
    try:
        cables = await nautobot_client.get_cables()
        devices = await nautobot_client.get_devices()

        known_names = {d["name"].lower() for d in devices}

        unknown = []
        for cable in cables:
            for side in ["termination_a", "termination_b"]:
                term = cable.get(side)
                if not term:
                    continue
                # Check connected device name
                device_info = term.get("device") or term.get("virtual_machine")
                if device_info:
                    name = device_info.get("display", "").lower()
                    if name and name not in known_names:
                        unknown.append({
                            "neighbor_name": device_info.get("display", ""),
                            "connected_to": cable.get("termination_a" if side == "termination_b" else "termination_b", {}).get("display", ""),
                        })

        return {"neighbors": unknown}
    except Exception as e:
        return {"neighbors": [], "error": str(e)}


class OnboardRequest(BaseModel):
    ip: str
    location_id: str
    secrets_group_id: str


# LEGACY plugin-based onboarding path (v1.0 Q1: plugin stays installed
# during v1.0 release). As of Prompt 8 this endpoint is no longer called
# from the Discover UI or the Nodes UI — both route through
# ``/api/onboarding/direct-rest`` instead. Prompt 10 (deferred out of
# v1.0 scope per Q1) removes this handler and its ``submit_onboarding_job``
# dependency entirely.
@app.post("/api/discover/onboard", dependencies=[Depends(require_auth)])
async def onboard_single(body: OnboardRequest):
    """LEGACY plugin-based onboarding — retained for API callers, not
    reachable from the UI."""
    try:
        result = await nautobot_client.submit_onboarding_job(
            ip=body.ip,
            location_id=body.location_id,
            secrets_group_id=body.secrets_group_id,
        )
        return {"status": "submitted", "job_result": result.get("job_result", {})}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------------
# Auto-discovery — hop-limited LLDP neighbor onboarding
# -------------------------------------------------------------------------

class AutoDiscoverRequest(BaseModel):
    node_name: str
    max_hops: int = 1
    location_id: str
    secrets_group_id: str
    snmp_community: str = ""


@app.post("/api/discover/auto", dependencies=[Depends(require_auth)])
async def trigger_auto_discover(body: AutoDiscoverRequest):
    """Manually trigger hop-limited auto-discovery from a specific node.

    Walks LLDP neighbors outward from the specified node, attempting to
    auto-onboard each unseen neighbor up to max_hops deep. Sequential
    onboarding — one device at a time. Respects exclusion lists and the
    MNM_AUTO_DISCOVER_MAX hard cap.
    """
    if body.max_hops < 1 or body.max_hops > 5:
        raise HTTPException(status_code=400, detail="max_hops must be 1-5")

    result = await auto_discover.auto_discover_from_node(
        seed_node_name=body.node_name,
        max_hops=body.max_hops,
        location_id=body.location_id,
        secrets_group_id=body.secrets_group_id,
        snmp_community=body.snmp_community,
    )
    return result


@app.get("/api/discover/auto/history", dependencies=[Depends(require_auth)])
async def auto_discover_history():
    """Return recent auto-discovery run summaries."""
    return {"history": await auto_discover.get_auto_discover_history()}


@app.get("/api/discover/auto/recent", dependencies=[Depends(require_auth)])
async def auto_discover_recent(hours: int = 24):
    """Return nodes auto-discovered in the last N hours.

    Used by the dashboard advisory card to show the operator what
    auto-discovery did recently (Rule 6 compliance).
    """
    return {"nodes": await auto_discover.get_recent_auto_discovered(hours)}


# -------------------------------------------------------------------------
# Sweep schedule management
# -------------------------------------------------------------------------
class SweepSchedule(BaseModel):
    cidr_ranges: list[str]
    location_id: str
    secrets_group_id: str
    interval_hours: int = 24
    snmp_community: str = ""


@app.get("/api/discover/schedule", dependencies=[Depends(require_auth)])
async def get_sweep_schedules():
    """Return current sweep schedules from config."""
    config = await load_config_async()
    return {"schedules": config.get("sweep_schedules", [])}


@app.post("/api/discover/schedule", dependencies=[Depends(require_auth)])
async def save_sweep_schedule(body: SweepSchedule):
    """Save a sweep schedule to config. Appends to existing list."""
    config = await load_config_async()
    schedules = config.get("sweep_schedules", [])
    schedules.append({
        "cidr_ranges": body.cidr_ranges,
        "location_id": body.location_id,
        "secrets_group_id": body.secrets_group_id,
        "interval_hours": body.interval_hours,
        "snmp_community": body.snmp_community,
        "last_run": "",
    })
    config["sweep_schedules"] = schedules
    await save_config_async(config)
    return {"status": "saved", "schedules": schedules}


# -------------------------------------------------------------------------
# Record discovered hosts to Nautobot IPAM
# -------------------------------------------------------------------------
class RecordRequest(BaseModel):
    ips: list[str] = []  # If empty, record all alive hosts from last sweep


@app.post("/api/discover/record", dependencies=[Depends(require_auth)])
async def record_to_nautobot(body: RecordRequest):
    """Record sweep results to Nautobot IPAM.

    If ips list is provided, record only those. Otherwise record all alive
    hosts from the last sweep that haven't been recorded yet.
    """
    state = discovery.get_sweep_state()
    hosts = state.get("hosts", {})

    if body.ips:
        targets = {ip: hosts[ip] for ip in body.ips if ip in hosts}
    else:
        targets = {
            ip: data for ip, data in hosts.items()
            if data.get("status") not in (
                discovery.SweepStatus.DEAD,
                discovery.SweepStatus.PENDING,
            )
        }

    results = {"recorded": 0, "failed": 0, "errors": []}

    for ip, data in targets.items():
        try:
            await nautobot_client.upsert_discovered_ip(ip, data)
            results["recorded"] += 1
        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"ip": ip, "error": str(e)})

    return results


# -------------------------------------------------------------------------
# Nautobot proxy
# -------------------------------------------------------------------------
@app.get("/api/nautobot/devices", dependencies=[Depends(require_auth)])
async def nautobot_devices():
    devices = await nautobot_client.get_devices()
    return {"devices": devices}


@app.get("/api/nautobot/devices/{device_id}", dependencies=[Depends(require_auth)])
async def nautobot_device(device_id: str):
    device = await nautobot_client.get_device(device_id)
    return device


class SyncNetworkDataRequest(BaseModel):
    device_ids: list[str] = []  # If empty, sync all onboarded devices
    sync_cables: bool = True
    sync_vlans: bool = False
    sync_vrfs: bool = False
    dryrun: bool = False


# LEGACY plugin Phase 2 wrapper. As of Prompt 8 Phase 2 network sync is
# driven automatically by the polling-loop one-shot ``phase2_populate``
# job (see ``onboarding/network_sync.py``). This endpoint is retained
# per Q1 but is no longer called from the UI. Prompt 10 removes it.
@app.post("/api/nautobot/sync-network-data", dependencies=[Depends(require_auth)])
async def sync_network_data(body: SyncNetworkDataRequest):
    """LEGACY plugin 'Sync Network Data From Network' job wrapper —
    retained for API callers, not reachable from the UI."""
    try:
        result = await nautobot_client.submit_sync_network_data(
            device_ids=body.device_ids,
            sync_cables=body.sync_cables,
            sync_vlans=body.sync_vlans,
            sync_vrfs=body.sync_vrfs,
            dryrun=body.dryrun,
        )
        return {"status": "submitted", "job_result": result.get("job_result", {})}
    except Exception as e:
        log.error("sync_failed", f"Sync network data failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/nautobot/secrets-groups", dependencies=[Depends(require_auth)])
async def nautobot_secrets_groups():
    groups = await nautobot_client.get_secrets_groups()
    return {"secrets_groups": groups}


@app.get("/api/nautobot/ip-count", dependencies=[Depends(require_auth)])
async def nautobot_ip_count():
    """Return the count of distinct IPs MNM has tracked across every source.

    This is the union view across the controller's `endpoints` table — it
    includes Proxmox-source endpoints, sweep results, and infrastructure
    collector results. Nautobot IPAM is a subset of this because not every
    source writes to Nautobot (the Proxmox connector does not, and the
    infrastructure collector's IPAM mirror sometimes fails to upsert).

    The route name is preserved for backward compatibility with the dashboard.
    """
    try:
        if db.is_ready():
            return {"count": await endpoint_store.count_distinct_ips()}
        ips = await nautobot_client.get_ip_addresses()
        return {"count": len(ips)}
    except Exception as e:
        log.warning("ip_count_failed", "Failed to fetch IP count",
                    context={"error": str(e)})
        return {"count": None, "error": str(e)}


@app.get("/api/nautobot/locations", dependencies=[Depends(require_auth)])
async def nautobot_locations():
    locations = await nautobot_client.get_locations()
    return {"locations": locations}


# -------------------------------------------------------------------------
# Logs
# -------------------------------------------------------------------------
@app.get("/api/logs", dependencies=[Depends(require_auth)])
async def api_logs(
    level: str | None = None,
    module: str | None = None,
    limit: int = 100,
):
    """Return recent log entries from the in-memory ring buffer."""
    entries = get_recent_logs(level=level, module=module, limit=min(limit, 1000))
    return {"entries": entries, "total": len(entries)}


@app.get("/api/logs/export", dependencies=[Depends(require_auth)])
async def api_logs_export():
    """Export the full in-memory log buffer plus system context as a single
    downloadable bundle. Intended for attaching to GitHub issues for remote
    troubleshooting.

    Secrets are already masked at log-write time by `_mask_secrets()`, but
    operators should still review the bundle before sharing publicly.
    """
    import json as _json

    entries = get_recent_logs(limit=10000)
    containers = []
    try:
        containers = docker_manager.get_containers()
    except Exception as e:
        containers = [{"error": str(e)}]
    sweep = discovery.get_sweep_state()
    poll = polling.get_poll_state()
    bundle = {
        "mnm_version": MNM_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uptime_seconds": round(time.time() - _start_time),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "db_connected": db.is_ready(),
        "endpoints_tracked": (await endpoint_store.count_endpoints()) if db.is_ready() else 0,
        "containers": containers,
        "last_sweep": {
            "running": sweep.get("running"),
            "started_at": sweep.get("started_at"),
            "finished_at": sweep.get("finished_at"),
            "summary": sweep.get("summary"),
        },
        "modular_poller": {
            "running": poll.get("running"),
            "last_check": poll.get("last_check"),
            "polls_dispatched": poll.get("polls_dispatched", 0),
        },
        "log_count": len(entries),
        "logs": entries,
    }
    filename = f"mnm-logs-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    return Response(
        content=_json.dumps(bundle, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------
@app.get("/api/config", dependencies=[Depends(require_auth)])
async def get_config():
    return await load_config_async()


@app.post("/api/config", dependencies=[Depends(require_auth)])
async def update_config(request: Request):
    data = await request.json()
    config = await load_config_async()
    config.update(data)
    await save_config_async(config)
    return config


# -------------------------------------------------------------------------
# Endpoints (infrastructure collection)
# -------------------------------------------------------------------------
async def _all_endpoints() -> list[dict]:
    if db.is_ready():
        return await endpoint_store.list_endpoints()
    return []


@app.get("/api/endpoints", dependencies=[Depends(require_auth)])
async def get_endpoints(
    vlan: str | None = None,
    switch: str | None = None,
    mac_vendor: str | None = None,
    source: str | None = None,
):
    """Return all endpoint records, optionally filtered."""
    endpoints = await _all_endpoints()

    if vlan:
        endpoints = [e for e in endpoints if str(e.get("vlan", "")) == vlan]
    if switch:
        endpoints = [e for e in endpoints if (e.get("device_name") or e.get("switch")) == switch]
    if mac_vendor:
        endpoints = [e for e in endpoints if mac_vendor.lower() in (e.get("mac_vendor") or "").lower()]
    if source:
        endpoints = [e for e in endpoints if e.get("source") == source]

    return {"endpoints": endpoints}


@app.get("/api/endpoints/summary", dependencies=[Depends(require_auth)])
async def endpoints_summary():
    """Return summary stats for endpoint collection."""
    poll_state = polling.get_poll_state()
    endpoints = await _all_endpoints()

    vlans = {e.get("vlan") for e in endpoints if e.get("vlan")}
    vendors = {e.get("mac_vendor") for e in endpoints if e.get("mac_vendor")}
    switches = {e.get("device_name") for e in endpoints if e.get("device_name")}

    return {
        "total_endpoints": len(endpoints),
        "vlans_active": len(vlans),
        "vendors_seen": len(vendors),
        "switches": len(switches),
        "last_collection": poll_state.get("last_check"),
        "running": poll_state.get("running", False),
    }


@app.get("/api/endpoints/history", dependencies=[Depends(require_auth)])
async def collection_history():
    """Return history of past collection runs from the controller DB."""
    if db.is_ready():
        history = await endpoint_store.list_collection_runs(limit=20)
    else:
        history = []
    return {"history": history}


# -------------------------------------------------------------------------
# Endpoint history / timeline / events / conflicts (Phase 2.7)
# -------------------------------------------------------------------------
@app.get("/api/endpoints/events", dependencies=[Depends(require_auth)])
async def endpoint_events_recent(type: str | None = None, since: str = "24h", limit: int = 200):
    """Return recent endpoint events filtered by type and time window.

    `since` accepts a duration string like '24h', '7d', or '1h'.
    """
    if not db.is_ready():
        return {"events": []}
    # Parse since string
    hours = 24
    s = since.strip().lower()
    try:
        if s.endswith("h"):
            hours = int(s[:-1])
        elif s.endswith("d"):
            hours = int(s[:-1]) * 24
        elif s.endswith("m"):
            hours = max(1, int(s[:-1]) // 60)
        else:
            hours = int(s)
    except ValueError:
        hours = 24
    events = await endpoint_store.get_recent_events(event_type=type, since_hours=hours, limit=limit)
    return {"events": events}


@app.get("/api/endpoints/conflicts", dependencies=[Depends(require_auth)])
async def endpoint_conflicts():
    """Return IPs currently claimed by more than one MAC."""
    if not db.is_ready():
        return {"conflicts": []}
    return {"conflicts": await endpoint_store.get_ip_conflicts()}


@app.get("/api/endpoints/anomalies", dependencies=[Depends(require_auth)])
async def endpoint_anomalies():
    """Return every anomalous endpoint bucket: ip_conflicts, multi_location,
    no_ip, unclassified, stale. Each entry includes a summary count map."""
    if not db.is_ready():
        return {"summary": {}, "ip_conflicts": [], "multi_location": [],
                "no_ip": [], "unclassified": [], "stale": []}
    return await endpoint_store.get_anomalies()


# -------------------------------------------------------------------------
# Endpoint watchlist (must be declared BEFORE the /{mac} routes so that
# 'watches' is not interpreted as a MAC path parameter)
# -------------------------------------------------------------------------
class WatchRequest(BaseModel):
    mac_address: str
    reason: str = ""


@app.get("/api/endpoints/watches", dependencies=[Depends(require_auth)])
async def list_watches():
    if not db.is_ready():
        return {"watches": []}
    return {"watches": await endpoint_store.list_watches()}


@app.post("/api/endpoints/watches", dependencies=[Depends(require_auth)])
async def add_watch(body: WatchRequest, request: Request):
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    user = os.environ.get("MNM_ADMIN_USER", "admin")
    return await endpoint_store.add_watch(body.mac_address, body.reason, user)


@app.delete("/api/endpoints/watches/{mac}", dependencies=[Depends(require_auth)])
async def delete_watch(mac: str):
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    removed = await endpoint_store.remove_watch(mac)
    if not removed:
        raise HTTPException(status_code=404, detail="Watch not found")
    return {"status": "removed"}


@app.get("/api/endpoints/{mac}", dependencies=[Depends(require_auth)])
async def endpoint_detail(mac: str):
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    ep = await endpoint_store.get_endpoint(mac)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return ep


@app.get("/api/endpoints/{mac}/history", dependencies=[Depends(require_auth)])
async def endpoint_history(mac: str):
    """Return all events for a single MAC."""
    if not db.is_ready():
        return {"mac": mac, "events": []}
    events = await endpoint_store.get_endpoint_events(mac)
    return {"mac": mac.upper(), "events": events}


@app.get("/api/endpoints/{mac}/locations", dependencies=[Depends(require_auth)])
async def endpoint_locations(mac: str):
    """Return every (switch, port, vlan) row this MAC has ever occupied."""
    if not db.is_ready():
        return {"mac": mac, "locations": []}
    return {"mac": mac.upper(), "locations": await endpoint_store.get_endpoint_history(mac)}


@app.get("/api/endpoints/{mac}/timeline", dependencies=[Depends(require_auth)])
async def endpoint_timeline(mac: str):
    """Return a chronological narrative of everywhere this endpoint has been."""
    if not db.is_ready():
        return {"mac": mac, "timeline": []}
    ep = await endpoint_store.get_endpoint(mac)
    events = await endpoint_store.get_endpoint_events(mac, limit=1000)
    # Build oldest-first narrative
    narrative = []
    for ev in reversed(events):
        et = ev["event_type"]
        ts = ev["timestamp"]
        details = ev.get("details") or {}
        if et == "appeared":
            text = f"First seen on switch {details.get('switch') or '?'} port {details.get('port') or '?'} (IP {details.get('ip') or '?'})"
        elif et == "moved_port":
            text = f"Moved from port {ev['old_value']} to port {ev['new_value']} on switch {details.get('switch') or '?'}"
        elif et == "moved_switch":
            text = f"Moved from switch {ev['old_value']} to switch {ev['new_value']}"
        elif et == "ip_changed":
            text = f"IP changed from {ev['old_value']} to {ev['new_value']}"
        elif et == "hostname_changed":
            text = f"Hostname changed from '{ev['old_value']}' to '{ev['new_value']}'"
        elif et == "disappeared":
            text = "No longer seen on the network"
        else:
            text = et
        narrative.append({"timestamp": ts, "event_type": et, "text": text})
    return {
        "mac": mac.upper(),
        "endpoint": ep,
        "timeline": narrative,
    }


@app.get("/api/endpoints/collection-status", dependencies=[Depends(require_auth)])
async def collection_status():
    """Return collection progress (backed by modular poller state)."""
    return polling.get_poll_state()


@app.post("/api/endpoints/collect", dependencies=[Depends(require_auth)])
async def trigger_collection():
    """Trigger an immediate poll cycle on all devices."""
    poll_state = polling.get_poll_state()
    if poll_state.get("running"):
        raise HTTPException(status_code=409, detail="Poll cycle already running")
    # Trigger all devices by setting their next_due to now
    try:
        devices = await nautobot_client.get_devices()
        triggered = 0
        for dev in devices:
            name = dev.get("name")
            if name:
                count = await polling.trigger_device(name)
                if count:
                    triggered += 1
        return {"status": "started", "devices_triggered": triggered}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------------
# Proxmox connector
# -------------------------------------------------------------------------
@app.get("/api/proxmox/status", dependencies=[Depends(require_auth)])
async def proxmox_status():
    """Return the latest Proxmox snapshot summary for the dashboard card."""
    return proxmox_connector.get_state()


@app.post("/api/proxmox/collect", dependencies=[Depends(require_auth)])
async def proxmox_trigger():
    """Manually trigger an out-of-cycle Proxmox collection run."""
    if not proxmox_connector.is_configured():
        raise HTTPException(status_code=503, detail="Proxmox connector not configured")
    asyncio.create_task(proxmox_connector.collect())
    return {"status": "started"}


@app.get("/api/prometheus/snmp-targets")
async def prometheus_snmp_targets():
    """Prometheus http_sd endpoint — returns the current set of onboarded
    devices with usable IPs as Prometheus http_sd JSON. The snmp_exporter
    job in `config/prometheus/prometheus.yml` polls this URL on its scrape
    interval, so newly onboarded devices automatically appear in monitoring
    without manual edits to the static target list.

    Unauthenticated by design — same access model as Nautobot's `/metrics`
    and the Proxmox connector's `/api/proxmox/metrics`. Prometheus scrapes
    this from inside the docker network as `controller:9090`.
    """
    targets: list[dict] = []
    try:
        devices = await nautobot_client.get_devices()
        ip_records = await nautobot_client.get_ip_addresses()
        ip_by_id = {rec.get("id"): rec for rec in ip_records}
    except Exception as e:
        log.warning("snmp_sd_failed", "Could not build SNMP target list",
                    context={"error": str(e)})
        return Response(content="[]", media_type="application/json")

    for dev in devices:
        name = dev.get("name") or ""
        if not name:
            continue

        # Resolve primary IP. The shape from Nautobot is either a nested
        # {id, url} object or a fully-rendered dict with `display`/`address`.
        # Fall back to any interface IP if primary isn't set.
        ip_str = ""
        primary = dev.get("primary_ip4") or dev.get("primary_ip6") or {}
        if isinstance(primary, dict):
            display = primary.get("display") or primary.get("address") or ""
            if display:
                ip_str = display.split("/")[0]
            elif primary.get("id"):
                ipo = ip_by_id.get(primary["id"])
                if ipo:
                    disp = ipo.get("display") or ipo.get("address") or ""
                    ip_str = disp.split("/")[0]

        if not ip_str:
            # Fall back: search the IP records for any address tied to this
            # device's interfaces.
            dev_id = dev.get("id")
            for rec in ip_records:
                ao = rec.get("assigned_object") or {}
                dev_ref = ao.get("device") if isinstance(ao, dict) else None
                if isinstance(dev_ref, dict) and dev_ref.get("id") == dev_id:
                    disp = rec.get("display") or rec.get("address") or ""
                    if disp:
                        ip_str = disp.split("/")[0]
                        break

        if not ip_str:
            continue

        targets.append({
            "targets": [ip_str],
            "labels": {
                "device_name": name,
                "device_id": str(dev.get("id", "")),
                "platform": ((dev.get("platform") or {}).get("display") or "").lower(),
            },
        })

    import json as _json
    return Response(
        content=_json.dumps(targets, indent=2),
        media_type="application/json",
    )


@app.get("/api/proxmox/metrics")
async def proxmox_metrics():
    """Prometheus exposition for the Proxmox connector — unauthenticated by
    design so the in-cluster Prometheus container can scrape it without an
    operator session cookie. Same access model as Nautobot's /metrics."""
    return Response(
        content=proxmox_connector.render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# -------------------------------------------------------------------------
# Database maintenance / pruning
# -------------------------------------------------------------------------
#
# A daily background task evicts old endpoint events, IP observations,
# orphaned watches, and stale sentinel rows. Operators can also preview or
# trigger a prune on demand from the dashboard.

_prune_state: dict = {
    "running": False,
    "last_run": None,
    "last_summary": None,
    "last_error": None,
}


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("MNM_RETENTION_DAYS", "365")))
    except (TypeError, ValueError):
        return 365


def _prune_interval_hours() -> int:
    try:
        return max(1, int(os.environ.get("MNM_PRUNE_INTERVAL_HOURS", "24")))
    except (TypeError, ValueError):
        return 24


async def _scheduled_prune_loop() -> None:
    """Background loop: run prune_all() once per MNM_PRUNE_INTERVAL_HOURS."""
    log.info("prune_loop_started", "Prune loop started",
             context={"interval_hours": _prune_interval_hours(),
                      "retention_days": _retention_days()})
    # Wait for the rest of the system to settle before the first prune
    await asyncio.sleep(120)
    while True:
        try:
            if db.is_ready():
                _prune_state["running"] = True
                summary = await endpoint_store.prune_all(_retention_days())
                _prune_state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _prune_state["last_summary"] = summary
                _prune_state["last_error"] = None
                _prune_state["running"] = False
        except asyncio.CancelledError:
            log.info("prune_loop_cancelled", "Prune loop cancelled")
            break
        except Exception as e:
            _prune_state["running"] = False
            _prune_state["last_error"] = str(e)
            log.error("prune_loop_error", "Prune loop error",
                      context={"error": str(e)}, exc_info=True)
        await asyncio.sleep(_prune_interval_hours() * 3600)


# Per-node NAPALM failure tracking for warmup backoff.
# Prevents hammering devices with limited NETCONF capacity (e.g., EX3300
# with 1GB RAM on Junos 12.3 where stale sessions persist 10-15 minutes).
_warmup_failures: dict[str, int] = {}  # node_name → consecutive failure count
_WARMUP_MAX_BACKOFF = 5  # max consecutive failures before skipping for 5 cycles


async def _node_info_warmup_loop() -> None:
    """Background loop: prefetch NAPALM facts/interfaces for all nodes so
    the node detail page loads instantly from cache.

    Runs every 4 minutes. Processes nodes sequentially to avoid overwhelming
    Nautobot's NAPALM proxy. Devices with repeated NAPALM failures get
    exponentially backed off to avoid session exhaustion on constrained
    platforms (Junos 12.3 on EX3300: 1GB RAM, stale NETCONF sessions).
    """
    await asyncio.sleep(10)  # brief pause for startup, then start warming caches
    cycle = 0
    while True:
        cycle += 1
        try:
            devices = await nautobot_client.get_devices()
            for dev in devices:
                name = dev.get("name")
                dev_id = dev.get("id")
                if not name or not dev_id:
                    continue

                # Exponential backoff: skip devices with repeated NAPALM failures.
                # After N consecutive failures, skip for N cycles (max 5).
                fail_count = _warmup_failures.get(name, 0)
                if fail_count > 0 and (cycle % (fail_count + 1)) != 0:
                    continue

                try:
                    now = time.time()
                    # Warm get_facts + get_interfaces (shared cache)
                    cached = _node_info_cache.get(name)
                    if not cached or now - cached[0] >= _NODE_INFO_TTL:
                        await get_node_info(name)
                    # Warm counters cache (slow call — only in background)
                    cc = _node_counters_cache.get(name)
                    if not cc or now - cc[0] >= _NODE_COUNTERS_TTL:
                        try:
                            data = await nautobot_client.napalm_get(dev_id, "get_interfaces_counters")
                            _node_counters_cache[name] = (time.time(), data.get("get_interfaces_counters", {}))
                        except Exception:
                            pass
                    # Rebuild the merged interfaces list with fresh counters
                    iface_data = await _get_raw_interfaces(dev_id, name)
                    counters = (_node_counters_cache.get(name) or (0, {}))[1]
                    if iface_data:
                        _node_iface_cache[name] = (time.time(), _build_iface_list(iface_data, counters))
                    # Success — reset failure counter
                    if name in _warmup_failures:
                        del _warmup_failures[name]
                except Exception:
                    # Track failure for backoff
                    _warmup_failures[name] = min(_warmup_failures.get(name, 0) + 1, _WARMUP_MAX_BACKOFF)
                    log.debug("warmup_backoff", f"NAPALM warmup failed for {name}, backoff={_warmup_failures[name]}",
                              context={"node": name, "failures": _warmup_failures[name]})
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("warmup_error", "Node info warmup failed",
                        context={"error": str(e)[:200]})
        await asyncio.sleep(240)  # 4 minutes between cycles


# -------------------------------------------------------------------------
# Comments + Change History (Phase 2.9)
# -------------------------------------------------------------------------

class CommentCreate(BaseModel):
    target_type: str
    target_id: str
    comment_text: str


@app.get("/api/comments", dependencies=[Depends(require_auth)])
async def list_comments_api(target_type: str, target_id: str):
    """Return all comments for a given endpoint or node, newest first.

    target_type: "endpoint" or "node"
    target_id: MAC address (for endpoints) or node name (for nodes)
    """
    if target_type not in ("endpoint", "node"):
        raise HTTPException(status_code=400, detail="target_type must be 'endpoint' or 'node'")
    if not db.is_ready():
        return []
    # Normalize endpoint MAC to uppercase to match storage convention
    if target_type == "endpoint":
        target_id = target_id.upper()
    return await endpoint_store.list_comments(target_type, target_id)


@app.post("/api/comments", dependencies=[Depends(require_auth)])
async def create_comment_api(body: CommentCreate, request: Request):
    """Create a comment on an endpoint or node. Records a change_history entry."""
    if body.target_type not in ("endpoint", "node"):
        raise HTTPException(status_code=400, detail="target_type must be 'endpoint' or 'node'")
    if not body.comment_text.strip():
        raise HTTPException(status_code=400, detail="comment_text cannot be empty")
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    target_id = body.target_id.upper() if body.target_type == "endpoint" else body.target_id
    created_by = os.environ.get("MNM_ADMIN_USER", "admin")
    return await endpoint_store.add_comment(
        target_type=body.target_type,
        target_id=target_id,
        comment_text=body.comment_text.strip(),
        created_by=created_by,
    )


@app.delete("/api/comments/{comment_id}", dependencies=[Depends(require_auth)])
async def delete_comment_api(comment_id: str):
    """Delete a comment by ID. Records a change_history entry."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    ok = await endpoint_store.delete_comment(comment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Comment not found")
    return Response(status_code=204)


@app.get("/api/history", dependencies=[Depends(require_auth)])
async def change_history_api(
    target_type: str,
    target_id: str,
    field_name: str | None = None,
    change_source: str | None = None,
    limit: int = 50,
):
    """Return change history for an endpoint or node, newest first.

    Optional filters: field_name, change_source. Max limit 500.
    """
    if target_type not in ("endpoint", "node"):
        raise HTTPException(status_code=400, detail="target_type must be 'endpoint' or 'node'")
    if not db.is_ready():
        return []
    if target_type == "endpoint":
        target_id = target_id.upper()
    limit = max(1, min(limit, 500))
    return await endpoint_store.list_change_history(
        target_type=target_type,
        target_id=target_id,
        field_name=field_name,
        change_source=change_source,
        limit=limit,
    )


# -------------------------------------------------------------------------
# Endpoint Probes (Phase 2.9)
# -------------------------------------------------------------------------

@app.post("/api/probes/run", dependencies=[Depends(require_auth)])
async def probe_run(request: Request):
    """Trigger an ICMP/TCP probe sweep. Optionally pass {"macs": [...]} to
    probe specific endpoints. Runs in background, results available via GET."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    macs = body.get("macs")
    targets = None
    if macs:
        eps = await endpoint_store.list_endpoints()
        targets = []
        for ep in eps:
            if ep.get("mac") in macs or ep.get("mac_address") in macs:
                ip = ep.get("ip") or ep.get("current_ip")
                if ip:
                    targets.append({"mac": ep.get("mac", ""), "ip": ip, "open_ports": []})
    asyncio.create_task(probes.probe_endpoints(targets))
    return {"status": "started", "targets": len(targets) if targets else "all"}


@app.get("/api/probes/status", dependencies=[Depends(require_auth)])
async def probe_status():
    """Return current probe run state."""
    return probes.get_state()


@app.get("/api/probes/results", dependencies=[Depends(require_auth)])
async def probe_results(mac: str = ""):
    """Latest probe result per endpoint (or for a specific MAC)."""
    if not db.is_ready():
        return []
    from sqlalchemy import select, func
    async with db.SessionLocal() as session:
        if mac:
            row = (await session.execute(
                select(db.EndpointProbe)
                .where(db.EndpointProbe.mac == mac.upper())
                .order_by(db.EndpointProbe.probed_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            return row.to_dict() if row else {}
        # Latest per MAC via subquery
        latest = (
            select(db.EndpointProbe.mac, func.max(db.EndpointProbe.probed_at).label("latest"))
            .group_by(db.EndpointProbe.mac)
            .subquery()
        )
        rows = (await session.execute(
            select(db.EndpointProbe).join(
                latest,
                (db.EndpointProbe.mac == latest.c.mac) &
                (db.EndpointProbe.probed_at == latest.c.latest),
            )
        )).scalars().all()
        return [r.to_dict() for r in rows]


@app.get("/api/probes/summary", dependencies=[Depends(require_auth)])
async def probe_summary():
    """Aggregate probe stats for dashboard."""
    if not db.is_ready():
        return {"total": 0, "reachable": 0, "unreachable": 0, "avg_latency_ms": None}
    from sqlalchemy import select, func
    async with db.SessionLocal() as session:
        # Latest probe per MAC
        latest = (
            select(db.EndpointProbe.mac, func.max(db.EndpointProbe.probed_at).label("latest"))
            .group_by(db.EndpointProbe.mac)
            .subquery()
        )
        rows = (await session.execute(
            select(db.EndpointProbe).join(
                latest,
                (db.EndpointProbe.mac == latest.c.mac) &
                (db.EndpointProbe.probed_at == latest.c.latest),
            )
        )).scalars().all()
        total = len(rows)
        reachable = sum(1 for r in rows if r.reachable)
        latencies = [r.latency_ms for r in rows if r.latency_ms is not None]
        avg_lat = round(sum(latencies) / len(latencies), 2) if latencies else None
        last_run = max((r.probed_at for r in rows), default=None)
    return {
        "total": total,
        "reachable": reachable,
        "unreachable": total - reachable,
        "avg_latency_ms": avg_lat,
        "last_run": last_run.isoformat() if last_run else None,
    }


@app.get("/api/admin/maintenance", dependencies=[Depends(require_auth)])
async def admin_maintenance_status():
    """Return DB row counts, oldest timestamps, retention setting, and the
    last prune run summary. Powers the Database Maintenance dashboard card."""
    if not db.is_ready():
        return {"db_ready": False}
    stats = await endpoint_store.maintenance_stats()
    return {
        "db_ready": True,
        "retention_days": _retention_days(),
        "prune_interval_hours": _prune_interval_hours(),
        "stats": stats,
        "last_run": _prune_state["last_run"],
        "last_summary": _prune_state["last_summary"],
        "last_error": _prune_state["last_error"],
        "running": _prune_state["running"],
    }


@app.get("/api/admin/prune/preview", dependencies=[Depends(require_auth)])
async def admin_prune_preview():
    """Preview what would be pruned at the current retention setting,
    without actually deleting anything."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    return {
        "retention_days": _retention_days(),
        "would_prune": await endpoint_store.prune_preview(_retention_days()),
    }


@app.post("/api/admin/prune", dependencies=[Depends(require_auth)])
async def admin_prune_now():
    """Trigger an immediate prune cycle. Returns the row counts that were
    deleted. Same operation the daily background task runs."""
    if not db.is_ready():
        raise HTTPException(status_code=503, detail="Controller DB not available")
    if _prune_state["running"]:
        raise HTTPException(status_code=409, detail="Prune already running")
    _prune_state["running"] = True
    try:
        summary = await endpoint_store.prune_all(_retention_days())
        _prune_state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _prune_state["last_summary"] = summary
        _prune_state["last_error"] = None
    except Exception as e:
        _prune_state["last_error"] = str(e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _prune_state["running"] = False
    return {
        "pruned": _prune_state["last_summary"],
        "retention_days": _retention_days(),
    }


# -------------------------------------------------------------------------
# Static files — must be last
# -------------------------------------------------------------------------
@app.get("/")
async def root():
    return FileResponse("app/static/index.html")


@app.get("/login")
async def login_page():
    return FileResponse("app/static/login.html")


@app.get("/discover")
async def discover_page():
    return FileResponse("app/static/discover.html")


@app.get("/nodes")
async def nodes_page():
    return FileResponse("app/static/nodes.html")


@app.get("/nodes/{node_name}")
async def node_detail_page(node_name: str):
    return FileResponse("app/static/node_detail.html")


@app.get("/investigate")
async def investigate_page():
    return FileResponse("app/static/investigate.html")


@app.get("/endpoints")
async def endpoints_page():
    return FileResponse("app/static/endpoints.html")


@app.get("/endpoints/{mac}")
async def endpoint_detail_page(mac: str):
    return FileResponse("app/static/endpoint_detail.html")


@app.get("/events")
async def events_page():
    return FileResponse("app/static/events.html")


@app.get("/logs")
async def logs_page():
    return FileResponse("app/static/logs.html")


@app.get("/jobs")
async def jobs_page():
    return FileResponse("app/static/jobs.html")


# -------------------------------------------------------------------------
# Polling — per-device, per-job-type collection tracking
# -------------------------------------------------------------------------

@app.get("/api/polling/status", dependencies=[Depends(require_auth)])
async def polling_status():
    """All devices, all job types, last poll times and status."""
    rows = await polling.get_all_poll_status()
    # Group by device for the dashboard
    by_device: dict[str, dict] = {}
    for r in rows:
        dn = r["device_name"]
        if dn not in by_device:
            by_device[dn] = {"device_name": dn, "jobs": {}}
        by_device[dn]["jobs"][r["job_type"]] = r
    return {"devices": list(by_device.values()), "poll_state": polling.get_poll_state()}


@app.get("/api/polling/status/{device_name}", dependencies=[Depends(require_auth)])
async def polling_status_device(device_name: str):
    """Single device, all job types."""
    rows = await polling.get_device_poll_status(device_name)
    if not rows:
        raise HTTPException(status_code=404, detail="Device not found in poll tracking")
    return {"device_name": device_name, "jobs": {r["job_type"]: r for r in rows}}


@app.post("/api/polling/trigger/{device_name}", dependencies=[Depends(require_auth)],
          status_code=202)
async def polling_trigger_device(device_name: str):
    """Trigger immediate poll of all job types for a device."""
    count = await polling.trigger_device(device_name)
    if count == 0:
        raise HTTPException(status_code=404, detail="No enabled poll rows found for this device")
    return {"status": "accepted", "device_name": device_name, "jobs_triggered": count}


@app.post("/api/polling/trigger/{device_name}/{job_type}",
          dependencies=[Depends(require_auth)], status_code=202)
async def polling_trigger_job(device_name: str, job_type: str):
    """Trigger a single job type for a device."""
    if job_type not in polling.JOB_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid job type. Must be one of: {polling.JOB_TYPES}")
    count = await polling.trigger_device(device_name, job_type)
    if count == 0:
        raise HTTPException(status_code=404, detail="No enabled poll row found")
    return {"status": "accepted", "device_name": device_name, "job_type": job_type}


class PollConfigUpdate(BaseModel):
    interval_sec: int | None = None
    enabled: bool | None = None


@app.put("/api/polling/config/{device_name}/{job_type}",
         dependencies=[Depends(require_auth)])
async def polling_config_update(device_name: str, job_type: str, body: PollConfigUpdate):
    """Update interval_sec or enabled flag for a specific device/job_type."""
    if job_type not in polling.JOB_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid job type. Must be one of: {polling.JOB_TYPES}")
    result = await polling.update_poll_config(
        device_name, job_type,
        interval_sec=body.interval_sec,
        enabled=body.enabled,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Device/job_type not found")
    return result


# -------------------------------------------------------------------------
# Nodes — onboarded infrastructure devices with poll health
# -------------------------------------------------------------------------

@app.get("/api/nodes", dependencies=[Depends(require_auth)])
async def list_nodes():
    """List all onboarded infrastructure nodes with poll health status.

    Nodes are infrastructure devices that MNM authenticates to and actively
    polls (switches, routers, firewalls). This is the union of Nautobot
    devices that have entries in the device_polls table.
    """
    # Get Nautobot devices and poll status
    try:
        devices = await nautobot_client.get_devices()
    except Exception as exc:
        log.warning("nodes_fetch_failed", "Could not fetch devices from Nautobot",
                    context={"error": str(exc)})
        devices = []

    poll_rows = await polling.get_all_poll_status()

    # Build poll lookup: device_name -> {job_type: row}
    poll_by_device: dict[str, dict[str, dict]] = {}
    for r in poll_rows:
        dn = r["device_name"]
        poll_by_device.setdefault(dn, {})[r["job_type"]] = r

    # Determine which device names are nodes (have poll entries)
    node_names = set(poll_by_device.keys())

    nodes = []
    for dev in devices:
        name = dev.get("name", "")
        if name not in node_names:
            continue

        jobs = poll_by_device.get(name, {})

        # Compute poll health: green/yellow/red/gray
        if not jobs:
            health = "gray"
            health_label = "No polls configured"
        else:
            successes = sum(1 for j in jobs.values() if j.get("last_success"))
            failures = sum(1 for j in jobs.values() if j.get("last_error") and not j.get("last_success"))
            stale = sum(1 for j in jobs.values() if not j.get("last_success") and not j.get("last_error"))
            total = len(jobs)
            if successes == total:
                health = "green"
                health_label = "All polls healthy"
            elif failures == total or (failures > 0 and successes == 0):
                health = "red"
                health_label = f"{failures}/{total} job types failing"
            elif failures > 0 or stale > 0:
                health = "yellow"
                health_label = f"{successes}/{total} healthy, {failures} failing, {stale} pending"
            else:
                health = "gray"
                health_label = "No poll data yet"

        # Find latest poll timestamp across all job types
        last_polled = None
        for j in jobs.values():
            ts = j.get("last_success") or j.get("last_attempt")
            if ts and (last_polled is None or ts > last_polled):
                last_polled = ts

        # Extract platform and location info from Nautobot device
        platform = dev.get("platform") or {}
        platform_name = platform.get("display", "") if isinstance(platform, dict) else ""
        location = dev.get("location") or {}
        location_name = location.get("display", "") if isinstance(location, dict) else ""
        role = dev.get("role") or dev.get("device_role") or {}
        role_name = role.get("display", "") if isinstance(role, dict) else ""
        primary_ip = dev.get("primary_ip4") or dev.get("primary_ip") or {}
        ip_display = primary_ip.get("display", "") if isinstance(primary_ip, dict) else ""

        # Prompt 8: expose Nautobot ``status`` and derived Phase 2 state
        # so the Nodes UI can surface onboarding progress + Retry Phase 2
        # button without per-row /api/onboarding/phase2-status polling.
        status_obj = dev.get("status") or {}
        status_name = status_obj.get("display", "") if isinstance(status_obj, dict) else ""
        phase2 = await polling.get_phase2_state(name)
        phase2_state = phase2["state"] if phase2 else None

        nodes.append({
            "name": name,
            "id": dev.get("id", ""),
            "platform": platform_name,
            "primary_ip": ip_display,
            "role": role_name,
            "location": location_name,
            "status_name": status_name,
            "phase2_state": phase2_state,
            "health": health,
            "health_label": health_label,
            "last_polled": last_polled,
            "jobs": jobs,
            "interface_count": dev.get("interface_count"),
            "nautobot_url": dev.get("url", ""),
        })

    return {"nodes": nodes}


@app.get("/api/nodes/macs", dependencies=[Depends(require_auth)])
async def node_macs():
    """Return MAC addresses and IPs belonging to onboarded nodes.

    Used by the endpoints page to filter out infrastructure devices
    so that only passively-discovered endpoints are shown. Returns both
    MACs (from interfaces) and IPs (from primary_ip4) since Nautobot
    may not populate MAC addresses for all device interfaces.
    """
    poll_rows = await polling.get_all_poll_status()
    node_names = {r["device_name"] for r in poll_rows}
    if not node_names:
        return {"macs": [], "ips": []}

    macs = set()
    ips = set()
    try:
        devices = await nautobot_client.get_devices()
        for dev in devices:
            if dev.get("name") not in node_names:
                continue

            # Collect primary IP
            pip = dev.get("primary_ip4") or {}
            if isinstance(pip, dict):
                addr = pip.get("display") or pip.get("address") or ""
                host = addr.split("/")[0] if "/" in addr else addr
                if host:
                    ips.add(host)

            # Collect interface MACs
            dev_id = dev.get("id")
            if not dev_id:
                continue
            try:
                interfaces = await nautobot_client.get_interfaces(dev_id)
                for iface in interfaces:
                    mac = (iface.get("mac_address") or "").upper()
                    if mac and mac not in ("", "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                        macs.add(mac)
            except Exception:
                pass
    except Exception as exc:
        log.warning("node_identifiers_failed", "Could not fetch node identifiers",
                    context={"error": str(exc)})

    return {"macs": sorted(macs), "ips": sorted(ips)}


# -------------------------------------------------------------------------
# Investigations — unified network search (Phase 2.9)
# -------------------------------------------------------------------------

def _normalize_mac_query(raw: str) -> str:
    """Normalize any MAC format to uppercase colon-separated.

    Accepts: AA:BB:CC:DD:EE:FF, aa-bb-cc-dd-ee-ff, aabb.ccdd.eeff, aabbccddeeff
    Returns: AA:BB:CC:DD:EE:FF or empty string if not a valid MAC.
    """
    import re
    clean = re.sub(r"[^0-9a-fA-F]", "", raw)
    if len(clean) != 12:
        return ""
    return ":".join(clean[i:i+2] for i in range(0, 12, 2)).upper()


def _detect_query_type(q: str) -> str:
    """Auto-detect query type from input string."""
    import ipaddress as _ipaddress
    import re
    # MAC: any common format with 12 hex digits
    mac_clean = re.sub(r"[^0-9a-fA-F]", "", q)
    if len(mac_clean) == 12 and re.match(r"^[0-9a-fA-F]+$", mac_clean):
        # Verify it looks intentional (has separators, or exactly 12 hex chars)
        if (re.match(r'^([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}$', q)
                or re.match(r'^[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}$', q)
                or re.match(r'^[0-9a-fA-F]{12}$', q)):
            return "mac"
    # CIDR prefix
    if "/" in q:
        try:
            _ipaddress.ip_network(q, strict=False)
            return "prefix"
        except ValueError:
            pass
    # IP address
    try:
        _ipaddress.ip_address(q)
        return "ip"
    except ValueError:
        pass
    return "text"


@app.get("/api/investigate", dependencies=[Depends(require_auth)])
async def investigate(
    q: str = "",
    node: str = "",
    vlan: str = "",
    type: str = "",
):
    """Contextual network investigation. Auto-detects query type:
    MAC (any format) → MAC search, IP → IP search, CIDR → prefix search, else hostname/text.

    Optional filters: node (filter to specific node), vlan (filter to VLAN),
    type (force query type: mac, ip, prefix, text).
    """
    import ipaddress as _ipaddress
    from sqlalchemy import select

    q = q.strip()
    if not q:
        return {"query": q, "query_type": "empty", "results": {}}

    query_type = type if type in ("mac", "ip", "prefix", "text") else _detect_query_type(q)
    results: dict = {}
    node_filter = node.strip() if node else ""
    vlan_filter = int(vlan) if vlan and vlan.isdigit() else None

    if query_type == "mac":
        mac_upper = _normalize_mac_query(q)
        if not mac_upper:
            mac_upper = q.upper()

        if db.is_ready():
            async with db.SessionLocal() as session:
                # ARP table hits
                arp_stmt = select(db.NodeArpEntry).where(db.NodeArpEntry.mac == mac_upper)
                if node_filter:
                    arp_stmt = arp_stmt.where(db.NodeArpEntry.node_name == node_filter)
                arp_rows = (await session.execute(arp_stmt)).scalars().all()
                results["arp_hits"] = [r.to_dict() for r in arp_rows]

                # MAC table hits
                mac_stmt = select(db.NodeMacEntry).where(db.NodeMacEntry.mac == mac_upper)
                if node_filter:
                    mac_stmt = mac_stmt.where(db.NodeMacEntry.node_name == node_filter)
                if vlan_filter is not None:
                    mac_stmt = mac_stmt.where(db.NodeMacEntry.vlan == vlan_filter)
                mac_rows = (await session.execute(mac_stmt)).scalars().all()
                results["mac_hits"] = [r.to_dict() for r in mac_rows]

                # Access port location — MAC table entries on non-uplink ports
                access_entries = [m for m in results["mac_hits"]
                                  if not _is_uplink_interface(m.get("interface", ""))]
                if access_entries:
                    loc = access_entries[0]
                    results["location"] = {
                        "switch": loc["node_name"],
                        "interface": loc["interface"],
                        "vlan": loc["vlan"],
                        "description": f"Connected to {loc['node_name']} port {loc['interface']} in VLAN {loc['vlan']}",
                    }

        # Endpoint record
        ep = await endpoint_store.get_endpoint(q if not _normalize_mac_query(q) else mac_upper)
        if ep:
            results["endpoint"] = ep

        # VM host lookup from Proxmox
        pxstate = proxmox_connector.get_state()
        if pxstate.get("configured"):
            for vm in pxstate.get("vms", []) + pxstate.get("containers", []):
                vm_mac = (vm.get("mac") or "").upper()
                if vm_mac and vm_mac == mac_upper:
                    results["vm_host"] = {
                        "name": vm.get("name", ""),
                        "vmid": vm.get("vmid"),
                        "node": vm.get("node", ""),
                        "status": vm.get("status", ""),
                        "type": vm.get("type", "qemu"),
                    }
                    break

    elif query_type == "ip":
        if db.is_ready():
            async with db.SessionLocal() as session:
                # ARP table hits
                arp_stmt = select(db.NodeArpEntry).where(db.NodeArpEntry.ip == q)
                if node_filter:
                    arp_stmt = arp_stmt.where(db.NodeArpEntry.node_name == node_filter)
                arp_rows = (await session.execute(arp_stmt)).scalars().all()
                results["arp_hits"] = [r.to_dict() for r in arp_rows]

                # Routing table hits — longest prefix match
                route_rows = (await session.execute(select(db.Route))).scalars().all()
                matching_routes = []
                try:
                    target = _ipaddress.ip_address(q)
                    for r in route_rows:
                        if node_filter and r.node_name != node_filter:
                            continue
                        try:
                            net = _ipaddress.ip_network(r.prefix, strict=False)
                            if target in net:
                                matching_routes.append(r.to_dict())
                        except ValueError:
                            continue
                except ValueError:
                    pass
                # Sort by prefix length (longest match first)
                matching_routes.sort(key=lambda r: int(r["prefix"].split("/")[1]) if "/" in r["prefix"] else 32, reverse=True)
                results["routes"] = matching_routes

                # FIB hits
                fib_rows = (await session.execute(select(db.NodeFibEntry))).scalars().all()
                matching_fib = []
                try:
                    target = _ipaddress.ip_address(q)
                    for f in fib_rows:
                        if node_filter and f.node_name != node_filter:
                            continue
                        try:
                            net = _ipaddress.ip_network(f.prefix, strict=False)
                            if target in net:
                                matching_fib.append(f.to_dict())
                        except ValueError:
                            continue
                except ValueError:
                    pass
                matching_fib.sort(key=lambda r: int(r["prefix"].split("/")[1]) if "/" in r["prefix"] else 32, reverse=True)
                if matching_fib:
                    results["fib"] = matching_fib

        # Endpoint record
        endpoints = await endpoint_store.list_endpoints()
        matching = [e for e in endpoints if e.get("ip") == q or q in (e.get("additional_ips") or [])]
        if matching:
            results["endpoints"] = matching

    elif query_type == "prefix":
        try:
            network = _ipaddress.ip_network(q, strict=False)
        except ValueError:
            return {"query": q, "query_type": query_type, "results": {}}

        if db.is_ready():
            async with db.SessionLocal() as session:
                # Routing table — exact and containing matches
                route_rows = (await session.execute(select(db.Route))).scalars().all()
                matching_routes = []
                for r in route_rows:
                    if node_filter and r.node_name != node_filter:
                        continue
                    try:
                        route_net = _ipaddress.ip_network(r.prefix, strict=False)
                        # Include routes that overlap with the query prefix
                        if route_net.overlaps(network):
                            matching_routes.append(r.to_dict())
                    except ValueError:
                        continue
                results["routes"] = matching_routes

                # FIB entries
                fib_rows = (await session.execute(select(db.NodeFibEntry))).scalars().all()
                matching_fib = []
                for f in fib_rows:
                    if node_filter and f.node_name != node_filter:
                        continue
                    try:
                        fib_net = _ipaddress.ip_network(f.prefix, strict=False)
                        if fib_net.overlaps(network):
                            matching_fib.append(f.to_dict())
                    except ValueError:
                        continue
                if matching_fib:
                    results["fib"] = matching_fib

                # Gateway identification — connected routes or lowest metric
                gateways = []
                for r in matching_routes:
                    if r.get("protocol") in ("local", "connected") or r.get("next_hop") in ("", "0.0.0.0"):
                        gateways.append(r)
                if not gateways and matching_routes:
                    by_metric = sorted(matching_routes, key=lambda r: r.get("metric") or 999999)
                    gateways = [by_metric[0]]
                if gateways:
                    results["gateways"] = gateways

                # ARP entries within the subnet
                arp_rows = (await session.execute(select(db.NodeArpEntry))).scalars().all()
                matching_arp = []
                for a in arp_rows:
                    if node_filter and a.node_name != node_filter:
                        continue
                    try:
                        if _ipaddress.ip_address(a.ip) in network:
                            matching_arp.append(a.to_dict())
                    except ValueError:
                        continue
                if matching_arp:
                    results["arp_hits"] = matching_arp

    else:
        # Text search — hostname, LLDP system names, DHCP hostnames
        q_lower = q.lower()
        if db.is_ready():
            async with db.SessionLocal() as session:
                lldp_stmt = select(db.NodeLldpEntry).where(
                    db.NodeLldpEntry.remote_system_name.ilike(f"%{q}%"))
                if node_filter:
                    lldp_stmt = lldp_stmt.where(db.NodeLldpEntry.node_name == node_filter)
                lldp_rows = (await session.execute(lldp_stmt)).scalars().all()
                if lldp_rows:
                    results["lldp"] = [r.to_dict() for r in lldp_rows]

        endpoints = await endpoint_store.list_endpoints()
        matching = [e for e in endpoints
                    if q_lower in (e.get("hostname") or "").lower()
                    or q_lower in (e.get("device_name") or "").lower()]
        if matching:
            results["endpoints"] = matching

    return {"query": q, "query_type": query_type, "results": results}


def _is_uplink_interface(name: str) -> bool:
    """Quick heuristic: return True if interface name looks like a trunk/uplink."""
    lower = name.lower()
    for prefix in ("ae", "irb", "lo", "vlan", "me", "em", "fxp", "bme",
                    "port-channel", "loopback", "nve", "sup-eth", "vtep"):
        if lower.startswith(prefix):
            return True
    return False


# -------------------------------------------------------------------------
# Per-node data tables (Phase 2.85)
# -------------------------------------------------------------------------

@app.get("/api/nodes/{node_name}/arp", dependencies=[Depends(require_auth)])
async def node_arp_table(node_name: str, ip: str | None = None, mac: str | None = None):
    """ARP table entries for a specific node."""
    entries = await endpoint_store.list_node_arp(node_name, ip=ip, mac=mac)
    return {"entries": entries, "count": len(entries), "node_name": node_name}


@app.get("/api/nodes/{node_name}/mac-table", dependencies=[Depends(require_auth)])
async def node_mac_table(node_name: str, mac: str | None = None,
                         interface: str | None = None, vlan: int | None = None):
    """MAC address table entries for a specific node."""
    entries = await endpoint_store.list_node_mac(node_name, mac=mac, interface=interface, vlan=vlan)
    return {"entries": entries, "count": len(entries), "node_name": node_name}


@app.get("/api/nodes/{node_name}/fib", dependencies=[Depends(require_auth)])
async def node_fib_table(node_name: str, prefix: str | None = None):
    """Forwarding table (FIB) entries for a specific node."""
    entries = await endpoint_store.list_node_fib(node_name, prefix=prefix)
    return {"entries": entries, "count": len(entries), "node_name": node_name}


@app.get("/api/nodes/{node_name}/lldp", dependencies=[Depends(require_auth)])
async def node_lldp_table(node_name: str):
    """LLDP neighbor entries for a specific node."""
    entries = await endpoint_store.list_node_lldp(node_name)
    return {"entries": entries, "count": len(entries), "node_name": node_name}


# -------------------------------------------------------------------------
# Node detail: info, interfaces, health (Phase 2.9 Part 6)
# -------------------------------------------------------------------------

_node_info_cache: dict[str, tuple[float, dict]] = {}
_node_iface_cache: dict[str, tuple[float, list]] = {}
_node_counters_cache: dict[str, tuple[float, dict]] = {}
_NODE_INFO_TTL = 300  # 5 minutes
_NODE_IFACE_TTL = 300  # 5 minutes — match info TTL so warmup keeps both hot
_NODE_COUNTERS_TTL = 300


@app.get("/api/nodes/{node_name}/info", dependencies=[Depends(require_auth)])
async def get_node_info(node_name: str):
    """Device info from NAPALM get_facts + Nautobot device data. Cached 5 min."""
    now = time.time()
    cached = _node_info_cache.get(node_name)
    if cached and now - cached[0] < _NODE_INFO_TTL:
        return cached[1]

    devices = await nautobot_client.get_devices()
    device = next((d for d in devices if d.get("name") == node_name), None)
    if not device:
        raise HTTPException(status_code=404, detail="Node not found")

    facts: dict = {}
    try:
        data = await nautobot_client.napalm_get(device["id"], "get_facts")
        facts = data.get("get_facts", {})
    except Exception as e:
        log.warning("node_info_facts_failed", "get_facts failed",
                    context={"node": node_name, "error": str(e)[:200]})

    iface_data = await _get_raw_interfaces(device["id"], node_name)

    uptime_secs = facts.get("uptime", 0) or 0
    up_days = int(uptime_secs // 86400)
    up_hours = int((uptime_secs % 86400) // 3600)
    uptime_str = f"{up_days}d {up_hours}h" if up_days else f"{up_hours}h {int((uptime_secs % 3600) // 60)}m"

    total_ifaces = len(iface_data)
    up_ifaces = sum(1 for v in iface_data.values() if v.get("is_up"))

    pip = (device.get("primary_ip4") or {})
    pip_display = pip.get("display") or pip.get("address") or ""

    result = {
        "name": node_name,
        "vendor": facts.get("vendor", ""),
        "model": facts.get("model", ""),
        "serial_number": facts.get("serial_number", ""),
        "os_version": facts.get("os_version", ""),
        "hostname": facts.get("hostname", ""),
        "fqdn": facts.get("fqdn", ""),
        "uptime_seconds": uptime_secs,
        "uptime": uptime_str,
        "interface_count": total_ifaces,
        "interfaces_up": up_ifaces,
        "primary_ip": pip_display,
        "role": ((device.get("role") or device.get("device_role") or {}).get("display") or ""),
        "location": ((device.get("location") or {}).get("display") or ""),
        "platform": ((device.get("platform") or {}).get("display") or ""),
        "device_id": device.get("id", ""),
    }
    _node_info_cache[node_name] = (now, result)
    return result


# Shared raw interface data cache (used by both /info and /interfaces)
_raw_iface_cache: dict[str, tuple[float, dict]] = {}
_RAW_IFACE_TTL = 120


async def _get_raw_interfaces(device_id: str, node_name: str) -> dict:
    """Get raw NAPALM get_interfaces data with caching."""
    now = time.time()
    cached = _raw_iface_cache.get(node_name)
    if cached and now - cached[0] < _RAW_IFACE_TTL:
        return cached[1]
    try:
        data = await nautobot_client.napalm_get(device_id, "get_interfaces")
        result = data.get("get_interfaces", {})
    except Exception as e:
        log.warning("node_ifaces_failed", "get_interfaces failed",
                    context={"node": node_name, "error": str(e)[:200]})
        result = {}
    _raw_iface_cache[node_name] = (now, result)
    return result


def _build_iface_list(iface_data: dict, counters: dict) -> list:
    """Merge get_interfaces + get_interfaces_counters into a flat list."""
    result = []
    for name in sorted(iface_data.keys()):
        ifc = iface_data[name]
        ctr = counters.get(name, {})

        errors_in = max(ctr.get("rx_errors", 0), 0)
        errors_out = max(ctr.get("tx_errors", 0), 0)
        discards_in = max(ctr.get("rx_discards", 0), 0)
        discards_out = max(ctr.get("tx_discards", 0), 0)
        octets_in = max(ctr.get("rx_octets", 0), 0)
        octets_out = max(ctr.get("tx_octets", 0), 0)

        is_up = ifc.get("is_up", False)
        is_enabled = ifc.get("is_enabled", True)

        if not is_enabled:
            health = "gray"
        elif not is_up:
            health = "red"
        elif (errors_in + errors_out) > 0:
            health = "red"
        elif (discards_in + discards_out) > 0:
            health = "yellow"
        else:
            health = "green"

        speed = ifc.get("speed", 0) or 0
        if speed >= 1000:
            speed_str = f"{int(speed / 1000)}G"
        elif speed > 0:
            speed_str = f"{int(speed)}M"
        else:
            speed_str = ""

        result.append({
            "name": name,
            "is_up": is_up,
            "is_enabled": is_enabled,
            "health": health,
            "description": ifc.get("description", ""),
            "speed": speed,
            "speed_display": speed_str,
            "mtu": ifc.get("mtu", 0),
            "mac_address": ifc.get("mac_address", ""),
            "last_flapped": ifc.get("last_flapped"),
            "errors_in": errors_in,
            "errors_out": errors_out,
            "discards_in": discards_in,
            "discards_out": discards_out,
            "octets_in": octets_in,
            "octets_out": octets_out,
        })
    return result


@app.get("/api/nodes/{node_name}/interfaces", dependencies=[Depends(require_auth)])
async def get_node_interfaces(node_name: str):
    """Interface details from NAPALM. Returns immediately from cache if
    available. Counters are included when cached but never block the response —
    the warmup loop fetches them in the background."""
    now = time.time()
    cached = _node_iface_cache.get(node_name)
    if cached and now - cached[0] < _NODE_IFACE_TTL:
        return cached[1]

    devices = await nautobot_client.get_devices()
    device = next((d for d in devices if d.get("name") == node_name), None)
    if not device:
        raise HTTPException(status_code=404, detail="Node not found")

    # Get interface status from shared cache. If cache is empty, kick off
    # a background fetch and return immediately so the page doesn't block.
    raw_cached = _raw_iface_cache.get(node_name)
    if raw_cached and time.time() - raw_cached[0] < _RAW_IFACE_TTL:
        iface_data = raw_cached[1]
    else:
        # Not cached — trigger background warmup, return empty with retry hint
        async def _bg_fetch():
            try:
                await _get_raw_interfaces(device["id"], node_name)
            except Exception:
                pass
        asyncio.create_task(_bg_fetch())
        return JSONResponse(content=[], headers={"X-MNM-Retry-After": "5"})

    # Use cached counters if available, otherwise return without them.
    # The warmup loop fetches counters in the background — never block here.
    counters: dict = {}
    cc = _node_counters_cache.get(node_name)
    if cc and now - cc[0] < _NODE_COUNTERS_TTL:
        counters = cc[1]

    result = _build_iface_list(iface_data, counters)
    _node_iface_cache[node_name] = (now, result)
    return result


@app.get("/api/nodes/{node_name}/health", dependencies=[Depends(require_auth)])
async def get_node_health(node_name: str):
    """Combined poll health + interface health summary."""
    poll_rows = await polling.get_device_poll_status(node_name)
    jobs = {r["job_type"]: r for r in poll_rows} if poll_rows else {}

    ifaces = []
    try:
        ifaces = await get_node_interfaces(node_name)
    except Exception:
        pass

    total = len(ifaces)
    up = sum(1 for i in ifaces if i.get("is_up"))
    down = sum(1 for i in ifaces if i.get("is_enabled") and not i.get("is_up"))
    admin_down = sum(1 for i in ifaces if not i.get("is_enabled"))
    with_errors = sum(1 for i in ifaces if (i.get("errors_in", 0) + i.get("errors_out", 0)) > 0)
    with_discards = sum(1 for i in ifaces if (i.get("discards_in", 0) + i.get("discards_out", 0)) > 0)

    return {
        "node_name": node_name,
        "jobs": jobs,
        "interface_health": {
            "total": total,
            "up": up,
            "down": down,
            "admin_down": admin_down,
            "with_errors": with_errors,
            "with_discards": with_discards,
        },
    }


@app.get("/api/nodes/{node_name}", dependencies=[Depends(require_auth)])
async def get_node(node_name: str):
    """Get details for a single node including poll status."""
    poll_rows = await polling.get_device_poll_status(node_name)
    if not poll_rows:
        raise HTTPException(status_code=404, detail="Node not found in poll tracking")

    # Try to find the device in Nautobot
    try:
        devices = await nautobot_client.get_devices()
        device = next((d for d in devices if d.get("name") == node_name), None)
    except Exception:
        device = None

    return {
        "node_name": node_name,
        "jobs": {r["job_type"]: r for r in poll_rows},
        "device": device,
    }



# -------------------------------------------------------------------------
# Routes — routing table data collected from nodes
# -------------------------------------------------------------------------

@app.get("/api/routes", dependencies=[Depends(require_auth)])
async def get_routes(
    node_name: str | None = None,
    vrf: str | None = None,
    protocol: str | None = None,
    prefix: str | None = None,
):
    """Query collected routing table entries.

    Routes are stored in the controller database (Nautobot has no native
    routing model). Collected by the modular polling engine via NAPALM.
    """
    routes = await endpoint_store.list_routes(
        node_name=node_name, vrf=vrf, protocol=protocol, prefix_search=prefix,
    )
    return {"routes": routes, "count": len(routes)}


@app.get("/api/routes/advisories", dependencies=[Depends(require_auth)])
async def route_advisories():
    """Routes with next-hops that don't match any known IP.

    These are discovery candidates — the node knows about a next-hop
    gateway that MNM hasn't seen in endpoint data or Nautobot IPAM.
    """
    # Build known IP set from endpoints + Nautobot IPAM
    known_ips: set[str] = set()
    try:
        endpoints = await endpoint_store.list_endpoints()
        for ep in endpoints:
            ip = ep.get("ip") or ep.get("current_ip")
            if ip:
                known_ips.add(ip)
            for aip in (ep.get("additional_ips") or []):
                if aip:
                    known_ips.add(aip)
    except Exception:
        pass
    try:
        ips = await nautobot_client.get_ip_addresses()
        for ipo in ips:
            addr = ipo.get("address") or ipo.get("display") or ""
            host = addr.split("/")[0] if "/" in addr else addr
            if host:
                known_ips.add(host)
    except Exception:
        pass

    advisories = await endpoint_store.route_advisories(known_ips)
    return {"advisories": advisories, "count": len(advisories)}


@app.get("/api/routes/{node_name}", dependencies=[Depends(require_auth)])
async def get_routes_for_node(
    node_name: str,
    vrf: str | None = None,
    protocol: str | None = None,
):
    """All routes for a specific node."""
    routes = await endpoint_store.list_routes(node_name=node_name, vrf=vrf, protocol=protocol)
    return {"routes": routes, "count": len(routes), "node_name": node_name}


# -------------------------------------------------------------------------
# BGP — BGP neighbor state collected from nodes
# -------------------------------------------------------------------------

@app.get("/api/bgp", dependencies=[Depends(require_auth)])
async def get_bgp_neighbors(
    node_name: str | None = None,
    state: str | None = None,
    vrf: str | None = None,
):
    """Query collected BGP neighbor entries."""
    neighbors = await endpoint_store.list_bgp_neighbors(
        node_name=node_name, state=state, vrf=vrf,
    )
    return {"neighbors": neighbors, "count": len(neighbors)}


@app.get("/api/bgp/{node_name}", dependencies=[Depends(require_auth)])
async def get_bgp_for_node(node_name: str, vrf: str | None = None):
    """All BGP neighbors for a specific node."""
    neighbors = await endpoint_store.list_bgp_neighbors(node_name=node_name, vrf=vrf)
    return {"neighbors": neighbors, "count": len(neighbors), "node_name": node_name}


# -------------------------------------------------------------------------
# Jobs — consolidated background task view
# -------------------------------------------------------------------------

@app.get("/api/jobs", dependencies=[Depends(require_auth)])
async def list_jobs():
    """Consolidated view of all background tasks for the Jobs page."""
    from datetime import datetime, timezone
    config = await load_config_async()

    # --- Sweep Scheduler ---
    sweep = discovery.get_sweep_state()
    schedules = config.get("sweep_schedules", [])
    sweep_interval_hours = (
        min((s.get("interval_hours", 24) for s in schedules), default=24)
        if schedules else None
    )
    sweep_last_run = sweep.get("finished_at") or (
        max((s.get("last_run", "") for s in schedules), default=None)
        if schedules else None
    )

    # --- Proxmox Collector ---
    px = proxmox_connector.get_state()
    px_interval = proxmox_connector.PROXMOX_INTERVAL_SECONDS

    # --- Database Prune ---
    prune_interval = _prune_interval_hours()

    jobs = [
        {
            "id": "sweep",
            "name": "Sweep Scheduler",
            "description": "Network discovery sweep across configured CIDR ranges",
            "status": "running" if sweep.get("running") else "idle",
            "running": sweep.get("running", False),
            "schedule_interval": f"{sweep_interval_hours}h" if sweep_interval_hours else None,
            "schedule_seconds": sweep_interval_hours * 3600 if sweep_interval_hours else None,
            "last_run": sweep_last_run,
            "duration_seconds": sweep.get("duration_seconds"),
            "summary": sweep.get("summary"),
            "error": None,
            "trigger_url": "/api/discover/sweep-scheduled",
            "schedule_count": len(schedules),
            "enabled": len(schedules) > 0,
        },
        {
            "id": "proxmox_collector",
            "name": "Proxmox Collector",
            "description": "Collect VM/container inventory from Proxmox VE",
            "status": "running" if px.get("running") else (
                "error" if px.get("last_error") else "idle"
            ),
            "running": px.get("running", False),
            "schedule_interval": f"{px_interval // 60}m",
            "schedule_seconds": px_interval,
            "last_run": px.get("last_run"),
            "duration_seconds": px.get("duration_seconds"),
            "summary": {
                "nodes": px.get("node_count", 0),
                "vms": px.get("vm_count", 0),
                "containers": px.get("container_count", 0),
            },
            "error": px.get("last_error"),
            "trigger_url": "/api/proxmox/collect",
            "enabled": px.get("configured", False),
        },
        {
            "id": "modular_poller",
            "name": "Modular Poller",
            "description": "Per-device ARP/MAC/DHCP/LLDP collection on independent schedules",
            "status": "running" if polling.get_poll_state().get("running") else "idle",
            "running": polling.get_poll_state().get("running", False),
            "schedule_interval": f"{polling.POLL_CHECK_INTERVAL}s check",
            "schedule_seconds": polling.POLL_CHECK_INTERVAL,
            "last_run": polling.get_poll_state().get("last_check"),
            "duration_seconds": None,
            "summary": {"dispatched": polling.get_poll_state().get("polls_dispatched", 0)},
            "error": None,
            "trigger_url": "/api/endpoints/collect",
            "enabled": True,
        },
        {
            "id": "db_prune",
            "name": "Database Prune",
            "description": f"Evict data older than {_retention_days()} days",
            "status": "running" if _prune_state["running"] else (
                "error" if _prune_state["last_error"] else "idle"
            ),
            "running": _prune_state["running"],
            "schedule_interval": f"{prune_interval}h",
            "schedule_seconds": prune_interval * 3600,
            "last_run": _prune_state["last_run"],
            "duration_seconds": None,
            "summary": _prune_state["last_summary"],
            "error": _prune_state["last_error"],
            "trigger_url": "/api/admin/prune",
            "enabled": True,
        },
    ]
    return {"jobs": jobs}


@app.post("/api/discover/sweep-scheduled", dependencies=[Depends(require_auth)])
async def trigger_scheduled_sweep():
    """Re-run the first saved sweep schedule. Used by the Jobs page Run Now."""
    state = discovery.get_sweep_state()
    if state["running"]:
        raise HTTPException(status_code=409, detail="Sweep already running")
    config = await load_config_async()
    schedules = config.get("sweep_schedules", [])
    if not schedules:
        raise HTTPException(
            status_code=404,
            detail="No sweep schedules configured. Set one up on the Discovery page first.",
        )
    s = schedules[0]
    asyncio.create_task(discovery.sweep(
        s["cidr_ranges"],
        s["location_id"],
        s["secrets_group_id"],
        snmp_community=s.get("snmp_community", ""),
    ))
    return {"status": "started", "cidr_ranges": s["cidr_ranges"]}


app.mount("/static", StaticFiles(directory="app/static"), name="static")
