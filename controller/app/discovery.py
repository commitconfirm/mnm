"""Seed-and-sweep network discovery engine for MNM.

Enumerates CIDR ranges, probes ports, collects SNMP/ARP/DNS/banner/TLS data,
classifies hosts, and feeds results into Nautobot.
"""

import asyncio
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
from datetime import datetime, timezone
from enum import Enum

from app import nautobot_client
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="discovery")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Scale concurrency to CPU count — IO-bound async tasks benefit from higher concurrency
_CPU_COUNT = os.cpu_count() or 4
MAX_CONCURRENT_PROBES = max(10, _CPU_COUNT * 4)
PROBE_TIMEOUT = 2
PROBE_PORTS = [22, 23, 80, 161, 443, 830, 8080, 8443, 9100]


class SweepStatus(str, Enum):
    PENDING = "pending"
    SCANNING = "scanning"
    ENRICHING = "enriching"
    ALIVE = "alive"
    DEAD = "dead"
    KNOWN = "known"
    ONBOARDING = "onboarding"
    ONBOARDED = "onboarded"
    RECORDED = "recorded"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# MAC OUI lookup table — common network vendor prefixes
# ---------------------------------------------------------------------------

# Keys are upper-case, colon-separated 3-byte OUI prefixes.
MAC_OUI: dict[str, str] = {
    # Juniper Networks
    "00:05:85": "Juniper Networks",
    "00:10:DB": "Juniper Networks",
    "00:12:1E": "Juniper Networks",
    "00:14:F6": "Juniper Networks",
    "00:17:CB": "Juniper Networks",
    "00:19:E2": "Juniper Networks",
    "00:1D:B5": "Juniper Networks",
    "00:21:59": "Juniper Networks",
    "00:22:83": "Juniper Networks",
    "00:23:9C": "Juniper Networks",
    "00:24:DC": "Juniper Networks",
    "00:26:88": "Juniper Networks",
    "28:8A:1C": "Juniper Networks",
    "28:C0:DA": "Juniper Networks",
    "2C:21:31": "Juniper Networks",
    "2C:6B:F5": "Juniper Networks",
    "3C:61:04": "Juniper Networks",
    "3C:8A:B0": "Juniper Networks",
    "40:A6:77": "Juniper Networks",
    "40:B4:F0": "Juniper Networks",
    "44:F4:77": "Juniper Networks",
    "4C:96:14": "Juniper Networks",
    "50:C5:8D": "Juniper Networks",
    "54:1E:56": "Juniper Networks",
    "54:4B:8C": "Juniper Networks",
    "5C:45:27": "Juniper Networks",
    "64:64:9B": "Juniper Networks",
    "64:87:88": "Juniper Networks",
    "78:FE:3D": "Juniper Networks",
    "80:71:1F": "Juniper Networks",
    "84:18:88": "Juniper Networks",
    "84:B5:9C": "Juniper Networks",
    "88:A2:5E": "Juniper Networks",
    "88:E0:F3": "Juniper Networks",
    "9C:CC:83": "Juniper Networks",
    "A8:D0:E5": "Juniper Networks",
    "AC:4B:C8": "Juniper Networks",
    "B0:A8:6E": "Juniper Networks",
    "B0:C6:9A": "Juniper Networks",
    "CC:E1:7F": "Juniper Networks",
    "D4:04:FF": "Juniper Networks",
    "EC:13:DB": "Juniper Networks",
    "EC:3E:F7": "Juniper Networks",
    "F0:1C:2D": "Juniper Networks",
    "F4:A7:39": "Juniper Networks",
    "F4:B5:2F": "Juniper Networks",
    "F4:CC:55": "Juniper Networks",
    # Cisco Systems
    "00:00:0C": "Cisco Systems",
    "00:01:42": "Cisco Systems",
    "00:01:63": "Cisco Systems",
    "00:01:64": "Cisco Systems",
    "00:01:96": "Cisco Systems",
    "00:01:97": "Cisco Systems",
    "00:01:C7": "Cisco Systems",
    "00:01:C9": "Cisco Systems",
    "00:02:17": "Cisco Systems",
    "00:02:4A": "Cisco Systems",
    "00:02:4B": "Cisco Systems",
    "00:02:B9": "Cisco Systems",
    "00:02:BA": "Cisco Systems",
    "00:03:6B": "Cisco Systems",
    "00:03:FD": "Cisco Systems",
    "00:04:9A": "Cisco Systems",
    "00:05:31": "Cisco Systems",
    "00:05:32": "Cisco Systems",
    "00:05:73": "Cisco Systems",
    "00:05:74": "Cisco Systems",
    "00:05:DC": "Cisco Systems",
    "00:06:28": "Cisco Systems",
    "00:06:7C": "Cisco Systems",
    "00:06:C1": "Cisco Systems",
    "00:06:D6": "Cisco Systems",
    "00:06:D7": "Cisco Systems",
    "00:07:0D": "Cisco Systems",
    "00:07:0E": "Cisco Systems",
    "00:07:4F": "Cisco Systems",
    "00:07:50": "Cisco Systems",
    "00:07:85": "Cisco Systems",
    "00:08:20": "Cisco Systems",
    "00:08:21": "Cisco Systems",
    "00:08:2F": "Cisco Systems",
    "00:08:30": "Cisco Systems",
    "00:08:31": "Cisco Systems",
    "00:08:7C": "Cisco Systems",
    "00:08:E3": "Cisco Systems",
    "00:09:12": "Cisco Systems",
    "00:09:43": "Cisco Systems",
    "00:09:44": "Cisco Systems",
    "00:09:7B": "Cisco Systems",
    "00:09:7C": "Cisco Systems",
    "00:09:B7": "Cisco Systems",
    "00:0A:41": "Cisco Systems",
    "00:0A:42": "Cisco Systems",
    "00:0A:8A": "Cisco Systems",
    "00:0A:B7": "Cisco Systems",
    "00:0A:B8": "Cisco Systems",
    "00:0A:F3": "Cisco Systems",
    "00:0A:F4": "Cisco Systems",
    "00:0B:45": "Cisco Systems",
    "00:0B:46": "Cisco Systems",
    "00:0B:85": "Cisco Systems",
    "00:0B:BE": "Cisco Systems",
    "00:0B:BF": "Cisco Systems",
    "00:0B:FC": "Cisco Systems",
    "00:0B:FD": "Cisco Systems",
    "00:0C:30": "Cisco Systems",
    "00:0C:31": "Cisco Systems",
    "00:0C:85": "Cisco Systems",
    "00:0C:86": "Cisco Systems",
    "00:0C:CE": "Cisco Systems",
    "00:0C:CF": "Cisco Systems",
    "00:0D:28": "Cisco Systems",
    "00:0D:29": "Cisco Systems",
    "00:0D:65": "Cisco Systems",
    "00:0D:66": "Cisco Systems",
    "00:0D:BC": "Cisco Systems",
    "00:0D:BD": "Cisco Systems",
    "00:0D:EC": "Cisco Systems",
    "00:0D:ED": "Cisco Systems",
    "00:0E:08": "Cisco Systems",
    "00:0E:38": "Cisco Systems",
    "00:0E:39": "Cisco Systems",
    "00:0E:83": "Cisco Systems",
    "00:0E:84": "Cisco Systems",
    "00:0E:D6": "Cisco Systems",
    "00:0E:D7": "Cisco Systems",
    "00:0F:23": "Cisco Systems",
    "00:0F:24": "Cisco Systems",
    "00:0F:34": "Cisco Systems",
    "00:0F:35": "Cisco Systems",
    "00:0F:8F": "Cisco Systems",
    "00:0F:90": "Cisco Systems",
    # Cisco (Meraki)
    "00:18:0A": "Cisco Meraki",
    "0C:8D:DB": "Cisco Meraki",
    "34:56:FE": "Cisco Meraki",
    "68:3A:1E": "Cisco Meraki",
    "AC:17:C8": "Cisco Meraki",
    "E0:55:3D": "Cisco Meraki",
    "E0:CB:BC": "Cisco Meraki",
    # Arista Networks
    "00:1C:73": "Arista Networks",
    "28:99:3A": "Arista Networks",
    "44:4C:A8": "Arista Networks",
    "50:01:00": "Arista Networks",
    "74:83:EF": "Arista Networks",
    "FC:BD:67": "Arista Networks",
    # Fortinet
    "00:09:0F": "Fortinet",
    "08:5B:0E": "Fortinet",
    "0C:B5:0D": "Fortinet",
    "24:C4:04": "Fortinet",
    "70:4C:A5": "Fortinet",
    "90:6C:AC": "Fortinet",
    "E8:1C:BA": "Fortinet",
    # HP / HPE / Aruba Networks
    "00:0B:CD": "HPE/Aruba",
    "00:0C:29": "HPE/Aruba",
    "00:0D:B4": "HPE/Aruba",
    "00:10:E3": "HPE/Aruba",
    "00:11:0A": "HPE/Aruba",
    "00:11:85": "HPE/Aruba",
    "00:12:79": "HPE/Aruba",
    "00:13:21": "HPE/Aruba",
    "00:14:38": "HPE/Aruba",
    "00:14:C2": "HPE/Aruba",
    "00:15:60": "HPE/Aruba",
    "00:17:A4": "HPE/Aruba",
    "00:18:FE": "HPE/Aruba",
    "00:1A:1E": "HPE/Aruba",
    "00:1A:4B": "HPE/Aruba",
    "00:1B:3F": "HPE/Aruba",
    "00:1C:C4": "HPE/Aruba",
    "00:1E:C1": "HPE/Aruba",
    "00:21:5A": "HPE/Aruba",
    "00:22:64": "HPE/Aruba",
    "00:23:47": "HPE/Aruba",
    "00:24:A8": "HPE/Aruba",
    "00:25:B3": "HPE/Aruba",
    "00:26:F1": "HPE/Aruba",
    "10:60:4B": "HPE/Aruba",
    "20:4C:03": "HPE/Aruba",
    "24:BE:05": "HPE/Aruba",
    "3C:D9:2B": "HPE/Aruba",
    "48:0F:CF": "HPE/Aruba",
    "6C:C2:17": "HPE/Aruba",
    "70:10:6F": "HPE/Aruba",
    "94:57:A5": "HPE/Aruba",
    "9C:8E:99": "HPE/Aruba",
    "A0:B3:CC": "HPE/Aruba",
    "B4:5D:50": "HPE/Aruba",
    "D0:D3:E0": "HPE/Aruba",
    "F0:92:1C": "HPE/Aruba",
    # Aruba standalone OUIs
    "00:0B:86": "Aruba Networks",
    "04:BD:88": "Aruba Networks",
    "18:64:72": "Aruba Networks",
    "20:4C:03": "Aruba Networks",
    "24:DE:C6": "Aruba Networks",
    "40:E3:D6": "Aruba Networks",
    "6C:F3:7F": "Aruba Networks",
    "84:D4:7E": "Aruba Networks",
    "9C:1C:12": "Aruba Networks",
    "AC:A3:1E": "Aruba Networks",
    "D8:C7:C8": "Aruba Networks",
    # Dell
    "00:06:5B": "Dell",
    "00:08:74": "Dell",
    "00:0B:DB": "Dell",
    "00:0D:56": "Dell",
    "00:0F:1F": "Dell",
    "00:11:43": "Dell",
    "00:12:3F": "Dell",
    "00:13:72": "Dell",
    "00:14:22": "Dell",
    "00:15:C5": "Dell",
    "00:18:8B": "Dell",
    "00:19:B9": "Dell",
    "00:1A:A0": "Dell",
    "00:1C:23": "Dell",
    "00:1D:09": "Dell",
    "00:1E:4F": "Dell",
    "00:1E:C9": "Dell",
    "00:21:70": "Dell",
    "00:21:9B": "Dell",
    "00:22:19": "Dell",
    "00:23:AE": "Dell",
    "00:24:E8": "Dell",
    "00:25:64": "Dell",
    "00:26:B9": "Dell",
    "14:18:77": "Dell",
    "14:FE:B5": "Dell",
    "18:03:73": "Dell",
    "18:66:DA": "Dell",
    "18:A9:9B": "Dell",
    "18:DB:F2": "Dell",
    "24:6E:96": "Dell",
    "24:B6:FD": "Dell",
    "28:F1:0E": "Dell",
    "34:17:EB": "Dell",
    # Ubiquiti
    "00:15:6D": "Ubiquiti",
    "00:27:22": "Ubiquiti",
    "04:18:D6": "Ubiquiti",
    "18:E8:29": "Ubiquiti",
    "24:5A:4C": "Ubiquiti",
    "44:D9:E7": "Ubiquiti",
    "68:72:51": "Ubiquiti",
    "70:A7:41": "Ubiquiti",
    "74:83:C2": "Ubiquiti",
    "78:8A:20": "Ubiquiti",
    "80:2A:A8": "Ubiquiti",
    "B4:FB:E4": "Ubiquiti",
    "DC:9F:DB": "Ubiquiti",
    "E0:63:DA": "Ubiquiti",
    "F0:9F:C2": "Ubiquiti",
    "FC:EC:DA": "Ubiquiti",
    # Palo Alto Networks
    "00:1B:17": "Palo Alto Networks",
    "00:86:9C": "Palo Alto Networks",
    "08:30:6B": "Palo Alto Networks",
    "08:66:1F": "Palo Alto Networks",
    "B4:0C:25": "Palo Alto Networks",
    "D4:F4:BE": "Palo Alto Networks",
    "E4:A7:49": "Palo Alto Networks",
    # Ruckus (CommScope)
    "00:13:92": "Ruckus",
    "00:22:7F": "Ruckus",
    "00:25:C4": "Ruckus",
    "04:4F:AA": "Ruckus",
    "0C:F4:D5": "Ruckus",
    "24:C9:A1": "Ruckus",
    "34:1B:22": "Ruckus",
    "58:B6:33": "Ruckus",
    "70:DF:2F": "Ruckus",
    "74:91:1A": "Ruckus",
    "84:18:3A": "Ruckus",
    "A4:1B:C0": "Ruckus",
    "AC:67:06": "Ruckus",
    "C4:01:7C": "Ruckus",
    "EC:58:EA": "Ruckus",
    # MikroTik
    "00:0C:42": "MikroTik",
    "08:55:31": "MikroTik",
    "18:FD:74": "MikroTik",
    "2C:C8:1B": "MikroTik",
    "48:8F:5A": "MikroTik",
    "4C:5E:0C": "MikroTik",
    "6C:3B:6B": "MikroTik",
    "74:4D:28": "MikroTik",
    "B8:69:F4": "MikroTik",
    "C4:AD:34": "MikroTik",
    "CC:2D:E0": "MikroTik",
    "D4:CA:6D": "MikroTik",
    "E4:8D:8C": "MikroTik",
    # Brocade / Ruckus ICX
    "00:00:4F": "Brocade",
    "00:04:80": "Brocade",
    "00:05:1E": "Brocade",
    "00:05:33": "Brocade",
    "00:08:25": "Brocade",
    "00:0C:DB": "Brocade",
    "00:12:F2": "Brocade",
    "00:24:38": "Brocade",
    "00:27:F8": "Brocade",
    "50:EB:1A": "Brocade",
    "74:8E:F8": "Brocade",
    "C4:F5:7C": "Brocade",
    # Extreme Networks
    "00:01:30": "Extreme Networks",
    "00:04:96": "Extreme Networks",
    "00:E0:2B": "Extreme Networks",
    "5C:0E:8B": "Extreme Networks",
    "74:67:F7": "Extreme Networks",
    "B4:C7:99": "Extreme Networks",
    "B8:50:01": "Extreme Networks",
    "FC:0A:81": "Extreme Networks",
    # Nokia (Alcatel-Lucent)
    "00:03:47": "Nokia/ALU",
    "00:08:0E": "Nokia/ALU",
    "00:0B:3B": "Nokia/ALU",
    "00:10:B5": "Nokia/ALU",
    "00:13:FA": "Nokia/ALU",
    "00:16:CA": "Nokia/ALU",
    "00:1A:F0": "Nokia/ALU",
    "00:20:D0": "Nokia/ALU",
    "00:20:DA": "Nokia/ALU",
    "3C:FA:06": "Nokia/ALU",
    "50:E0:EF": "Nokia/ALU",
    "88:5A:92": "Nokia/ALU",
    "D0:99:D5": "Nokia/ALU",
    # TP-Link
    "00:23:CD": "TP-Link",
    "00:27:19": "TP-Link",
    "10:FE:ED": "TP-Link",
    "14:CC:20": "TP-Link",
    "30:B5:C2": "TP-Link",
    "50:C7:BF": "TP-Link",
    "54:C8:0F": "TP-Link",
    "60:E3:27": "TP-Link",
    "64:66:B3": "TP-Link",
    "64:70:02": "TP-Link",
    # Netgear
    "00:09:5B": "Netgear",
    "00:0F:B5": "Netgear",
    "00:14:6C": "Netgear",
    "00:1B:2F": "Netgear",
    "00:1E:2A": "Netgear",
    "00:1F:33": "Netgear",
    "00:22:3F": "Netgear",
    "00:24:B2": "Netgear",
    "00:26:F2": "Netgear",
    "20:0C:C8": "Netgear",
    "28:C6:8E": "Netgear",
    "2C:B0:5D": "Netgear",
    "30:46:9A": "Netgear",
    "44:94:FC": "Netgear",
    # Huawei
    "00:E0:FC": "Huawei",
    "00:18:82": "Huawei",
    "00:1E:10": "Huawei",
    "00:25:9E": "Huawei",
    "00:25:68": "Huawei",
    "00:46:4B": "Huawei",
    "04:BD:70": "Huawei",
    "04:C0:6F": "Huawei",
    "04:F9:38": "Huawei",
    "08:19:A6": "Huawei",
    "0C:96:BF": "Huawei",
    "10:47:80": "Huawei",
    "10:C6:1F": "Huawei",
    "20:08:ED": "Huawei",
    "20:A6:CD": "Huawei",
    "24:09:95": "Huawei",
    "28:6E:D4": "Huawei",
    "28:31:52": "Huawei",
    "30:D1:7E": "Huawei",
    "34:6B:D3": "Huawei",
    "48:AD:08": "Huawei",
    "4C:1F:CC": "Huawei",
    "54:A5:1B": "Huawei",
    "58:2A:F7": "Huawei",
    "70:72:3C": "Huawei",
    "80:FB:06": "Huawei",
    "88:28:B3": "Huawei",
    "AC:85:3D": "Huawei",
    "CC:A2:23": "Huawei",
    "D4:6A:A8": "Huawei",
    "E0:24:7F": "Huawei",
    "F4:C7:14": "Huawei",
    "F8:01:13": "Huawei",
    # Cambium / Motorola / Canopy
    "00:04:56": "Cambium Networks",
    "58:C1:7A": "Cambium Networks",
    # Allied Telesis
    "00:00:CD": "Allied Telesis",
    "00:09:41": "Allied Telesis",
    "00:15:77": "Allied Telesis",
    "EC:CD:6D": "Allied Telesis",
    # Sophos / Cyberoam
    "00:1A:8C": "Sophos",
    # WatchGuard
    "00:90:7F": "WatchGuard",
    # SonicWall
    "00:06:B1": "SonicWall",
    "00:17:C5": "SonicWall",
    "C0:EA:E4": "SonicWall",
    # Check Point
    "00:1C:7F": "Check Point",
}

