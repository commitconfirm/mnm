"""Endpoint correlation utilities and IPAM recording.

Provides the correlation engine that merges ARP + MAC + DHCP data into
unified endpoint records, and the IPAM recording function that mirrors
endpoint data to Nautobot. These are used by the modular polling engine
(polling.py) which handles all device collection scheduling.

The legacy monolithic collector (collect_all, scheduled_collection_loop)
was removed in Phase 2.9 — the modular polling engine fully replaces it.
"""

import json
import os
import re
from datetime import datetime, timezone

import docker

from app import nautobot_client

from app.discovery import _mac_vendor
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="collector")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Interfaces that indicate a trunk/aggregation/management path rather than
# an access port where an endpoint physically connects.
_NON_ACCESS_PREFIXES = ("ae", "irb", "lo", "vlan", "me", "em", "fxp", "bme",
                        "jsrv", "pip", "vtep", "lsi", ".32767",
                        "port-channel", "loopback", "vlan", "mgmt",
                        "management", "nve", "sup-eth")


def _headers() -> dict:
    return nautobot_client._headers()


# ---------------------------------------------------------------------------
# DHCP collection (Junos-specific, via PyEZ in nautobot container)
# ---------------------------------------------------------------------------

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
        result = container.exec_run(["python3", "-c", script], demux=True)
        stdout = result.output[0] if result.output[0] else b""
        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return []
        bindings = json.loads(output)
        if bindings:
            log.debug("dhcp_bindings_collected", "Collected DHCP bindings",
                      context={"device": device_name, "count": len(bindings)})
        return bindings
    except docker.errors.NotFound:
        log.warning("dhcp_container_missing", "mnm-nautobot container not found for DHCP exec")
        return []
    except json.JSONDecodeError:
        log.warning("dhcp_bad_output", "Non-JSON output from DHCP exec",
                    context={"device": device_name})
        return []
    except Exception as e:
        log.debug("dhcp_failed", "DHCP collection failed",
                  context={"device": device_name, "error": str(e)})
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
        if mac in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"):
            continue

        mac_data = mac_info.get(mac, {})
        dhcp_data = dhcp_info.get(mac, {})

        now_iso = datetime.now(timezone.utc).isoformat()
        first_seen = now_iso
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
# Record endpoints to Nautobot IPAM
# ---------------------------------------------------------------------------

async def _record_endpoint(ip: str, endpoint: dict) -> None:
    """Create or update a Nautobot IPAM record with endpoint custom fields."""
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

    client = nautobot_client._get_client()
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
        existing_cf = existing.get("custom_fields", {})
        if not existing_cf.get("discovery_first_seen"):
            custom_fields["discovery_first_seen"] = now

        resp = await client.patch(
            f"/api/ipam/ip-addresses/{existing['id']}/",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"custom_fields": custom_fields},
        )
        resp.raise_for_status()
        log.debug("endpoint_recorded", "Updated endpoint in IPAM",
                  context={"ip": ip, "action": "update"})
    else:
        custom_fields["discovery_first_seen"] = now
        custom_fields["discovery_method"] = "infrastructure"

        try:
            import ipaddress as _ipaddress
            parent_net = _ipaddress.ip_network(f"{ip}/24", strict=False)
            await nautobot_client.ensure_prefix(str(parent_net), "Global")
        except Exception:
            pass

        active_status_id = None
        for s in await nautobot_client.get_statuses():
            if s.get("name") == "Active":
                active_status_id = s["id"]
                break

        ns_id = None
        for ns in await nautobot_client.get_namespaces():
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
            body = ""
            try:
                body = resp.text[:500]
            except Exception:
                pass
            log.warning("ipam_post_400", "Nautobot rejected IPAM POST",
                        context={"ip": ip, "status": resp.status_code,
                                 "body": body, "payload": payload})
        resp.raise_for_status()
        log.debug("endpoint_recorded", "Created endpoint in IPAM",
                  context={"ip": ip, "action": "create"})
