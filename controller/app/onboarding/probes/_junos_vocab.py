"""Junos chassis_model normalization vocabulary (Block F1).

Translates the descriptive long-form strings returned by
``JUNIPER-MIB::jnxBoxDescr`` (or the sysDescr-style fallback used
on older firmware) to the canonical short form indexed by the
netbox-community DeviceType library.

Examples:

    "Juniper EX2300-24P Switch"
        -> "EX2300-24P"

    "Juniper Networks, Inc. ex2300-c-12p Internet Router, kernel
     JUNOS 22.4R3-S5.6 #0: ..."
        -> "EX2300-C-12P"

    "Juniper SRX320 Internet Router"
        -> "SRX320"

When no vocabulary entry matches, ``normalize_chassis_model``
returns the input unchanged. The orchestrator's existing
``MissingReferenceError`` path then surfaces the gap with
operator-actionable text — per the D3 detection-and-clear-
reporting discipline. **Never auto-create the DeviceType.**

## Extension pattern

When a new Junos hardware model appears in a lab or production
deployment and onboarding fails because the long-form description
isn't recognized:

  1. Capture the actual ``jnxBoxDescr`` string the device returns.
     Easiest: run the probe (``onboard_probe.py``) and copy the
     value out of the orchestrator log line that reports the
     unrecognized chassis_model fall-through, OR walk the OID
     directly: ``snmpget -v2c -c <community> <ip> 1.3.6.1.4.1.2636.3.1.2.0``.
  2. Add one tuple to ``JUNOS_CHASSIS_VOCAB`` below. Most-specific
     patterns first; first-match wins.
  3. Add a test case in ``tests/unit/test_junos_probe.py``
     (the ``ChassisModelNormalizeTests`` block) covering the new
     long form -> canonical short form transform.
  4. Run ``pytest tests/unit/test_junos_probe.py`` to confirm
     the vocabulary still passes for every prior case.

Codifying as data (this module) rather than inline regex in
``junos.py`` is intentional per design §10.4d R1 — operators
extending the vocabulary update one file + add one test case in
the same change rather than hunting regex patterns through a
probe module.
"""

from __future__ import annotations

import re
from typing import Callable, List, Tuple, Union


# A vocabulary entry is ``(compiled_regex, replacement)``. Replacement
# is either a backreference template (``r"EX2300-\1P"``) or a callable
# that takes the regex match and returns the canonical short form —
# used for transforms more expressive than back-substitution (e.g.,
# uppercase the lowercase long-form model name).
_VocabEntry = Tuple[re.Pattern, Union[str, Callable[[re.Match], str]]]


def _compile(entries):
    out: List[_VocabEntry] = []
    for pattern, replacement in entries:
        out.append((re.compile(pattern), replacement))
    return out


# ---------------------------------------------------------------------------
# Vocabulary — most specific first; first match wins.
# ---------------------------------------------------------------------------
#
# Two pattern shapes covered by this vocabulary:
#
#   1. JUNIPER-MIB::jnxBoxDescr "marketing" form:
#        "Juniper EX2300-24P Switch"
#        "Juniper SRX320 Internet Router"
#
#   2. sysDescr-style "Juniper Networks, Inc. <model> ..." long form,
#      typically lowercase model name with a kernel version trailer:
#        "Juniper Networks, Inc. ex2300-c-12p Internet Router,
#         kernel JUNOS 22.4R3-S5.6 #0: ..."
#
# v1.0 lab matrix coverage: EX2300, EX3300, EX4300, EX4600, SRX-series.
# Forward compatibility: MX-series patterns included so the first MX in
# a future deployment doesn't trip the fall-through.
# ---------------------------------------------------------------------------
JUNOS_CHASSIS_VOCAB: List[_VocabEntry] = _compile([
    # EX-series switches (jnxBoxDescr marketing form).
    (r"^Juniper\s+(EX\d+-\d+[A-Z]+)\s+(Ethernet\s+)?Switch$",
     r"\1"),
    (r"^Juniper\s+(EX\d+-\d+[A-Z]+)\s+Internet\s+Router$",
     r"\1"),

    # EX-series with extra modifier (e.g., EX4600-40F Ethernet Switch).
    # Captured by the same pattern above; left here for documentation.

    # SRX-series firewalls / routers (jnxBoxDescr marketing form).
    (r"^Juniper\s+(SRX\d+[A-Z]?\d*)\s+Internet\s+Router$",
     r"\1"),
    (r"^Juniper\s+(SRX\d+[A-Z]?\d*)\s+Services\s+Gateway$",
     r"\1"),

    # MX-series routers (jnxBoxDescr marketing form).
    (r"^Juniper\s+(MX\d+)\s+(Internet\s+)?Router$",
     r"\1"),

    # sysDescr-style "Juniper Networks, Inc. <model> ..." long form.
    # Lowercase model in the wire payload; uppercase the canonical form.
    (r"^Juniper Networks,\s*Inc\.\s+(ex\d+[a-z\d-]*)\b.*$",
     lambda m: m.group(1).upper()),
    (r"^Juniper Networks,\s*Inc\.\s+(srx\d+[a-z\d]*)\b.*$",
     lambda m: m.group(1).upper()),
    (r"^Juniper Networks,\s*Inc\.\s+(mx\d+[a-z\d-]*)\b.*$",
     lambda m: m.group(1).upper()),
])


def normalize_chassis_model(raw):
    """Normalize a Junos chassis_model string to library short form.

    Args:
        raw: The string returned by JUNIPER-MIB::jnxBoxDescr or a
            sysDescr-style fallback. May be ``str``, ``None``, or
            ``bytes`` — bytes are decoded UTF-8 with errors=replace.

    Returns:
        The canonical short form (e.g., ``"EX2300-24P"``) when the
        vocabulary matches. The input unchanged (after whitespace
        strip and bytes decode) when no entry matches. ``None`` if
        the input is ``None``.

    Examples:
        >>> normalize_chassis_model("Juniper EX2300-24P Switch")
        'EX2300-24P'
        >>> normalize_chassis_model(
        ...     "Juniper Networks, Inc. ex2300-c-12p Internet Router, "
        ...     "kernel JUNOS 22.4R3-S5.6 #0: ..."
        ... )
        'EX2300-C-12P'
        >>> normalize_chassis_model("Some Future Junos Model We Haven't Seen")
        "Some Future Junos Model We Haven't Seen"
        >>> normalize_chassis_model(None) is None
        True
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return raw

    text = raw.strip()
    if not text:
        return text

    for pattern, replacement in JUNOS_CHASSIS_VOCAB:
        match = pattern.match(text)
        if match is None:
            continue
        if callable(replacement):
            return replacement(match)
        return match.expand(replacement)

    return text