# Vendors whose MACs indicate an access point
AP_VENDORS = {"Aruba Networks", "Ubiquiti", "Ruckus", "Cisco Meraki", "Cambium Networks"}

# Vendors recognized as network equipment manufacturers
NETWORK_VENDORS = {
    "Juniper Networks", "Cisco Systems", "Cisco Meraki", "Arista Networks",
    "Fortinet", "HPE/Aruba", "Aruba Networks", "Dell", "Palo Alto Networks",
    "MikroTik", "Brocade", "Extreme Networks", "Nokia/ALU", "TP-Link",
    "Netgear", "Huawei", "Cambium Networks", "Allied Telesis", "Sophos",
    "WatchGuard", "SonicWall", "Check Point", "Ruckus",
}

# sysDescr substrings that identify known network operating systems
NETWORK_SYSDESCR_KEYWORDS = [
    "junos", "juniper", "cisco", "ios", "nx-os", "nxos", "asa",
    "fortigate", "fortios", "arista", "eos", "routeros", "mikrotik",
    "procurve", "aruba", "comware", "hp ", "hpe ", "dell networking",
    "powerconnect", "palo alto", "pan-os", "extreme", "exos",
    "brocade", "fastiron", "icx", "alcatel", "nokia", "sros",
    "netgear", "sonicwall", "sonicos", "watchguard", "check point",
    "gaia", "huawei", "vrp", "allied telesis", "alliedware",
]


# ---------------------------------------------------------------------------
# In-memory sweep state
# ---------------------------------------------------------------------------

_sweep_state: dict = {
    "running": False,
    "hosts": {},
    "summary": None,
    "log": [],
    "started_at": None,
    "finished_at": None,
    "duration_seconds": None,
}

# Sweep history — last N completed sweeps
_sweep_history: list[dict] = []
MAX_HISTORY = 20


_sweep_cancel = False


