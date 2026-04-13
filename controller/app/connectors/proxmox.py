"""Proxmox VE connector (read-only).

Collects hypervisor inventory, VM/container metadata + metrics, and ZFS/disk
storage status from a Proxmox VE cluster. All endpoints are GET-only — see
inviolable Rule 1 in CLAUDE.md.

Authenticates via API token: ``PVEAPIToken=user@realm!tokenid=secret``.
TLS verification defaults to off because Proxmox ships with self-signed certs.

The collector runs on a schedule from ``app.main`` and:
  1. Caches the latest snapshot in module-level state for the dashboard.
  2. Upserts every VM/container with a MAC address into the endpoint store.
  3. Exposes a Prometheus exposition-format render via ``render_metrics()``.

Disabled-by-default: if ``PROXMOX_HOST`` is not set, every entry point becomes
a no-op so the controller still starts cleanly.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app import db, endpoint_store
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="proxmox")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXMOX_HOST = os.environ.get("PROXMOX_HOST", "").rstrip("/")
PROXMOX_TOKEN_ID = os.environ.get("PROXMOX_TOKEN_ID", "")
PROXMOX_TOKEN_SECRET = os.environ.get("PROXMOX_TOKEN_SECRET", "")
PROXMOX_VERIFY_SSL = os.environ.get("PROXMOX_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
PROXMOX_TIMEOUT = 30
PROXMOX_INTERVAL_SECONDS = int(os.environ.get("PROXMOX_INTERVAL_SECONDS", "300"))


def is_configured() -> bool:
    return bool(PROXMOX_HOST and PROXMOX_TOKEN_ID and PROXMOX_TOKEN_SECRET)


def _headers() -> dict:
    return {"Authorization": f"PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}"}


# ---------------------------------------------------------------------------
# In-memory state — exposed via API + Prometheus + dashboard card
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {
    "last_run": None,
    "last_error": None,
    "running": False,
    "duration_seconds": None,
    "warnings": [],       # per-call API warnings (permission denied etc.)
    "nodes": [],          # list of node dicts
    "vms": [],            # list of VM dicts
    "containers": [],     # list of LXC dicts
    "storage": [],        # list of storage pool dicts
    "zfs_pools": [],      # list of ZFS pool dicts
    "disks": [],          # list of physical disk dicts
}

# Populated per-collection-run; merged into _state["warnings"] at the end.
_run_warnings: list[str] = []


def get_state() -> dict:
    """Return a shallow copy of the current Proxmox snapshot for the API."""
    return {
        "configured": is_configured(),
        "last_run": _state["last_run"],
        "last_error": _state["last_error"],
        "warnings": _state.get("warnings", []),
        "running": _state["running"],
        "duration_seconds": _state["duration_seconds"],
        "node_count": len(_state["nodes"]),
        "vm_count": len(_state["vms"]),
        "container_count": len(_state["containers"]),
        "zfs_pool_count": len(_state["zfs_pools"]),
        "storage_count": len(_state["storage"]),
        "disk_count": len(_state["disks"]),
        "nodes": _state["nodes"],
        "vms": _state["vms"],
        "containers": _state["containers"],
        "storage": _state["storage"],
        "zfs_pools": _state["zfs_pools"],
        "disks": _state["disks"],
    }


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

async def _get_quiet(client: httpx.AsyncClient, path: str) -> dict | list | None:
    """Like _get() but does not record warnings — for endpoints that are
    expected to fail in normal operation (e.g. QEMU agent on a VM that hasn't
    installed it)."""
    try:
        resp = await client.get(path, headers=_headers())
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json().get("data")
    except Exception:
        return None


async def _get(client: httpx.AsyncClient, path: str) -> dict | list | None:
    """GET an endpoint. Returns the unwrapped ``data`` field or None on error.

    Permission errors and other non-200 responses are recorded in the
    per-run warning list so the dashboard / API can surface them.
    """
    try:
        resp = await client.get(path, headers=_headers())
    except Exception as e:
        msg = f"{path}: {e}"
        _run_warnings.append(msg)
        log.debug("proxmox_http_error", "Proxmox HTTP error", context={"path": path, "error": str(e)})
        return None
    if resp.status_code != 200:
        # Try to extract Proxmox's "Permission check failed" message
        detail = ""
        try:
            j = resp.json()
            detail = (j.get("message") or "").strip()
        except Exception:
            pass
        msg = f"{path}: HTTP {resp.status_code}" + (f" — {detail}" if detail else "")
        _run_warnings.append(msg)
        log.warning("proxmox_http_status", "Proxmox returned non-200",
                    context={"path": path, "status": resp.status_code, "detail": detail})
        return None
    try:
        return resp.json().get("data")
    except Exception:
        _run_warnings.append(f"{path}: invalid JSON response")
        return None


# ---------------------------------------------------------------------------
# VM / container network parsing
# ---------------------------------------------------------------------------

_NET_KEY_RE = re.compile(r"^net\d+$")
# Junos-style key=value list, e.g. "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=110"
_KV_RE = re.compile(r"(?P<k>[a-zA-Z0-9_]+)=(?P<v>[^,]+)")


def _parse_net_string(value: str) -> dict:
    """Parse a Proxmox net config line into a dict.

    Example input: ``virtio=BC:24:11:C3:C7:41,bridge=vmbr0,tag=110``
    Returns: ``{"model": "virtio", "mac": "BC:24:11:C3:C7:41", "bridge": "vmbr0", "vlan": 110}``
    """
    out: dict = {"mac": "", "bridge": "", "vlan": None, "model": ""}
    for match in _KV_RE.finditer(value):
        k, v = match.group("k"), match.group("v").strip()
        kl = k.lower()
        if kl in ("virtio", "e1000", "rtl8139", "vmxnet3", "name") and ":" in v:
            # Model = key, value = MAC
            out["model"] = kl
            out["mac"] = v.upper()
        elif kl == "macaddr" or kl == "hwaddr":
            out["mac"] = v.upper()
        elif kl == "bridge":
            out["bridge"] = v
        elif kl == "tag":
            try:
                out["vlan"] = int(v)
            except ValueError:
                pass
    # Some configs put the MAC as the bare first token
    if not out["mac"]:
        first = value.split(",", 1)[0]
        if "=" in first:
            _, candidate = first.split("=", 1)
            if ":" in candidate and len(candidate) >= 17:
                out["mac"] = candidate.upper()
    return out


def _interfaces_from_config(cfg: dict) -> list[dict]:
    """Extract network interfaces from a VM/container config dict."""
    interfaces = []
    for k, v in (cfg or {}).items():
        if _NET_KEY_RE.match(k) and isinstance(v, str):
            iface = _parse_net_string(v)
            iface["device"] = k
            iface["ip"] = ""
            iface["ips"] = []  # populated by agent / lxc enrichment
            interfaces.append(iface)
    return interfaces


def _ips_from_addresses(addrs: list) -> list[str]:
    """Return every non-loopback / non-link-local address from an agent or LXC
    interface entry. Both IPv4 and IPv6 globally routable addresses are kept."""
    out: list[str] = []
    for a in addrs or []:
        if not isinstance(a, dict):
            continue
        ip = a.get("ip-address", "")
        if not ip:
            continue
        if ip.startswith("127.") or ip == "::1" or ip.startswith("fe80"):
            continue
        if ip not in out:
            out.append(ip)
    return out


def _ipv4_from_addresses(addrs: list) -> str:
    """First non-loopback IPv4 from a list of address dicts (back-compat helper)."""
    for ip in _ips_from_addresses(addrs):
        if "." in ip:
            return ip
    return ""


async def _attach_qemu_agent_ips(client: httpx.AsyncClient, node: str, vmid: int,
                                  interfaces: list[dict]) -> None:
    """Best-effort: query the QEMU guest agent for in-guest IP addresses and
    attach them to interfaces matched by MAC. Silently noops when the agent
    is not installed or running in the VM."""
    data = await _get_quiet(
        client, f"/api2/json/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
    )
    if not data:
        return
    # Proxmox wraps the agent reply in {"result": [...]}
    result = data.get("result") if isinstance(data, dict) else data
    if not isinstance(result, list):
        return
    by_mac: dict[str, list[str]] = {}
    for entry in result:
        if not isinstance(entry, dict):
            continue
        hw = (entry.get("hardware-address") or entry.get("hwaddr") or "").upper()
        if not hw:
            continue
        ips = _ips_from_addresses(entry.get("ip-addresses", []))
        if ips:
            by_mac[hw] = ips
    for iface in interfaces:
        ips = by_mac.get(iface.get("mac", ""))
        if ips:
            iface["ips"] = ips
            if not iface.get("ip"):
                # Prefer IPv4 as the primary
                iface["ip"] = next((ip for ip in ips if "." in ip), ips[0])


async def _attach_lxc_ips(client: httpx.AsyncClient, node: str, vmid: int,
                           interfaces: list[dict]) -> None:
    """Pull container interface IPs from /lxc/{vmid}/interfaces. Unlike VMs
    this works without a guest agent because the host can read the netns."""
    data = await _get_quiet(client, f"/api2/json/nodes/{node}/lxc/{vmid}/interfaces")
    if not isinstance(data, list):
        return
    by_mac: dict[str, list[str]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        hw = (entry.get("hardware-address") or entry.get("hwaddr") or "").upper()
        if not hw:
            continue
        ips = _ips_from_addresses(entry.get("ip-addresses", []))
        if not ips:
            inet = entry.get("inet", "")
            if inet:
                ips = [inet.split("/", 1)[0]]
        if ips:
            by_mac[hw] = ips
    for iface in interfaces:
        ips = by_mac.get(iface.get("mac", ""))
        if ips:
            iface["ips"] = ips
            if not iface.get("ip"):
                iface["ip"] = next((ip for ip in ips if "." in ip), ips[0])


# ---------------------------------------------------------------------------
# Collection — main entrypoint
# ---------------------------------------------------------------------------

async def collect() -> dict:
    """Run a full Proxmox collection cycle.

    Updates module state, upserts MAC-keyed endpoints into the store, and
    returns a summary dict.
    """
    if not is_configured():
        return {"status": "skipped", "reason": "not_configured"}

    if _state["running"]:
        return {"status": "skipped", "reason": "already_running"}

    _state["running"] = True
    _state["last_error"] = None
    _run_warnings.clear()
    started = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()

    log.info("proxmox_collect_start", "Starting Proxmox collection",
             context={"host": PROXMOX_HOST})

    nodes_out: list[dict] = []
    vms_out: list[dict] = []
    cts_out: list[dict] = []
    storage_out: list[dict] = []
    zfs_out: list[dict] = []
    disks_out: list[dict] = []

    try:
        async with httpx.AsyncClient(
            base_url=PROXMOX_HOST,
            timeout=PROXMOX_TIMEOUT,
            verify=PROXMOX_VERIFY_SSL,
        ) as client:
            nodes = await _get(client, "/api2/json/nodes") or []
            for node in nodes:
                node_name = node.get("node") or node.get("name") or ""
                if not node_name:
                    continue

                status = await _get(client, f"/api2/json/nodes/{node_name}/status") or {}
                cpuinfo = status.get("cpuinfo") or {}
                loadavg = status.get("loadavg", []) or []

                def _f(idx: int) -> float:
                    try:
                        return float(loadavg[idx])
                    except (IndexError, ValueError, TypeError):
                        return 0.0

                node_record = {
                    "name": node_name,
                    "status": node.get("status", ""),
                    "uptime": status.get("uptime", node.get("uptime", 0)),
                    "cpu": status.get("cpu", node.get("cpu", 0.0)),
                    "iowait": status.get("wait", 0.0),
                    "cpu_count": cpuinfo.get("cpus", 0),
                    "cpu_cores": cpuinfo.get("cores", 0),
                    "cpu_sockets": cpuinfo.get("sockets", 0),
                    "cpu_model": cpuinfo.get("model", ""),
                    "cpu_mhz": float(cpuinfo.get("mhz", 0) or 0),
                    "memory_used": (status.get("memory") or {}).get("used", 0),
                    "memory_total": (status.get("memory") or {}).get("total", 0),
                    "memory_free": (status.get("memory") or {}).get("free", 0),
                    "swap_used": (status.get("swap") or {}).get("used", 0),
                    "swap_total": (status.get("swap") or {}).get("total", 0),
                    "rootfs_used": (status.get("rootfs") or {}).get("used", 0),
                    "rootfs_total": (status.get("rootfs") or {}).get("total", 0),
                    "ksm_shared": (status.get("ksm") or {}).get("shared", 0),
                    "loadavg_1m": _f(0),
                    "loadavg_5m": _f(1),
                    "loadavg_15m": _f(2),
                    "kernel": status.get("kversion", ""),
                    "pveversion": status.get("pveversion", ""),
                }
                nodes_out.append(node_record)

                # ----- VMs (qemu) -----
                qemu_list = await _get(client, f"/api2/json/nodes/{node_name}/qemu") or []
                for vm in qemu_list:
                    vmid = vm.get("vmid")
                    if vmid is None:
                        continue
                    cur = await _get(client, f"/api2/json/nodes/{node_name}/qemu/{vmid}/status/current") or {}
                    cfg = await _get(client, f"/api2/json/nodes/{node_name}/qemu/{vmid}/config") or {}
                    interfaces = _interfaces_from_config(cfg)
                    # Best-effort guest-agent IP enrichment (silent on failure)
                    if cur.get("status") == "running":
                        await _attach_qemu_agent_ips(client, node_name, vmid, interfaces)
                    vms_out.append({
                        "node": node_name,
                        "vmid": vmid,
                        "name": vm.get("name", f"vm{vmid}"),
                        "status": cur.get("status", vm.get("status", "")),
                        "cpu": cur.get("cpu", 0.0),
                        "cpus": cur.get("cpus", cfg.get("cores", 0) or 0),
                        "mem": cur.get("mem", 0),
                        "maxmem": cur.get("maxmem", 0),
                        "disk": cur.get("disk", 0),
                        "maxdisk": cur.get("maxdisk", 0),
                        "diskread": cur.get("diskread", 0),
                        "diskwrite": cur.get("diskwrite", 0),
                        "netin": cur.get("netin", 0),
                        "netout": cur.get("netout", 0),
                        "uptime": cur.get("uptime", 0),
                        "interfaces": interfaces,
                    })

                # ----- LXC containers -----
                lxc_list = await _get(client, f"/api2/json/nodes/{node_name}/lxc") or []
                for ct in lxc_list:
                    vmid = ct.get("vmid")
                    if vmid is None:
                        continue
                    cur = await _get(client, f"/api2/json/nodes/{node_name}/lxc/{vmid}/status/current") or {}
                    cfg = await _get(client, f"/api2/json/nodes/{node_name}/lxc/{vmid}/config") or {}
                    interfaces = _interfaces_from_config(cfg)
                    if cur.get("status") == "running":
                        await _attach_lxc_ips(client, node_name, vmid, interfaces)
                    cts_out.append({
                        "node": node_name,
                        "vmid": vmid,
                        "name": ct.get("name", f"ct{vmid}"),
                        "status": cur.get("status", ct.get("status", "")),
                        "cpu": cur.get("cpu", 0.0),
                        "cpus": cur.get("cpus", cfg.get("cores", 0) or 0),
                        "mem": cur.get("mem", 0),
                        "maxmem": cur.get("maxmem", 0),
                        "disk": cur.get("disk", 0),
                        "maxdisk": cur.get("maxdisk", 0),
                        "diskread": cur.get("diskread", 0),
                        "diskwrite": cur.get("diskwrite", 0),
                        "netin": cur.get("netin", 0),
                        "netout": cur.get("netout", 0),
                        "uptime": cur.get("uptime", 0),
                        "interfaces": interfaces,
                    })

                # ----- Storage pools -----
                storages = await _get(client, f"/api2/json/nodes/{node_name}/storage") or []
                for s in storages:
                    storage_out.append({
                        "node": node_name,
                        "storage": s.get("storage", ""),
                        "type": s.get("type", ""),
                        "used": s.get("used", 0),
                        "total": s.get("total", 0),
                        "avail": s.get("avail", 0),
                        "enabled": int(s.get("enabled", 1)),
                        "active": int(s.get("active", 0)),
                    })

                # ----- ZFS pools -----
                zfs = await _get(client, f"/api2/json/nodes/{node_name}/disks/zfs") or []
                for pool in zfs:
                    zfs_out.append({
                        "node": node_name,
                        "name": pool.get("name", ""),
                        "size": pool.get("size", 0),
                        "alloc": pool.get("alloc", 0),
                        "free": pool.get("free", 0),
                        "frag": pool.get("frag", 0),
                        "health": pool.get("health", ""),
                        "dedup": pool.get("dedup", 1.0),
                    })

                # ----- Physical disks -----
                phys = await _get(client, f"/api2/json/nodes/{node_name}/disks/list") or []
                for d in phys:
                    disks_out.append({
                        "node": node_name,
                        "devpath": d.get("devpath", ""),
                        "model": d.get("model", ""),
                        "serial": d.get("serial", ""),
                        "size": d.get("size", 0),
                        "type": d.get("type", ""),  # ssd, hdd, nvme, usb
                        "health": d.get("health", ""),  # PASSED, FAILED, UNKNOWN
                        "wearout": d.get("wearout", ""),
                        "vendor": d.get("vendor", ""),
                    })

    except Exception as e:
        _state["last_error"] = str(e)
        log.error("proxmox_collect_failed", "Proxmox collection failed",
                  context={"error": str(e)}, exc_info=True)
        _state["running"] = False
        return {"status": "error", "error": str(e)}

    # Commit snapshot
    _state["nodes"] = nodes_out
    _state["vms"] = vms_out
    _state["containers"] = cts_out
    _state["storage"] = storage_out
    _state["zfs_pools"] = zfs_out
    _state["disks"] = disks_out
    _state["last_run"] = started_iso
    _state["duration_seconds"] = round(time.monotonic() - started, 2)
    _state["warnings"] = list(_run_warnings)
    _state["running"] = False

    # Upsert VMs/containers as endpoints (one row per MAC)
    # Build a subnet→VLAN map from node ARP interfaces for native VLAN inference.
    # When a Proxmox VM has no tag= in its config (native VLAN on the bridge),
    # infer the VLAN from the VM's IP address matching a known VLAN subnet.
    subnet_vlan_map: dict[str, int] = {}
    try:
        from sqlalchemy import select as _select
        async with db.SessionLocal() as session:
            arp_rows = (await session.execute(
                _select(db.NodeArpEntry.interface).distinct()
            )).scalars().all()
            for iface_str in arp_rows:
                from app.endpoint_collector import _infer_vlan_from_interface
                vlan_id = _infer_vlan_from_interface(iface_str or "")
                if vlan_id:
                    subnet_vlan_map[vlan_id] = vlan_id  # keyed by VLAN for reverse lookup
    except Exception:
        pass

    def _infer_vlan_from_ip(ip_addr: str) -> int:
        """Infer VLAN from IP by matching against known VLAN subnets.

        Uses the third octet heuristic: if VLANs 110, 120, 130, 140 exist
        and the IP is 172.21.140.x, the third octet 140 matches VLAN 140.
        This works for the common pattern where VLAN ID = third octet.
        Falls back to 0 if no match.
        """
        if not ip_addr:
            return 0
        try:
            parts = ip_addr.split(".")
            if len(parts) == 4:
                third_octet = int(parts[2])
                if third_octet in subnet_vlan_map:
                    return third_octet
        except (ValueError, IndexError):
            pass
        return 0

    upserted = 0
    if db.is_ready():
        for guest, kind in ((vms_out, "virtual_machine"), (cts_out, "container")):
            for g in guest:
                for iface in g.get("interfaces", []):
                    mac = iface.get("mac")
                    if not mac:
                        continue
                    vlan = iface.get("vlan") or 0
                    ip = iface.get("ip") or None
                    # Infer VLAN from IP when no tag is set in Proxmox config
                    if not vlan and ip:
                        vlan = _infer_vlan_from_ip(ip)
                    try:
                        await endpoint_store.upsert_endpoint({
                            "mac": mac,
                            "ip": ip,
                            "additional_ips": iface.get("ips") or [],
                            "hostname": g.get("name"),
                            "classification": kind,
                            "device_name": g.get("node"),
                            "switch_port": iface.get("bridge") or "",
                            "vlan": vlan,
                        }, source="proxmox", change_source="proxmox")
                        upserted += 1
                    except Exception as e:
                        log.debug("proxmox_upsert_failed", "Proxmox endpoint upsert failed",
                                  context={"mac": mac, "error": str(e)})

    log.info("proxmox_collect_done", "Proxmox collection complete", context={
        "nodes": len(nodes_out), "vms": len(vms_out), "cts": len(cts_out),
        "zfs_pools": len(zfs_out), "disks": len(disks_out),
        "endpoints_upserted": upserted,
        "duration_seconds": _state["duration_seconds"],
    })

    return {
        "status": "ok",
        "nodes": len(nodes_out),
        "vms": len(vms_out),
        "containers": len(cts_out),
        "zfs_pools": len(zfs_out),
        "disks": len(disks_out),
        "endpoints_upserted": upserted,
        "duration_seconds": _state["duration_seconds"],
    }


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

async def scheduled_loop() -> None:
    """Run ``collect()`` on a fixed interval. Quietly noops when not configured."""
    if not is_configured():
        log.info("proxmox_disabled", "Proxmox connector disabled (PROXMOX_HOST not set)")
        return
    log.info("proxmox_loop_started", "Proxmox collection loop started",
             context={"interval_seconds": PROXMOX_INTERVAL_SECONDS})
    # Wait for the rest of the system to settle before the first run
    await asyncio.sleep(20)
    while True:
        try:
            await collect()
        except asyncio.CancelledError:
            log.info("proxmox_loop_cancelled", "Proxmox loop cancelled")
            break
        except Exception as e:
            log.error("proxmox_loop_error", "Proxmox loop error",
                      context={"error": str(e)}, exc_info=True)
        await asyncio.sleep(PROXMOX_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Prometheus exposition
# ---------------------------------------------------------------------------

def _esc(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _line(name: str, labels: dict, value: float | int) -> str:
    if labels:
        rendered = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items() if v != "")
        return f'{name}{{{rendered}}} {value}'
    return f"{name} {value}"


_HEALTH_OK = {"ONLINE", "PASSED", "OK", "HEALTHY"}


def _health_to_int(value: str) -> int:
    return 1 if (value or "").upper() in _HEALTH_OK else 0


def render_metrics() -> str:
    """Return the current snapshot as Prometheus exposition text."""
    lines: list[str] = []

    if not is_configured():
        lines.append("# HELP mnm_proxmox_configured Whether the Proxmox connector is configured (1/0)")
        lines.append("# TYPE mnm_proxmox_configured gauge")
        lines.append("mnm_proxmox_configured 0")
        return "\n".join(lines) + "\n"

    lines.append("# HELP mnm_proxmox_configured Whether the Proxmox connector is configured (1/0)")
    lines.append("# TYPE mnm_proxmox_configured gauge")
    lines.append("mnm_proxmox_configured 1")

    if _state["last_run"]:
        try:
            ts = datetime.fromisoformat(_state["last_run"]).timestamp()
            lines.append("# TYPE mnm_proxmox_last_collection_timestamp gauge")
            lines.append(f"mnm_proxmox_last_collection_timestamp {ts}")
        except ValueError:
            pass

    # ---- Nodes ----
    for metric in (
        "cpu_usage", "iowait", "cpu_count", "cpu_cores", "cpu_sockets", "cpu_mhz",
        "memory_used_bytes", "memory_total_bytes", "memory_free_bytes",
        "swap_used_bytes", "swap_total_bytes",
        "rootfs_used_bytes", "rootfs_total_bytes",
        "ksm_shared_bytes",
        "loadavg_1m", "loadavg_5m", "loadavg_15m",
        "uptime_seconds",
    ):
        lines.append(f"# TYPE mnm_proxmox_node_{metric} gauge")
    for n in _state["nodes"]:
        lbl = {"node": n["name"]}
        lines.append(_line("mnm_proxmox_node_cpu_usage", lbl, n.get("cpu", 0)))
        lines.append(_line("mnm_proxmox_node_iowait", lbl, n.get("iowait", 0)))
        lines.append(_line("mnm_proxmox_node_cpu_count", lbl, n.get("cpu_count", 0)))
        lines.append(_line("mnm_proxmox_node_cpu_cores", lbl, n.get("cpu_cores", 0)))
        lines.append(_line("mnm_proxmox_node_cpu_sockets", lbl, n.get("cpu_sockets", 0)))
        lines.append(_line("mnm_proxmox_node_cpu_mhz", lbl, n.get("cpu_mhz", 0)))
        lines.append(_line("mnm_proxmox_node_memory_used_bytes", lbl, n.get("memory_used", 0)))
        lines.append(_line("mnm_proxmox_node_memory_total_bytes", lbl, n.get("memory_total", 0)))
        lines.append(_line("mnm_proxmox_node_memory_free_bytes", lbl, n.get("memory_free", 0)))
        lines.append(_line("mnm_proxmox_node_swap_used_bytes", lbl, n.get("swap_used", 0)))
        lines.append(_line("mnm_proxmox_node_swap_total_bytes", lbl, n.get("swap_total", 0)))
        lines.append(_line("mnm_proxmox_node_rootfs_used_bytes", lbl, n.get("rootfs_used", 0)))
        lines.append(_line("mnm_proxmox_node_rootfs_total_bytes", lbl, n.get("rootfs_total", 0)))
        lines.append(_line("mnm_proxmox_node_ksm_shared_bytes", lbl, n.get("ksm_shared", 0)))
        lines.append(_line("mnm_proxmox_node_loadavg_1m", lbl, n.get("loadavg_1m", 0)))
        lines.append(_line("mnm_proxmox_node_loadavg_5m", lbl, n.get("loadavg_5m", 0)))
        lines.append(_line("mnm_proxmox_node_loadavg_15m", lbl, n.get("loadavg_15m", 0)))
        lines.append(_line("mnm_proxmox_node_uptime_seconds", lbl, n.get("uptime", 0)))

    # ---- VMs ----
    for prefix, items in (("vm", _state["vms"]), ("ct", _state["containers"])):
        for metric in ("cpu_usage", "memory_used_bytes", "memory_total_bytes",
                        "disk_read_bytes", "disk_write_bytes",
                        "netin_bytes", "netout_bytes", "status", "uptime_seconds"):
            lines.append(f"# TYPE mnm_proxmox_{prefix}_{metric} gauge")
        for g in items:
            lbl = {"node": g["node"], "vmid": str(g["vmid"]), "name": g.get("name", "")}
            running = 1 if g.get("status") == "running" else 0
            lines.append(_line(f"mnm_proxmox_{prefix}_cpu_usage", lbl, g.get("cpu", 0)))
            lines.append(_line(f"mnm_proxmox_{prefix}_memory_used_bytes", lbl, g.get("mem", 0)))
            lines.append(_line(f"mnm_proxmox_{prefix}_memory_total_bytes", lbl, g.get("maxmem", 0)))
            lines.append(_line(f"mnm_proxmox_{prefix}_disk_read_bytes", lbl, g.get("diskread", 0)))
            lines.append(_line(f"mnm_proxmox_{prefix}_disk_write_bytes", lbl, g.get("diskwrite", 0)))
            lines.append(_line(f"mnm_proxmox_{prefix}_netin_bytes", lbl, g.get("netin", 0)))
            lines.append(_line(f"mnm_proxmox_{prefix}_netout_bytes", lbl, g.get("netout", 0)))
            lines.append(_line(f"mnm_proxmox_{prefix}_status", lbl, running))
            lines.append(_line(f"mnm_proxmox_{prefix}_uptime_seconds", lbl, g.get("uptime", 0)))

    # ---- Storage pools ----
    lines.append("# TYPE mnm_proxmox_storage_used_bytes gauge")
    lines.append("# TYPE mnm_proxmox_storage_total_bytes gauge")
    lines.append("# TYPE mnm_proxmox_storage_available_bytes gauge")
    for s in _state["storage"]:
        lbl = {"node": s["node"], "storage": s["storage"], "type": s["type"]}
        lines.append(_line("mnm_proxmox_storage_used_bytes", lbl, s.get("used", 0)))
        lines.append(_line("mnm_proxmox_storage_total_bytes", lbl, s.get("total", 0)))
        lines.append(_line("mnm_proxmox_storage_available_bytes", lbl, s.get("avail", 0)))

    # ---- ZFS pools ----
    for metric in ("size_bytes", "used_bytes", "free_bytes",
                    "fragmentation_percent", "health", "dedup_ratio"):
        lines.append(f"# TYPE mnm_proxmox_zfs_pool_{metric} gauge")
    for p in _state["zfs_pools"]:
        lbl = {"node": p["node"], "pool": p["name"]}
        lines.append(_line("mnm_proxmox_zfs_pool_size_bytes", lbl, p.get("size", 0)))
        lines.append(_line("mnm_proxmox_zfs_pool_used_bytes", lbl, p.get("alloc", 0)))
        lines.append(_line("mnm_proxmox_zfs_pool_free_bytes", lbl, p.get("free", 0)))
        lines.append(_line("mnm_proxmox_zfs_pool_fragmentation_percent", lbl, p.get("frag", 0)))
        lines.append(_line("mnm_proxmox_zfs_pool_health", lbl, _health_to_int(p.get("health", ""))))
        lines.append(_line("mnm_proxmox_zfs_pool_dedup_ratio", lbl, p.get("dedup", 1.0)))

    # ---- Physical disks ----
    lines.append("# TYPE mnm_proxmox_disk_size_bytes gauge")
    lines.append("# TYPE mnm_proxmox_disk_health gauge")
    for d in _state["disks"]:
        lbl = {
            "node": d["node"],
            "device": d.get("devpath", "").split("/")[-1],
            "model": d.get("model", ""),
            "type": d.get("type", ""),
        }
        lines.append(_line("mnm_proxmox_disk_size_bytes", lbl, d.get("size", 0)))
        lines.append(_line("mnm_proxmox_disk_health", lbl, _health_to_int(d.get("health", ""))))

    return "\n".join(lines) + "\n"
