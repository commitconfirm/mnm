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

**IPAM-noise filtering.** Some vendors expose internal control-plane
addresses on ``lo0.X``-style virtual interfaces that are per-device-
internal — not routable, not operationally meaningful, and identical
across multiple devices of the same family. Tracking them in IPAM
creates duplicate-key collisions when a second device of the same
family onboards. Currently filtered:
  * Junos ``128.0.0.0/8`` — Juniper-reserved internal control-plane
    range (lo0.X management addresses, packet-forwarding-host stub,
    chassis-backplane addresses). Surfaces with bizarre /2 prefix
    masks via SNMP and collides across every Junos device. Per
    Juniper documentation these aren't operator-visible and have no
    routing value; filter them out at the SNMP collection boundary.

**IP-create parent semantics.** Nautobot 3.x rejects
``parent=<same-length-prefix>`` on IP creation (e.g. cannot use
``128.0.0.0/2`` as parent for ``128.0.0.1/2``) but accepts a
namespace-resolved parent of the same shape (i.e., POST with
``namespace=<uuid>``, no ``parent``, and Nautobot picks the same
prefix as parent without complaining). It also rejects POST with
just ``namespace=`` if no containing prefix exists in the namespace.
The flow that handles all three classes (normal /24, Junos /2,
PAN-OS /0):
  1. Ensure a covering prefix exists via ``ensure_prefix`` (creates
     if missing).
  2. POST with ``namespace=<uuid>`` (NOT ``parent=<id>``); Nautobot
     auto-resolves the most-specific containing prefix.
This sidesteps the same-length-rejection and the prefix_length=0
(SNMP-degenerate) cases without per-vendor branching.

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


# Per-vendor internal-control-plane address ranges that show up via
# SNMP but aren't operationally meaningful and collide across devices
# of the same family. See module docstring "IPAM-noise filtering".
_IPAM_NOISE_RANGES_V4: tuple[ipaddress.IPv4Network, ...] = (
    # Junos lo0.X / pfh / cbp internal management. Per Juniper docs,
    # 128.0.0.0/8 is reserved for internal use; SNMP exposes these
    # with bizarre /2 prefix masks that fail Nautobot's parent-
    # validation and the addresses themselves duplicate across every
    # Junos chassis. Filter at collection.
    ipaddress.IPv4Network("128.0.0.0/8"),
)


def _is_ipam_noise(ip_str: str) -> bool:
    """True if ``ip_str`` falls in a known per-vendor internal range.

    See ``_IPAM_NOISE_RANGES_V4``. Filtered at the boundary so the
    rest of Phase 2 doesn't have to think about them.
    """
    try:
        addr = ipaddress.IPv4Address(ip_str)
    except (ValueError, TypeError):
        return False
    return any(addr in net for net in _IPAM_NOISE_RANGES_V4)


