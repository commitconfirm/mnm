"""Async HTTP client for Nautobot REST API."""

import asyncio
import os
import re
import time

import docker
import httpx

from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="nautobot")

NAUTOBOT_URL = os.environ.get("NAUTOBOT_URL", "http://nautobot:8080")
_api_token: str | None = None


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


async def get_devices() -> list[dict]:
    t0 = time.monotonic()
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
        # depth=1 ensures nested objects (primary_ip4, platform, location, role)
        # include display/address fields, not just id/url references.
        resp = await client.get("/api/dcim/devices/?limit=1000&depth=1", headers=_headers())
        resp.raise_for_status()
        results = resp.json().get("results", [])
        log.debug("api_call", "GET devices", context={"path": "/api/dcim/devices/", "status": resp.status_code, "count": len(results), "duration_ms": round((time.monotonic() - t0) * 1000)})
        return results


async def get_device(device_id: str) -> dict:
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
        resp = await client.get(f"/api/dcim/devices/{device_id}/", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_interfaces(device_id: str) -> list[dict]:
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=30) as client:
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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=30) as client:
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
            async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=60) as client:
                resp = await client.get(
                    f"/api/dcim/devices/{dev_id}/napalm/",
                    params={"method": "get_lldp_neighbors"},
                    headers=_headers(),
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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
        resp = await client.get("/api/ipam/ip-addresses/?limit=1000", headers=_headers())
        resp.raise_for_status()
        results = resp.json().get("results", [])
        log.debug("api_call", "GET ip-addresses", context={"path": "/api/ipam/ip-addresses/", "status": resp.status_code, "count": len(results), "duration_ms": round((time.monotonic() - t0) * 1000)})
        return results


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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=30) as client:
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
        return resp.json()


async def get_job_result(job_result_id: str) -> dict:
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
        resp = await client.get(
            f"/api/extras/job-results/{job_result_id}/",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def delete_standalone_ip(ip: str) -> bool:
    """Delete IPAddress records for ``ip`` that are NOT attached to any
    device interface.

    The controller's sweep records every alive IP into IPAM with discovery
    custom fields. When the same IP is later onboarded as a device, the
    nautobot-device-onboarding plugin tries to ``IPAddress.objects.create``
    that IP and crashes with a UniqueViolation. Removing the standalone
    record beforehand lets the plugin own the IPAddress; the discovery CFs
    will be re-applied to the newly-created (now interface-attached) record
    on the next sweep.

    Skips deletion when the IP is already attached to an interface (i.e.
    the device exists), to avoid orphaning a real device.
    """
    address = f"{ip}/32"
    deleted = False
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
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

    Implementation: looks up the IPAddress record by host address, then
    finds any interface that lists that IPAddress, and resolves the
    interface's parent device. The Nautobot 3.x ``primary_ip4__host``
    filter is rejected (HTTP 400), so we cannot query Devices directly
    by IP.
    """
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=30) as client:
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
            # Surface Nautobot's actual validation message instead of the
            # generic httpx error string. Particularly useful for sync runs
            # that fail because the device has no IP to connect to.
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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
        resp = await client.get("/api/extras/statuses/?limit=100", headers=_headers())
        resp.raise_for_status()
        return resp.json().get("results", [])


async def get_namespaces() -> list[dict]:
    """Get namespace objects to find the Global namespace ID."""
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
        resp = await client.get("/api/ipam/namespaces/?limit=100", headers=_headers())
        resp.raise_for_status()
        return resp.json().get("results", [])


async def ensure_prefix(cidr: str, namespace: str = "Global") -> dict:
    """Create a prefix in Nautobot IPAM if it doesn't exist.

    Returns the prefix object (existing or newly created).
    """
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
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

    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
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
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=60) as client:
        resp = await client.get(
            f"/api/dcim/devices/{device_id}/napalm/",
            params=params,
            headers=_headers(),
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

    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
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
