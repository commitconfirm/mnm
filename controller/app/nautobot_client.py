"""Async HTTP client for Nautobot REST API.

Uses a shared httpx.AsyncClient for connection pooling across all API calls.
Frequently-read reference data (devices, statuses, namespaces) is cached with
a short TTL to avoid redundant round-trips during polling and correlation.
"""

import asyncio
import os
import re
import time
from typing import Any

import docker
import httpx

from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="nautobot")

NAUTOBOT_URL = os.environ.get("NAUTOBOT_URL", "http://nautobot:8080")
_api_token: str | None = None


# ---------------------------------------------------------------------------
# Exception hierarchy — consumed by the direct-REST onboarding orchestrator
# (Prompt 4). See .claude/design/nautobot_rest_schema_notes.md §4 for the
# reality-check findings each exception maps to.
# ---------------------------------------------------------------------------

class NautobotError(Exception):
    """Base class for Nautobot REST errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: "dict | str | None" = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class NautobotDuplicateError(NautobotError):
    """400 for a uniqueness-constraint violation (reality-check Test 5, Test 6)."""


class NautobotValidationError(NautobotError):
    """400 for a field-validation failure that is NOT a duplicate.

    Includes Nautobot's rejection of ``primary_ip4`` when the target IP is not
    linked to any interface on the device (reality-check Test 2).
    """


class NautobotNotFoundError(NautobotError):
    """404 for missing resource on GET / PATCH / DELETE."""


def _classify_400(body: "dict | str") -> type[NautobotError]:
    """Pick the right exception class for a 400 response body.

    Reality-check findings:
      - Test 5 duplicate device: ``{"__all__": ["A device named ... already
        exists in this location ..."]}``
      - Test 6 duplicate interface: ``{"non_field_errors": ["The fields
        device, name must make a unique set."]}``
    Any other 400 shape is a validation error.
    """
    if isinstance(body, dict):
        flat = " ".join(
            str(v) if not isinstance(v, list) else " ".join(str(x) for x in v)
            for v in body.values()
        ).lower()
    else:
        flat = str(body).lower()
    if "already exists" in flat or "must make a unique set" in flat:
        return NautobotDuplicateError
    return NautobotValidationError


def _raise_for_write(resp: "httpx.Response", *, operation: str) -> None:
    """Translate a write response into the typed exception hierarchy.

    Any 2xx is accepted silently; 400 is classified via ``_classify_400``;
    404 becomes :class:`NautobotNotFoundError`; other 4xx/5xx become the
    generic :class:`NautobotError`. ``operation`` is folded into the
    message for log correlation (e.g. ``"create_device"``).
    """
    if 200 <= resp.status_code < 300:
        return
    try:
        body: "dict | str" = resp.json()
    except Exception:
        body = resp.text
    if resp.status_code == 400:
        raise _classify_400(body)(
            f"{operation} rejected by Nautobot (400)",
            status_code=400,
            response_body=body,
        )
    if resp.status_code == 404:
        raise NautobotNotFoundError(
            f"{operation} target not found (404)",
            status_code=404,
            response_body=body,
        )
    raise NautobotError(
        f"{operation} failed ({resp.status_code})",
        status_code=resp.status_code,
        response_body=body,
    )

# ---------------------------------------------------------------------------
# Shared HTTP client — connection pooling across all Nautobot API calls
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


_DEFAULT_TIMEOUT = httpx.Timeout(connect=30, read=120, write=30, pool=10)
_NAPALM_TIMEOUT = httpx.Timeout(connect=30, read=180, write=30, pool=10)


def _get_client() -> httpx.AsyncClient:
    """Return the shared httpx client, creating it lazily on first use."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=_DEFAULT_TIMEOUT)
    return _client


async def close_client() -> None:
    """Close the shared httpx client. Call on app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.close()
        _client = None


# ---------------------------------------------------------------------------
# Response cache — short TTL for reference data that rarely changes
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 30.0  # seconds


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is not None:
        ts, value = entry
        if time.monotonic() - ts < _CACHE_TTL:
            return value
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    """Clear all cached responses. Call after data-mutating operations."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_token() -> str:
    """Get or create an API token via docker exec into the nautobot container."""
    global _api_token
    if _api_token:
        return _api_token

    admin_user = os.environ.get("MNM_ADMIN_USER", "mnm-admin")

    try:
        client = docker.from_env()
        container = client.containers.get("mnm-nautobot")
        result = container.exec_run(
            ["bash", "-c", f'''echo "
from nautobot.users.models import Token
from django.contrib.auth import get_user_model
User = get_user_model()
user = User.objects.get(username=\\"{admin_user}\\")
token, _ = Token.objects.get_or_create(user=user)
import sys; sys.stderr.write(token.key + chr(10))
" | nautobot-server nbshell'''],
            stderr=True,
        )
        output = result.output.decode("utf-8", errors="replace")
        match = re.search(r"[0-9a-f]{40}", output)
        if match:
            _api_token = match.group(0)
            log.info("token_obtained", "Obtained Nautobot API token via docker exec")
            return _api_token
    except Exception as e:
        log.error("token_failed", "Docker exec token fetch failed", context={"error": str(e)}, exc_info=True)

    raise RuntimeError("Failed to get Nautobot API token")


def _headers() -> dict:
    return {
        "Authorization": f"Token {_get_token()}",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Device / inventory queries
# ---------------------------------------------------------------------------

async def get_devices() -> list[dict]:
    cached = _cache_get("devices")
    if cached is not None:
        return cached
    t0 = time.monotonic()
    client = _get_client()
    # depth=1 ensures nested objects (primary_ip4, platform, location, role)
    # include display/address fields, not just id/url references.
    resp = await client.get("/api/dcim/devices/?limit=1000&depth=1", headers=_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])
    log.debug("api_call", "GET devices", context={"path": "/api/dcim/devices/", "status": resp.status_code, "count": len(results), "duration_ms": round((time.monotonic() - t0) * 1000)})
    _cache_set("devices", results)
    return results


async def get_device(device_id: str) -> dict:
    client = _get_client()
    resp = await client.get(f"/api/dcim/devices/{device_id}/", headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def get_interfaces(device_id: str) -> list[dict]:
    client = _get_client()
    resp = await client.get(
        f"/api/dcim/interfaces/?device_id={device_id}&limit=1000",
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


async def get_device_interface_subnets() -> set[str]:
    """Return the set of /24 (v4) or /64 (v6) subnets that contain IPs
    assigned to onboarded device interfaces.

    These are subnets the operator has implicitly approved by onboarding
    the devices — they should be included in periodic sweeps automatically.
    """
    import ipaddress as _ipaddress
    subnets: set[str] = set()
    devices = await get_devices()
    client = _get_client()
    for dev in devices:
        dev_id = dev.get("id")
        if not dev_id:
            continue
        ifaces = await get_interfaces(dev_id)
        for ifc in ifaces:
            ifc_id = ifc.get("id")
            if not ifc_id:
                continue
            resp = await client.get(
                f"/api/ipam/ip-addresses/?interfaces={ifc_id}&limit=50",
                headers=_headers(),
            )
            if resp.status_code != 200:
                continue
            for ip_obj in resp.json().get("results", []):
                display = ip_obj.get("display") or ip_obj.get("address") or ""
                host = display.split("/")[0] if "/" in display else display
                if not host:
                    continue
                try:
                    addr = _ipaddress.ip_address(host)
                    prefix_len = 24 if addr.version == 4 else 64
                    net = _ipaddress.ip_network(f"{host}/{prefix_len}", strict=False)
                    subnets.add(str(net))
                except ValueError:
                    continue
    return subnets


async def get_secrets_groups() -> list[dict]:
    client = _get_client()
    resp = await client.get("/api/extras/secrets-groups/", headers=_headers())
    resp.raise_for_status()
    return resp.json().get("results", [])


async def get_locations() -> list[dict]:
    """Return only locations whose location_type accepts dcim.device.

    Nautobot 3.x rejects device creation against location types that don't
    list ``dcim | device`` in their content types (e.g. a Region). The
    onboarding job fails with a ValidationError that the plugin then
    swallows into a generic OnboardException, leaving JobResult stuck in
    PENDING. Filtering here means the UI/API only ever surfaces locations
    that can actually hold a device.
    """
    client = _get_client()
    resp = await client.get("/api/dcim/locations/?limit=100", headers=_headers())
    resp.raise_for_status()
    locations = resp.json().get("results", [])

    # Build a map of location-type id -> set of content type strings
    types_resp = await client.get(
        "/api/dcim/location-types/?limit=100", headers=_headers()
    )
    types_resp.raise_for_status()
    type_accepts_device: dict[str, bool] = {}
    for lt in types_resp.json().get("results", []):
        cts = lt.get("content_types") or []
        type_accepts_device[lt.get("id")] = any(
            (isinstance(c, str) and c == "dcim.device")
            or (isinstance(c, dict) and c.get("app_label") == "dcim" and c.get("model") == "device")
            for c in cts
        )

    def _accepts(loc: dict) -> bool:
        lt = loc.get("location_type") or {}
        lt_id = lt.get("id") if isinstance(lt, dict) else lt
        return type_accepts_device.get(lt_id, False)

    return [loc for loc in locations if _accepts(loc)]


async def get_cables() -> list[dict]:
    """Get cable connections to identify LLDP neighbors."""
    client = _get_client()
    resp = await client.get("/api/dcim/cables/?limit=1000", headers=_headers())
    resp.raise_for_status()
    return resp.json().get("results", [])


async def get_uplinks() -> set[tuple[str, str]]:
    """Return the set of (device_name, interface_name) pairs that are uplinks.

    A port is considered an uplink when either:
      1. It is cabled to another onboarded Nautobot device (preferred), OR
      2. (Fallback) Its LLDP neighbor (from interface metadata) reports a
         system name that matches an onboarded device name.

    The endpoint correlator uses this set to skip MACs seen transiting
    trunk/cross-link ports — those are not endpoint locations.
    """
    uplinks: set[tuple[str, str]] = set()

    # 1. Cable-based detection (preferred)
    try:
        cables = await get_cables()
    except Exception:
        cables = []

    for cable in cables:
        for side in ("termination_a", "termination_b"):
            term = cable.get(side) or {}
            dev = term.get("device") or {}
            dev_name = dev.get("display") or dev.get("name") or ""
            iface_name = term.get("name") or term.get("display") or ""
            if dev_name and iface_name:
                uplinks.add((dev_name, iface_name))

    if uplinks:
        return uplinks

    # 2. LLDP fallback — walk every onboarded device's interfaces and check
    # for LLDP neighbor metadata. Any neighbor whose system name matches
    # another onboarded device is treated as an uplink.
    try:
        devices = await get_devices()
    except Exception:
        return uplinks

    onboarded_names = {(d.get("name") or "").lower() for d in devices if d.get("name")}

    async def _device_uplinks(dev: dict) -> set[tuple[str, str]]:
        local_name = dev.get("name") or ""
        dev_id = dev.get("id")
        if not dev_id or not local_name:
            return set()
        try:
            ifaces = await get_interfaces(dev_id)
        except Exception:
            return set()
        found: set[tuple[str, str]] = set()
        for iface in ifaces:
            iface_name = iface.get("name") or ""
            if not iface_name:
                continue
            # Nautobot interface objects may expose lldp_neighbors via custom
            # fields or a connected_endpoint. Check both shapes.
            neighbors = []
            cf = iface.get("custom_fields") or {}
            if isinstance(cf.get("lldp_neighbors"), list):
                neighbors = cf["lldp_neighbors"]
            elif isinstance(iface.get("lldp_neighbors"), list):
                neighbors = iface["lldp_neighbors"]
            for n in neighbors:
                neigh_name = ""
                if isinstance(n, dict):
                    neigh_name = (n.get("system_name") or n.get("hostname")
                                  or n.get("device") or "").lower()
                elif isinstance(n, str):
                    neigh_name = n.lower()
                # Match either exact onboarded name or hostname-prefix match
                for known in onboarded_names:
                    if neigh_name == known or neigh_name.startswith(known + "."):
                        found.add((local_name, iface_name))
                        break
        return found

    results = await asyncio.gather(*[_device_uplinks(d) for d in devices],
                                    return_exceptions=True)
    for r in results:
        if isinstance(r, set):
            uplinks |= r
    if uplinks:
        return uplinks

    # 3. Last-resort fallback: query NAPALM get_lldp_neighbors via the
    # Nautobot proxy. This is the same path the collector uses for ARP/MAC
    # tables, so it works whenever onboarding works.
    async def _napalm_lldp(dev: dict) -> set[tuple[str, str]]:
        local_name = dev.get("name") or ""
        dev_id = dev.get("id")
        if not dev_id or not local_name:
            return set()
        try:
            client = _get_client()
            resp = await client.get(
                f"/api/dcim/devices/{dev_id}/napalm/",
                params={"method": "get_lldp_neighbors"},
                headers=_headers(),
                timeout=_NAPALM_TIMEOUT,
            )
            if resp.status_code != 200:
                return set()
            data = resp.json().get("get_lldp_neighbors", {}) or {}
        except Exception:
            return set()
        found: set[tuple[str, str]] = set()
        if not isinstance(data, dict):
            return found
        for iface_name, neighbors in data.items():
            if not isinstance(neighbors, list):
                continue
            for n in neighbors:
                if not isinstance(n, dict):
                    continue
                neigh_name = (n.get("hostname") or n.get("system_name") or "").lower()
                if not neigh_name:
                    continue
                for known in onboarded_names:
                    if neigh_name == known or neigh_name.startswith(known + "."):
                        found.add((local_name, iface_name))
                        break
        return found

    napalm_results = await asyncio.gather(*[_napalm_lldp(d) for d in devices],
                                           return_exceptions=True)
    for r in napalm_results:
        if isinstance(r, set):
            uplinks |= r
    return uplinks


async def get_ip_addresses() -> list[dict]:
    t0 = time.monotonic()
    client = _get_client()
    resp = await client.get("/api/ipam/ip-addresses/?limit=1000", headers=_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])
    log.debug("api_call", "GET ip-addresses", context={"path": "/api/ipam/ip-addresses/", "status": resp.status_code, "count": len(results), "duration_ms": round((time.monotonic() - t0) * 1000)})
    return results


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

async def submit_onboarding_job(
    ip: str,
    location_id: str,
    secrets_group_id: str,
    port: int = 22,
    platform_slug: str | None = None,
) -> dict:
    """Submit a 'Sync Devices From Network' job.

    If platform_slug is provided (e.g. 'juniper_junos'), resolves it to
    a Nautobot Platform ID and passes it to the job so Netmiko doesn't
    have to auto-detect the platform from the SSH banner.
    """
    log.info("onboarding_job_submit", "Submitting onboarding job", context={"ip": ip, "platform": platform_slug})
    client = _get_client()
    # Find the single-device onboarding job (not the SSoT/CSV-based one)
    jobs_resp = await client.get(
        "/api/extras/jobs/?name=Perform+Device+Onboarding+(Original)",
        headers=_headers(),
    )
    jobs_resp.raise_for_status()
    jobs = jobs_resp.json().get("results", [])
    if not jobs:
        # Fallback to the SSoT job name
        jobs_resp = await client.get(
            "/api/extras/jobs/?name=Sync+Devices+From+Network",
            headers=_headers(),
        )
        jobs_resp.raise_for_status()
        jobs = jobs_resp.json().get("results", [])
    if not jobs:
        raise RuntimeError("Device onboarding job not found in Nautobot")
    job_id = jobs[0]["id"]

    # Resolve platform slug to ID if provided
    platform_id = None
    if platform_slug:
        plat_resp = await client.get(
            f"/api/dcim/platforms/?network_driver={platform_slug}&limit=1",
            headers=_headers(),
        )
        if plat_resp.status_code == 200:
            plat_results = plat_resp.json().get("results", [])
            if plat_results:
                platform_id = plat_results[0]["id"]
                log.debug("platform_resolved", "Resolved platform slug to ID", context={"slug": platform_slug, "id": platform_id})

    # Build job data
    job_data: dict = {
        "ip_address": ip,
        "port": port,
        "timeout": 30,
        "location": location_id,
        "credentials": secrets_group_id,
    }
    if platform_id:
        job_data["platform"] = platform_id

    # Run the job
    resp = await client.post(
        f"/api/extras/jobs/{job_id}/run/",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"data": job_data},
    )
    resp.raise_for_status()
    # Bust device cache since a new device may be created
    clear_cache()
    return resp.json()


async def get_job_result(job_result_id: str) -> dict:
    client = _get_client()
    resp = await client.get(
        f"/api/extras/job-results/{job_result_id}/",
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json()


async def delete_standalone_ip(ip: str) -> bool:
    """Delete IPAddress records for ``ip`` that are NOT attached to any
    device interface.

    The controller's sweep records every alive IP into Nautobot IPAM with
    discovery custom fields. When the same IP is later onboarded as a device,
    the nautobot-device-onboarding plugin tries to ``IPAddress.objects.create``
    and crashes with a UniqueViolation. Removing the standalone record
    beforehand lets the plugin own the IPAddress.
    """
    deleted = False
    client = _get_client()
    resp = await client.get(
        f"/api/ipam/ip-addresses/?address={ip}&limit=10",
        headers=_headers(),
    )
    if resp.status_code != 200:
        return False
    for ipo in resp.json().get("results", []):
        if not (ipo.get("address") or "").startswith(ip):
            continue
        interfaces = ipo.get("interfaces") or []
        if interfaces:
            # Already on a device interface — leave it alone.
            continue
        del_resp = await client.delete(
            f"/api/ipam/ip-addresses/{ipo['id']}/",
            headers=_headers(),
        )
        if del_resp.status_code in (200, 202, 204):
            log.info(
                "ip_pre_onboard_deleted",
                "Deleted standalone IP record before onboarding",
                context={"ip": ip, "id": ipo["id"]},
            )
            deleted = True
    return deleted


async def find_device_by_ip(ip: str) -> dict | None:
    """Return the Nautobot device whose primary IP (or any assigned IP) is ``ip``.

    Used as the authoritative success signal for onboarding: the
    nautobot-device-onboarding plugin can leave its JobResult row stuck in
    PENDING after raising OnboardException, so polling JobResult.status is
    unreliable. Device presence is the actual outcome that matters.
    """
    client = _get_client()
    ip_resp = await client.get(
        f"/api/ipam/ip-addresses/?address={ip}&limit=5",
        headers=_headers(),
    )
    if ip_resp.status_code != 200:
        return None
    ip_results = ip_resp.json().get("results", [])
    if not ip_results:
        return None

    for ipo in ip_results:
        ip_id = ipo.get("id")
        if not ip_id:
            continue
        iface_resp = await client.get(
            f"/api/dcim/interfaces/?ip_addresses={ip_id}&limit=5",
            headers=_headers(),
        )
        if iface_resp.status_code != 200:
            continue
        for iface in iface_resp.json().get("results", []):
            dev_obj = iface.get("device") or {}
            dev_id = dev_obj.get("id") if isinstance(dev_obj, dict) else None
            if not dev_id:
                continue
            dev_resp = await client.get(
                f"/api/dcim/devices/{dev_id}/",
                headers=_headers(),
            )
            if dev_resp.status_code == 200:
                return dev_resp.json()
    return None


async def submit_sync_network_data(
    device_ids: list[str] | None = None,
    sync_cables: bool = True,
    sync_vlans: bool = False,
    sync_vrfs: bool = False,
    dryrun: bool = False,
) -> dict:
    """Submit 'Sync Network Data From Network' job.

    If device_ids is empty, fetches all onboarded devices and syncs them all.
    """
    client = _get_client()
    # Find the job
    jobs_resp = await client.get(
        "/api/extras/jobs/?name=Sync+Network+Data+From+Network",
        headers=_headers(),
    )
    jobs_resp.raise_for_status()
    jobs = jobs_resp.json().get("results", [])
    if not jobs:
        raise RuntimeError("Sync Network Data From Network job not found")
    job_id = jobs[0]["id"]

    # If no device IDs provided, get all onboarded devices
    if not device_ids:
        devices = await get_devices()
        device_ids = [d["id"] for d in devices]

    if not device_ids:
        raise RuntimeError("No devices to sync")

    # Resolve required job parameters: the Sync Network Data job demands
    # explicit namespace + status IDs even though they're typically "Global"
    # / "Active". Skipping them yields HTTP 400 with field errors.
    namespace_id = None
    for ns in await get_namespaces():
        if ns.get("name") == "Global":
            namespace_id = ns["id"]
            break
    active_status_id = None
    for s in await get_statuses():
        if s.get("name") == "Active":
            active_status_id = s["id"]
            break

    log.info("sync_submit", f"Submitting sync for {len(device_ids)} devices", context={
        "device_count": len(device_ids),
        "sync_cables": sync_cables,
        "sync_vlans": sync_vlans,
        "dryrun": dryrun,
    })

    # Submit the job
    resp = await client.post(
        f"/api/extras/jobs/{job_id}/run/",
        headers={**_headers(), "Content-Type": "application/json"},
        json={
            "data": {
                "devices": device_ids,
                "sync_cables": sync_cables,
                "sync_vlans": sync_vlans,
                "sync_vrfs": sync_vrfs,
                "namespace": namespace_id,
                "interface_status": active_status_id,
                "ip_address_status": active_status_id,
                "default_prefix_status": active_status_id,
                "debug": False,
            },
            "schedule": {"interval": "immediately"},
            "task_queue": "default",
        },
    )
    if resp.status_code >= 400:
        body = ""
        try:
            body = resp.text[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"Nautobot rejected sync job (HTTP {resp.status_code}): {body}"
        )
    return resp.json()


# ---------------------------------------------------------------------------
# IPAM helpers — new for Phase 2 discovery enrichment
# ---------------------------------------------------------------------------

async def get_statuses() -> list[dict]:
    """Get status objects to find the Active status ID."""
    cached = _cache_get("statuses")
    if cached is not None:
        return cached
    client = _get_client()
    resp = await client.get("/api/extras/statuses/?limit=100", headers=_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])
    _cache_set("statuses", results)
    return results


async def get_namespaces() -> list[dict]:
    """Get namespace objects to find the Global namespace ID."""
    cached = _cache_get("namespaces")
    if cached is not None:
        return cached
    client = _get_client()
    resp = await client.get("/api/ipam/namespaces/?limit=100", headers=_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])
    _cache_set("namespaces", results)
    return results


async def ensure_prefix(cidr: str, namespace: str = "Global") -> dict:
    """Create a prefix in Nautobot IPAM if it doesn't exist.

    Returns the prefix object (existing or newly created).
    """
    client = _get_client()
    # Check if prefix already exists
    resp = await client.get(
        f"/api/ipam/prefixes/?prefix={cidr}&limit=1",
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        return results[0]

    # Resolve namespace ID
    ns_id = None
    namespaces = await get_namespaces()
    for ns in namespaces:
        if ns.get("name") == namespace or ns.get("display") == namespace:
            ns_id = ns["id"]
            break

    # Resolve Active status ID
    active_status_id = None
    statuses = await get_statuses()
    for s in statuses:
        if s.get("name") == "Active":
            active_status_id = s["id"]
            break

    payload: dict = {
        "prefix": cidr,
        "status": active_status_id,
    }
    if ns_id:
        payload["namespace"] = ns_id

    resp = await client.post(
        "/api/ipam/prefixes/",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


async def upsert_discovered_ip(ip: str, data: dict) -> dict:
    """Create or update an IP address in Nautobot IPAM with discovery custom fields.

    data contains: classification, ports_open, mac_address, mac_vendor, dns_name,
    snmp dict, first_seen, last_seen, etc.

    If IP exists: update last_seen and changed fields. Do NOT overwrite first_seen.
    If IP doesn't exist: create with all fields, set both first_seen and last_seen.
    """
    # Build custom fields payload
    snmp = data.get("snmp", {})
    ports_str = ",".join(str(p) for p in data.get("ports_open", []))

    import json as _json

    # Serialize dicts to JSON strings for text custom fields
    banners = data.get("banners", {})
    http_headers = data.get("http_headers", {})

    custom_fields = {
        "discovery_classification": data.get("classification", ""),
        "discovery_ports_open": ports_str,
        "discovery_mac_address": data.get("mac_address", ""),
        "discovery_mac_vendor": data.get("mac_vendor", ""),
        "discovery_dns_name": data.get("dns_name", ""),
        "discovery_snmp_sysname": snmp.get("sysName", ""),
        "discovery_snmp_sysdescr": snmp.get("sysDescr", ""),
        "discovery_snmp_syslocation": snmp.get("sysLocation", ""),
        "discovery_last_seen": data.get("last_seen", ""),
        "discovery_method": data.get("discovery_method", "sweep"),
        "discovery_banners": _json.dumps(banners) if banners else "",
        "discovery_http_headers": _json.dumps(http_headers) if http_headers else "",
        "discovery_http_title": data.get("http_title", ""),
        "discovery_tls_subject": data.get("tls_subject", ""),
        "discovery_tls_issuer": data.get("tls_issuer", ""),
        "discovery_tls_expiry": data.get("tls_expiry", ""),
        "discovery_tls_sans": data.get("tls_sans", ""),
        "discovery_ssh_banner": data.get("ssh_banner", ""),
    }

    # Use /32 for host addresses
    address = f"{ip}/32"

    client = _get_client()
    # Check if IP already exists
    resp = await client.get(
        f"/api/ipam/ip-addresses/?address={ip}&limit=5",
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])

    existing = None
    for r in results:
        # Match on the host portion of the address
        addr_display = r.get("address", "")
        if addr_display.startswith(ip):
            existing = r
            break

    if existing:
        # Update — preserve first_seen
        existing_cf = existing.get("custom_fields", {})
        if existing_cf.get("discovery_first_seen"):
            custom_fields.pop("discovery_first_seen", None)
        else:
            custom_fields["discovery_first_seen"] = data.get("first_seen", "")

        resp = await client.patch(
            f"/api/ipam/ip-addresses/{existing['id']}/",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"custom_fields": custom_fields},
        )
        resp.raise_for_status()
        log.debug("upsert_discovered_ip", "Updated existing IP", context={"ip": ip, "action": "update"})
        return resp.json()
    else:
        # Create new IP address
        custom_fields["discovery_first_seen"] = data.get("first_seen", "")

        # Nautobot 2.x requires a parent Prefix to exist for the IP.
        # Auto-create a /24 in the Global namespace if missing.
        try:
            import ipaddress as _ipaddress
            parent_net = _ipaddress.ip_network(f"{ip}/24", strict=False)
            await ensure_prefix(str(parent_net), "Global")
        except Exception:
            pass

        # Resolve Active status
        active_status_id = None
        statuses = await get_statuses()
        for s in statuses:
            if s.get("name") == "Active":
                active_status_id = s["id"]
                break

        # Resolve namespace
        ns_id = None
        namespaces = await get_namespaces()
        for ns in namespaces:
            if ns.get("name") == "Global":
                ns_id = ns["id"]
                break

        payload: dict = {
            "address": address,
            "status": active_status_id,
            "custom_fields": custom_fields,
        }
        if ns_id:
            payload["namespace"] = ns_id

        resp = await client.post(
            "/api/ipam/ip-addresses/",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        log.debug("upsert_discovered_ip", "Created new IP", context={"ip": ip, "action": "create"})
        return resp.json()


async def napalm_get(device_id: str, method: str, **kwargs) -> dict:
    """Call a NAPALM getter via Nautobot's proxy endpoint.

    Extra kwargs are passed as query params to the Nautobot proxy, which
    forwards them to the NAPALM getter as keyword arguments.
    """
    params = {"method": method, **kwargs}
    client = _get_client()
    resp = await client.get(
        f"/api/dcim/devices/{device_id}/napalm/",
        params=params,
        headers=_headers(),
        timeout=_NAPALM_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


async def upsert_endpoint_ip(ip: str, endpoint: dict) -> dict:
    """Create or update an IP address with endpoint_* custom fields.

    Merges with existing discovery_* fields. Sets endpoint_data_source=both
    when sweep data already exists.
    """
    custom_fields = {
        "endpoint_mac_address": endpoint.get("mac", ""),
        "endpoint_mac_vendor": endpoint.get("mac_vendor", ""),
        "endpoint_switch": endpoint.get("switch", ""),
        "endpoint_port": endpoint.get("port", ""),
        "endpoint_vlan": str(endpoint.get("vlan", "")) if endpoint.get("vlan") else "",
        "endpoint_dhcp_hostname": endpoint.get("dhcp_hostname", ""),
        "endpoint_dhcp_server": endpoint.get("dhcp_server", ""),
        "endpoint_dhcp_lease_start": endpoint.get("dhcp_lease_start", ""),
        "endpoint_dhcp_lease_expiry": endpoint.get("dhcp_lease_expiry", ""),
        "endpoint_data_source": endpoint.get("source", "infrastructure"),
        "discovery_last_seen": endpoint.get("last_seen", ""),
    }

    address = f"{ip}/32"

    client = _get_client()
    resp = await client.get(
        f"/api/ipam/ip-addresses/?address={ip}&limit=5",
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])

    if results:
        existing = results[0]
        existing_cf = existing.get("custom_fields", {})
        if existing_cf.get("discovery_classification"):
            custom_fields["endpoint_data_source"] = "both"

        resp = await client.patch(
            f"/api/ipam/ip-addresses/{existing['id']}/",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"custom_fields": custom_fields},
        )
        resp.raise_for_status()
        log.debug("upsert_endpoint_ip", "Updated endpoint IP", context={"ip": ip, "action": "update"})
        return resp.json()
    else:
        custom_fields["discovery_first_seen"] = endpoint.get("first_seen", "")
        custom_fields["discovery_method"] = "infrastructure"

        active_status_id = None
        for s in await get_statuses():
            if s.get("name") == "Active":
                active_status_id = s["id"]
                break

        ns_id = None
        for ns in await get_namespaces():
            if ns.get("name") == "Global":
                ns_id = ns["id"]
                break

        payload: dict = {
            "address": address,
            "status": active_status_id,
            "custom_fields": custom_fields,
        }
        if ns_id:
            payload["namespace"] = ns_id

        resp = await client.post(
            "/api/ipam/ip-addresses/",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        log.debug("upsert_endpoint_ip", "Created endpoint IP", context={"ip": ip, "action": "create"})
        return resp.json()


# ===========================================================================
# Direct-REST onboarding primitives (Prompt 2 of the v1.0 onboarding
# workstream). Every function cites the section of
# .claude/design/nautobot_rest_schema_notes.md it was built against so that
# later edits can reverify the contract against the reality-check doc.
#
# Style: each write goes through ``_raise_for_write`` so orchestrator code
# can distinguish duplicate / validation / not-found / generic failures
# without parsing response bodies itself.
# ===========================================================================


# ---------------------------------------------------------------------------
# Category 1 — reference lookups (GET by name/slug → record or None).
# All five endpoints documented in reality-check §1.1.
# ---------------------------------------------------------------------------

async def get_manufacturer_by_name(name: str) -> "dict | None":
    """Look up a manufacturer by name. Reality-check §1.1."""
    client = _get_client()
    resp = await client.get(
        "/api/dcim/manufacturers/",
        params={"name": name, "limit": 1},
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


async def get_role_by_name(name: str) -> "dict | None":
    """Look up a DCIM-device role by name.

    Reality-check §1.1: the canonical Roles endpoint is ``/api/extras/roles/``.
    The original design spike used ``/api/dcim/roles/`` which returns 404 on
    Nautobot 3.x. This function must NOT fall back to the dcim path.
    """
    client = _get_client()
    resp = await client.get(
        "/api/extras/roles/",
        params={"name": name, "content_types": "dcim.device", "limit": 1},
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


async def get_devicetype_by_model(model: str) -> "dict | None":
    """Look up a DeviceType by its ``model`` field. Reality-check §1.1."""
    client = _get_client()
    resp = await client.get(
        "/api/dcim/device-types/",
        params={"model": model, "limit": 1},
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


async def get_platform_by_name(name: str) -> "dict | None":
    """Look up a Platform by name (e.g. ``juniper_junos``). Reality-check §1.1."""
    client = _get_client()
    resp = await client.get(
        "/api/dcim/platforms/",
        params={"name": name, "limit": 1},
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


async def get_status_by_name(
    name: str,
    content_type: "str | None" = None,
) -> "dict | None":
    """Exact-name lookup against ``/api/extras/statuses/``.

    Optionally filter by ``content_type`` (e.g. ``"dcim.device"``) to
    disambiguate same-named Statuses that exist across multiple content
    types — Nautobot ships an ``Active`` Status attached to both
    ``dcim.device`` and ``dcim.interface``, for example.

    Reality-check §1.1 (the ``?name=`` filter is supported) and §2
    (custom Status bootstrap).
    """
    params: dict = {"name": name, "limit": 2}
    if content_type:
        params["content_types"] = content_type
    client = _get_client()
    resp = await client.get(
        "/api/extras/statuses/",
        params=params,
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        log.debug("status_lookup_miss", "status not found",
                  context={"name": name, "content_type": content_type})
        return None
    if len(results) > 1:
        log.warning("status_name_ambiguous",
                    "multiple statuses matched exact name filter",
                    context={"name": name, "content_type": content_type,
                             "count": len(results)})
    log.info("status_lookup_hit", "status resolved by name",
             context={"name": name, "content_type": content_type,
                      "status_id": results[0].get("id")})
    return results[0]


# ---------------------------------------------------------------------------
# Category 2 — Device creation.
# Reality-check §1.2 (required fields), §1.3 (ordering), Test 5 (duplicate
# name at location scope).
# ---------------------------------------------------------------------------

async def create_device(
    *,
    name: str,
    device_type_id: str,
    location_id: str,
    role_id: str,
    status_id: str,
    platform_id: "str | None" = None,
    tenant_id: "str | None" = None,
) -> dict:
    """POST /api/dcim/devices/ — create a new device.

    Required fields per reality-check §1.2: ``device_type``, ``location``,
    ``role``, ``status``. ``platform`` is optional. Uniqueness is
    ``(name, tenant, location)`` per Test 5; a collision raises
    :class:`NautobotDuplicateError` with the Nautobot error body preserved.
    """
    payload: dict = {
        "name": name,
        "device_type": device_type_id,
        "location": location_id,
        "role": role_id,
        "status": status_id,
    }
    if platform_id:
        payload["platform"] = platform_id
    if tenant_id:
        payload["tenant"] = tenant_id

    t0 = time.monotonic()
    client = _get_client()
    resp = await client.post(
        "/api/dcim/devices/",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    _raise_for_write(resp, operation="create_device")
    body = resp.json()
    log.info("nautobot_write", "create_device ok", context={
        "name": name, "device_id": body.get("id"), "status_code": resp.status_code,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    })
    return body


# ---------------------------------------------------------------------------
# Category 3 — Interface management (query-and-reuse per Test 4).
# Reality-check Test 4: creating a device auto-populates interfaces from the
# DeviceType template, so blind POST of the management interface may 400 on
# Test 6's (device, name) uniqueness constraint. ``ensure_management_interface``
# is the function Prompt 4 actually calls.
# ---------------------------------------------------------------------------

async def get_interfaces_for_device(device_id: str) -> list[dict]:
    """GET /api/dcim/interfaces/?device_id=... — list every interface for a device.

    Reality-check Test 4: device-type templates may have auto-created up to
    several dozen interfaces at device creation time.
    """
    client = _get_client()
    resp = await client.get(
        "/api/dcim/interfaces/",
        params={"device_id": device_id, "limit": 0},
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


async def find_interface_by_name(device_id: str, name: str) -> "dict | None":
    """Return the interface record for (device, name) or None.

    Reality-check Test 6: Nautobot enforces ``(device, name)`` uniqueness —
    this function lets callers avoid the 400 by checking first.
    """
    client = _get_client()
    resp = await client.get(
        "/api/dcim/interfaces/",
        params={"device_id": device_id, "name": name, "limit": 1},
        headers=_headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


async def create_interface(
    *,
    device_id: str,
    name: str,
    type_: str = "virtual",
    status_id: str,
) -> dict:
    """POST /api/dcim/interfaces/ — create an interface on a device.

    Required fields per reality-check §1.2: ``name``, ``status``, ``type``
    (plus ``device`` writable-optional; we always send it). Duplicate
    ``(device, name)`` raises :class:`NautobotDuplicateError` per Test 6.
    """
    payload = {
        "device": device_id,
        "name": name,
        "type": type_,
        "status": status_id,
    }
    t0 = time.monotonic()
    client = _get_client()
    resp = await client.post(
        "/api/dcim/interfaces/",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    _raise_for_write(resp, operation="create_interface")
    body = resp.json()
    log.info("nautobot_write", "create_interface ok", context={
        "device_id": device_id, "name": name, "interface_id": body.get("id"),
        "status_code": resp.status_code,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    })
    return body


async def ensure_management_interface(
    device_id: str,
    expected_name: str,
    status_id: str,
) -> dict:
    """Return the management interface for a device, creating if absent.

    Reality-check Test 4 finding: device-type templates auto-create
    interfaces, so the management interface may already exist when the
    orchestrator reaches its Step B. This helper is the query-and-reuse
    wrapper the orchestrator calls — it never raises on duplicate.
    """
    existing = await find_interface_by_name(device_id, expected_name)
    if existing is not None:
        log.debug("ensure_mgmt_iface", "reused template-created interface",
                  context={"device_id": device_id, "name": expected_name,
                           "interface_id": existing.get("id")})
        return existing
    return await create_interface(
        device_id=device_id, name=expected_name,
        type_="virtual", status_id=status_id,
    )


# ---------------------------------------------------------------------------
# Category 4 — IPAddress management.
# Reality-check §1.3 (ordering), §1.4 (IP↔Interface through model),
# Test 7 (IP does NOT cascade on device delete).
# ---------------------------------------------------------------------------

async def create_ip_address(
    *,
    address: str,
    status_id: str,
    namespace_id: "str | None" = None,
    parent_prefix_id: "str | None" = None,
) -> dict:
    """POST /api/ipam/ip-addresses/ — create an IP address.

    Required fields per reality-check §1.2: ``address``, ``status``. The
    covering prefix (``parent_prefix_id`` if passed, otherwise any matching
    prefix in the namespace) must already exist — Nautobot 3.x rejects
    orphan IPs.
    """
    payload: dict = {
        "address": address,
        "status": status_id,
        "type": "host",
    }
    if namespace_id:
        payload["namespace"] = namespace_id
    if parent_prefix_id:
        payload["parent"] = parent_prefix_id

    t0 = time.monotonic()
    client = _get_client()
    resp = await client.post(
        "/api/ipam/ip-addresses/",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    _raise_for_write(resp, operation="create_ip_address")
    body = resp.json()
    log.info("nautobot_write", "create_ip_address ok", context={
        "address": address, "ip_id": body.get("id"),
        "status_code": resp.status_code,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    })
    return body


async def link_ip_to_interface(
    ip_id: str,
    interface_id: str,
    *,
    is_primary: bool = False,
) -> dict:
    """Link an IP to an interface via the IPAddressToInterface through model.

    Reality-check §1.4: Nautobot 3.x moved IP↔Interface linking to a through
    model at ``/api/ipam/ip-address-to-interface/``. The design spike's
    earlier ``PATCH /ipam/ip-addresses/{id}/`` approach is superseded.
    Reality-check Test 2 confirmed that ``primary_ip4`` cannot be set until
    this link exists — Nautobot validates assignment, not just the UUID.
    """
    payload = {
        "ip_address": ip_id,
        "interface": interface_id,
        "is_primary": is_primary,
    }
    t0 = time.monotonic()
    client = _get_client()
    resp = await client.post(
        "/api/ipam/ip-address-to-interface/",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
    )
    _raise_for_write(resp, operation="link_ip_to_interface")
    body = resp.json()
    log.info("nautobot_write", "link_ip_to_interface ok", context={
        "ip_id": ip_id, "interface_id": interface_id, "link_id": body.get("id"),
        "status_code": resp.status_code,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    })
    return body


async def delete_ip_address(ip_id: str) -> None:
    """DELETE /api/ipam/ip-addresses/{id}/ — remove an IP.

    Reality-check Test 7: deleting a device cascades to interfaces but
    **not** to IPs. Orchestrator rollback must call this explicitly for
    every IP created during a failed onboarding.
    """
    t0 = time.monotonic()
    client = _get_client()
    resp = await client.delete(
        f"/api/ipam/ip-addresses/{ip_id}/",
        headers=_headers(),
    )
    _raise_for_write(resp, operation="delete_ip_address")
    log.info("nautobot_write", "delete_ip_address ok", context={
        "ip_id": ip_id, "status_code": resp.status_code,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    })


# ---------------------------------------------------------------------------
# Category 5 — Device primary IP + status + delete.
# Reality-check Test 2 (primary_ip4 requires through-model link),
# Test 7 (device delete cascades interfaces, not IPs).
# ---------------------------------------------------------------------------

async def set_device_primary_ip4(device_id: str, ip_id: str) -> dict:
    """PATCH /api/dcim/devices/{id}/ with ``primary_ip4 = ip_id``.

    Reality-check Test 2: Nautobot rejects with 400 and body
    ``{"primary_ip4": ["The specified IP address (…) is not assigned to
    this device."]}`` if the IP has not been linked to one of the device's
    interfaces via :func:`link_ip_to_interface`. The orchestrator must call
    link_ip_to_interface first.
    """
    t0 = time.monotonic()
    client = _get_client()
    resp = await client.patch(
        f"/api/dcim/devices/{device_id}/",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"primary_ip4": ip_id},
    )
    _raise_for_write(resp, operation="set_device_primary_ip4")
    body = resp.json()
    log.info("nautobot_write", "set_device_primary_ip4 ok", context={
        "device_id": device_id, "ip_id": ip_id,
        "status_code": resp.status_code,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    })
    return body


async def set_device_status(device_id: str, status_id: str) -> dict:
    """PATCH /api/dcim/devices/{id}/ with ``status = status_id``.

    Used by the orchestrator to move a device between the Active /
    Onboarding Incomplete / Onboarding Failed statuses (reality-check §2).
    """
    t0 = time.monotonic()
    client = _get_client()
    resp = await client.patch(
        f"/api/dcim/devices/{device_id}/",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"status": status_id},
    )
    _raise_for_write(resp, operation="set_device_status")
    body = resp.json()
    log.info("nautobot_write", "set_device_status ok", context={
        "device_id": device_id, "status_id": status_id,
        "status_code": resp.status_code,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    })
    return body


async def delete_device(device_id: str) -> None:
    """DELETE /api/dcim/devices/{id}/ — remove a device.

    Reality-check Test 7: interfaces cascade on delete (204 → interfaces
    gone), but IPs do NOT. Orchestrator rollback must explicitly call
    :func:`delete_ip_address` for IPs it created.
    """
    t0 = time.monotonic()
    client = _get_client()
    resp = await client.delete(
        f"/api/dcim/devices/{device_id}/",
        headers=_headers(),
    )
    _raise_for_write(resp, operation="delete_device")
    log.info("nautobot_write", "delete_device ok", context={
        "device_id": device_id, "status_code": resp.status_code,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    })


# ---------------------------------------------------------------------------
# Category 6 — Pre-check for strict-new onboarding (operator Q2 decision).
# Reality-check Test 5: uniqueness is ``(name, tenant, location)``.
# ---------------------------------------------------------------------------

async def device_exists_at_location(name: str, location_id: str) -> bool:
    """Return True if a device with this name already exists at this location.

    Operator Q2 decision: onboarding is strict-new. The orchestrator uses
    this pre-check to fail fast with a clear error rather than let create
    raise :class:`NautobotDuplicateError` mid-sequence.
    """
    client = _get_client()
    resp = await client.get(
        "/api/dcim/devices/",
        params={"name": name, "location_id": location_id, "limit": 1},
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json().get("count", 0) > 0


# ---------------------------------------------------------------------------
# Category 7 — Custom Status bootstrap (operator Q5 decision).
# Reality-check §2: the two custom Statuses must exist before Prompt 4 fires.
# ---------------------------------------------------------------------------

_CUSTOM_STATUSES = [
    {
        "slug": "onboarding-incomplete",
        "name": "Onboarding Incomplete",
        "color": "ff9800",
        "description": (
            "Device created via MNM onboarding; Phase 2 network data sync "
            "failed or incomplete."
        ),
        "content_types": ["dcim.device"],
    },
    {
        "slug": "onboarding-failed",
        "name": "Onboarding Failed",
        "color": "b71c1c",
        "description": (
            "Device onboarding failed before device creation completed. "
            "Retry or delete."
        ),
        "content_types": ["dcim.device"],
    },
]


async def ensure_custom_statuses() -> dict[str, str]:
    """Ensure ``Onboarding Incomplete`` and ``Onboarding Failed`` Statuses exist.

    Returns ``{slug: uuid}`` for both Statuses. Idempotent: queries by name
    first and only POSTs when absent, so safe to call at every controller
    startup. Reality-check §2 describes the request shape.

    The caller is expected to tolerate Nautobot being briefly unavailable —
    this function propagates httpx exceptions up; ``main.on_startup`` catches
    them and logs a warning so the controller still comes up.
    """
    client = _get_client()

    out: dict[str, str] = {}
    actions: list[str] = []
    for desired in _CUSTOM_STATUSES:
        slug = desired["slug"]
        name = desired["name"]
        existing = await get_status_by_name(name, content_type="dcim.device")
        if existing is not None:
            out[slug] = existing["id"]
            actions.append(f"{slug}=verified")
            continue
        payload = {k: v for k, v in desired.items() if k != "slug"}
        t0 = time.monotonic()
        post_resp = await client.post(
            "/api/extras/statuses/",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        _raise_for_write(post_resp, operation="ensure_custom_status")
        body = post_resp.json()
        out[slug] = body["id"]
        actions.append(f"{slug}=created")
        # Invalidate any cached statuses list so downstream callers see the
        # new Status on their next request.
        clear_cache()
        log.info("nautobot_write", "ensure_custom_status ok", context={
            "slug": slug, "status_id": body["id"],
            "status_code": post_resp.status_code,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1),
        })

    log.info("db_init_custom_statuses",
             "custom status bootstrap complete",
             context={"slugs": [d["slug"] for d in _CUSTOM_STATUSES],
                      "actions": actions})
    return out