async def _collect_ips(
    device_ip: str, snmp_community: str,
) -> tuple[list[tuple[str, "int | None", int]], bool]:
    """Return (list of (ip, prefix_length, ifIndex), used_fallback).

    Walk ``ipAddressTable`` first (modern MIB; supports v4+v6). If the
    walk yields no rows, fall back to ``ipAddrTable`` (RFC1213, v4-only).

    IPAM-noise addresses (per ``_IPAM_NOISE_RANGES_V4``) are filtered
    at this boundary — see module docstring.
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
                if _is_ipam_noise(ip_str):
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
            if _is_ipam_noise(suffix):
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


async def _lookup_ip_address(address: str, namespace_id: str) -> "dict | None":
    """Find an existing IPAddress in Nautobot by address + namespace.

    Used by the per-IP fallback path to reuse an existing IPAddress
    record that the bulk-create flagged as duplicate. Returns the IP
    record dict (id, address, etc.) or None if not found.

    IPAddress at depth=0 doesn't expose a ``namespace`` field directly —
    namespace lives on the parent Prefix. Query with depth=1 so
    ``parent.namespace`` is expanded, then filter by parent's
    namespace id.
    """
    from app import nautobot_client as _nc
    client = _nc._get_client()
    resp = await client.get(
        f"/api/ipam/ip-addresses/?address={address}&depth=1&limit=10",
        headers=_nc._headers(),
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    for rec in results:
        parent = rec.get("parent") or {}
        if not isinstance(parent, dict):
            continue
        parent_ns = parent.get("namespace") or {}
        if isinstance(parent_ns, dict) and parent_ns.get("id") == namespace_id:
            return rec
    return None


async def _create_ips_per_ip_fallback(
    payloads: list[dict], namespace_id: str,
) -> "list[dict] | None":
    """Per-IP create with reuse-on-duplicate semantics.

    Called when the bulk create_ip_addresses_bulk POST fails with a 400
    (Nautobot's bulk endpoint rolls back the whole batch on any single
    record's validation error). Iterates the payloads and creates each
    IP individually; on the per-IP duplicate-detection error, looks up
    the existing record and reuses its UUID.

    Returns the resulting list of IP records in the same order as
    ``payloads``, or ``None`` if a non-duplicate error occurred (caller
    should treat as a hard failure). Each returned record carries an
    extra ``_phase2_origin`` key set to ``"created"`` or ``"reused"``
    so the caller can track per-IP outcomes.
    """
    from app import nautobot_client as _nc
    out: list[dict] = []
    for payload in payloads:
        addr_with_mask = payload["address"]
        try:
            single = await _nc.create_ip_addresses_bulk([payload])
            if single:
                rec = dict(single[0])
                rec["_phase2_origin"] = "created"
                out.append(rec)
            else:
                # Empty result on success is unexpected but non-fatal; treat
                # as if Nautobot validated then returned no body.
                out.append({"_phase2_origin": "created", "id": None,
                            "address": addr_with_mask})
        except Exception as exc:  # noqa: BLE001
            body = getattr(exc, "response_body", None)
            is_duplicate = False
            if isinstance(body, list) and body:
                first = body[0]
                if isinstance(first, dict):
                    msgs = first.get("__all__") or []
                    if any("already exists" in str(m).lower() for m in msgs):
                        is_duplicate = True
            if not is_duplicate:
                log.warning("phase2_per_ip_create_failed",
                            "per-IP create raised non-duplicate error",
                            context={"address": addr_with_mask,
                                     "error": str(exc)[:200]})
                return None
            existing = await _lookup_ip_address(addr_with_mask, namespace_id)
            if existing is None:
                log.warning("phase2_per_ip_lookup_missed",
                            "duplicate-detected IP not found on lookup",
                            context={"address": addr_with_mask})
                return None
            rec = dict(existing)
            rec["_phase2_origin"] = "reused"
            out.append(rec)
            log.debug("phase2_per_ip_reused_existing",
                      "reused existing IPAddress for cross-device-shared IP",
                      context={"address": addr_with_mask,
                               "ip_id": existing.get("id")})
    return out


async def _link_ips_per_link_fallback(payloads: list[dict]) -> bool:
    """Per-link create with skip-on-duplicate semantics.

    Called when ``link_ips_to_interfaces_bulk`` fails with a 400. Most
    common cause: idempotent re-run — the link record
    ``(interface, ip_address)`` already exists from a prior Phase 2
    attempt. Per-link POST: create if absent, skip silently if
    "already exists" / "must make a unique set" 400, return False on
    any other error.

    Returns True if every payload was either created or accepted as
    already-linked; False on any non-duplicate error (caller should
    treat as Phase 2 failure).
    """
    from app import nautobot_client as _nc
    for payload in payloads:
        try:
            await _nc.link_ips_to_interfaces_bulk([payload])
        except Exception as exc:  # noqa: BLE001
            body = getattr(exc, "response_body", None)
            already_linked = False
            if isinstance(body, list) and body:
                first = body[0]
                if isinstance(first, dict):
                    msgs = first.get("non_field_errors") or []
                    msgs += first.get("__all__") or []
                    blob = " ".join(str(m) for m in msgs).lower()
                    if "must make a unique set" in blob \
                            or "already exists" in blob:
                        already_linked = True
            if not already_linked:
                log.warning("phase2_per_link_create_failed",
                            "per-link create raised non-duplicate error",
                            context={"ip_id": payload.get("ip_address"),
                                     "interface": payload.get("interface"),
                                     "error": str(exc)[:200]})
                return False
            log.debug("phase2_per_link_already_linked",
                      "skipping link — record already exists",
                      context={"ip_id": payload.get("ip_address"),
                               "interface": payload.get("interface")})
    return True


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

    # Resolve the Global namespace UUID once. Per the module docstring
    # "IP-create parent semantics", we POST IPs with namespace=<uuid>
    # rather than parent=<prefix-id> so Nautobot auto-resolves the
    # parent — sidesteps the same-length-prefix rejection that surfaces
    # on Junos /2 lo0 addresses and the prefix_length=0 (SNMP-degenerate)
    # case on PAN-OS.
    global_namespace_id: "str | None" = None
    try:
        for ns in await nautobot_client.get_namespaces():
            if ns.get("name") == "Global" or ns.get("display") == "Global":
                global_namespace_id = ns["id"]
                break
    except Exception as exc:  # noqa: BLE001
        result.error = f"phase2_resolve_namespace failed: {exc}"
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        return result
    if global_namespace_id is None:
        result.error = "Global namespace not found in Nautobot"
        result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
        return result

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
        # ensure_prefix guarantees a containing prefix exists in the
        # namespace — required for namespace-resolved parent on the
        # subsequent IP POST. We don't pass the resulting prefix as an
        # explicit parent (see docstring "IP-create parent semantics").
        try:
            await nautobot_client.ensure_prefix(
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
            "namespace": global_namespace_id,
        })
        link_plans.append((ifindex, address_with_mask, prefix_len))

    created_ips: list[dict] = []
    if ip_create_payloads:
        try:
            created_ips = await nautobot_client.create_ip_addresses_bulk(
                ip_create_payloads,
            )
            result.ips_added = len(created_ips)
            log.info("phase2_bulk_create_ips", "IPs bulk-created",
                     context={"device_id": device_id,
                              "count": len(created_ips)})
        except Exception as exc:  # noqa: BLE001
            # Nautobot bulk POST is transactional: any single record's
            # validation failure rolls back the whole batch. Most common
            # cause in v1.0 lab: cross-device IP collision (e.g.,
            # 10.0.0.1 exists on cisco-mnm Loopback and pa-440 mgmt
            # interface — same address, different interfaces). Fall
            # back to per-IP create with reuse-on-duplicate semantics
            # so one collision doesn't sink the whole device's Phase 2.
            log.info("phase2_bulk_create_ips_fallback",
                     "bulk create rejected, falling back per-IP",
                     context={"device_id": device_id,
                              "payload_count": len(ip_create_payloads),
                              "error": str(exc)[:200]})
            created_ips = await _create_ips_per_ip_fallback(
                ip_create_payloads, global_namespace_id,
            )
            if created_ips is None:
                # Per-IP fallback raised on a non-duplicate error; bail.
                result.error = f"phase2_bulk_create_ips failed: {exc}"
                result.duration_ms = round((time.monotonic() - t0) * 1000, 1)
                log.warning("phase2_failed",
                            "bulk + per-IP fallback both failed",
                            context={"device_id": device_id,
                                     "error": str(exc)})
                return result
            result.ips_added = sum(1 for r in created_ips
                                   if r.get("_phase2_origin") == "created")
            result.ips_reused += sum(1 for r in created_ips
                                     if r.get("_phase2_origin") == "reused")
            log.info("phase2_bulk_create_ips",
                     "IPs reconciled (per-IP fallback)",
                     context={"device_id": device_id,
                              "added": result.ips_added,
                              "reused_existing": sum(1 for r in created_ips
                                  if r.get("_phase2_origin") == "reused")})

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
            # Same transactional-rollback class as bulk_create_ips. The
            # most common cause: idempotent re-run on a partially-onboarded
            # device — link record (interface, ip_address) already exists.
            # Fall back to per-link create with skip-on-duplicate.
            log.info("phase2_bulk_link_fallback",
                     "bulk link rejected, falling back per-link",
                     context={"device_id": device_id,
                              "payload_count": len(link_payloads),
                              "error": str(exc)[:200]})
            ok = await _link_ips_per_link_fallback(link_payloads)
            if not ok:
                result.error = f"phase2_bulk_link failed: {exc}"
                result.duration_ms = round(
                    (time.monotonic() - t0) * 1000, 1,
                )
                log.warning("phase2_failed",
                            "bulk + per-link fallback both failed",
                            context={"device_id": device_id,
                                     "error": str(exc)})
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
