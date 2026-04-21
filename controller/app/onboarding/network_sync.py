"""Phase 2 network data sync for direct-REST onboarding (Prompt 6).

Runs after Phase 1 completes. Walks the device's interface and IP tables
over SNMP, reconciles the results against Nautobot, and bulk-creates any
interfaces / IPs that don't already exist. Does NOT modify existing
records (interface-property updates are v1.1 scope — flagged inline).

**Execution model.** Phase 2 runs as a one-shot polling job type
(``phase2_populate``), not as an async task launched directly from the
orchestrator. The operator chose this over the design-doc-recommended
async-fire-and-forget (Section 2 Question B) so that Phase 2 inherits
retry, container-restart recovery, and observability from the existing
polling loop infrastructure. Tradeoff: up to ``MNM_POLL_CHECK_INTERVAL``
(default 30s) latency between Phase 1 success and Phase 2 start.

**Status state machine (three-state).**
- Phase 2 success → device stays ``Active`` (Phase 1 set it).
- Phase 2 failure → polling dispatch flips device to
  ``Onboarding Incomplete`` and leaves the poll row enabled with a
  backed-off ``next_due``; the retry happens naturally on the next
  polling cycle. A successful retry transitions back to ``Active``.
- ``Onboarding Failed`` is reachable only from catastrophic Phase 1
  failure; Phase 2 never transitions to it.

**Bulk writes.** Nautobot 3.x accepts a JSON array body on its list
endpoints for one-shot multi-record creation; we use
:func:`nautobot_client.create_interfaces_bulk`,
:func:`create_ip_addresses_bulk`, and
:func:`link_ips_to_interfaces_bulk`. One round trip per category
instead of N.

**Fallback patterns** (reality-check §5.2):
  * ``ipAddressTable`` (IP-MIB, modern, v4+v6) empty → fall back to
    ``ipAddrTable`` (RFC1213-MIB, legacy, v4-only). EX3300 12.3R12
    doesn't implement the modern table.
  * ``ifName`` blank per-row → fall back to ``ifDescr``. FortiGate
    returns empty ``ifDescr``; others sometimes do the opposite.

**Template-interface reuse** (reality-check §4.4): device creation
auto-populates interfaces from the DeviceType template. Phase 1's
management interface is one example; physical DeviceTypes may have
dozens. Phase 2 always queries the full interface list for the device
and only POSTs names that aren't already there.

Credential hygiene (CLAUDE.md Rule 8): SNMP community must never appear
in any log record. All logging uses UUIDs, IPs, and interface counts.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass

from app import nautobot_client, snmp_collector
from app.logging_config import StructuredLogger
from app.snmp_collector import SnmpError, oid, walk_table

log = StructuredLogger(__name__, module="phase2")


@dataclass
class Phase2Result:
    """Outcome of a Phase 2 run."""

    success: bool
    device_id: str
    interfaces_added: int = 0
    interfaces_reused: int = 0
    ips_added: int = 0
    ips_reused: int = 0
    error: "str | None" = None
    used_ipaddrtable_fallback: bool = False
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# SNMP collection
# ---------------------------------------------------------------------------

def _decode_bytes(val) -> str:
    if val is None:
        return ""
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")
    return str(val)


async def _collect_interfaces(
    device_ip: str, snmp_community: str,
) -> list[tuple[int, str]]:
    """Return (ifIndex, name) tuples from ifName preferred, ifDescr fallback.

    Skips rows with blank names after both fallbacks, and skips ``ifIndex=0``.
    """
    # Walk ifName (ifXTable col 1) — each row is {ifIndex: name}.
    ifname_rows = await walk_table(
        device_ip, snmp_community, oid("IF-MIB::ifName"),
    )
    ifname_map: dict[int, str] = {}
    for row in ifname_rows:
        for suffix, value in row.items():
            try:
                idx = int(suffix)
            except ValueError:
                continue
            text = _decode_bytes(value).strip()
            if text:
                ifname_map[idx] = text

    # Walk ifDescr (ifTable col 2) as the fallback source.
    ifdescr_rows = await walk_table(
        device_ip, snmp_community, oid("IF-MIB::ifDescr"),
    )
    ifdescr_map: dict[int, str] = {}
    for row in ifdescr_rows:
        for suffix, value in row.items():
            try:
                idx = int(suffix)
            except ValueError:
                continue
            text = _decode_bytes(value).strip()
            if text:
                ifdescr_map[idx] = text

    # Merge: ifName preferred; fall back to ifDescr. Union keys to catch
    # cases where one table has a row the other doesn't.
    all_indexes = set(ifname_map) | set(ifdescr_map)
    out: list[tuple[int, str]] = []
    for idx in sorted(all_indexes):
        if idx == 0:
            continue
        name = ifname_map.get(idx) or ifdescr_map.get(idx) or ""
        if not name:
            continue
        out.append((idx, name))
    return out


async def _collect_ips(
    device_ip: str, snmp_community: str,
) -> tuple[list[tuple[str, "int | None", int]], bool]:
    """Return (list of (ip, prefix_length, ifIndex), used_fallback).

    Walk ``ipAddressTable`` first (modern MIB; supports v4+v6). If the
    walk yields no rows, fall back to ``ipAddrTable`` (RFC1213, v4-only).
    """
    modern_rows: list = []
    modern_ok = True
    try:
        modern_rows = await walk_table(
            device_ip, snmp_community, oid("IP-MIB::ipAddressIfIndex"),
        )
    except SnmpError:
        modern_ok = False
        modern_rows = []

    if modern_ok and modern_rows:
        # Index shape for ipAddressTable: InetAddressType.InetAddress,
        # encoded as "<type>.<len>.<octets>" — e.g. "1.4.10.0.0.1" for
        # IPv4 10.0.0.1. Parse octets; skip anything that doesn't look
        # like a v4 row for now (v6 full support is v2.0 scope per
        # CLAUDE.md).
        ifindex_map: dict[str, int] = {}
        for row in modern_rows:
            for suffix, value in row.items():
                try:
                    idx = int(str(value))
                except (TypeError, ValueError):
                    continue
                ifindex_map[suffix] = idx

        # Walk ipAddressPrefix for prefix length information. The column
        # value is an OID pointing into ipAddressPrefixTable; the last
        # segment of that OID is the prefix length.
        prefix_map: dict[str, "int | None"] = {}
        try:
            prefix_rows = await walk_table(
                device_ip, snmp_community, oid("IP-MIB::ipAddressPrefix"),
            )
            for row in prefix_rows:
                for suffix, value in row.items():
                    try:
                        last = str(value).rsplit(".", 1)[-1]
                        prefix_map[suffix] = int(last)
                    except (ValueError, AttributeError):
                        prefix_map[suffix] = None
        except SnmpError:
            pass

        out: list[tuple[str, "int | None", int]] = []
        for suffix, ifidx in ifindex_map.items():
            parts = suffix.split(".")
            # IPv4 entry shape: "1.4.a.b.c.d"
            if len(parts) >= 6 and parts[0] == "1" and parts[1] == "4":
                ip_str = ".".join(parts[2:6])
                try:
                    ipaddress.IPv4Address(ip_str)
                except ValueError:
                    continue
                out.append((ip_str, prefix_map.get(suffix), ifidx))
        return (out, False)

    # Fallback: ipAdEntTable (legacy).
    used_fallback = True
    try:
        ifindex_rows = await walk_table(
            device_ip, snmp_community, oid("RFC1213-MIB::ipAdEntIfIndex"),
        )
    except SnmpError:
        ifindex_rows = []

    try:
        netmask_rows = await walk_table(
            device_ip, snmp_community, oid("RFC1213-MIB::ipAdEntNetMask"),
        )
    except SnmpError:
        netmask_rows = []

    netmask_map: dict[str, str] = {}
    for row in netmask_rows:
        for suffix, value in row.items():
            netmask_map[suffix] = _decode_bytes(value) or str(value)

    out = []
    for row in ifindex_rows:
        for suffix, value in row.items():
            try:
                idx = int(str(value))
            except (TypeError, ValueError):
                continue
            try:
                ipaddress.IPv4Address(suffix)  # suffix IS the IP for ipAdEntTable
            except ValueError:
                continue
            prefix_len: "int | None" = None
            mask_str = netmask_map.get(suffix, "")
            if mask_str:
                try:
                    prefix_len = ipaddress.IPv4Network(
                        f"0.0.0.0/{mask_str}", strict=False,
                    ).prefixlen
                except ValueError:
                    prefix_len = None
            out.append((suffix, prefix_len, idx))
    return (out, used_fallback)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

async def _active_status_id() -> "str | None":
    rec = await nautobot_client.get_status_by_name(
        "Active", content_type="dcim.device",
    )
    return rec["id"] if rec else None


def _covering_cidr(ip: str, prefix_length: "int | None") -> tuple[str, str]:
    length = prefix_length if prefix_length else 32
    try:
        net = ipaddress.ip_network(f"{ip}/{length}", strict=False)
        return (f"{ip}/{length}", str(net))
    except ValueError:
        return (f"{ip}/32", f"{ip}/32")


async def run_phase2(
    device_id: str,
    device_name: str,
    device_ip: str,
    snmp_community: str,
) -> Phase2Result:
    """Phase 2 network data sync. See module docstring for semantics."""
    t0 = time.monotonic()
    result = Phase2Result(success=False, device_id=device_id)

    log.info("phase2_start", "Phase 2 network sync starting",
             context={"device_id": device_id, "device_name": device_name,
                      "device_ip": device_ip,
                      "snmp_community_set": bool(snmp_community)})

    # ---- Collect interfaces ----
    try:
        interface_rows = await _collect_interfaces(device_ip, snmp_community)
    except Exception as exc:  # noqa: BLE001
        result.error = f"phase2_collect_ifaces failed: {exc}"
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        log.warning("phase2_failed", "interface collection failed",
                    context={"device_id": device_id, "error": str(exc)})
        return result

    if not interface_rows:
        result.error = "phase2_collect_ifaces returned no rows (device unreachable?)"
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        log.warning("phase2_failed", "empty ifTable",
                    context={"device_id": device_id, "device_ip": device_ip})
        return result

    log.info("phase2_collect_ifaces", "interfaces collected",
             context={"device_id": device_id, "count": len(interface_rows)})

    # ---- Collect IPs ----
    try:
        ip_rows, used_fallback = await _collect_ips(device_ip, snmp_community)
    except Exception as exc:  # noqa: BLE001
        result.error = f"phase2_collect_ips failed: {exc}"
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        log.warning("phase2_failed", "IP collection failed",
                    context={"device_id": device_id, "error": str(exc)})
        return result
    result.used_ipaddrtable_fallback = used_fallback
    if used_fallback:
        log.info("phase2_ipaddrtable_fallback",
                 "ipAddressTable empty; using legacy ipAdEntTable",
                 context={"device_id": device_id, "ip_count": len(ip_rows)})
    log.info("phase2_collect_ips", "IPs collected",
             context={"device_id": device_id, "count": len(ip_rows)})

    # ---- Reconcile interfaces ----
    try:
        existing_ifaces = await nautobot_client.get_interfaces_for_device(device_id)
    except Exception as exc:  # noqa: BLE001
        result.error = f"phase2_get_interfaces failed: {exc}"
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        return result

    existing_name_to_id: dict[str, str] = {}
    for ifc in existing_ifaces:
        name = ifc.get("name")
        if name:
            existing_name_to_id[name] = ifc["id"]

    status_id = await _active_status_id()
    if status_id is None:
        result.error = "Active status not found in Nautobot"
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        return result

    to_create_ifaces: list[dict] = []
    ifindex_to_name: dict[int, str] = {}
    for ifindex, name in interface_rows:
        ifindex_to_name[ifindex] = name
        if name in existing_name_to_id:
            result.interfaces_reused += 1
            continue
        to_create_ifaces.append({
            "device": device_id,
            "name": name,
            "type": "other",  # generic; interface-type refinement is v1.1
            "status": status_id,
        })
    # TODO(v1.1): interface-property updates (mtu/speed/type) on existing
    # rows. Phase 2 creates missing interfaces only.

    if to_create_ifaces:
        try:
            created = await nautobot_client.create_interfaces_bulk(to_create_ifaces)
        except Exception as exc:  # noqa: BLE001
            result.error = f"phase2_bulk_create_ifaces failed: {exc}"
            result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
            log.warning("phase2_failed", "bulk create_interfaces failed",
                        context={"device_id": device_id, "error": str(exc)})
            return result
        for rec in created:
            name = rec.get("name")
            if name:
                existing_name_to_id[name] = rec["id"]
        result.interfaces_added = len(created)
        log.info("phase2_bulk_create_ifaces", "interfaces bulk-created",
                 context={"device_id": device_id, "count": len(created)})

    # ---- Reconcile IPs ----
    # Build existing-IP set for the device to skip duplicates (primary_ip4,
    # plus any IPs assigned in the interim).
    try:
        # depth=1 is required to get primary_ip4.address. Without it,
        # primary_ip4 has only id/url and the skip-primary branch below
        # silently fails to match.
        existing_device = await nautobot_client.get_device(device_id, depth=1)
    except Exception as exc:  # noqa: BLE001
        result.error = f"phase2_get_device failed: {exc}"
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        return result
    primary_ip_address = ""
    pip = existing_device.get("primary_ip4") or {}
    if isinstance(pip, dict):
        primary_ip_address = (pip.get("display") or pip.get("address") or "").split("/")[0]

    ip_create_payloads: list[dict] = []
    link_plans: list[tuple[int, str, "int | None"]] = []  # (ifindex, address_with_mask, prefix_len)
    for ip_str, prefix_len, ifindex in ip_rows:
        if ip_str == primary_ip_address:
            result.ips_reused += 1
            continue
        # Pre-clean any standalone IPAM record (sweep pipeline may have
        # registered it). Safe per Prompt 5 pattern.
        try:
            await nautobot_client.delete_standalone_ip(ip_str)
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.debug("phase2_preclean_ip_failed",
                      "standalone IP pre-clean raised (non-fatal)",
                      context={"ip": ip_str, "error": str(exc)})
        address_with_mask, covering_cidr = _covering_cidr(ip_str, prefix_len)
        try:
            prefix_rec = await nautobot_client.ensure_prefix(
                covering_cidr, namespace="Global",
            )
        except Exception as exc:  # noqa: BLE001
            result.error = f"phase2_ensure_prefix failed for {covering_cidr}: {exc}"
            result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
            return result
        ip_create_payloads.append({
            "address": address_with_mask,
            "status": status_id,
            "type": "host",
            "parent": prefix_rec["id"],
        })
        link_plans.append((ifindex, address_with_mask, prefix_len))

    created_ips: list[dict] = []
    if ip_create_payloads:
        try:
            created_ips = await nautobot_client.create_ip_addresses_bulk(
                ip_create_payloads,
            )
        except Exception as exc:  # noqa: BLE001
            result.error = f"phase2_bulk_create_ips failed: {exc}"
            result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
            log.warning("phase2_failed", "bulk create_ip_addresses failed",
                        context={"device_id": device_id, "error": str(exc)})
            return result
        result.ips_added = len(created_ips)
        log.info("phase2_bulk_create_ips", "IPs bulk-created",
                 context={"device_id": device_id, "count": len(created_ips)})

    # Link IPs to interfaces. Match up by ordering — the bulk POST
    # returns records in request order, and our ``link_plans`` list is in
    # the same order as ``ip_create_payloads``.
    link_payloads: list[dict] = []
    for (ifindex, _addr, _plen), ip_rec in zip(link_plans, created_ips):
        name = ifindex_to_name.get(ifindex)
        iface_uuid = existing_name_to_id.get(name or "")
        if not iface_uuid:
            log.warning("phase2_link_skip",
                        "ifIndex has no matching interface in Nautobot",
                        context={"device_id": device_id, "ifindex": ifindex,
                                 "name": name, "ip_id": ip_rec.get("id")})
            continue
        link_payloads.append({
            "ip_address": ip_rec["id"],
            "interface": iface_uuid,
            "is_primary": False,
        })

    if link_payloads:
        try:
            await nautobot_client.link_ips_to_interfaces_bulk(link_payloads)
        except Exception as exc:  # noqa: BLE001
            result.error = f"phase2_bulk_link failed: {exc}"
            result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
            log.warning("phase2_failed", "bulk link_ip_to_interface failed",
                        context={"device_id": device_id, "error": str(exc)})
            # Phase 2 has no rollback — retry-in-place is the semantic.
            return result

    result.success = True
    result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
    log.info("phase2_complete", "Phase 2 network sync complete",
             context={"device_id": device_id, "device_name": device_name,
                      "interfaces_added": result.interfaces_added,
                      "interfaces_reused": result.interfaces_reused,
                      "ips_added": result.ips_added,
                      "ips_reused": result.ips_reused,
                      "used_fallback": result.used_ipaddrtable_fallback,
                      "duration_ms": result.duration_ms})
    return result