# ---------------------------------------------------------------------------
# Onboarding progress tracker
# ---------------------------------------------------------------------------
# Per-IP onboarding state, surfaced in the host Details panel and via the
# /api/discover/onboarding/{ip} endpoint. Each entry has:
#   stage: one of submitting | queued | running | succeeded | failed | timeout
#   message: human-readable status text
#   started_at / updated_at: ISO timestamps
#   job_result_id: Nautobot JobResult UUID (when known)
#   error: detailed error string (when stage == failed)
_onboarding_state: dict[str, dict] = {}


def _onb_set(ip: str, stage: str, message: str, **extra) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = _onboarding_state.get(ip, {})
    cur.update({"stage": stage, "message": message, "updated_at": now_iso, **extra})
    cur.setdefault("started_at", now_iso)
    cur["ip"] = ip
    _onboarding_state[ip] = cur


def get_onboarding_state(ip: str | None = None):
    if ip:
        return _onboarding_state.get(ip)
    return list(_onboarding_state.values())


def get_sweep_state() -> dict:
    state = _sweep_state.copy()
    state["history"] = _sweep_history.copy()
    state["onboarding"] = list(_onboarding_state.values())
    return state


def stop_sweep():
    """Signal the running sweep to stop."""
    global _sweep_cancel
    if _sweep_state["running"]:
        _sweep_cancel = True
        log.info("sweep_stop_requested", "Sweep stop requested by operator")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

async def _tcp_probe(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Try a TCP connect to ip:port. Returns True if port is open."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        log.debug("tcp_probe_open", "TCP port open", context={"ip": ip, "port": port})
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def _icmp_ping(ip: str) -> bool:
    """Check if host responds to ICMP ping. Uses system ping command."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "2", ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        returncode = await asyncio.wait_for(proc.wait(), timeout=3)
        return returncode == 0
    except (asyncio.TimeoutError, OSError):
        return False


async def _udp_probe(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Try a UDP probe. Sends a small packet and checks for a response.

    UDP is connectionless so we can only confirm a port is open if we get
    a response. No response could mean open (silently accepted) or filtered.
    We only mark as open if we get a response.
    """
    try:
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.settimeout(timeout)
        await loop.run_in_executor(None, sock.sendto, b"\x00", (ip, port))
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, sock.recv, 1024),
                timeout=timeout,
            )
            return True  # Got a response — port is open
        except (asyncio.TimeoutError, OSError):
            return False  # No response — can't confirm open
        finally:
            sock.close()
    except (OSError, Exception):
        return False


# UDP ports to probe
UDP_PROBE_PORTS = [53, 123, 161, 500, 514, 1812]


async def _probe_all_ports(ip: str, semaphore: asyncio.Semaphore) -> list[str]:
    """Probe all TCP and UDP ports for a single host.

    Returns list of protocol-prefixed port strings: ["tcp/22", "tcp/830", "udp/161", "icmp"]
    """
    open_ports: list[str] = []

    # ICMP ping
    async def _try_icmp():
        if _sweep_cancel:
            return
        if await _icmp_ping(ip):
            open_ports.append("icmp")

    # TCP probes — bail quickly when sweep is cancelled instead of
    # waiting for a semaphore slot that may never come.
    async def _try_tcp(port: int):
        if _sweep_cancel:
            return
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=2.0)
        except asyncio.TimeoutError:
            if _sweep_cancel:
                return
            # Retry once, then give up on cancel
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=2.0)
            except asyncio.TimeoutError:
                return
        try:
            if not _sweep_cancel:
                if await _tcp_probe(ip, port):
                    open_ports.append(f"tcp/{port}")
        finally:
            semaphore.release()

    # UDP probes
    async def _try_udp(port: int):
        if _sweep_cancel:
            return
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=2.0)
        except asyncio.TimeoutError:
            return
        try:
            if not _sweep_cancel:
                if await _udp_probe(ip, port):
                    open_ports.append(f"udp/{port}")
        finally:
            semaphore.release()

    tasks = [_try_icmp()]
    tasks.extend(_try_tcp(p) for p in PROBE_PORTS)
    tasks.extend(_try_udp(p) for p in UDP_PROBE_PORTS)

    await asyncio.gather(*tasks)
    return sorted(open_ports)


def _has_port(ports: list[str], port: int, proto: str = "tcp") -> bool:
    """Check if a port is in the protocol-prefixed port list."""
    return f"{proto}/{port}" in ports


def _has_any_port(ports: list[str], port_list: list[int], proto: str = "tcp") -> bool:
    """Check if any of the given ports are open."""
    return any(f"{proto}/{p}" in ports for p in port_list)


def _get_tcp_ports(ports: list[str]) -> list[int]:
    """Extract TCP port numbers from the protocol-prefixed list."""
    result = []
    for p in ports:
        if p.startswith("tcp/"):
            try:
                result.append(int(p[4:]))
            except ValueError:
                pass
    return result


def _lookup_dns(ip: str) -> str:
    """Reverse DNS lookup. Returns FQDN or empty string."""
    try:
        return socket.getfqdn(ip)
    except Exception:
        return ""


def _lookup_arp(ip: str) -> str:
    """Get MAC address from the local ARP / neighbor table."""
    try:
        result = subprocess.run(
            ["ip", "neigh", "show", ip],
            capture_output=True, text=True, timeout=5,
        )
        # Example output: 192.0.2.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if "lladdr" in parts:
                idx = parts.index("lladdr")
                if idx + 1 < len(parts):
                    return parts[idx + 1].upper()
    except Exception:
        pass
    return ""


# Full OUI lookup via mac-vendor-lookup (IEEE database with auto-updates)
# We use BaseMacLookup directly to avoid the async/sync auto-detection issue
# where MacLookup.lookup() returns a coroutine inside an event loop.
_oui_prefixes: dict = {}
try:
    from mac_vendor_lookup import BaseMacLookup
    import pathlib
    _cache_path = pathlib.Path.home() / ".cache" / "mac-vendors.txt"
    if _cache_path.exists():
        with open(_cache_path, "rb") as f:
            for line in f:
                line = line.strip()
                if b":" in line:
                    parts = line.split(b":", 1)
                    if len(parts) == 2 and len(parts[0]) == 6:
                        _oui_prefixes[parts[0].upper()] = parts[1].decode("utf-8", errors="replace")
        log.info("oui_loaded", "Loaded OUI prefixes from IEEE database", context={"count": len(_oui_prefixes)})
except Exception:
    pass


def _mac_vendor(mac: str) -> str:
    """Look up vendor from MAC. Uses built-in network OUI table first, then full IEEE database."""
    if not mac:
        return ""
    # Normalize to colon-separated uppercase
    normalized = mac.upper().replace("-", ":").replace(".", ":")
    clean = normalized.replace(":", "")
    if len(clean) < 6:
        return ""

    # Check built-in network vendor OUI table first
    oui = f"{clean[0:2]}:{clean[2:4]}:{clean[4:6]}"
    result = MAC_OUI.get(oui, "")
    if result:
        return result

    # Fallback to full IEEE OUI database (direct dict lookup, no async issues)
    if _oui_prefixes:
        oui_key = clean[:6].upper().encode()
        vendor = _oui_prefixes.get(oui_key, "")
        if vendor:
            return vendor

    return ""


# ---------------------------------------------------------------------------
# SNMP helpers  (pysnmp async hlapi)
# ---------------------------------------------------------------------------

SNMP_OIDS = {
    "sysDescr":    "1.3.6.1.2.1.1.1.0",
    "sysObjectID": "1.3.6.1.2.1.1.2.0",
    "sysUpTime":   "1.3.6.1.2.1.1.3.0",
    "sysContact":  "1.3.6.1.2.1.1.4.0",
    "sysName":     "1.3.6.1.2.1.1.5.0",
    "sysLocation": "1.3.6.1.2.1.1.6.0",
}


async def _snmp_get(ip: str, community: str, snmp_v3: dict | None = None) -> dict[str, str]:
    """Perform async SNMP GET for system MIB objects using pysnmp asyncio hlapi."""
    result: dict[str, str] = {}
    try:
        from pysnmp.hlapi.asyncio import (
            CommunityData, ContextData, ObjectIdentity, ObjectType,
            SnmpEngine, UdpTransportTarget, getCmd,
        )

        engine = SnmpEngine()
        transport = UdpTransportTarget((ip, 161), timeout=3, retries=1)
        context = ContextData()
        auth = CommunityData(community, mpModel=1)  # SNMPv2c

        for name, oid_str in SNMP_OIDS.items():
            error_indication, error_status, error_index, var_binds = await getCmd(
                engine, auth, transport, context,
                ObjectType(ObjectIdentity(oid_str)),
            )
            if error_indication or error_status:
                continue
            for _oid, val in var_binds:
                result[name] = str(val)

    except Exception as exc:
        log.warning("snmp_get_failed", "SNMP GET failed", context={"ip": ip, "error": str(exc)})

    if result:
        log.debug("snmp_get_success", "SNMP GET succeeded", context={"ip": ip, "keys": list(result.keys())})

    return result


# ---------------------------------------------------------------------------
# Service fingerprinting — banners, HTTP, TLS, SSH
# ---------------------------------------------------------------------------

BANNER_TIMEOUT = 2
BANNER_MAX_BYTES = 4096


class _TitleParser(object):
    """Minimal HTML title extractor without html.parser import overhead per call."""
    def __init__(self):
        self.title = ""
        self._in_title = False
        self._done = False

    def feed(self, data: str):
        m = re.search(r"<title[^>]*>(.*?)</title>", data, re.IGNORECASE | re.DOTALL)
        if m:
            self.title = m.group(1).strip()


