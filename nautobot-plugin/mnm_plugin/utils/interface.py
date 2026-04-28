"""Cross-vendor interface naming helper.

Per E0 §2d: every plugin view that references a vendor-native
interface name MUST go through this helper. Direct ``==``
comparison of raw interface names is forbidden — vendor naming
heterogeneity (Junos slot/port form, Junos logical-unit form,
Arista numeric, Fortinet alias, Cisco short, Cisco long) makes
literal equality unsafe.

Public interface:

  - :func:`normalize` — return ``(canonical, original)`` for a
    vendor-native interface name.
  - :func:`is_sentinel` — detect the ``ifindex:N`` sentinel that
    SNMP collectors produce when bridge-port → ifIndex resolution
    fails (per Block C P3/P4/P5 discipline).
  - :func:`get_interface` — look up a Nautobot
    ``dcim.Interface`` row using a vendor-native name. Tries
    multiple candidate forms in order (literal, normalized,
    short→long expansion). Returns ``None`` on miss; never raises
    on missing lookups.

Tested forms (see ``tests/test_utils_interface.py``):

  - Junos: ``ge-0/0/12``, ``ge-0/0/12.0``, ``ae0``, ``ae0.0``,
    ``irb.0``, ``lo0``, ``xe-0/2/0``
  - Arista: ``Ethernet1``, ``Ethernet1/1``, ``Management1``
  - Fortinet: ``wan``, ``lan1``, ``fortilink``, ``port1``,
    ``internal``
  - Cisco short: ``Gi1``, ``Gi0/0``, ``Gi0/0/1``, ``Nu0``,
    ``Te0/1``, ``Vl100``
  - Cisco long: ``GigabitEthernet1``, ``GigabitEthernet0/0``,
    ``Null0``, ``TenGigabitEthernet0/1``, ``Vlan100``
  - Sentinel: ``ifindex:7``, ``ifindex:42``

If a vendor form not in the test matrix surfaces during E2-E6
or live deployment, **extend this helper AND add the test in the
same change** — the test file is the contract.
"""

from __future__ import annotations

import re
from typing import Optional


SENTINEL_RE = re.compile(r"^ifindex:\d+$")


# Cisco short → long expansion. Conservative — only the prefixes
# the v1.0 lab matrix has surfaced. Long-form values match Cisco's
# actual ``ifName`` output (which Nautobot's
# nautobot-device-onboarding stores when the device exists).
#
# Order matters: longer prefixes first so ``Te`` doesn't shadow
# ``TenGigE``. A list of (short_prefix, long_prefix) tuples — the
# match is anchored at the start of the string and the prefix is
# replaced verbatim with the long form.
_CISCO_SHORT_TO_LONG: list[tuple[str, str]] = [
    ("TwentyFiveGigE", "TwentyFiveGigE"),  # already long; no-op
    ("HundredGigE", "HundredGigE"),
    ("FortyGigabitEthernet", "FortyGigabitEthernet"),
    ("TenGigabitEthernet", "TenGigabitEthernet"),
    ("GigabitEthernet", "GigabitEthernet"),
    ("Port-channel", "Port-channel"),
    ("Loopback", "Loopback"),
    ("Tunnel", "Tunnel"),
    ("Serial", "Serial"),
    ("Ethernet", "Ethernet"),
    ("Vlan", "Vlan"),
    ("Null", "Null"),
    # Short-form prefixes (one or two chars). Order: the more
    # specific/longer first.
    ("Twe", "TwentyFiveGigE"),
    ("Hu", "HundredGigE"),
    ("Fo", "FortyGigabitEthernet"),
    ("Te", "TenGigabitEthernet"),
    ("Gi", "GigabitEthernet"),
    ("Po", "Port-channel"),
    ("Lo", "Loopback"),
    ("Tu", "Tunnel"),
    ("Se", "Serial"),
    ("Vl", "Vlan"),
    ("Nu", "Null"),
]


def is_sentinel(name: Optional[str]) -> bool:
    """Return ``True`` if ``name`` matches ``^ifindex:\\d+$``.

    Sentinel rows are produced by SNMP collectors when bridge-port
    or ifIndex resolution fails. They preserve data per Rule 7 but
    can't be linked to a Nautobot Interface — :func:`get_interface`
    always returns ``None`` for them.
    """
    if not name:
        return False
    return bool(SENTINEL_RE.match(name))


