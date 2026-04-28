"""Phase 1 onboarding orchestrator — direct Nautobot REST API (Prompt 4).

Drives the 6-step creation sequence documented in
``.claude/design/nautobot_rest_schema_notes.md`` §1.3 for a single
device. Junos is the only supported vendor in v1.0 Prompt 4; Arista
(Prompt 5), PAN-OS (Prompt 7), and FortiGate (Prompt 7.5) follow.

Flow:

  Step 0    classify (:mod:`app.onboarding.classifier`) + probe
            (:mod:`app.onboarding.probes.<vendor>`)
  Step 0.5  strict-new pre-check: refuse if (name, location) already
            exists (operator Q2 decision, reality-check §4.5)
  Step A    POST /api/dcim/devices/ with status=Active
  Step B    query-and-reuse management interface (reality-check §4.4 —
            device-type templates auto-create interfaces)
  Step C    ensure_prefix for the management CIDR
  Step D    POST /api/ipam/ip-addresses/
  Step E    POST /api/ipam/ip-address-to-interface/ (through model,
            reality-check §1.4)
  Step F    PATCH device.primary_ip4 (Nautobot validates Step E
            ran first — reality-check §4.2)
  Step G    polling.ensure_device_polls hand-off
  Step H    nautobot_client.clear_cache

Rollback asymmetry (reality-check §4.7): DELETE /dcim/devices/ cascades
interfaces but NOT IPs. Steps E/F rollback explicitly call
:func:`nautobot_client.delete_standalone_ip` in addition to deleting the
device. Step G failure is a special case: the device is fully wired and
correct in Nautobot — tearing it down to heal a polling seed failure is
worse than marking status "Onboarding Incomplete" and returning a
non-success result. Operators retry polling or re-onboard.

The orchestrator **returns** an :class:`OnboardingResult` rather than
raising on failure. Both the sweep UI and future single-device
onboarding form need structured failure reporting; exceptions would
force every caller to wrap every call site.

Logging discipline (CLAUDE.md Rule 8): never log SNMP communities,
SecretsGroup contents, or anything resembling a credential. IPs,
hostnames, chassis models, and UUIDs are fine — they are in Nautobot
anyway.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field

from app import nautobot_client
from app.logging_config import StructuredLogger
from app.onboarding.classifier import ClassifierResult, classify
from app.onboarding.probes import arista as _arista_probe
from app.onboarding.probes import cisco as _cisco_probe
from app.onboarding.probes import fortinet as _fortinet_probe
from app.onboarding.probes import junos as _junos_probe
from app.onboarding.probes import paloalto as _paloalto_probe

log = StructuredLogger(__name__, module="onboarding")


# ---------------------------------------------------------------------------
# Public surface — result + exception hierarchy
# ---------------------------------------------------------------------------

@dataclass
class OnboardingResult:
    """Structured outcome of a single Phase 1 onboarding attempt."""

    success: bool
    device_id: "str | None" = None
    device_name: "str | None" = None
    classification: "ClassifierResult | None" = None
    phase1_steps_completed: list[str] = field(default_factory=list)
    error: "str | None" = None
    rollback_performed: bool = False


class OnboardingError(Exception):
    """Base class for orchestrator-specific failures."""


class AlreadyOnboardedError(OnboardingError):
    """Strict-new pre-check refused (operator Q2). A device with the same
    name exists at the target location."""


class ClassificationFailedError(OnboardingError):
    """The classifier returned ``vendor=None`` (no signal matched) or
    raised during probing."""


class UnsupportedVendorError(OnboardingError):
    """Classifier succeeded but the vendor is not yet in the v1.0
    probe-module allowlist."""


class NautobotWriteError(OnboardingError):
    """Any 4xx/5xx from Nautobot during one of Steps A–F."""


class ProbeFailedError(OnboardingError):
    """The vendor probe (e.g. junos.probe_device_facts) raised."""


# ---------------------------------------------------------------------------
# Classification → Role + vendor → probe dispatch
# ---------------------------------------------------------------------------

# Classifier `classification` strings map onto Nautobot Role names created
# at bootstrap. ``network_device`` → ``Router`` is a generic fallback: it
# lets ambiguous network gear onboard; operators can retag post-hoc in
# Nautobot. ``unknown`` refuses onboarding entirely because there is no
# sensible Role to pick.
CLASSIFICATION_TO_ROLE_NAME: dict[str, "str | None"] = {
    "switch":         "Switch",
    "router":         "Router",
    "firewall":       "Firewall",
    "network_device": "Router",
    "access_point":   "Access Point",
    "endpoint":       "Endpoint",
    "unknown":        None,
}

# Vendor allowlist — v1.0 minimum vendor matrix per operator Q3.
# Prompt 4: juniper. Prompt 5: arista. Prompt 7: palo_alto. Prompt 7.5: fortinet.
# Block C.5: cisco (IOS-XE lab-validated on c8000v; classic IOS text-only).
SUPPORTED_VENDORS: set[str] = {"juniper", "arista", "palo_alto", "fortinet", "cisco"}

# Per-platform management interface name — the interface every vendor
# auto-creates (via device-type template) or that we POST if absent. This
# mirrors the map flagged for the orchestrator in reality-check §6.
MGMT_INTERFACE_NAME: dict[str, str] = {
    "juniper_junos":     "me0",
    "paloalto_panos":    "mgmt",
    "fortinet_fortios":  "mgmt",
    "arista_eos":        "Management1",
    # Cisco SNMP agents (verified on c8000v 17.16.1a) return short-form
    # interface names via ifName: ``Gi1``/``Gi2``/``Nu0`` rather than the
    # full ``GigabitEthernet1``/etc form shown in CLI. Match the SNMP
    # form so Phase 2's ifName walk doesn't create a *second* interface
    # record for the same physical port.
    "cisco_iosxe":       "Gi1",
    "cisco_ios":         "Gi0/0",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_role_id(role_name: str) -> "str | None":
    role = await nautobot_client.get_role_by_name(role_name)
    return role["id"] if role else None


async def _resolve_devicetype_id(chassis_model: "str | None") -> "str | None":
    if not chassis_model:
        return None
    # Reality-check §6: Nautobot device-type library uses uppercase model
    # names (EX2300-24P). Junos sysDescr / jnxBoxDescr return mixed case,
    # usually lowercase. Try upper first, then the literal form.
    for candidate in (chassis_model.upper(), chassis_model):
        rec = await nautobot_client.get_devicetype_by_model(candidate)
        if rec:
            return rec["id"]
    return None


async def _resolve_platform_id(platform_slug: "str | None") -> "str | None":
    if not platform_slug:
        return None
    rec = await nautobot_client.get_platform_by_name(platform_slug)
    return rec["id"] if rec else None


async def _resolve_active_status_id() -> "str | None":
    rec = await nautobot_client.get_status_by_name("Active", content_type="dcim.device")
    return rec["id"] if rec else None


async def _resolve_incomplete_status_id() -> "str | None":
    rec = await nautobot_client.get_status_by_name(
        "Onboarding Incomplete", content_type="dcim.device",
    )
    return rec["id"] if rec else None


def _covering_prefix(ip: str, prefix_length: "int | None") -> tuple[str, str]:
    """Return (ip_with_mask, covering_cidr) for Step C/D.

    When the probe couldn't determine the real interface prefix, we default
    to a /32 host route. Operators get the authoritative prefix once Phase 2
    walks ipAddressTable (Prompt 6).
    """
    length = prefix_length if prefix_length else 32
    try:
        net = ipaddress.ip_network(f"{ip}/{length}", strict=False)
        return (f"{ip}/{length}", str(net))
    except ValueError:
        # Fall back defensively — a malformed IP shouldn't reach here since
        # the operator supplied it, but guard anyway.
        return (f"{ip}/32", f"{ip}/32")


async def _probe_vendor(
    vendor: str,
    ip: str,
    snmp_community: str,
) -> _junos_probe.DeviceFacts:
    """Dispatch to the per-vendor probe module.

    Raises :class:`UnsupportedVendorError` for any vendor not in
    :data:`SUPPORTED_VENDORS`.
    """
    if vendor == "juniper":
        return await _junos_probe.probe_device_facts(ip, snmp_community)
    if vendor == "arista":
        return await _arista_probe.probe_device_facts(ip, snmp_community)
    if vendor == "palo_alto":
        return await _paloalto_probe.probe_device_facts(ip, snmp_community)
    if vendor == "fortinet":
        return await _fortinet_probe.probe_device_facts(ip, snmp_community)
    if vendor == "cisco":
        return await _cisco_probe.probe_device_facts(ip, snmp_community)
    raise UnsupportedVendorError(
        f"Phase 1 onboarding for vendor={vendor!r} not implemented in v1.0"
    )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

async def _rollback(
    *,
    device_id: "str | None",
    ip_id: "str | None",
    ip_address: "str | None",
    ctx: dict,
) -> bool:
    """Best-effort rollback. Returns True on clean rollback, False if any
    sub-step errored (all errors are logged, never raised).

    ``ip_id`` is the UUID from a Step D create; ``ip_address`` is the
    host-form string we can hand to
    :func:`nautobot_client.delete_standalone_ip` as a secondary path if the
    UUID-specific delete fails.
    """
    ok = True

    if device_id:
        try:
            await nautobot_client.delete_device(device_id)
            # Device delete cascades interfaces (reality-check §4.7) but
            # not the IP — that's handled below.
        except Exception as exc:  # noqa: BLE001 — defensive rollback
            ok = False
            log.error("onboarding_rollback_failed",
                      "device delete during rollback failed",
                      context={**ctx, "sub_step": "delete_device",
                               "device_id": device_id, "error": str(exc)})

    # Reality-check §4.7 asymmetry: IP does not cascade on device delete.
    # Clean it up explicitly. Try the UUID-targeted helper first; fall back
    # to the IP-host string helper if available.
    if ip_id:
        try:
            await nautobot_client.delete_ip_address(ip_id)
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.error("onboarding_rollback_failed",
                      "ip delete during rollback failed",
                      context={**ctx, "sub_step": "delete_ip_address",
                               "ip_id": ip_id, "error": str(exc)})
            if ip_address:
                try:
                    await nautobot_client.delete_standalone_ip(ip_address)
                except Exception as exc2:  # noqa: BLE001
                    log.error("onboarding_rollback_failed",
                              "fallback delete_standalone_ip failed",
                              context={**ctx, "sub_step": "delete_standalone_ip",
                                       "ip": ip_address, "error": str(exc2)})

    return ok


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def onboard_device(
    ip: str,
    snmp_community: str,
    secrets_group_id: str,
    location_id: str,
) -> OnboardingResult:
    """Phase 1 onboarding for a single device at ``ip``.

    Returns an :class:`OnboardingResult`. On any failure partway through
    Steps A–F the orchestrator rolls back (delete device; delete IP
    explicitly because of the reality-check §4.7 cascade asymmetry). Step G
    failure marks the device ``Onboarding Incomplete`` instead of rolling
    back (the device is valid; polling seeding can be retried). Step H
    failure is non-fatal — cache staleness is a 30-second annoyance.
    """
    t0 = time.monotonic()
    steps_done: list[str] = []
    ctx = {"ip": ip, "location_id": location_id,
           "secrets_group_id": secrets_group_id}

    log.info("onboarding_start", "Phase 1 onboarding starting", context=ctx)

    # ---------- Step 0: classify + probe ----------
    try:
        classification = await classify(ip, snmp_community)
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding_classify_failed", "classifier raised",
                    context={**ctx, "error": str(exc)})
        return OnboardingResult(
            success=False,
            classification=None,
            phase1_steps_completed=steps_done,
            error=f"ClassificationFailedError: classifier raised: {exc}",
        )
    steps_done.append("classify")
    log.info("onboarding_classify", "classifier result",
             context={**ctx,
                      "vendor": classification.vendor,
                      "platform": classification.platform,
                      "classification": classification.classification,
                      "confidence": classification.confidence})

    if classification.vendor is None:
        return OnboardingResult(
            success=False,
            classification=classification,
            phase1_steps_completed=steps_done,
            error="ClassificationFailedError: no vendor signal matched",
        )

    if classification.vendor not in SUPPORTED_VENDORS:
        return OnboardingResult(
            success=False,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=(f"UnsupportedVendorError: Phase 1 onboarding for "
                   f"vendor={classification.vendor!r} not yet implemented"),
        )

    try:
        facts = await _probe_vendor(classification.vendor, ip, snmp_community)
    except UnsupportedVendorError:
        raise
    except Exception as exc:  # noqa: BLE001 — probe failure is reported, not raised
        log.warning("onboarding_probe_failed", "vendor probe raised",
                    context={**ctx, "vendor": classification.vendor, "error": str(exc)})
        return OnboardingResult(
            success=False,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=f"ProbeFailedError: {exc}",
        )
    steps_done.append("probe")
    log.info("onboarding_probe", "vendor probe facts",
             context={**ctx,
                      "hostname": facts.hostname,
                      "serial": facts.serial,
                      "chassis_model": facts.chassis_model,
                      "os_version": facts.os_version})

    # ---------- Step 0.5: strict-new pre-check (Q2) ----------
    try:
        existing = await nautobot_client.find_device_at_location(
            facts.hostname, location_id,
        )
    except Exception as exc:  # noqa: BLE001 — bad input shouldn't crash
        return OnboardingResult(
            success=False,
            classification=classification,
            device_name=facts.hostname,
            phase1_steps_completed=steps_done,
            error=(f"NautobotWriteError: strict-new pre-check failed — "
                   f"check location_id is a valid UUID: {exc}"),
        )
    if existing is not None:
        log.info("onboarding_already_onboarded",
                 "strict-new refusal — device exists at location",
                 context={**ctx, "hostname": facts.hostname,
                          "existing_device_id": existing.get("id")})
        return OnboardingResult(
            success=False,
            device_id=existing.get("id"),
            device_name=facts.hostname,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=(f"AlreadyOnboardedError: device {facts.hostname!r} already "
                   f"exists at location {location_id}"),
        )
    steps_done.append("strict_new_check")

    # ---------- Reference lookups (run before Step A so we fail cleanly
    # without any rollback if a reference is missing) ----------
    role_name = CLASSIFICATION_TO_ROLE_NAME.get(classification.classification)
    if role_name is None:
        return OnboardingResult(
            success=False,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=(f"NautobotWriteError: classifier returned "
                   f"classification={classification.classification!r} which "
                   f"has no Nautobot Role mapping"),
        )

    role_id = await _resolve_role_id(role_name)
    if role_id is None:
        return OnboardingResult(
            success=False,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=(f"NautobotWriteError: Role {role_name!r} not found in "
                   "Nautobot. Ensure bootstrap created standard roles."),
        )

    devicetype_id = await _resolve_devicetype_id(facts.chassis_model)
    if devicetype_id is None:
        return OnboardingResult(
            success=False,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=(f"NautobotWriteError: DeviceType {facts.chassis_model!r} "
                   "not found. Run welcome-wizard device-type library import."),
        )

    platform_id = await _resolve_platform_id(classification.platform)

    active_status_id = await _resolve_active_status_id()
    if active_status_id is None:
        return OnboardingResult(
            success=False,
            classification=classification,
            phase1_steps_completed=steps_done,
            error="NautobotWriteError: stock 'Active' status missing in Nautobot",
        )

    # ---------- Step A: POST /api/dcim/devices/ ----------
    device_id: "str | None" = None
    iface_id: "str | None" = None
    ip_id: "str | None" = None
    address_with_mask, covering_cidr = _covering_prefix(
        ip, facts.management_prefix_length,
    )

    try:
        device_rec = await nautobot_client.create_device(
            name=facts.hostname,
            device_type_id=devicetype_id,
            location_id=location_id,
            role_id=role_id,
            status_id=active_status_id,
            platform_id=platform_id,
            serial=facts.serial,
        )
        device_id = device_rec["id"]
        steps_done.append("create_device")
        log.info("onboarding_step_A", "device created",
                 context={**ctx, "device_id": device_id,
                          "hostname": facts.hostname})
    except Exception as exc:  # noqa: BLE001
        return OnboardingResult(
            success=False,
            classification=classification,
            device_name=facts.hostname,
            phase1_steps_completed=steps_done,
            error=f"NautobotWriteError: Step A (create_device): {exc}",
        )

    # ---------- Step B: ensure management interface ----------
    mgmt_iface_name = MGMT_INTERFACE_NAME.get(
        classification.platform or "", "mgmt",
    )
    try:
        iface_rec = await nautobot_client.ensure_management_interface(
            device_id, mgmt_iface_name, active_status_id,
        )
        iface_id = iface_rec["id"]
        steps_done.append("ensure_mgmt_iface")
        log.info("onboarding_step_B", "management interface ready",
                 context={**ctx, "device_id": device_id,
                          "interface_id": iface_id,
                          "interface_name": mgmt_iface_name})
    except Exception as exc:  # noqa: BLE001
        rolled = await _rollback(
            device_id=device_id, ip_id=None, ip_address=None, ctx=ctx,
        )
        return OnboardingResult(
            success=False,
            device_name=facts.hostname,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=f"NautobotWriteError: Step B (ensure_mgmt_iface): {exc}",
            rollback_performed=rolled,
        )

    # ---------- Step C: ensure covering prefix ----------
    prefix_id: "str | None" = None
    try:
        prefix_rec = await nautobot_client.ensure_prefix(
            covering_cidr, namespace="Global",
        )
        prefix_id = prefix_rec.get("id") if isinstance(prefix_rec, dict) else None
        steps_done.append("ensure_prefix")
        log.info("onboarding_step_C", "covering prefix ready",
                 context={**ctx, "prefix": covering_cidr,
                          "prefix_id": prefix_id})
    except Exception as exc:  # noqa: BLE001
        rolled = await _rollback(
            device_id=device_id, ip_id=None, ip_address=None, ctx=ctx,
        )
        return OnboardingResult(
            success=False,
            device_name=facts.hostname,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=f"NautobotWriteError: Step C (ensure_prefix): {exc}",
            rollback_performed=rolled,
        )

    # ---------- Step D: POST /api/ipam/ip-addresses/ ----------
    # Pre-clean any standalone IPAM record for this address. The MNM sweep
    # pipeline records every alive IP into Nautobot IPAM during discovery,
    # so by the time we onboard the device, the target IP often already
    # exists as a standalone record. Step D's POST would 400 on uniqueness.
    # ``delete_standalone_ip`` only deletes when no device/interface is
    # assigned, so the strict-new pre-check at Step 0.5 already guarantees
    # this is safe. (Pattern copied from the legacy plugin path — see
    # Phase 2.5 "IPAM collision on onboarding" lesson in CLAUDE.md.)
    try:
        await nautobot_client.delete_standalone_ip(ip)
    except Exception as exc:  # noqa: BLE001 — pre-clean is best-effort
        log.debug("onboarding_step_D_preclean_failed",
                  "standalone IP pre-clean raised (non-fatal)",
                  context={**ctx, "error": str(exc)})

    try:
        # Nautobot 3.x requires one of ``parent`` or ``namespace`` on IP
        # creation (400 "One of parent or namespace must be provided"
        # otherwise). We always have the covering prefix from Step C —
        # pass its UUID as parent. Discovered live on vEOS 2026-04-20.
        ip_rec = await nautobot_client.create_ip_address(
            address=address_with_mask,
            status_id=active_status_id,
            parent_prefix_id=prefix_id,
        )
        ip_id = ip_rec["id"]
        steps_done.append("create_ip")
        log.info("onboarding_step_D", "IP address created",
                 context={**ctx, "ip_id": ip_id, "address": address_with_mask})
    except Exception as exc:  # noqa: BLE001
        rolled = await _rollback(
            device_id=device_id, ip_id=None, ip_address=None, ctx=ctx,
        )
        return OnboardingResult(
            success=False,
            device_name=facts.hostname,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=f"NautobotWriteError: Step D (create_ip_address): {exc}",
            rollback_performed=rolled,
        )

    # ---------- Step E: link IP to interface (through model) ----------
    try:
        await nautobot_client.link_ip_to_interface(
            ip_id, iface_id, is_primary=False,
        )
        steps_done.append("link_ip_iface")
        log.info("onboarding_step_E", "ip-to-interface link created",
                 context={**ctx, "ip_id": ip_id, "interface_id": iface_id})
    except Exception as exc:  # noqa: BLE001
        rolled = await _rollback(
            device_id=device_id, ip_id=ip_id, ip_address=ip, ctx=ctx,
        )
        return OnboardingResult(
            success=False,
            device_name=facts.hostname,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=f"NautobotWriteError: Step E (link_ip_to_interface): {exc}",
            rollback_performed=rolled,
        )

    # ---------- Step F: set primary_ip4 on device ----------
    try:
        await nautobot_client.set_device_primary_ip4(device_id, ip_id)
        steps_done.append("set_primary_ip4")
        log.info("onboarding_step_F", "primary_ip4 set",
                 context={**ctx, "device_id": device_id, "ip_id": ip_id})
    except Exception as exc:  # noqa: BLE001
        rolled = await _rollback(
            device_id=device_id, ip_id=ip_id, ip_address=ip, ctx=ctx,
        )
        return OnboardingResult(
            success=False,
            device_name=facts.hostname,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=f"NautobotWriteError: Step F (set_device_primary_ip4): {exc}",
            rollback_performed=rolled,
        )

    # ---------- Step G: polling seed hand-off ----------
    # Step G failure is handled specially: the device is fully wired and
    # valid in Nautobot. Tearing it down to repair a polling-seed issue is
    # worse than marking status "Onboarding Incomplete" and telling the
    # operator to retry polling. Operator can re-run onboarding (which will
    # hit AlreadyOnboardedError and surface the state) or invoke
    # polling.ensure_device_polls manually.
    from app import polling  # local import avoids a circular on app startup
    try:
        await polling.ensure_device_polls(facts.hostname)
        steps_done.append("ensure_device_polls")
        log.info("onboarding_step_G", "device_polls seeded",
                 context={**ctx, "device_name": facts.hostname})
        # Step G.5 — seed the one-shot phase2_populate row. Phase 2
        # (walk ifTable + ipAddressTable → bulk-create in Nautobot)
        # runs on the next polling-loop tick (≤ POLL_CHECK_INTERVAL).
        await polling.ensure_phase2_populate_row(facts.hostname)
        steps_done.append("ensure_phase2_populate")
        log.info("onboarding_step_G5", "phase2_populate row seeded",
                 context={**ctx, "device_name": facts.hostname})
    except Exception as exc:  # noqa: BLE001
        incomplete_status_id = await _resolve_incomplete_status_id()
        set_ok = False
        if incomplete_status_id:
            try:
                await nautobot_client.set_device_status(
                    device_id, incomplete_status_id,
                )
                set_ok = True
            except Exception as exc2:  # noqa: BLE001
                log.error("onboarding_rollback_failed",
                          "could not mark device Onboarding Incomplete",
                          context={**ctx, "device_id": device_id,
                                   "error": str(exc2)})
        log.warning("onboarding_step_G_failed",
                    "polling seed failed; device kept as Onboarding Incomplete",
                    context={**ctx, "device_id": device_id, "error": str(exc)})
        return OnboardingResult(
            success=False,
            device_id=device_id,
            device_name=facts.hostname,
            classification=classification,
            phase1_steps_completed=steps_done,
            error=(f"NautobotWriteError: Step G (ensure_device_polls) failed; "
                   f"device marked Onboarding Incomplete "
                   f"({'status set' if set_ok else 'status set ALSO failed'}): {exc}"),
            rollback_performed=False,
        )

    # ---------- Step H: invalidate reference-data cache ----------
    try:
        nautobot_client.clear_cache()
        steps_done.append("clear_cache")
    except Exception as exc:  # noqa: BLE001
        # Non-fatal — next GET just hits the API a second time.
        log.warning("onboarding_step_H_failed",
                    "cache invalidation failed (non-fatal)",
                    context={**ctx, "error": str(exc)})

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    log.info("onboarding_complete", "Phase 1 onboarding succeeded",
             context={**ctx, "device_id": device_id,
                      "device_name": facts.hostname,
                      "duration_ms": elapsed_ms,
                      "steps": steps_done})

    return OnboardingResult(
        success=True,
        device_id=device_id,
        device_name=facts.hostname,
        classification=classification,
        phase1_steps_completed=steps_done,
    )