async def _grab_banner(ip: str, port: int) -> str:
    """Connect to a port and read the initial banner (up to 4KB, 2s timeout).

    Passive read-only: connect, read what the service sends, disconnect.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=BANNER_TIMEOUT,
        )
        try:
            banner = await asyncio.wait_for(
                reader.read(BANNER_MAX_BYTES),
                timeout=BANNER_TIMEOUT,
            )
            return banner.decode("utf-8", errors="replace").strip()
        finally:
            writer.close()
            await writer.wait_closed()
    except (asyncio.TimeoutError, OSError):
        return ""


async def _fingerprint_http(ip: str, port: int, use_tls: bool = False) -> dict:
    """Send a minimal HTTP GET and capture response headers and title.

    Read-only: single GET /, no redirects, no form submission.
    """
    result: dict = {"headers": {}, "title": "", "server": ""}
    try:
        if use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port, ssl=ctx),
                timeout=BANNER_TIMEOUT,
            )
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=BANNER_TIMEOUT,
            )

        try:
            request = f"GET / HTTP/1.0\r\nHost: {ip}\r\nConnection: close\r\n\r\n"
            writer.write(request.encode())
            await writer.drain()

            response = await asyncio.wait_for(
                reader.read(BANNER_MAX_BYTES),
                timeout=BANNER_TIMEOUT,
            )
            text = response.decode("utf-8", errors="replace")

            # Parse headers
            header_end = text.find("\r\n\r\n")
            if header_end == -1:
                header_end = text.find("\n\n")
            if header_end > 0:
                header_block = text[:header_end]
                body = text[header_end:]
                for line in header_block.split("\n"):
                    if ":" in line:
                        key, _, val = line.partition(":")
                        key = key.strip()
                        val = val.strip()
                        result["headers"][key] = val
                        if key.lower() == "server":
                            result["server"] = val

                # Extract title from body
                parser = _TitleParser()
                parser.feed(body)
                if parser.title:
                    result["title"] = parser.title
        finally:
            writer.close()
            await writer.wait_closed()
    except (asyncio.TimeoutError, OSError, ssl.SSLError):
        pass
    return result


async def _fingerprint_tls(ip: str, port: int) -> dict:
    """Read TLS certificate details. Does NOT validate — just reads the presented cert."""
    result: dict = {}
    loop = asyncio.get_event_loop()

    def _get_cert():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((ip, port), timeout=BANNER_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=ip) as ssock:
                cert = ssock.getpeercert(binary_form=True)
                # Decode with ssl
                decoded = ssl.DER_cert_to_PEM_cert(cert)
                # Get parsed cert info via a second connection with CERT_NONE
                # Actually, getpeercert() with binary_form=False needs verify
                # Use the binary cert and parse manually with ssl module
                return ssock.getpeercert(binary_form=False)

    try:
        # getpeercert(False) only works when verify is on; use a workaround
        def _get_cert_info():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((ip, port), timeout=BANNER_TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=ip) as ssock:
                    binary_cert = ssock.getpeercert(binary_form=True)
                    # Parse the DER cert using ssl internals
                    import ssl as _ssl
                    pem = _ssl.DER_cert_to_PEM_cert(binary_cert)
                    # Extract fields from PEM using openssl-style parsing
                    # Simpler: use the x509 decoder
                    try:
                        decoded = _ssl._ssl._test_decode_cert(  # type: ignore
                            # This is CPython internal, write to tempfile
                            ""
                        )
                    except Exception:
                        decoded = {}

                    # Fallback: parse common fields from the raw cert
                    cert_text = pem
                    return binary_cert, cert_text

        binary_cert, pem_text = await loop.run_in_executor(None, _get_cert_info)

        # Use a simpler approach: connect and read server cert subject via SSL
        def _simple_cert_info():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            info = {}
            with socket.create_connection((ip, port), timeout=BANNER_TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=ip) as ssock:
                    cipher = ssock.cipher()
                    cert_bin = ssock.getpeercert(binary_form=True)
                    # Parse subject CN from DER cert
                    pem = ssl.DER_cert_to_PEM_cert(cert_bin)
                    # Use regex on PEM-decoded output isn't great
                    # Best we can do without pyOpenSSL: write temp file and use _test_decode_cert
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".pem", mode="w", delete=False) as f:
                        f.write(pem)
                        tmp_path = f.name
                    try:
                        decoded = ssl._ssl._test_decode_cert(tmp_path)  # type: ignore
                        if decoded:
                            # subject is tuple of tuples
                            subject = decoded.get("subject", ())
                            for rdn in subject:
                                for attr_type, attr_value in rdn:
                                    if attr_type == "commonName":
                                        info["subject"] = attr_value
                            issuer = decoded.get("issuer", ())
                            issuer_parts = []
                            for rdn in issuer:
                                for attr_type, attr_value in rdn:
                                    issuer_parts.append(f"{attr_type}={attr_value}")
                            if issuer_parts:
                                info["issuer"] = ", ".join(issuer_parts)
                            info["expiry"] = decoded.get("notAfter", "")
                            sans = decoded.get("subjectAltName", ())
                            san_list = [v for _, v in sans]
                            if san_list:
                                info["sans"] = ", ".join(san_list)
                    finally:
                        import os as _os
                        _os.unlink(tmp_path)
            return info

        result = await loop.run_in_executor(None, _simple_cert_info)

    except Exception as exc:
        log.debug("tls_fingerprint_failed", "TLS fingerprint failed", context={"ip": ip, "port": port, "error": str(exc)})

    return result


async def _fingerprint_host(ip: str, ports_open: list[str]) -> dict:
    """Collect all fingerprint data for a host based on its open ports."""
    fp: dict = {
        "banners": {},
        "http_headers": {},
        "http_title": "",
        "tls_subject": "",
        "tls_issuer": "",
        "tls_expiry": "",
        "tls_sans": "",
        "ssh_banner": "",
    }

    # SSH banner (port 22)
    if _has_port(ports_open, 22):
        banner = await _grab_banner(ip, 22)
        if banner:
            fp["ssh_banner"] = banner.split("\n")[0][:256]
            fp["banners"]["22"] = fp["ssh_banner"]

    # Generic banners for other ports (not HTTP)
    non_http_ports = [int(p.split("/")[1]) for p in ports_open
                      if p.startswith("tcp/") and int(p.split("/")[1]) not in (22, 80, 443, 8080, 8443)]
    for port in non_http_ports[:5]:  # Limit to avoid slowness
        banner = await _grab_banner(ip, port)
        if banner:
            fp["banners"][str(port)] = banner[:512]

    # HTTP fingerprinting
    tcp_ports = _get_tcp_ports(ports_open)
    for port in tcp_ports:
        if port in (80, 8080):
            http_info = await _fingerprint_http(ip, port, use_tls=False)
            if http_info["headers"]:
                fp["http_headers"].update(http_info["headers"])
            if http_info["title"] and not fp["http_title"]:
                fp["http_title"] = http_info["title"][:256]
            if http_info["server"]:
                fp["banners"][str(port)] = http_info["server"]
        elif port in (443, 8443):
            http_info = await _fingerprint_http(ip, port, use_tls=True)
            if http_info["headers"]:
                fp["http_headers"].update(http_info["headers"])
            if http_info["title"] and not fp["http_title"]:
                fp["http_title"] = http_info["title"][:256]
            if http_info["server"]:
                fp["banners"][str(port)] = http_info["server"]

    # TLS certificate (prefer 443, fallback to 8443)
    for port in (443, 8443):
        if _has_port(ports_open, port):
            tls_info = await _fingerprint_tls(ip, port)
            if tls_info:
                fp["tls_subject"] = tls_info.get("subject", "")
                fp["tls_issuer"] = tls_info.get("issuer", "")
                fp["tls_expiry"] = tls_info.get("expiry", "")
                fp["tls_sans"] = tls_info.get("sans", "")
                break  # Got cert from one port, no need for the other

    collected = [k for k, v in fp.items() if v]
    log.debug("fingerprint_complete", "Host fingerprinting complete", context={"ip": ip, "collected_fields": collected})
    return fp


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Valid classifications surfaced in UI filters and dropdowns. Sweeps produce
# the network/server/etc. set; the Proxmox connector contributes virtual_machine,
# container, and hypervisor.
CLASSIFICATIONS: tuple[str, ...] = (
    "router", "switch", "firewall", "access_point",
    "network_device", "printer", "phone", "camera",
    "server", "web_service", "endpoint", "unknown",
    "virtual_machine", "container", "hypervisor",
)

# Specific sysDescr patterns for sub-classification (Tier 1)
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

# OUI-based classification (Tier 2)
PRINTER_VENDORS = {
    "Hewlett Packard", "HP Inc.", "Ricoh", "Xerox", "Canon", "Brother",
    "Lexmark", "Konica Minolta", "Kyocera", "Epson", "Samsung Electronics",
}
PHONE_VENDORS = {
    "Polycom", "Yealink", "Grandstream", "Snom", "Mitel Networks",
    "Avaya", "Fanvil Technology", "Gigaset Communications",
}
CAMERA_VENDORS = {
    "Axis Communications", "Hikvision", "Dahua", "Vivotek",
    "Hanwha Techwin", "Bosch Security", "FLIR Systems",
}


def classify_endpoint(
    ports_open: list[str],
    mac_vendor: str,
    snmp_data: dict[str, str],
    fingerprint: dict | None = None,
) -> tuple[str, str, list[str]]:
    """Classify a discovered host using tiered heuristics.

    Returns: (classification, confidence, signals_matched)

    Tiers (evaluated in order, higher tiers override lower):
      1. SNMP sysDescr — highest confidence, most specific
      2. OUI (MAC vendor) — medium confidence
      3. Open ports — medium confidence
      4. Banners/headers — lower confidence

    When multiple tiers agree, confidence is "high". Single-tier match is
    "medium". No match → "unknown" with "low".

    Sweep-discovered hosts never become virtual_machine/container/hypervisor —
    those are reserved for the Proxmox connector.
    """
    fp = fingerprint or {}
    sysdescr_lower = snmp_data.get("sysDescr", "").lower()
    ssh_banner = fp.get("ssh_banner", "").lower()
    http_title = fp.get("http_title", "").lower()
    http_server = fp.get("http_server", "").lower()

    votes: list[tuple[str, str]] = []  # (classification, signal)

    # ---- Tier 1: SNMP sysDescr (highest confidence) ----
    if sysdescr_lower:
        # Try specific patterns first (SRX→firewall, EX→switch, etc.)
        for pattern, cls in _SYSDESCR_SPECIFIC:
            if pattern in sysdescr_lower:
                votes.append((cls, f"snmp_sysdescr:{pattern}"))
                break
        else:
            # Fall back to generic network device detection
            for keyword in NETWORK_SYSDESCR_KEYWORDS:
                if keyword in sysdescr_lower:
                    votes.append(("network_device", f"snmp_sysdescr:{keyword}"))
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
        # No signals — fall back to port-based heuristics
        if _has_any_port(ports_open, [80, 443, 8080, 8443]):
            if _has_port(ports_open, 22):
                return ("server", "low", ["port:22+http"])
            return ("web_service", "low", ["port:http"])
        if _has_port(ports_open, 22):
            return ("server", "low", ["port:22/ssh"])
        if snmp_data:
            return ("network_device", "low", ["snmp_responds"])
        tcp_ports = _get_tcp_ports(ports_open)
        if not tcp_ports:
            return ("endpoint", "low", [])
        return ("unknown", "low", [])

    # Count unique classifications voted for
    from collections import Counter
    cls_counts = Counter(cls for cls, _ in votes)
    winner, count = cls_counts.most_common(1)[0]
    signals = [sig for cls, sig in votes if cls == winner]

    # Confidence: multiple tiers agree = high, single vote = medium
    # Check how many distinct tiers contributed (snmp vs oui vs port vs banner)
    tier_prefixes = set()
    for sig in signals:
        prefix = sig.split(":")[0]
        tier_prefixes.add(prefix)
    confidence = "high" if len(tier_prefixes) >= 2 else "medium"

    return (winner, confidence, signals)


def _classify(
    ports_open: list[str],
    mac_vendor: str,
    snmp_data: dict[str, str],
    fingerprint: dict | None = None,
) -> str:
    """Legacy wrapper — returns just the classification string.

    Used by the sweep pipeline. Calls classify_endpoint() internally.
    """
    cls, _, _ = classify_endpoint(ports_open, mac_vendor, snmp_data, fingerprint)
    return cls


# ---------------------------------------------------------------------------
# Check known devices in Nautobot
# ---------------------------------------------------------------------------

async def _check_known(ip: str) -> bool:
    """Check if this IP already exists as a device in Nautobot.

    Queries IPAM for the IP address, then checks if any device has it
    as a primary IP. Falls back to checking if the IP is assigned to
    any device interface.
    """
    try:
        # Check if an IP address record exists and is assigned to a device
        ip_addresses = await nautobot_client.get_ip_addresses()
        for addr in ip_addresses:
            display = addr.get("display", "")
            # display is like "192.0.2.1/24"
            if display.startswith(ip + "/") or display == ip:
                # Check if it's assigned to a device interface
                assigned = addr.get("assigned_object")
                if assigned and assigned.get("device"):
                    log.debug("check_known_result", "Known device check complete", context={"ip": ip, "is_known": True})
                    return True
                # Even if not assigned to an interface, if it exists
                # in IPAM as a device's primary IP, check devices
                break

        # Also check devices directly by resolving their primary IP
        devices = await nautobot_client.get_devices()
        for device in devices:
            pip = device.get("primary_ip4") or device.get("primary_ip6")
            if not pip:
                continue
            # Resolve the IP address object to get the actual address
            pip_id = pip.get("id")
            if pip_id:
                for addr in ip_addresses:
                    if addr.get("id") == pip_id:
                        display = addr.get("display", "")
                        if display.startswith(ip + "/") or display == ip:
                            log.debug("check_known_result", "Known device check complete", context={"ip": ip, "is_known": True})
                            return True
                        break
    except Exception as exc:
        log.debug("check_known_error", "Failed to check if IP is known", context={"ip": ip, "error": str(exc)})

    log.debug("check_known_result", "Known device check complete", context={"ip": ip, "is_known": False})
    return False


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

# Platform detection patterns: (substring_to_match, platform_slug)
# Checked against SNMP sysDescr (lowercased) and SSH banner (lowercased).
# Order matters — more specific patterns before general ones.
# Every slug here MUST have a matching Platform in Nautobot (see bootstrap.sh).
_PLATFORM_PATTERNS: list[tuple[list[str], str]] = [
    # Juniper
    (["junos", "juniper"], "juniper_junos"),
    # Palo Alto
    (["pan-os", "palo alto", "paloalto"], "paloalto_panos"),
    # Cisco NX-OS (before generic IOS)
    (["nx-os", "nxos", "nexus"], "cisco_nxos"),
    # Cisco IOS-XR (before generic IOS)
    (["ios-xr", "iosxr", "ios xr"], "cisco_iosxr"),
    # Cisco IOS / IOS-XE (same NAPALM ios driver)
    (["cisco ios", "ios-xe", "iosxe", "ios xe", "catalyst"], "cisco_ios"),
    # Cisco ASA (uses ios driver as best-effort)
    (["cisco asa", "adaptive security"], "cisco_ios"),
    # Arista
    (["arista", "eos"], "arista_eos"),
    # Fortinet
    (["fortigate", "fortios", "fortinet"], "fortinet_fortios"),
    # MikroTik
    (["routeros", "mikrotik"], "mikrotik_routeros"),
    # Aruba / HPE
    (["arubaos-cx", "arubaos", "aruba", "procurve", "comware"], "aruba_aoscx"),
    # Extreme
    (["extremexos", "exos", "extreme"], "extreme_exos"),
    # Ubiquiti (EdgeOS uses Vyatta/VyOS, no dedicated NAPALM driver — use ios as fallback)
    (["ubiquiti", "edgeos", "unifi"], "ubiquiti_edgeos"),
    # Huawei
    (["huawei", "vrp"], "huawei_vrp"),
]


def _detect_platform(snmp_data: dict, fingerprint: dict | None = None) -> str | None:
    """Detect the Nautobot platform slug from SNMP sysDescr and SSH banner.

    Returns a platform network_driver string (e.g. 'juniper_junos') that
    Nautobot can match to a pre-loaded Platform, or None if unrecognized.

    Checks SNMP sysDescr first (highest confidence), then falls back to
    SSH banner analysis. All returned slugs must have matching Platform
    records in Nautobot (created by the bootstrap script).
    """
    fp = fingerprint or {}
    sources = [
        snmp_data.get("sysDescr", "").lower(),
        fp.get("ssh_banner", "").lower(),
        fp.get("http_title", "").lower(),
    ]

    for source in sources:
        if not source:
            continue
        for keywords, slug in _PLATFORM_PATTERNS:
            for kw in keywords:
                if kw in source:
                    log.debug("detect_platform", "Platform detected",
                              context={"platform": slug, "matched": kw, "source": source[:80]})
                    return slug

    return None


async def _ensure_primary_ip(ip: str) -> None:
    """Ensure the newly onboarded device has its management IP set as primary.

    After onboarding, the device may exist in Nautobot but without a
    primary_ip4 — which is needed for NAPALM connections, SNMP polling,
    and the /api/nodes display.

    Runs a Django ORM script inside the Nautobot container via nbshell.
    The script finds/creates the IP, assigns it to a management interface,
    and sets it as primary_ip4.
    """
    import docker as _docker
    import io
    import tarfile

    repair_code = f"""\