def _strip_logical_unit(name: str) -> str:
    """Strip a Junos logical-unit suffix when conventional.

    ``ge-0/0/12.0`` → ``ge-0/0/12``
    ``ae0.0``       → ``ae0``
    ``xe-0/2/0.100`` → ``xe-0/2/0``
    ``irb.0``       → ``irb.0``  (irb is logical at any unit; .0 is canonical)
    ``vlan.100``    → ``vlan.100`` (vlan is logical at any unit)
    ``lo0.0``       → ``lo0`` (loopback physical=lo0; .0 is the unit)
    ``Ethernet1.100`` → ``Ethernet1`` (Arista subinterface)

    Only strip when the trailing ``.N`` is plausibly a unit number
    on a physical interface. ``irb`` and ``vlan`` are logical
    interfaces whose ``.N`` is meaningful; preserve them.
    """
    # Logical interfaces that keep their unit
    if re.match(r"^(irb|vlan)\.\d+$", name, re.IGNORECASE):
        return name
    # Strip trailing .digits from anything else
    return re.sub(r"\.\d+$", "", name)


def _expand_cisco_short(name: str) -> Optional[str]:
    """Expand Cisco short-form to long-form. Return ``None`` if no
    expansion applies.

    ``Gi1``       → ``GigabitEthernet1``
    ``Te0/1``     → ``TenGigabitEthernet0/1``
    ``Vl100``     → ``Vlan100``
    ``Lo0``       → ``Loopback0``
    Junos ``ge-0/0/0`` → no match (the short→long table doesn't
    include hyphenated Junos forms).
    """
    if not name or not name[0].isupper():
        # Cisco short-form is capitalized; Junos / Fortinet / Arista
        # are not in the same shape.
        return None
    for short, long_ in _CISCO_SHORT_TO_LONG:
        if name.startswith(short):
            # Idempotent on already-long forms (the long==long
            # entries in the table cover this).
            if name.startswith(long_):
                return name
            return long_ + name[len(short):]
    return None


def normalize(name: Optional[str]) -> tuple[str, str]:
    """Return ``(canonical, original)`` for a vendor-native name.

    Canonical form:
      - Sentinel: pass through unchanged.
      - Junos logical-unit suffix on a physical: stripped.
      - Cisco short form: expanded to long form.
      - Everything else: returned verbatim.

    Original is always the input — preserved for display so
    operators see what was actually stored.
    """
    if not name:
        return ("", "")
    original = name
    if is_sentinel(name):
        return (name, original)

    canonical = _strip_logical_unit(name)
    expanded = _expand_cisco_short(canonical)
    if expanded:
        canonical = expanded
    return (canonical, original)


def get_interface(device_name: Optional[str], interface_name: Optional[str]):
    """Lookup ``dcim.Interface`` by vendor-native name.

    Tries (in order): literal match, normalized form, logical-unit-
    stripped form, Cisco short→long expansion. Returns the
    ``Interface`` instance or ``None``.

    Returns ``None`` for sentinels (``ifindex:N``), missing
    devices, and missing interfaces. Never raises on lookup misses.

    Imported lazily so the helper module is importable in test
    contexts that don't have the Django app registry ready.
    """
    if not device_name or not interface_name:
        return None
    if is_sentinel(interface_name):
        return None

    try:
        from nautobot.dcim.models import Interface
    except Exception:  # noqa: BLE001
        # Django not configured (e.g., bare module test). The
        # caller is responsible for handling this in test
        # contexts; production rendering paths are inside the
        # Nautobot process where the app registry is always ready.
        return None

    # Build the candidate list. Order: literal first (cheapest
    # win), then transformations.
    canonical, _ = normalize(interface_name)
    candidates = [interface_name]
    if canonical != interface_name:
        candidates.append(canonical)

    stripped = _strip_logical_unit(interface_name)
    if stripped != interface_name and stripped not in candidates:
        candidates.append(stripped)

    expanded = _expand_cisco_short(interface_name)
    if expanded and expanded not in candidates:
        candidates.append(expanded)

    for candidate in candidates:
        try:
            return Interface.objects.get(
                device__name=device_name, name=candidate,
            )
        except Interface.DoesNotExist:
            continue
        except Interface.MultipleObjectsReturned:
            # Two interfaces on the same device with the same
            # canonical name shouldn't happen in well-formed
            # Nautobot data, but if it does, prefer the first
            # match deterministically.
            return Interface.objects.filter(
                device__name=device_name, name=candidate,
            ).first()

    return None
