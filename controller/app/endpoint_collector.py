"""Endpoint collector for MNM — queries onboarded devices for forwarding plane data.

Collects ARP tables, MAC address tables, and DHCP bindings from onboarded network
devices via Nautobot's NAPALM proxy API. Correlates the data into unified endpoint
records (IP + MAC + port + VLAN + hostname) and stores them in Nautobot IPAM.

All device queries are read-only and go through Nautobot — the controller never
SSHes to devices directly.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

import docker
import httpx

# Concurrency from env or scaled to CPU count
MAX_CONCURRENT_DEVICES = int(os.environ.get(
    "MNM_COLLECTION_CONCURRENCY",
    str(max(4, (os.cpu_count() or 4) * 2))
))

from app import db, endpoint_store, nautobot_client
from app.config import load_config
from app.discovery import _mac_vendor
from app.logging_config import StructuredLogger

    # _mac_vendor is imported directly from app.discovery (includes IEEE OUI fallback)

log = StructuredLogger(__name__, module="collector")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NAUTOBOT_URL = os.environ.get("NAUTOBOT_URL", "http://nautobot:8080")
NAPALM_TIMEOUT = 60  # seconds — NAPALM proxy calls can be slow

# Interfaces that indicate a trunk/aggregation/management path rather than
# an access port where an endpoint physically connects.
_NON_ACCESS_PREFIXES = ("ae", "irb", "lo", "vlan", "me", "em", "fxp", "bme",
                        "jsrv", "pip", "vtep", "lsi", ".32767",
                        "port-channel", "loopback", "vlan", "mgmt",
                        "management", "nve", "sup-eth")

# ---------------------------------------------------------------------------
# In-memory collection state
# ---------------------------------------------------------------------------

# Live state for the in-flight run only. Persistent endpoint data lives in
# Postgres (see app.endpoint_store).
_collection_state: dict = {
    "running": False,
    "last_run": None,
    "summary": None,
    "errors": [],
    "progress": {
        "devices_total": 0,
        "devices_done": 0,
        "endpoints_found": 0,
        "phase": "",
    },
}


def get_collection_state() -> dict:
    """Return current endpoint collection state (safe copy of summary info)."""
    return {
        "running": _collection_state["running"],
        "last_run": _collection_state["last_run"],
        "summary": _collection_state["summary"],
        "errors": _collection_state["errors"][-50:],
        "progress": _collection_state["progress"].copy(),
    }


# ---------------------------------------------------------------------------
# NAPALM proxy helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return nautobot_client._headers()


async def _get_arp_table(device_id: str) -> list[dict]:
    """Fetch ARP table from a device via Nautobot's NAPALM proxy.

    Returns list of dicts with keys: interface, mac, ip, age.
    """
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=NAPALM_TIMEOUT) as client:
        resp = await client.get(
            f"/api/dcim/devices/{device_id}/napalm/",
            params={"method": "get_arp_table"},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        # NAPALM proxy returns {"get_arp_table": [...]}
        entries = data.get("get_arp_table", [])
        log.debug("arp_table_fetched", "ARP table fetched", context={"device_id": device_id, "count": len(entries)})
        return entries


async def _get_mac_table(device_id: str) -> list[dict]:
    """Fetch MAC address table from a device via Nautobot's NAPALM proxy.

    Returns list of dicts with keys: mac, interface, vlan, static, active, moves, last_move.
    """
    async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=NAPALM_TIMEOUT) as client:
        resp = await client.get(
            f"/api/dcim/devices/{device_id}/napalm/",
            params={"method": "get_mac_address_table"},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("get_mac_address_table", [])
        log.debug("mac_table_fetched", "MAC address table fetched", context={"device_id": device_id, "count": len(entries)})
        return entries


def _get_dhcp_bindings_sync(device_ip: str, device_name: str) -> list[dict]:
    """Get DHCP server bindings via docker exec PyEZ RPC (Junos only).

    Synchronous — intended to be called via run_in_executor from async code.
    Runs a small Python snippet inside the nautobot container to query the
    device's DHCP binding table using PyEZ. Gracefully returns [] if the
    device doesn't run a DHCP server or isn't Junos.
    """
    napalm_user = os.environ.get("NAUTOBOT_NAPALM_USERNAME", "")
    napalm_pass = os.environ.get("NAUTOBOT_NAPALM_PASSWORD", "")

    if not napalm_user or not napalm_pass:
        log.debug("dhcp_skip_no_creds", "NAPALM credentials not set, skipping DHCP collection")
        return []

    # Escape special chars for shell embedding
    safe_user = napalm_user.replace("'", "'\\''")
    safe_pass = napalm_pass.replace("'", "'\\''")
    safe_ip = device_ip.replace("'", "'\\''")

    script = (
        "from jnpr.junos import Device\n"
        "import json, sys\n"
        f"dev = Device(host='{safe_ip}', user='{safe_user}', password='{safe_pass}')\n"
        "dev.open()\n"
        "try:\n"
        "    rpc = dev.rpc.get_dhcp_server_binding_information()\n"
        "    bindings = []\n"
        "    for b in rpc.findall('.//dhcp-binding-table'):\n"
        "        bindings.append({\n"
        "            'ip': b.findtext('dhcp-binding-ip-address', ''),\n"
        "            'mac': b.findtext('dhcp-binding-mac-address', ''),\n"
        "            'hostname': b.findtext('dhcp-binding-client-hostname', ''),\n"
        "            'lease_start': b.findtext('dhcp-binding-lease-start-time', ''),\n"
        "            'lease_expiry': b.findtext('dhcp-binding-lease-expiry-time', ''),\n"
        "            'state': b.findtext('dhcp-binding-state', ''),\n"
        "        })\n"
        "    print(json.dumps(bindings))\n"
        "except Exception:\n"
        "    print(json.dumps([]))\n"
        "finally:\n"
        "    dev.close()\n"
    )

    try:
        client = docker.from_env()
        container = client.containers.get("mnm-nautobot")
        result = container.exec_run(
            ["python3", "-c", script],
            demux=True,
        )
        stdout = result.output[0] if result.output[0] else b""
        output = stdout.decode("utf-8", errors="replace").strip()

        if not output:
            return []

        bindings = json.loads(output)
        if bindings:
            log.debug("dhcp_bindings_collected", "Collected DHCP bindings", context={"device": device_name, "count": len(bindings)})
        return bindings

    except docker.errors.NotFound:
        log.warning("dhcp_container_missing", "mnm-nautobot container not found for DHCP exec")
        return []
    except json.JSONDecodeError:
        log.warning("dhcp_bad_output", "Non-JSON output from DHCP exec", context={"device": device_name})
        return []
    except Exception as e:
        log.debug("dhcp_failed", "DHCP collection failed", context={"device": device_name, "error": str(e)})
        return []


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def _normalize_mac(mac: str) -> str:
    """Normalize a MAC address to upper-case colon-separated format."""
    if not mac:
        return ""
    clean = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(clean) != 12:
        return mac.upper()
    return ":".join(clean[i:i+2] for i in range(0, 12, 2)).upper()


def _is_access_interface(name: str) -> bool:
    """Heuristic: return True if the interface name looks like an access port."""
    lower = name.lower()
    for prefix in _NON_ACCESS_PREFIXES:
        if lower.startswith(prefix):
            return False
    return True


def _correlate_endpoints(
    arp_entries: list[dict],
    mac_entries: list[dict],
    dhcp_entries: list[dict],
    device_name: str,
) -> list[dict]:
    """Merge ARP (IP->MAC) + MAC table (MAC->port->VLAN) + DHCP (MAC->hostname).

    Returns a list of endpoint dicts, each containing:
        ip, mac, mac_vendor, interface, vlan, hostname, device_name,
        is_access_port, lease_start, lease_expiry, dhcp_state
    """
    # --- Build lookup: MAC -> MAC table info (port + VLAN) ---
    mac_info: dict[str, dict] = {}
    for entry in mac_entries:
        mac = _normalize_mac(entry.get("mac", ""))
        if not mac:
            continue
        iface = entry.get("interface", "")
        vlan = entry.get("vlan", 0)
        is_access = _is_access_interface(iface)

        existing = mac_info.get(mac)
        if existing is None:
            mac_info[mac] = {
                "interface": iface,
                "vlan": vlan,
                "is_access_port": is_access,
            }
        elif is_access and not existing.get("is_access_port"):
            # Prefer the access port entry over trunk/irb
            mac_info[mac] = {
                "interface": iface,
                "vlan": vlan,
                "is_access_port": is_access,
            }

    # --- Build lookup: MAC -> DHCP info ---
    dhcp_info: dict[str, dict] = {}
    for entry in dhcp_entries:
        mac = _normalize_mac(entry.get("mac", ""))
        if not mac:
            continue
        dhcp_info[mac] = {
            "hostname": entry.get("hostname", ""),
            "lease_start": entry.get("lease_start", ""),
            "lease_expiry": entry.get("lease_expiry", ""),
            "dhcp_state": entry.get("state", ""),
        }

    # --- Iterate ARP entries, enrich with MAC table + DHCP ---
    endpoints: list[dict] = []
    for arp in arp_entries:
        ip = arp.get("ip", "")
        mac = _normalize_mac(arp.get("mac", ""))
        arp_iface = arp.get("interface", "")

        if not ip or not mac:
            continue
        # Skip incomplete ARP entries (all-zero or broadcast MACs)
        if mac in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"):
            continue

        mac_data = mac_info.get(mac, {})
        dhcp_data = dhcp_info.get(mac, {})

        now_iso = datetime.now(timezone.utc).isoformat()

        # first_seen will be preserved by the DB upsert when this MAC already exists.
        first_seen = now_iso

        # Hostname priority: DHCP hostname (existing hostname is preserved by upsert)
        hostname = dhcp_data.get("hostname", "")

        endpoint = {
            "ip": ip,
            "mac": mac,
            "mac_vendor": _mac_vendor(mac),
            "arp_interface": arp_iface,
            "switch_port": mac_data.get("interface", ""),
            "vlan": mac_data.get("vlan", 0),
            "is_access_port": mac_data.get("is_access_port", False),
            "hostname": hostname,
            "dhcp_hostname": dhcp_data.get("hostname", ""),
            "lease_start": dhcp_data.get("lease_start", ""),
            "lease_expiry": dhcp_data.get("lease_expiry", ""),
            "dhcp_state": dhcp_data.get("dhcp_state", ""),
            "device_name": device_name,
            "first_seen": first_seen,
            "last_seen": now_iso,
            "source": "infrastructure",
        }
        endpoints.append(endpoint)

    log.debug("correlate_complete", "Endpoint correlation complete", context={
        "device": device_name, "arp_input": len(arp_entries),
        "mac_input": len(mac_entries), "dhcp_input": len(dhcp_entries),
        "endpoints_output": len(endpoints),
    })
    return endpoints


# ---------------------------------------------------------------------------
# Per-device collection
# ---------------------------------------------------------------------------

async def collect_from_device(device: dict) -> list[dict]:
    """Collect ARP + MAC + DHCP data from a single device via Nautobot NAPALM proxy.

    Returns a list of correlated endpoint records.
    """
    device_id = device.get("id", "")
    device_name = device.get("name", device_id)

    # Extract primary IP for DHCP exec (needs the actual IP address, not UUID)
    device_ip = ""
    primary_ip_obj = device.get("primary_ip4") or device.get("primary_ip") or {}
    if isinstance(primary_ip_obj, dict):
        # The nested object may have 'address' or 'display', or just 'id'
        addr = primary_ip_obj.get("address", "") or primary_ip_obj.get("display", "")
        if addr:
            device_ip = addr.split("/")[0]
        elif primary_ip_obj.get("id"):
            # Resolve the IP address from Nautobot
            try:
                ip_addrs = await nautobot_client.get_ip_addresses()
                for ipa in ip_addrs:
                    if ipa.get("id") == primary_ip_obj["id"]:
                        device_ip = ipa.get("display", "").split("/")[0]
                        break
            except Exception:
                pass

    # Determine if this is a Junos device (for DHCP collection)
    platform = device.get("platform") or {}
    platform_name = ""
    if isinstance(platform, dict):
        platform_name = (platform.get("name", "") or platform.get("display", "")).lower()
        if not platform_name and platform.get("id"):
            # Resolve platform name — the nested object may only have id/url
            try:
                async with httpx.AsyncClient(base_url=NAUTOBOT_URL, timeout=15) as client:
                    resp = await client.get(
                        f"/api/dcim/platforms/{platform['id']}/",
                        headers=_headers(),
                    )
                    if resp.status_code == 200:
                        platform_name = resp.json().get("name", "").lower()
            except Exception:
                pass

    is_junos = "junos" in platform_name or "juniper" in platform_name

    # --- Collect data in parallel ---
    arp_task = _get_arp_table(device_id)
    mac_task = _get_mac_table(device_id)

    tasks = [arp_task, mac_task]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    arp_entries: list[dict] = []
    mac_entries: list[dict] = []

    if isinstance(results[0], list):
        arp_entries = results[0]
    elif isinstance(results[0], Exception):
        log.warning("arp_collection_failed", "ARP collection failed", context={"device": device_name, "error": str(results[0])})
        _collection_state["errors"].append(
            f"ARP failed on {device_name}: {results[0]}"
        )

    if isinstance(results[1], list):
        mac_entries = results[1]
    elif isinstance(results[1], Exception):
        log.warning("mac_collection_failed", "MAC table collection failed", context={"device": device_name, "error": str(results[1])})
        _collection_state["errors"].append(
            f"MAC table failed on {device_name}: {results[1]}"
        )

    # DHCP — Junos only, runs via docker exec (synchronous, run in executor)
    dhcp_entries: list[dict] = []
    if is_junos and device_ip:
        try:
            loop = asyncio.get_running_loop()
            dhcp_entries = await loop.run_in_executor(
                None, _get_dhcp_bindings_sync, device_ip, device_name
            )
        except Exception as e:
            log.debug("dhcp_skipped", "DHCP collection skipped", context={"device": device_name, "error": str(e)})

    log.info("device_collection_done", "Device data collected", context={
        "device": device_name, "arp_count": len(arp_entries),
        "mac_count": len(mac_entries), "dhcp_count": len(dhcp_entries),
    })

    return _correlate_endpoints(arp_entries, mac_entries, dhcp_entries, device_name)


# ---------------------------------------------------------------------------
# Record endpoints to Nautobot IPAM
# ---------------------------------------------------------------------------

async def _record_endpoint(ip: str, endpoint: dict) -> None:
    """Create or update a Nautobot IPAM record with endpoint custom fields.

    Uses the same IP upsert pattern as discovery but writes endpoint-specific
    custom fields: MAC, vendor, switch port, VLAN, hostname, source device.
    """
    now = datetime.now(timezone.utc).isoformat()

    custom_fields = {
        "endpoint_mac_address": endpoint.get("mac", ""),
        "endpoint_mac_vendor": endpoint.get("mac_vendor", ""),
        "endpoint_switch": endpoint.get("device_name", ""),
        "endpoint_port": endpoint.get("switch_port", ""),
        "endpoint_vlan": str(endpoint.get("vlan", "")) if endpoint.get("vlan") else "",
        "endpoint_dhcp_hostname": endpoint.get("hostname", ""),
        "endpoint_dhcp_server": endpoint.get("dhcp_server", ""),
        "endpoint_dhcp_lease_start": endpoint.get("lease_start", ""),
        "endpoint_dhcp_lease_expiry": endpoint.get("lease_expiry", ""),
        "endpoint_data_source": endpoint.get("source", "infrastructure"),
        "discovery_last_seen": now,
    }

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
            addr_display = r.get("address", "")
            if addr_display.startswith(ip):
                existing = r
                break

        if existing:
            # Preserve discovery_first_seen
            existing_cf = existing.get("custom_fields", {})
            if not existing_cf.get("discovery_first_seen"):
                custom_fields["discovery_first_seen"] = now

            resp = await client.patch(
                f"/api/ipam/ip-addresses/{existing['id']}/",
                headers={**_headers(), "Content-Type": "application/json"},
                json={"custom_fields": custom_fields},
            )
            resp.raise_for_status()
            log.debug("endpoint_recorded", "Updated endpoint in IPAM", context={"ip": ip, "action": "update"})
        else:
            # Create new IP address
            custom_fields["discovery_first_seen"] = now
            custom_fields["discovery_method"] = "infrastructure"

            # Nautobot 2.x requires every IP address to live inside a defined
            # parent Prefix in the namespace. Auto-create a /24 if missing —
            # this is the same logic the sweep ought to use, hoisted into a
            # helper. Falls back gracefully on invalid input.
            try:
                import ipaddress as _ipaddress
                parent_net = _ipaddress.ip_network(f"{ip}/24", strict=False)
                await nautobot_client.ensure_prefix(str(parent_net), "Global")
            except Exception as exc:
                log.debug("ensure_prefix_failed", "Could not ensure parent prefix",
                          context={"ip": ip, "error": str(exc)})

            # Resolve Active status
            active_status_id = None
            statuses = await nautobot_client.get_statuses()
            for s in statuses:
                if s.get("name") == "Active":
                    active_status_id = s["id"]
                    break

            # Resolve Global namespace
            ns_id = None
            namespaces = await nautobot_client.get_namespaces()
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
            if resp.status_code >= 400:
                # Surface Nautobot's actual validation error so 400s aren't opaque
                body = ""
                try:
                    body = resp.text[:500]
                except Exception:
                    pass
                log.warning("ipam_post_400", "Nautobot rejected IPAM POST",
                            context={"ip": ip, "status": resp.status_code,
                                     "body": body, "payload": payload})
            resp.raise_for_status()
            log.debug("endpoint_recorded", "Created endpoint in IPAM", context={"ip": ip, "action": "create"})


# ---------------------------------------------------------------------------
# Main collection orchestrator
# ---------------------------------------------------------------------------

async def collect_all() -> dict:
    """Iterate all onboarded devices, collect forwarding plane data, correlate, record.

    Returns a summary dict with counts and any errors encountered.
    """
    _collection_state["running"] = True
    _collection_state["errors"] = []
    _collection_state["progress"] = {
        "devices_total": 0,
        "devices_done": 0,
        "endpoints_found": 0,
        "phase": "collecting",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": 0,
    }
    started = datetime.now(timezone.utc)

    log.info("collection_start", "Starting endpoint collection run")

    try:
        devices = await nautobot_client.get_devices()
    except Exception as e:
        log.error("collection_device_fetch_failed", "Failed to fetch devices from Nautobot", context={"error": str(e)}, exc_info=True)
        _collection_state["running"] = False
        _collection_state["progress"]["phase"] = "error"
        _collection_state["errors"].append(f"Device fetch failed: {e}")
        return {"status": "error", "error": str(e)}

    _collection_state["progress"]["devices_total"] = len(devices)

    # Per-run dedupe map keyed on MAC (preferred) or IP fallback
    all_endpoints: dict[str, dict] = {}
    devices_queried = 0
    devices_failed = 0

    # Collect from devices with bounded concurrency
    sem = asyncio.Semaphore(MAX_CONCURRENT_DEVICES)
    results_lock = asyncio.Lock()

    async def _collect_one(device):
        nonlocal devices_queried, devices_failed
        device_name = device.get("name", device.get("id", "unknown"))
        async with sem:
            try:
                endpoints = await collect_from_device(device)
                async with results_lock:
                    devices_queried += 1
                    for ep in endpoints:
                        ip = ep["ip"]
                        existing = all_endpoints.get(ip)
                        if existing is None:
                            all_endpoints[ip] = ep
                        elif ep.get("is_access_port") and not existing.get("is_access_port"):
                            all_endpoints[ip] = ep
                    _collection_state["progress"]["devices_done"] = devices_queried + devices_failed
                    _collection_state["progress"]["endpoints_found"] = len(all_endpoints)
                    _collection_state["progress"]["elapsed_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
            except Exception as e:
                async with results_lock:
                    devices_failed += 1
                    _collection_state["progress"]["devices_done"] = devices_queried + devices_failed
                    _collection_state["progress"]["elapsed_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
                    err_msg = f"Collection failed on {device_name}: {e}"
                    log.warning("device_collection_error", "Collection failed on device", context={"device": device_name, "error": str(e)})
                    _collection_state["errors"].append(err_msg)

    await asyncio.gather(*[_collect_one(d) for d in devices])

    # --- Enrich hostnames from sweep/IPAM data ---
    # If an endpoint has no hostname from DHCP, check if sweep data has DNS/SNMP info
    try:
        ipam_records = await nautobot_client.get_ip_addresses()
        ipam_by_ip: dict[str, dict] = {}
        for addr in ipam_records:
            display = addr.get("display", "")
            ip_part = display.split("/")[0] if "/" in display else display
            if ip_part:
                ipam_by_ip[ip_part] = addr.get("custom_fields", {})

        for ip, ep in all_endpoints.items():
            if ep.get("hostname"):
                continue  # Already has a hostname
            cf = ipam_by_ip.get(ip, {})
            # Priority: DNS name > SNMP sysName
            dns = cf.get("discovery_dns_name", "")
            snmp_name = cf.get("discovery_snmp_sysname", "")
            if dns:
                ep["hostname"] = dns
            elif snmp_name:
                ep["hostname"] = snmp_name
    except Exception as exc:
        log.debug("hostname_enrichment_failed", "Hostname enrichment from IPAM failed", context={"error": str(exc)})

    # --- Pre-fetch uplinks (cabled inter-device ports) for correlation ---
    uplinks: set[tuple[str, str]] = set()
    try:
        uplinks = await nautobot_client.get_uplinks()
        log.info("uplinks_loaded", "Uplink port set fetched from Nautobot",
                 context={"count": len(uplinks)})
    except Exception as e:
        log.warning("uplinks_fetch_failed", "Failed to fetch uplinks from Nautobot",
                    context={"error": str(e)})

    # --- Pre-fetch operator-defined IP exclusions (Rule 6) ---
    excluded_ips: set[str] = set()
    if db.is_ready():
        try:
            excluded_ips = await endpoint_store.get_excluded_ips()
            if excluded_ips:
                log.info("excludes_loaded", "Discovery exclusion list loaded",
                         context={"count": len(excluded_ips)})
        except Exception as e:
            log.warning("excludes_fetch_failed", "Could not load exclusion list",
                        context={"error": str(e)})

    # --- Persist to controller DB + Nautobot IPAM ---
    _collection_state["progress"]["phase"] = "recording"
    recorded = 0
    record_failed = 0
    new_count = 0
    updated_count = 0
    moved_count = 0

    for ip, endpoint in all_endpoints.items():
        # Operator exclusion (Rule 6): skip ARP/MAC entries for IPs the
        # operator has marked as excluded. Neither the controller DB nor
        # Nautobot IPAM gets updated for them.
        if ip in excluded_ips:
            continue
        # Persist to controller DB (with diff/event detection)
        if db.is_ready():
            # Always propagate the (mac, ip) binding to any existing rows for
            # this MAC, even if the upsert below decides this entry is on an
            # uplink/LAG and skips row creation. The IP belongs to the MAC,
            # not to the (switch, port, vlan) location.
            try:
                if endpoint.get("mac") and ip:
                    await endpoint_store.register_mac_ip(endpoint["mac"], ip)
            except Exception as e:
                log.debug("mac_ip_register_failed", "register_mac_ip failed",
                          context={"mac": endpoint.get("mac"), "ip": ip, "error": str(e)})

            try:
                res = await endpoint_store.upsert_endpoint(
                    endpoint, source="infrastructure", uplinks=uplinks
                )
                if res["action"] == "skipped_uplink":
                    continue
                if res["action"] == "new":
                    new_count += 1
                elif res["action"] == "updated":
                    updated_count += 1
                if any(e in ("moved_port", "moved_switch") for e in res["events"]):
                    moved_count += 1
            except Exception as e:
                log.warning("db_upsert_failed", "Endpoint DB upsert failed",
                            context={"ip": ip, "mac": endpoint.get("mac"), "error": str(e)})
                _collection_state["errors"].append(f"DB upsert {ip}: {e}")

        # Mirror into Nautobot IPAM (best effort — IPAM remains source of truth for IPs)
        try:
            await _record_endpoint(ip, endpoint)
            recorded += 1
        except Exception as e:
            record_failed += 1
            err_msg = f"Failed to record {ip}: {e}"
            log.warning("endpoint_record_failed", "Failed to record endpoint", context={"ip": ip, "error": str(e)})
            _collection_state["errors"].append(err_msg)

    # --- Update state ---
    finished = datetime.now(timezone.utc)
    summary = {
        "status": "complete",
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "devices_queried": devices_queried,
        "devices_failed": devices_failed,
        "endpoints_found": len(all_endpoints),
        "endpoints_new": new_count,
        "endpoints_updated": updated_count,
        "endpoints_moved": moved_count,
        "endpoints_recorded": recorded,
        "endpoints_record_failed": record_failed,
        "errors": len(_collection_state["errors"]),
    }

    _collection_state["running"] = False
    _collection_state["last_run"] = finished.isoformat()
    _collection_state["progress"]["phase"] = "complete"

    # Persist run summary to controller DB (collection_runs table)
    if db.is_ready():
        try:
            await endpoint_store.record_collection_run(summary)
        except Exception as e:
            log.warning("collection_run_persist_failed", "Failed to persist collection run",
                        context={"error": str(e)})
    _collection_state["summary"] = summary

    log.info("collection_complete", "Endpoint collection complete", context={
        "devices_queried": devices_queried, "endpoints_found": len(all_endpoints),
        "recorded": recorded, "record_failed": record_failed,
        "duration_seconds": summary["duration_seconds"],
    })

    return summary


# ---------------------------------------------------------------------------
# Scheduled background loop
# ---------------------------------------------------------------------------

async def scheduled_collection_loop() -> None:
    """Background loop that runs collect_all on a configurable interval.

    Reads endpoint_collection_interval_minutes from config (default 15).
    Runs indefinitely, sleeping between collection runs.
    """
    log.info("collection_loop_started", "Endpoint collection loop started")

    # Wait for Nautobot to be ready before first run
    await asyncio.sleep(60)

    from app.config import load_config_async
    while True:
        try:
            config = await load_config_async()
            interval_minutes = config.get("endpoint_collection_interval_minutes", 15)

            if _collection_state["running"]:
                log.debug("collection_skip_busy", "Collection already running, skipping scheduled run")
                await asyncio.sleep(60)
                continue

            log.info("collection_schedule_trigger", "Running scheduled endpoint collection", context={"interval_minutes": interval_minutes})
            await collect_all()

            await asyncio.sleep(interval_minutes * 60)

        except asyncio.CancelledError:
            log.info("collection_loop_cancelled", "Endpoint collection loop cancelled")
            break
        except Exception as e:
            log.error("collection_loop_error", "Error in endpoint collection loop", context={"error": str(e)}, exc_info=True)
            # Back off on errors to avoid tight retry loops
            await asyncio.sleep(300)