import sys
from nautobot.dcim.models import Device, Interface
from nautobot.ipam.models import IPAddress, Namespace, Prefix
from nautobot.extras.models import Status
from netaddr import IPNetwork
ip_str = '{ip}'
dev = None
for ipa in IPAddress.objects.filter(host=ip_str):
    for iface in ipa.interfaces.all():
        if iface.device:
            dev = iface.device
            break
    if dev:
        break
if not dev:
    candidates = Device.objects.filter(primary_ip4__isnull=True).order_by('-created')
    if candidates.exists():
        dev = candidates.first()
if not dev:
    sys.stderr.write('NO_DEVICE: ' + ip_str + chr(10))
elif dev.primary_ip4:
    sys.stderr.write('ALREADY_SET: ' + dev.name + chr(10))
else:
    active = Status.objects.get(name='Active')
    ns = Namespace.objects.get(name='Global')
    prefix_str = str(IPNetwork(ip_str + '/24').network) + '/24'
    pfx, _ = Prefix.objects.get_or_create(prefix=prefix_str, namespace=ns, defaults={{'status': active}})
    ipa, _ = IPAddress.objects.get_or_create(host=ip_str, mask_length=32, parent=pfx, defaults={{'status': active}})
    mgmt_iface = None
    for iface in Interface.objects.filter(device=dev).order_by('name'):
        iname = iface.name.lower()
        if any(x in iname for x in ['mgmt', 'me0', 'fxp0', 'em0', 'management', 'vme']):
            mgmt_iface = iface
            break
    if not mgmt_iface:
        mgmt_iface = Interface.objects.filter(device=dev).order_by('name').first()
    if mgmt_iface:
        if ipa not in mgmt_iface.ip_addresses.all():
            mgmt_iface.ip_addresses.add(ipa)
        dev.primary_ip4 = ipa
        dev.validated_save()
        sys.stderr.write('SET: ' + dev.name + ' -> ' + str(ipa) + ' on ' + mgmt_iface.name + chr(10))
    else:
        sys.stderr.write('NO_INTERFACE: ' + dev.name + chr(10))
"""
    try:
        client = _docker.from_env()
        container = client.containers.get("mnm-nautobot")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            script_bytes = repair_code.encode('utf-8')
            info = tarfile.TarInfo(name='_set_pip.py')
            info.size = len(script_bytes)
            tar.addfile(info, io.BytesIO(script_bytes))
        buf.seek(0)
        container.put_archive('/tmp', buf)
        result = container.exec_run(
            ["nautobot-server", "nbshell", "--command", "exec(open('/tmp/_set_pip.py').read())"],
            stderr=True,
        )
        output = result.output.decode("utf-8", errors="replace")
        if "SET:" in output:
            log.info("primary_ip_set", f"Set primary IP for device at {ip}",
                     context={"output": output.strip()[-300:]})
        elif "ALREADY_SET:" in output:
            log.debug("primary_ip_exists", f"Device at {ip} already has primary IP")
        else:
            log.warning("primary_ip_issue", f"Primary IP assignment issue",
                        context={"ip": ip, "output": output.strip()[-300:]})
    except Exception as exc:
        log.warning("primary_ip_failed", f"Could not set primary IP: {exc}",
                    context={"ip": ip, "error": str(exc)})


async def repair_missing_primary_ips() -> dict:
    """Startup repair: find onboarded devices without primary_ip4 and fix them.

    Called once at startup to handle devices onboarded before this fix existed.
    Uses docker exec into nautobot container to run a Django script via nbshell.
    Returns summary of actions taken.
    """
    import docker as _docker
    import tempfile
    import os

    # Write the repair script to a temp file on the host, then copy into container
    repair_code = """\
import sys
from nautobot.dcim.models import Device, Interface
from nautobot.ipam.models import IPAddress
fixed = 0
skipped = 0
failed = 0
for dev in Device.objects.filter(primary_ip4__isnull=True):
    mgmt_ip = None
    mgmt_iface = None
    for iface in Interface.objects.filter(device=dev).order_by('name'):
        for ipa in iface.ip_addresses.all():
            mgmt_ip = ipa
            mgmt_iface = iface
            break
        if mgmt_ip:
            break
    if mgmt_ip:
        dev.primary_ip4 = mgmt_ip
        try:
            dev.validated_save()
            sys.stderr.write('FIXED: ' + dev.name + ' -> ' + str(mgmt_ip) + chr(10))
            fixed += 1
        except Exception as e:
            sys.stderr.write('FAIL: ' + dev.name + ': ' + str(e) + chr(10))
            failed += 1
    else:
        sys.stderr.write('SKIP: ' + dev.name + ' has no IP on any interface' + chr(10))
        skipped += 1
