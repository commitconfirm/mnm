"""MNM Controller — FastAPI application."""

import asyncio
import hashlib
import hmac
import os
import platform
import secrets
import sys
import time

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.logging_config import StructuredLogger, setup_logging, get_recent_logs

# Initialize structured logging before anything else
setup_logging()

from app import db, discovery, docker_manager, endpoint_collector, endpoint_store, nautobot_client
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

    # Launch background tasks
    asyncio.create_task(discovery.scheduled_sweep_loop())
    asyncio.create_task(endpoint_collector.scheduled_collection_loop())
    asyncio.create_task(proxmox_connector.scheduled_loop())
    asyncio.create_task(_scheduled_prune_loop())
    log.info("background_tasks",
             "Background tasks launched: sweep scheduler, endpoint collector, proxmox connector, prune loop",
             context={"proxmox_configured": proxmox_connector.is_configured(),
                      "retention_days": _retention_days(),
                      "prune_interval_hours": _prune_interval_hours()})


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

    ep_state = endpoint_collector.get_collection_state()
    sweep_state = discovery.get_sweep_state()
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
        "last_collection": ep_state.get("last_run"),
        "endpoints_tracked": ep_count,
        "log_level": os.environ.get("MNM_LOG_LEVEL", "INFO"),
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
        )
    )
    return {"status": "started"}


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


@app.post("/api/discover/onboard", dependencies=[Depends(require_auth)])
async def onboard_single(body: OnboardRequest):
    """Onboard a single device (e.g., from LLDP neighbor advisory)."""
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


@app.post("/api/nautobot/sync-network-data", dependencies=[Depends(require_auth)])
async def sync_network_data(body: SyncNetworkDataRequest):
    """Trigger 'Sync Network Data From Network' job for specified or all devices."""
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
    coll = endpoint_collector.get_collection_state()
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
        "last_collection": {
            "running": coll.get("running"),
            "last_run": coll.get("last_run"),
            "summary": coll.get("summary"),
            "errors": coll.get("errors", [])[-50:],
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
    state = endpoint_collector.get_collection_state()
    endpoints = await _all_endpoints()

    vlans = {e.get("vlan") for e in endpoints if e.get("vlan")}
    vendors = {e.get("mac_vendor") for e in endpoints if e.get("mac_vendor")}
    switches = {e.get("device_name") for e in endpoints if e.get("device_name")}

    return {
        "total_endpoints": len(endpoints),
        "vlans_active": len(vlans),
        "vendors_seen": len(vendors),
        "switches": len(switches),
        "last_collection": state.get("last_run"),
        "running": state.get("running", False),
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
    """Return collection progress for the progress bar."""
    return endpoint_collector.get_collection_state()


@app.post("/api/endpoints/collect", dependencies=[Depends(require_auth)])
async def trigger_collection():
    """Manually trigger an endpoint collection run."""
    state = endpoint_collector.get_collection_state()
    if state.get("running"):
        raise HTTPException(status_code=409, detail="Collection already running")

    asyncio.create_task(endpoint_collector.collect_all())
    return {"status": "started"}


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


app.mount("/static", StaticFiles(directory="app/static"), name="static")
