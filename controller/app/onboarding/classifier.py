"""Vendor + platform + role classification for onboarding candidates.

Extracted from ``app.discovery.classify_endpoint`` in v1.0 (Prompt 3 of the
direct-REST onboarding workstream). Two consumers:

  1. The existing sweep pipeline (``app.discovery._classify``) uses the
     signal-fusion core via :func:`classify_from_signals` with pre-collected
     snmp/ports/mac-vendor/banner data. Behaviour is identical to the
     pre-refactor classifier — same inputs, same classification string.

  2. The upcoming onboarding orchestrator (Prompt 4) uses :func:`classify`,
     which collects SNMP itself via :mod:`app.snmp_collector` and returns a
     :class:`ClassifierResult` that additionally exposes the detected
     ``vendor`` and NAPALM-style ``platform`` slug.

Signal layering:

  * **SNMP tier** — the authoritative vendor/platform signal.
    ``sysDescr`` substring match first (Juniper / Palo Alto / Arista /
    Cisco proper-noun prefixes are case-sensitive; FortiGate is
    case-insensitive because operator-customized sysDescr is common).
    ``sysObjectID`` prefix match is the fallback — useful when a FortiGate
    admin has rewritten sysDescr to something generic.
  * **Cisco two-stage** — once vendor=cisco is established, refine the
    platform to ``cisco_iosxe`` if any of the IOS-XE markers
    (``IOSXE`` / ``IOS-XE`` / ``IOS XE``) appears in sysDescr; otherwise
    ``cisco_ios``. All three variants are in active use across the Cisco
    platform matrix — c8000v at 17.16.1a reports the no-hyphen bracketed
    form ``[IOSXE]``.
  * **Non-SNMP tiers** — SSH/HTTP banners, OUI/MAC vendor, open-ports. No
    I/O is performed here; the sweep pipeline passes these in.

OctetString contract (see lesson in CLAUDE.md): ``sysDescr`` from
:func:`app.snmp_collector.get_scalar` is raw ``bytes``. Substring matches
operate on bytes directly where possible; UTF-8 decoding with
``errors="replace"`` happens only for the case-insensitive FortiGate path.

Reality-check citations:
  * Section 3 signal matrix — source of the captured ``sysDescr`` / ``sysObjectID``
    values used as test fixtures.
  * Section 6 Prompt 3 recommendations — Cisco two-stage variants, OctetString
    contract. Arista rule validated against live vEOS (172.21.140.16) on
    2026-04-20.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.snmp_collector import SnmpError, get_scalar, oid

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vendor / platform signal tables
# ---------------------------------------------------------------------------

# sysObjectID prefix → (vendor, default_platform). Cisco's platform is
# refined to ``cisco_iosxe`` by the two-stage sysDescr check when applicable.
SYSOBJECTID_TO_VENDOR: dict[str, tuple[str, str]] = {
    "1.3.6.1.4.1.2636":  ("juniper",   "juniper_junos"),
    "1.3.6.1.4.1.25461": ("palo_alto", "paloalto_panos"),
    "1.3.6.1.4.1.12356": ("fortinet",  "fortinet_fortios"),
    "1.3.6.1.4.1.30065": ("arista",    "arista_eos"),
    "1.3.6.1.4.1.9":     ("cisco",     "cisco_ios"),
}

# All three variants must be checked — c8000v reports `[IOSXE]` (no hyphen,
# no space); mainline IOS-XE platforms have historically used the hyphenated
# and spaced variants.
CISCO_IOSXE_MARKERS: tuple[bytes, ...] = (b"IOSXE", b"IOS-XE", b"IOS XE")


# ---------------------------------------------------------------------------
# Role / classification signal tables (migrated from discovery.py).
# These are used by classify_from_signals to produce the coarse
# "switch"|"router"|"firewall"|... classification that the sweep pipeline
# stores on each host record.
# ---------------------------------------------------------------------------

# Specific sysDescr substrings → role classification (Tier 1 in the fusion
# layer). Match is case-insensitive on the decoded sysDescr string.
_SYSDESCR_SPECIFIC: list[tuple[str, str]] = [
    # Firewalls
    ("srx", "firewall"), ("asa", "firewall"), ("fortigate", "firewall"),
    ("fortios", "firewall"), ("palo alto", "firewall"), ("pan-os", "firewall"),
    ("sonicwall", "firewall"), ("sonicos", "firewall"), ("watchguard", "firewall"),
    ("check point", "firewall"), ("gaia", "firewall"), ("netscreen", "firewall"),
    ("sophos", "firewall"),
    # Switches
    ("ex2", "switch"), ("ex3", "switch"), ("ex4", "switch"), ("ex8", "switch"),
    ("qfx", "switch"), ("icx", "switch"), ("fastiron", "switch"),
    ("procurve", "switch"), ("comware", "switch"), ("powerconnect", "switch"),
    ("catalyst", "switch"), ("nx-os", "switch"), ("nxos", "switch"),
    ("exos", "switch"), ("alliedware", "switch"),
    # Routers
    ("mx", "router"), ("mx5", "router"), ("mx10", "router"), ("mx80", "router"),
    ("mx104", "router"), ("mx204", "router"), ("mx240", "router"),
    ("sros", "router"), ("routeros", "router"),
    # Access points
    ("aruba ap", "access_point"), ("unifi", "access_point"),
]

# Generic "network device" keyword fallback for sysDescr. If nothing in
# _SYSDESCR_SPECIFIC matched, any of these keywords causes a
# "network_device" vote.
NETWORK_SYSDESCR_KEYWORDS: list[str] = [
    "junos", "juniper", "cisco", "ios", "nx-os", "nxos", "asa",
    "fortigate", "fortios", "arista", "eos", "routeros", "mikrotik",
    "procurve", "aruba", "comware", "hp ", "hpe ", "dell networking",
    "powerconnect", "palo alto", "pan-os", "extreme", "exos",
    "brocade", "fastiron", "icx", "alcatel", "nokia", "sros",
    "netgear", "sonicwall", "sonicos", "watchguard", "check point",
    "gaia", "huawei", "vrp", "allied telesis", "alliedware",
]

# OUI-based role signals (Tier 2).
NETWORK_VENDORS: set[str] = {
    "Juniper Networks", "Cisco Systems", "Cisco Meraki", "Arista Networks",
    "Fortinet", "HPE/Aruba", "Aruba Networks", "Dell", "Palo Alto Networks",
    "MikroTik", "Brocade", "Extreme Networks", "Nokia/ALU", "TP-Link",
    "Netgear", "Huawei", "Cambium Networks", "Allied Telesis", "Sophos",
    "WatchGuard", "SonicWall", "Check Point", "Ruckus",
}
AP_VENDORS: set[str] = {
    "Aruba Networks", "Ubiquiti", "Ruckus", "Cisco Meraki", "Cambium Networks",
}
PRINTER_VENDORS: set[str] = {
    "Hewlett Packard", "HP Inc.", "Ricoh", "Xerox", "Canon", "Brother",
    "Lexmark", "Konica Minolta", "Kyocera", "Epson", "Samsung Electronics",
}
PHONE_VENDORS: set[str] = {
    "Polycom", "Yealink", "Grandstream", "Snom", "Mitel Networks",
    "Avaya", "Fanvil Technology", "Gigaset Communications",
}
CAMERA_VENDORS: set[str] = {
    "Axis Communications", "Hikvision", "Dahua", "Vivotek",
    "Hanwha Techwin", "Bosch Security", "FLIR Systems",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ClassifierResult:
    """Unified classification output.

    ``classification`` is the sweep-compatible role string (matches the
    pre-refactor :func:`app.discovery.classify_endpoint` return value).
    ``vendor`` / ``platform`` are the new Prompt 3 additions — populated
    when SNMP sysDescr or sysObjectID yields a hit, ``None`` otherwise.
    """

    classification: str
    vendor: str | None = None
    platform: str | None = None
    confidence: str = "low"  # "high" | "medium" | "low"
    signals_matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "vendor": self.vendor,
            "platform": self.platform,
            "confidence": self.confidence,
            "signals_matched": list(self.signals_matched),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_port(ports: list[str], port: int, proto: str = "tcp") -> bool:
    token = f"{port}/{proto}"
    return any(p == token or p.startswith(f"{port}/") for p in ports)


def _has_any_port(ports: list[str], port_list: list[int], proto: str = "tcp") -> bool:
    return any(_has_port(ports, p, proto) for p in port_list)


def _get_tcp_ports(ports: list[str]) -> list[int]:
    out: list[int] = []
    for p in ports:
        if p.endswith("/tcp"):
            try:
                out.append(int(p.split("/")[0]))
            except ValueError:
                pass
    return out


def _sysdescr_to_text(sysdescr: bytes | str | None) -> str:
    """Decode sysDescr to a lowercase UTF-8 string for case-insensitive ops.

    Accepts bytes (the canonical form from :func:`snmp_collector.get_scalar`)
    or str (legacy; the sweep pipeline stashes a decoded string). Non-UTF-8
    bytes are replaced rather than raising — the OctetString contract
    promises raw bytes, so we never assume UTF-8 cleanliness.
    """
    if sysdescr is None:
        return ""
    if isinstance(sysdescr, bytes):
        return sysdescr.decode("utf-8", errors="replace").lower()
    return str(sysdescr).lower()


# ---------------------------------------------------------------------------
# Vendor/platform detection (SNMP tier)
# ---------------------------------------------------------------------------

def detect_vendor_platform(
    sysdescr: bytes | str | None,
    sysobjectid: str | None,
) -> tuple[str | None, str | None, list[str]]:
    """Detect (vendor, platform, signals) from SNMP system MIB data.

    Returns ``(None, None, [])`` when no signal matched.
    """
    signals: list[str] = []

    descr_bytes: bytes | None = None
    if isinstance(sysdescr, bytes):
        descr_bytes = sysdescr
    elif isinstance(sysdescr, str) and sysdescr:
        descr_bytes = sysdescr.encode("utf-8", errors="replace")

    # Tier 1: sysDescr substring match (case-sensitive on proper nouns).
    if descr_bytes:
        if b"Juniper Networks" in descr_bytes:
            return ("juniper", "juniper_junos",
                    ["sysdescr:juniper_networks"])
        if b"Palo Alto Networks" in descr_bytes:
            return ("palo_alto", "paloalto_panos",
                    ["sysdescr:palo_alto_networks"])
        # Validated against live Arista device 172.21.140.16 on 2026-04-20:
        # sysDescr contains b"Arista Networks" as documented.
        if b"Arista Networks" in descr_bytes:
            return ("arista", "arista_eos",
                    ["sysdescr:arista_networks"])
        if b"Cisco" in descr_bytes:
            platform = "cisco_ios"
            iosxe_marker: bytes | None = None
            for marker in CISCO_IOSXE_MARKERS:
                if marker in descr_bytes:
                    platform = "cisco_iosxe"
                    iosxe_marker = marker
                    break
            if iosxe_marker is not None:
                signals.append(
                    f"sysdescr:cisco_iosxe:{iosxe_marker.decode('ascii')}"
                )
            else:
                signals.append("sysdescr:cisco_ios")
            return ("cisco", platform, signals)

        # FortiGate — case-insensitive because operator-customized sysDescr
        # is common (reality-check §3).
        descr_lower = descr_bytes.decode("utf-8", errors="replace").lower()
        if "fortigate" in descr_lower:
            return ("fortinet", "fortinet_fortios", ["sysdescr:fortigate"])

    # Tier 2: sysObjectID prefix fallback.
    if sysobjectid:
        for prefix, (vendor, default_platform) in SYSOBJECTID_TO_VENDOR.items():
            # Match on prefix + "." so "1.3.6.1.4.1.9" does not accidentally
            # match "1.3.6.1.4.1.99999.1". An exact match (prefix == sysObjectID)
            # is also valid, though unusual at the enterprise-root level.
            if sysobjectid == prefix or sysobjectid.startswith(prefix + "."):
                platform = default_platform
                # Cisco may still be promoted to cisco_iosxe if sysDescr
                # carries a marker (common when sysDescr is present but
                # lacks the "Cisco" vendor string — rare but possible on
                # heavily customized images).
                if vendor == "cisco" and descr_bytes:
                    for marker in CISCO_IOSXE_MARKERS:
                        if marker in descr_bytes:
                            platform = "cisco_iosxe"
                            break
                return (vendor, platform, [f"sysobjectid:{prefix}"])

    return (None, None, [])


# ---------------------------------------------------------------------------
# Role classification (signal-fusion core)
# ---------------------------------------------------------------------------

def classify_from_signals(
    *,
    sysdescr: bytes | str | None = None,
    sysobjectid: str | None = None,
    ports_open: list[str] | None = None,
    mac_vendor: str = "",
    fingerprint: dict | None = None,
    snmp_responds: bool | None = None,
) -> ClassifierResult:
    """Pure-signal classifier: no I/O, no SNMP collection.

    ``snmp_responds`` is an optional hint for the no-signal fallback path —
    if SNMP responded at all (even with empty payload) a bare
    ``network_device`` classification is preferred over ``unknown``.
    Consumers that don't know this can pass ``True`` when ``sysdescr`` or
    ``sysobjectid`` is non-empty (the auto-computed default) or ``None``.
    """
    ports_open = ports_open or []
    fp = fingerprint or {}
    sysdescr_lower = _sysdescr_to_text(sysdescr)
    ssh_banner = str(fp.get("ssh_banner", "") or "").lower()
    http_title = str(fp.get("http_title", "") or "").lower()
    http_server = str(fp.get("http_server", "") or "").lower()

    if snmp_responds is None:
        snmp_responds = bool(sysdescr_lower or sysobjectid)

    # Run vendor/platform detection once — separate from classification
    # voting so Prompt 4's orchestrator gets the structured vendor field.
    vendor, platform, vp_signals = detect_vendor_platform(sysdescr, sysobjectid)

    votes: list[tuple[str, str]] = []

    # ---- Tier 1: SNMP sysDescr role signal ----
    if sysdescr_lower:
        for pattern, cls in _SYSDESCR_SPECIFIC:
            if pattern in sysdescr_lower:
                votes.append((cls, f"snmp_sysdescr:{pattern}"))
                break
        else:
            for keyword in NETWORK_SYSDESCR_KEYWORDS:
                if keyword in sysdescr_lower:
                    votes.append(("network_device",
                                  f"snmp_sysdescr:{keyword}"))
                    break

    # ---- Tier 1b: SSH/HTTP banner for network devices ----
    if ssh_banner:
        for keyword in ("juniper", "junos", "cisco", "fortissl", "fortissh",
                        "netscreen", "arista", "routeros", "mikrotik",
                        "comware", "dell force", "extreme", "brocade"):
            if keyword in ssh_banner:
                votes.append(("network_device", f"ssh_banner:{keyword}"))
                break

    if http_title:
        for keyword in ("fortigate", "juniper", "meraki", "cisco", "aruba",
                        "netgear", "ubiquiti", "unifi", "mikrotik", "palo alto",
                        "sonicwall", "watchguard", "checkpoint", "sophos"):
            if keyword in http_title:
                votes.append(("network_device", f"http_title:{keyword}"))
                break

    # ---- Tier 2: OUI / MAC vendor ----
    if mac_vendor in NETWORK_VENDORS:
        votes.append(("network_device", f"oui:{mac_vendor}"))
    if mac_vendor in AP_VENDORS:
        votes.append(("access_point", f"oui:{mac_vendor}"))
    if mac_vendor in PRINTER_VENDORS:
        votes.append(("printer", f"oui:{mac_vendor}"))
    if mac_vendor in PHONE_VENDORS:
        votes.append(("phone", f"oui:{mac_vendor}"))
    if mac_vendor in CAMERA_VENDORS:
        votes.append(("camera", f"oui:{mac_vendor}"))

    # ---- Tier 3: Open ports ----
    if _has_port(ports_open, 830):
        votes.append(("network_device", "port:830/netconf"))
    if _has_port(ports_open, 9100):
        votes.append(("printer", "port:9100/jetdirect"))
    if _has_port(ports_open, 631):
        votes.append(("printer", "port:631/ipp"))
    if _has_port(ports_open, 554):
        votes.append(("camera", "port:554/rtsp"))
    if _has_any_port(ports_open, [5060, 5061]):
        votes.append(("phone", "port:5060/sip"))

    # ---- Tier 4: Banner/header content ----
    if http_title:
        for kw in ("camera", "webcam", "ipcam"):
            if kw in http_title:
                votes.append(("camera", f"http_title:{kw}"))
                break
        for kw in ("printer", "laserjet", "ricoh", "xerox", "brother"):
            if kw in http_title:
                votes.append(("printer", f"http_title:{kw}"))
                break
    if http_server:
        for kw in ("vivotek", "axis", "hikvision", "dahua"):
            if kw in http_server:
                votes.append(("camera", f"http_server:{kw}"))
                break

    # ---- Score votes ----
    if not votes:
        # No signals — fall back to port-based heuristics (matches the
        # pre-refactor discovery.py behaviour byte-for-byte).
        classification: str
        confidence: str
        fallback_signals: list[str]
        if _has_any_port(ports_open, [80, 443, 8080, 8443]):
            if _has_port(ports_open, 22):
                classification, confidence, fallback_signals = (
                    "server", "low", ["port:22+http"])
            else:
                classification, confidence, fallback_signals = (
                    "web_service", "low", ["port:http"])
        elif _has_port(ports_open, 22):
            classification, confidence, fallback_signals = (
                "server", "low", ["port:22/ssh"])
        elif snmp_responds:
            classification, confidence, fallback_signals = (
                "network_device", "low", ["snmp_responds"])
        elif not _get_tcp_ports(ports_open):
            classification, confidence, fallback_signals = (
                "endpoint", "low", [])
        else:
            classification, confidence, fallback_signals = (
                "unknown", "low", [])

        return ClassifierResult(
            classification=classification,
            vendor=vendor,
            platform=platform,
            confidence=confidence,
            signals_matched=vp_signals + fallback_signals,
        )

    cls_counts = Counter(cls for cls, _ in votes)
    winner, _count = cls_counts.most_common(1)[0]
    signals = [sig for cls, sig in votes if cls == winner]

    tier_prefixes = {sig.split(":")[0] for sig in signals}
    confidence = "high" if len(tier_prefixes) >= 2 else "medium"

    return ClassifierResult(
        classification=winner,
        vendor=vendor,
        platform=platform,
        confidence=confidence,
        signals_matched=vp_signals + signals,
    )


# ---------------------------------------------------------------------------
# Async public entry — SNMP collection + classification
# ---------------------------------------------------------------------------

async def classify(
    device_ip: str,
    snmp_community: str,
    ssh_creds: dict | None = None,
    **extra_signals: Any,
) -> ClassifierResult:
    """Collect SNMP sysDescr/sysObjectID from the device and classify.

    Non-SNMP signals (``ports_open``, ``mac_vendor``, ``fingerprint``) flow
    through via ``**extra_signals`` — the onboarding orchestrator (Prompt 4)
    typically passes none of them (classification is SNMP-only during
    onboarding); the sweep pipeline uses :func:`classify_from_signals`
    directly with pre-collected signals.

    SNMP failures are tolerated — a timeout or auth error yields
    ``sysdescr=None``, which degrades classification gracefully to
    ``unknown`` if no other signal is provided.

    ``ssh_creds`` is accepted for interface compatibility but is unused
    here — SSH banner grabbing is the sweep pipeline's responsibility;
    the classifier consumes the banner via ``fingerprint`` if supplied.
    """
    _ = ssh_creds  # reserved for interface-compat; see docstring

    sysdescr: bytes | None = None
    sysobjectid: str | None = None

    try:
        result = await get_scalar(
            device_ip, snmp_community, oid("SNMPv2-MIB::sysDescr"),
        )
        if isinstance(result, (bytes, bytearray)):
            sysdescr = bytes(result)
        elif result is not None:
            sysdescr = str(result).encode("utf-8", errors="replace")
    except SnmpError as exc:
        log.debug("classifier_sysdescr_unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001 — defensive, log and continue
        log.debug("classifier_sysdescr_error: %s", exc)

    try:
        result = await get_scalar(
            device_ip, snmp_community, oid("SNMPv2-MIB::sysObjectID"),
        )
        if result is not None:
            sysobjectid = str(result)
    except SnmpError as exc:
        log.debug("classifier_sysobjectid_unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.debug("classifier_sysobjectid_error: %s", exc)

    return classify_from_signals(
        sysdescr=sysdescr,
        sysobjectid=sysobjectid,
        ports_open=extra_signals.get("ports_open"),
        mac_vendor=extra_signals.get("mac_vendor", ""),
        fingerprint=extra_signals.get("fingerprint"),
        snmp_responds=extra_signals.get(
            "snmp_responds",
            sysdescr is not None or sysobjectid is not None,
        ),
    )