sys.stderr.write('SUMMARY: fixed=' + str(fixed) + ' skipped=' + str(skipped) + ' failed=' + str(failed) + chr(10))
"""
    try:
        client = _docker.from_env()
        container = client.containers.get("mnm-nautobot")

        # Use put_archive to copy script into container (avoids heredoc issues)
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            script_bytes = repair_code.encode('utf-8')
            info = tarfile.TarInfo(name='_repair_ips.py')
            info.size = len(script_bytes)
            tar.addfile(info, io.BytesIO(script_bytes))
        buf.seek(0)
        container.put_archive('/tmp', buf)

        result = container.exec_run(
            ["nautobot-server", "nbshell", "--command", "exec(open('/tmp/_repair_ips.py').read())"],
            stderr=True,
        )
        output = result.output.decode("utf-8", errors="replace")
        log.info("primary_ip_repair", "Primary IP repair completed",
                 context={"output": output.strip()[-500:]})

        summary: dict = {"fixed": 0, "skipped": 0, "failed": 0}
        for line in output.splitlines():
            if "SUMMARY:" in line:
                import re
                for k in ("fixed", "skipped", "failed"):
                    m = re.search(f"{k}=(\\d+)", line)
                    if m:
                        summary[k] = int(m.group(1))
        return summary
    except Exception as exc:
        log.warning("primary_ip_repair_failed", "Primary IP repair failed",
                    context={"error": str(exc)})
        return {"fixed": 0, "skipped": 0, "failed": 0, "error": str(exc)}


async def _onboard_host(
    ip: str,
    location_id: str,
    secrets_group_id: str,
    snmp_data: dict | None = None,
    fingerprint: dict | None = None,
) -> bool:
    """Submit onboarding job for a single host and poll for completion.

    Auto-detects the platform from SNMP sysDescr and SSH banner, then passes
    it to the onboarding job so Netmiko doesn't have to guess the driver.
    """
    platform_slug = _detect_platform(snmp_data or {}, fingerprint=fingerprint)

    log.info("onboard_submit", "Submitting onboarding job", context={"ip": ip, "platform": platform_slug})
    _onb_set(ip, "submitting", f"Detected platform {platform_slug or 'unknown'}; submitting onboarding job", platform=platform_slug)

    # Pre-clean: the controller's sweep records every alive IP into IPAM,
    # but the onboarding plugin tries to create the same IPAddress and
    # crashes with a UniqueViolation. Drop any standalone IP record (one
    # that's not yet attached to a device interface) before submitting.
    try:
        await nautobot_client.delete_standalone_ip(ip)
    except Exception as exc:
        log.debug("ip_pre_onboard_delete_failed", "Could not pre-clean IP", context={"ip": ip, "error": str(exc)})

    try:
        result = await nautobot_client.submit_onboarding_job(
            ip=ip,
            location_id=location_id,
            secrets_group_id=secrets_group_id,
            platform_slug=platform_slug,
        )
        job_result_id = result.get("job_result", {}).get("id")
        if not job_result_id:
            log.error("onboard_no_job_id", "Onboarding job returned no result ID", context={"ip": ip})
            _onb_set(ip, "failed", "Onboarding job submission returned no result ID", error="no job_result_id")
            return False

        _onb_set(ip, "queued", "Onboarding job submitted; waiting for Celery worker to pick it up",
                 job_result_id=job_result_id)

        # Poll for completion. Three signals are checked in priority order:
        #   1. Device shows up in Nautobot (authoritative — the goal)
        #   2. JobResult.status reaches a terminal Celery state
        #   3. Redis celery-task-meta has a terminal state (fallback for
        #      the known plugin bug where OnboardException leaves the
        #      JobResult DB row stuck in PENDING forever)
        #
        # Nautobot 3.x JobResult.status uses uppercase Celery names
        # (PENDING, RECEIVED, STARTED, SUCCESS, FAILURE, REVOKED, RETRY).
        TERMINAL_OK = {"SUCCESS"}
        TERMINAL_FAIL = {"FAILURE", "REVOKED"}
        # 10 minutes total: slow Junos devices regularly need 4-6 min.
        max_polls = 120
        last_db_status = ""
        for poll_idx in range(max_polls):
            await asyncio.sleep(5)
            elapsed = (poll_idx + 1) * 5

            # Check for sweep cancellation — without this, a single
            # onboarding poll loop (up to 10 min) blocks the entire
            # asyncio.gather from finishing after the operator clicks Stop.
            if _sweep_cancel:
                _onb_set(ip, "failed", "Sweep cancelled by operator during onboarding poll",
                         error="sweep_cancelled")
                log.info("onboard_cancelled", "Onboarding cancelled (sweep stop)",
                         context={"ip": ip, "job_result_id": job_result_id})
                return False

            # 1. Authoritative success: device exists in Nautobot
            try:
                dev = await nautobot_client.find_device_by_ip(ip)
            except Exception:
                dev = None
            if dev:
                log.info(
                    "onboard_success",
                    "Device onboarded successfully (device present in Nautobot)",
                    context={"ip": ip, "job_result_id": job_result_id, "device_id": dev.get("id")},
                )
                _onb_set(ip, "succeeded", f"Device onboarded ({dev.get('name', 'unnamed')})",
                         device_id=dev.get("id"), device_name=dev.get("name"))
                return True

            # 2. JobResult.status from DB
            db_status = ""
            try:
                jr = await nautobot_client.get_job_result(job_result_id)
                status = jr.get("status", {})
                if isinstance(status, dict):
                    status = status.get("value", "")
                db_status = (status or "").upper()
            except Exception:
                pass

            if db_status and db_status != last_db_status:
                last_db_status = db_status
                # Surface stage transitions in the tracker
                if db_status == "STARTED":
                    _onb_set(ip, "running", f"Worker is executing the onboarding job ({elapsed}s)")
                elif db_status == "RECEIVED":
                    _onb_set(ip, "queued", f"Worker received the job ({elapsed}s)")
            else:
                # Lightweight elapsed-time update so the UI keeps moving
                stage = _onboarding_state.get(ip, {}).get("stage", "queued")
                if stage in ("queued", "running"):
                    _onb_set(ip, stage, f"{stage.capitalize()} ({elapsed}s elapsed)")

            if db_status in TERMINAL_OK:
                await asyncio.sleep(2)
                dev = await nautobot_client.find_device_by_ip(ip)
                if dev:
                    log.info("onboard_success", "Device onboarded successfully", context={"ip": ip, "job_result_id": job_result_id})
                    _onb_set(ip, "succeeded", f"Device onboarded ({dev.get('name','unnamed')})",
                             device_id=dev.get("id"), device_name=dev.get("name"))
                    return True
                log.error("onboard_success_no_device", "JobResult SUCCESS but no device in Nautobot",
                          context={"ip": ip, "job_result_id": job_result_id})
                _onb_set(ip, "failed", "JobResult reports SUCCESS but no device exists in Nautobot",
                         error="success_no_device")
                return False
            if db_status in TERMINAL_FAIL:
                err = await _fetch_celery_error(job_result_id)
                log.error("onboard_failed", "Onboarding job failed",
                          context={"ip": ip, "job_result_id": job_result_id, "status": db_status, "error": err})
                _onb_set(ip, "failed", err or f"Job failed ({db_status})",
                         error=err or db_status, job_result_status=db_status)
                return False

            # 3. Redis fallback: after ~30 s of "PENDING" with no progress,
            # check celery-task-meta directly. This catches the plugin bug
            # where the JobResult DB row stays PENDING forever even when
            # the underlying Celery task has reached a terminal state
            # (whether SUCCESS or FAILURE).
            if db_status in ("", "PENDING") and elapsed >= 30 and elapsed % 15 == 0:
                meta = await _read_celery_meta(job_result_id)
                if meta:
                    meta_status = (meta.get("status") or "").upper()
                    if meta_status in TERMINAL_FAIL:
                        err = _format_celery_meta_error(meta)
                        log.error("onboard_failed_redis",
                                  "Onboarding job failed (recovered from Celery result backend; DB row stuck PENDING)",
                                  context={"ip": ip, "job_result_id": job_result_id, "error": err})
                        _onb_set(ip, "failed", err,
                                 error=err, job_result_status="FAILURE (DB stuck PENDING)")
                        return False
                    if meta_status in TERMINAL_OK:
                        # Celery says success — give Nautobot a moment to
                        # commit the device row, then look it up.
                        await asyncio.sleep(2)
                        dev = await nautobot_client.find_device_by_ip(ip)
                        if dev:
                            log.info("onboard_success_redis",
                                     "Device onboarded successfully (recovered from Celery result backend)",
                                     context={"ip": ip, "job_result_id": job_result_id, "device_id": dev.get("id")})
                            _onb_set(ip, "succeeded", f"Device onboarded ({dev.get('name','unnamed')})",
                                     device_id=dev.get("id"), device_name=dev.get("name"))
                            return True
                        # Celery SUCCESS but no device — surface clearly
                        log.error("onboard_success_no_device_redis",
                                  "Celery reports SUCCESS but device not found in Nautobot",
                                  context={"ip": ip, "job_result_id": job_result_id})
                        _onb_set(ip, "failed",
                                 "Celery reports SUCCESS but device was not created in Nautobot",
                                 error="success_no_device", job_result_status="SUCCESS (DB stuck PENDING)")
                        return False

        _onb_set(ip, "timeout", f"Onboarding did not complete within {max_polls*5}s",
                 job_result_id=job_result_id)
        log.error("onboard_timeout", "Onboarding job timed out", context={"ip": ip, "job_result_id": job_result_id})
        return False
    except Exception as exc:
        log.error("onboard_error", "Onboarding job raised exception", context={"ip": ip, "error": str(exc)}, exc_info=True)
        _onb_set(ip, "failed", f"Controller exception: {exc}", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Celery result backend fallback
# ---------------------------------------------------------------------------
# nautobot-device-onboarding 5.x can raise OnboardException without letting
# Nautobot's run_job wrapper update the JobResult row. The terminal state
# always lands in the Celery result backend (Redis db 1) though, so we
# read it directly when the DB row stays PENDING for too long.

async def _read_celery_meta(job_result_id: str) -> dict | None:
    try:
        import redis.asyncio as aioredis  # type: ignore
    except Exception:
        return None
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/1")
    # Force db 1 (Celery result backend) regardless of REDIS_URL's db
    try:
        client = aioredis.from_url(redis_url, db=1)
        raw = await client.get(f"celery-task-meta-{job_result_id}")
        await client.aclose()
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _format_celery_meta_error(meta: dict) -> str:
    """Turn a Celery result-backend meta dict into a readable one-liner."""
    result = meta.get("result") or {}
    if isinstance(result, dict):
        exc_type = result.get("exc_type") or ""
        exc_msg = result.get("exc_message") or []
        if isinstance(exc_msg, list):
            exc_msg_str = " ".join(str(m) for m in exc_msg).strip()
        else:
            exc_msg_str = str(exc_msg)
        if exc_type and exc_msg_str:
            return f"{exc_type}: {exc_msg_str}"
        if exc_msg_str:
            return exc_msg_str
        if exc_type:
            return exc_type
    return str(result)[:500] if result else "(no error detail)"


async def _fetch_celery_error(job_result_id: str) -> str:
    """Best-effort: pull a useful error string from the Celery result backend."""
    meta = await _read_celery_meta(job_result_id)
    if not meta:
        return ""
    return _format_celery_meta_error(meta)


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

async def sweep(
    cidr_ranges: list[str],
    location_id: str,
    secrets_group_id: str,
    snmp_community: str = "",
    auto_discover_hops: int = 0,
) -> dict:
    """Run a seed-and-sweep discovery across the given CIDR ranges.

    Phases:
      1. TCP port scan (all ports)
      2. Enrich alive hosts (DNS, ARP/MAC, SNMP, classify)
      3. Check Nautobot for known devices
      4. Onboard eligible network devices, record everything else to IPAM
      5. (Optional) Auto-discover LLDP neighbors from newly onboarded nodes
    """
    global _sweep_state

    if not snmp_community:
        snmp_community = os.environ.get("SNMP_COMMUNITY", "public")

    now_iso = datetime.now(timezone.utc).isoformat()

    global _sweep_cancel
    _sweep_cancel = False
    _sweep_state["running"] = True
    _sweep_state["hosts"] = {}
    _sweep_state["summary"] = None
    _sweep_state["log"] = []
    _sweep_state["started_at"] = now_iso
    _sweep_state["finished_at"] = None
    _sweep_state["duration_seconds"] = None

    def _log(msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        _sweep_state["log"].append(entry)
        log.info("sweep_progress", msg)
        # Keep log bounded
        if len(_sweep_state["log"]) > 500:
            _sweep_state["log"] = _sweep_state["log"][-500:]

    _log(f"Starting sweep of {len(cidr_ranges)} range(s)")

    # Enumerate all IPs
    all_ips: list[str] = []
    for cidr in cidr_ranges:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            all_ips.extend(str(ip) for ip in network.hosts())
        except ValueError:
            continue

    for ip in all_ips:
        _sweep_state["hosts"][ip] = {
            "ip": ip,
            "status": SweepStatus.PENDING,
            "ports_open": [],
            "mac_address": "",
            "mac_vendor": "",
            "dns_name": "",
            "snmp": {},
            "classification": "",
            "first_seen": now_iso,
            "last_seen": now_iso,
            "onboarded": False,
            "onboard_eligible": False,
            "banners": {},
            "http_headers": {},
            "http_title": "",
            "tls_subject": "",
            "tls_issuer": "",
            "tls_expiry": "",
            "tls_sans": "",
            "ssh_banner": "",
        }

    # ------------------------------------------------------------------
    _log(f"Enumerated {len(all_ips)} IPs to scan")

    # Pre-fetch known IPs from Nautobot (one bulk call instead of per-host)
    _known_ips: set[str] = set()
    try:
        ip_addresses = await nautobot_client.get_ip_addresses()
        devices = await nautobot_client.get_devices()
        device_ip_ids = set()
        for dev in devices:
            pip = dev.get("primary_ip4") or dev.get("primary_ip6")
            if pip and pip.get("id"):
                device_ip_ids.add(pip["id"])
        for addr in ip_addresses:
            display = addr.get("display", "")
            ip_part = display.split("/")[0] if "/" in display else display
            # Known if assigned to a device interface or is a device's primary IP
            assigned = addr.get("assigned_object")
            if (assigned and assigned.get("device")) or addr.get("id") in device_ip_ids:
                _known_ips.add(ip_part)
        _log(f"Pre-fetched {len(_known_ips)} known device IPs from Nautobot")
    except Exception as exc:
        _log(f"Warning: failed to pre-fetch known IPs: {exc}")

    # Load endpoint data for MAC/vendor enrichment (from infrastructure collection)
    _endpoint_macs: dict[str, tuple[str, str]] = {}  # ip -> (mac, vendor)
    _excluded_ips: set[str] = set()
    try:
        from app import db as _db
        from app import endpoint_store as _es
        if _db.is_ready():
            for ep in await _es.list_endpoints():
                ep_ip = ep.get("ip") or ep.get("current_ip")
                mac = ep.get("mac", "")
                vendor = ep.get("mac_vendor", "")
                if ep_ip and mac:
                    _endpoint_macs[ep_ip] = (mac, vendor)
            if _endpoint_macs:
                _log(f"Loaded {len(_endpoint_macs)} MAC addresses from endpoint store")
            # Operator-defined exclusion list (Rule 6)
            _excluded_ips = await _es.get_excluded_ips()
            if _excluded_ips:
                _log(f"Skipping {len(_excluded_ips)} excluded IP(s) per discovery exclusion list")
    except Exception:
        pass

    # Concurrency from env or default
    sweep_concurrency = int(os.environ.get("MNM_SWEEP_CONCURRENCY", str(MAX_CONCURRENT_PROBES)))

    # ------------------------------------------------------------------
    # Full pipeline per host: scan → enrich → classify → check → onboard
    # Each host completes fully before appearing in the UI with data.
    # Enrichment steps (DNS, ARP, SNMP, banners) run in parallel per host.
    # ------------------------------------------------------------------
    semaphore = asyncio.Semaphore(sweep_concurrency)
    loop = asyncio.get_event_loop()
    _completed = {"count": 0}
    _ipam_queue: list[tuple[str, dict]] = []  # batch IPAM writes

    async def process_host(ip: str):
        if _sweep_cancel:
            _completed["count"] += 1
            return

        host = _sweep_state["hosts"][ip]

        # Operator exclusion list — skip before any probing happens.
        # The host stays in the table marked DEAD so the count is honest:
        # the operator told us to ignore it, we ignored it.
        if ip in _excluded_ips:
            host["status"] = SweepStatus.DEAD
            host["classification"] = "excluded"
            _completed["count"] += 1
            return

        host["status"] = SweepStatus.SCANNING
        t0 = datetime.now(timezone.utc)

        # Step 1: Port scan
        open_ports = await _probe_all_ports(ip, semaphore)
        if _sweep_cancel:
            _completed["count"] += 1
            return
        if not open_ports:
            host["status"] = SweepStatus.DEAD
            _completed["count"] += 1
            return

        host["ports_open"] = open_ports
        host["status"] = SweepStatus.ENRICHING

        if _sweep_cancel:
            _completed["count"] += 1
            return

        # Steps 2-5: Run enrichment in parallel (all are IO-bound)
        # Always attempt SNMP — port 161 is UDP so TCP scan won't detect it.
        # The SNMP GET has its own timeout and fails gracefully.
        snmp_coro = _snmp_get(ip, snmp_community)
        dns_coro = loop.run_in_executor(None, _lookup_dns, ip)
        arp_coro = loop.run_in_executor(None, _lookup_arp, ip)
        fp_coro = _fingerprint_host(ip, open_ports)

        results = await asyncio.gather(snmp_coro, dns_coro, arp_coro, fp_coro, return_exceptions=True)

        # Unpack results
        snmp_data = results[0] if isinstance(results[0], dict) else {}
        dns_name = results[1] if isinstance(results[1], str) else ""
        mac = results[2] if isinstance(results[2], str) else ""
        fp = results[3] if isinstance(results[3], dict) else {}

        host["snmp"] = snmp_data
        host["dns_name"] = dns_name if dns_name != ip else ""
        host["mac_address"] = mac
        host["mac_vendor"] = _mac_vendor(mac)

        # Fallback: enrich MAC/vendor from endpoint collector data
        if not mac and ip in _endpoint_macs:
            ep_mac, ep_vendor = _endpoint_macs[ip]
            host["mac_address"] = ep_mac
            host["mac_vendor"] = ep_vendor or _mac_vendor(ep_mac)
        host["banners"] = fp.get("banners", {})
        host["http_headers"] = fp.get("http_headers", {})
        host["http_title"] = fp.get("http_title", "")
        host["tls_subject"] = fp.get("tls_subject", "")
        host["tls_issuer"] = fp.get("tls_issuer", "")
        host["tls_expiry"] = fp.get("tls_expiry", "")
        host["tls_sans"] = fp.get("tls_sans", "")
        host["ssh_banner"] = fp.get("ssh_banner", "")

        if _sweep_cancel:
            host["status"] = SweepStatus.RECORDED
            _completed["count"] += 1
            return

        # Step 6: Classify (tiered heuristics with confidence scoring)
        cls, confidence, signals = classify_endpoint(open_ports, host["mac_vendor"], host["snmp"], fp)
        host["classification"] = cls
        host["classification_confidence"] = confidence
        host["onboard_eligible"] = cls in ("network_device", "router", "switch", "firewall", "access_point")
        log.debug("classify_result", "Host classified", context={
            "ip": ip, "classification": cls, "confidence": confidence,
            "signals": signals[:5],
            "tcp_ports": _get_tcp_ports(open_ports), "mac_vendor": host["mac_vendor"],
            "has_snmp": bool(host["snmp"]), "has_ssh_banner": bool(host.get("ssh_banner")),
        })

        # Step 7: Check if already known (O(1) set lookup, no API call)
        if ip in _known_ips:
            host["status"] = SweepStatus.KNOWN
            host["onboarded"] = True
            _completed["count"] += 1
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
            _log(f"{ip}: known ({host['classification']}, {host.get('snmp', {}).get('sysName', '')}) [{elapsed:.1f}s]")
            return

        # Step 8: Onboard or record
        if _sweep_cancel:
            host["status"] = SweepStatus.RECORDED
            _completed["count"] += 1
            return

        if host["onboard_eligible"]:
            host["status"] = SweepStatus.ONBOARDING
            success = await _onboard_host(ip, location_id, secrets_group_id, snmp_data=host.get("snmp"),
                                            fingerprint={"ssh_banner": host.get("ssh_banner", ""), "http_title": host.get("http_title", "")})
            if success:
                host["status"] = SweepStatus.ONBOARDED
                host["onboarded"] = True
                # Ensure device has primary IP set (onboarding job may not set it)
                try:
                    await _ensure_primary_ip(ip)
                except Exception as exc:
                    log.warning("primary_ip_failed", f"Failed to set primary IP for {ip}", context={"error": str(exc)})
            else:
                host["status"] = SweepStatus.FAILED
        else:
            host["status"] = SweepStatus.RECORDED

        # Queue for batch IPAM write (instead of per-host API call)
        _ipam_queue.append((ip, host.copy()))

        _completed["count"] += 1
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        _log(f"{ip}: {host['status']} ({host['classification']}, ports={open_ports}) [{elapsed:.1f}s]")

    await asyncio.gather(*[process_host(ip) for ip in all_ips])

    if _sweep_cancel:
        _log("Sweep cancelled by operator")

    # ------------------------------------------------------------------
    # Batch IPAM writes (skip if cancelled) (instead of per-host API calls during sweep)
    # ------------------------------------------------------------------
    if _ipam_queue:
        _log(f"Recording {len(_ipam_queue)} hosts to Nautobot IPAM...")
        ipam_ok = 0
        ipam_fail = 0
        for ip, host_data in _ipam_queue:
            try:
                await nautobot_client.upsert_discovered_ip(ip, host_data)
                ipam_ok += 1
            except Exception as exc:
                ipam_fail += 1
                log.warning("ipam_record_failed", "Failed to record IP to IPAM", context={"ip": ip, "error": str(exc)})
        _log(f"IPAM recording: {ipam_ok} succeeded, {ipam_fail} failed")

    # ------------------------------------------------------------------
    # Auto-sync network data for newly onboarded devices
    # ------------------------------------------------------------------
    onboarded_hosts = [
        h for h in _sweep_state["hosts"].values()
        if h["status"] == SweepStatus.ONBOARDED
    ]
    if onboarded_hosts:
        _log(f"Triggering network data sync for {len(onboarded_hosts)} newly onboarded device(s)")
        try:
            # Get device IDs for the newly onboarded IPs
            devices = await nautobot_client.get_devices()
            ip_addrs = await nautobot_client.get_ip_addresses()

            # Build IP -> device ID map
            device_ip_map: dict[str, str] = {}
            for dev in devices:
                pip = dev.get("primary_ip4") or {}
                pip_id = pip.get("id")
                if pip_id:
                    for addr in ip_addrs:
                        if addr.get("id") == pip_id:
                            ip_part = addr.get("display", "").split("/")[0]
                            device_ip_map[ip_part] = dev["id"]
                            break

            sync_device_ids = []
            for h in onboarded_hosts:
                dev_id = device_ip_map.get(h["ip"])
                if dev_id:
                    sync_device_ids.append(dev_id)

            if sync_device_ids:
                await nautobot_client.submit_sync_network_data(
                    device_ids=sync_device_ids,
                    sync_cables=True,
                )
                _log(f"Network data sync submitted for {len(sync_device_ids)} device(s)")
        except Exception as exc:
            _log(f"Warning: network data sync failed: {exc}")
            log.warning("sync_after_onboard_failed", "Network data sync failed after onboarding", context={"error": str(exc)})

    # ------------------------------------------------------------------
    # Auto-discovery: walk LLDP neighbors from newly onboarded nodes
    # ------------------------------------------------------------------
    if auto_discover_hops > 0 and onboarded_hosts and not _sweep_cancel:
        from app import auto_discover
        for h in onboarded_hosts:
            if _sweep_cancel:
                break
            # Resolve the device name for the onboarded IP
            onb = _onboarding_state.get(h["ip"], {})
            device_name = onb.get("device_name", "")
            if not device_name:
                continue
            _log(f"Starting auto-discovery from {device_name} (hops={auto_discover_hops})")
            try:
                ad_result = await auto_discover.auto_discover_from_node(
                    seed_node_name=device_name,
                    max_hops=auto_discover_hops,
                    location_id=location_id,
                    secrets_group_id=secrets_group_id,
                    snmp_community=snmp_community,
                    log_fn=_log,
                )
                _log(f"Auto-discovery from {device_name}: "
                     f"{ad_result['succeeded']} succeeded, "
                     f"{ad_result['failed']} failed, "
                     f"{ad_result['skipped']} skipped")
            except Exception as exc:
                _log(f"Auto-discovery from {device_name} failed: {exc}")
                log.warning("auto_discover_failed", "Auto-discovery failed",
                            context={"seed": device_name, "error": str(exc)})

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    hosts = _sweep_state["hosts"]
    _sweep_state["summary"] = {
        "total": len(hosts),
        "alive": sum(
            1 for h in hosts.values()
            if h["status"] not in (SweepStatus.DEAD, SweepStatus.PENDING)
        ),
        "known": sum(1 for h in hosts.values() if h["status"] == SweepStatus.KNOWN),
        "onboarded": sum(1 for h in hosts.values() if h["status"] == SweepStatus.ONBOARDED),
        "recorded": sum(1 for h in hosts.values() if h["status"] == SweepStatus.RECORDED),
        "failed": sum(1 for h in hosts.values() if h["status"] == SweepStatus.FAILED),
    }
    finished_iso = datetime.now(timezone.utc).isoformat()
    started = datetime.fromisoformat(now_iso)
    finished = datetime.fromisoformat(finished_iso)
    duration = (finished - started).total_seconds()

    _sweep_state["running"] = False
    _sweep_state["finished_at"] = finished_iso
    _sweep_state["duration_seconds"] = round(duration, 1)

    _log(f"Sweep complete in {duration:.1f}s: {_sweep_state['summary']}")

    # Save to history (in-memory ring buffer for the live UI)
    _sweep_history.insert(0, {
        "started_at": now_iso,
        "finished_at": finished_iso,
        "duration_seconds": round(duration, 1),
        "cidr_ranges": cidr_ranges,
        "summary": _sweep_state["summary"].copy(),
    })
    if len(_sweep_history) > MAX_HISTORY:
        _sweep_history.pop()

    # Persist to controller DB: sweep run + per-IP observations + endpoint upserts
    try:
        from app import db as _db
        from app import endpoint_store as _es
        if _db.is_ready():
            await _es.record_sweep_run(cidr_ranges, started, finished, _sweep_state["summary"])
            for ip, host_data in hosts.items():
                if host_data["status"] in (SweepStatus.DEAD, SweepStatus.PENDING):
                    continue
                await _es.record_ip_observation(ip, host_data)
                # If the host has a MAC, mirror it into the endpoints table
                if host_data.get("mac_address"):
                    sweep_ep = {
                        "mac": host_data["mac_address"],
                        "ip": ip,
                        "mac_vendor": host_data.get("mac_vendor", ""),
                        "hostname": host_data.get("dns_name") or (host_data.get("snmp") or {}).get("sysName", ""),
                        "classification": host_data.get("classification", ""),
                    }
                    try:
                        await _es.upsert_endpoint(sweep_ep, source="sweep", change_source="sweep")
                    except Exception as exc:
                        log.debug("sweep_endpoint_upsert_failed", "Sweep upsert failed",
                                  context={"ip": ip, "error": str(exc)})
    except Exception as exc:
        log.warning("sweep_db_persist_failed", "Failed to persist sweep to controller DB",
                    context={"error": str(exc)})

    return _sweep_state["summary"]


# ---------------------------------------------------------------------------
# Scheduled sweep loop
# ---------------------------------------------------------------------------

async def scheduled_sweep_loop() -> None:
    """Background loop that runs sweeps on schedule based on config.

    Reads sweep_schedules from app.config.load_config(). Each schedule has:
        cidr_ranges, location_id, secrets_group_id, interval_hours, last_run
    """
    from app.config import load_config_async, save_config_async

    log.info("sweep_loop_started", "Scheduled sweep loop started")

    # One-shot migration: if a saved schedule references a location whose
    # type cannot accept devices, repoint it at the first valid location.
    # Without this the onboarding job fails inside Nautobot with
    # "Devices may not associate to locations of type X" and the JobResult
    # row gets stuck in PENDING (plugin bug), causing every sweep to time
    # out on the controller side.
    try:
        cfg = await load_config_async()
        schedules = cfg.get("sweep_schedules", []) or []
        if schedules:
            valid_locations = await nautobot_client.get_locations()
            valid_ids = {loc.get("id") for loc in valid_locations if loc.get("id")}
            valid_default = next(iter(valid_locations), None)
            changed = False
            for i, sched in enumerate(schedules):
                lid = sched.get("location_id")
                if lid and lid not in valid_ids and valid_default:
                    log.warning(
                        "sweep_schedule_location_fixed",
                        "Repointed saved schedule from invalid location to a device-capable one",
                        context={
                            "schedule_index": i,
                            "old_location_id": lid,
                            "new_location_id": valid_default.get("id"),
                            "new_location_name": valid_default.get("name"),
                        },
                    )
                    schedules[i]["location_id"] = valid_default.get("id")
                    changed = True
            if changed:
                cfg["sweep_schedules"] = schedules
                await save_config_async(cfg)
    except Exception as exc:
        log.warning(
            "sweep_schedule_migration_failed",
            "Could not validate saved sweep schedules at startup",
            context={"error": str(exc)},
        )

    while True:
        try:
            config = await load_config_async()
            schedules = config.get("sweep_schedules", []) or []

            # Auto-expand sweep ranges to include subnets from onboarded
            # device interfaces. If a device has an IP on a subnet not yet
            # in any schedule's cidr_ranges, add it to the first schedule.
            # The operator implicitly approved these subnets by onboarding
            # the devices that live on them.
            if schedules:
                try:
                    device_subnets = await nautobot_client.get_device_interface_subnets()
                    if device_subnets:
                        existing_cidrs: set[str] = set()
                        for s in schedules:
                            existing_cidrs.update(s.get("cidr_ranges", []))
                        missing = device_subnets - existing_cidrs
                        if missing:
                            schedules[0]["cidr_ranges"] = list(
                                set(schedules[0].get("cidr_ranges", [])) | missing
                            )
                            config["sweep_schedules"] = schedules
                            await save_config_async(config)
                            log.info(
                                "sweep_scope_auto_expanded",
                                "Added device-interface subnets to sweep schedule",
                                context={"added": sorted(missing)},
                            )
                except Exception as exc:
                    log.debug(
                        "sweep_scope_expand_failed",
                        "Could not auto-expand sweep scope from device interfaces",
                        context={"error": str(exc)},
                    )

            now = datetime.now(timezone.utc)

            for i, schedule in enumerate(schedules):
                interval_hours = schedule.get("interval_hours", 24)
                last_run_str = schedule.get("last_run", "")

                # Determine if this schedule is due
                should_run = False
                if not last_run_str:
                    should_run = True
                else:
                    try:
                        last_run = datetime.fromisoformat(last_run_str)
                        elapsed_hours = (now - last_run).total_seconds() / 3600
                        if elapsed_hours >= interval_hours:
                            should_run = True
                    except (ValueError, TypeError):
                        should_run = True

                if not should_run:
                    continue

                if _sweep_state["running"]:
                    log.info("sweep_skip_busy", "Skipping scheduled sweep — another sweep is running")
                    continue

                cidr_ranges = schedule.get("cidr_ranges", [])
                location_id = schedule.get("location_id", "")
                secrets_group_id = schedule.get("secrets_group_id", "")
                snmp_community = schedule.get("snmp_community", "")

                if not cidr_ranges or not location_id or not secrets_group_id:
                    log.warning("sweep_schedule_incomplete", "Sweep schedule missing required fields", context={"schedule_index": i})
                    continue

                log.info("sweep_schedule_trigger", "Running scheduled sweep", context={"schedule_index": i, "cidr_ranges": cidr_ranges})
                await sweep(cidr_ranges, location_id, secrets_group_id, snmp_community)

                # Update last_run
                config = await load_config_async()
                if i < len(config.get("sweep_schedules", [])):
                    config["sweep_schedules"][i]["last_run"] = now.isoformat()
                    await save_config_async(config)

        except Exception as exc:
            log.error("sweep_loop_error", "Scheduled sweep loop error", context={"error": str(exc)}, exc_info=True)

        # Check every 5 minutes
        await asyncio.sleep(300)
