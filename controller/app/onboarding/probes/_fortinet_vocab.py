"""FortiGate chassis_model normalization vocabulary (Block F2).

Translates the underscore-slug strings returned by FortiOS via
``ENTITY-MIB::entPhysicalModelName`` (e.g., ``"FGT_40F_3G4G"``) to
the hyphenated marketing form indexed by the netbox-community
DeviceType library (e.g., ``"FortiGate 40F-3G4G"``).

Examples:

    "FGT_40F_3G4G"
        -> "FortiGate 40F-3G4G"

    "FGT_60F"
        -> "FortiGate 60F"

    "FortiGate 80F-DSL"
        -> "FortiGate 80F-DSL"  (already canonical; passthrough)

When no vocabulary entry matches, ``normalize_chassis_model``
returns the input unchanged. The orchestrator's existing
``MissingReferenceError`` path then surfaces the gap with
operator-actionable text -- per the D3 detection-and-clear-
reporting discipline. **Never auto-create the DeviceType.**

## Extension pattern

When a new FortiGate hardware model appears in a lab or production
deployment and onboarding fails because the underscore-slug form
isn't recognized:

  1. Capture the actual ``entPhysicalModelName`` string the device
     returns. Easiest: run the probe (``onboard_probe.py``) and
     copy the value out of the orchestrator log line that reports
     the unrecognized chassis_model fall-through, OR walk the OID
     directly (chassis row -- typically suffix ``.1``):
     ``snmpwalk -v2c -c <community> <ip> 1.3.6.1.2.1.47.1.1.1.1.13``.
  2. Add one tuple to ``FORTINET_CHASSIS_VOCAB`` below. Most-
     specific patterns first; first-match wins.
  3. Add a test case in ``tests/unit/test_fortinet_probe.py``
     (the chassis-normalize block) covering the new underscore-
     slug -> canonical hyphenated form transform.
  4. Run ``pytest tests/unit/test_fortinet_probe.py`` to confirm
     the vocabulary still passes for every prior case.

Codifying as data (this module) rather than inline regex in
``fortinet.py`` is intentional per design §10.4d R1 -- operators
extending the vocabulary update one file + add one test case in
the same change rather than hunting regex patterns through a
probe module. Pattern inherited verbatim from F1's
``_junos_vocab.py``.
"""

from __future__ import annotations

import re
from typing import Callable, List, Tuple, Union


# A vocabulary entry is ``(compiled_regex, replacement)``. Replacement
# is either a backreference template (``r"FortiGate \1"``) or a callable
# that takes the regex match and returns the canonical short form --
# used for transforms more expressive than back-substitution (e.g.,
# the underscore-to-hyphen substitution between the model and submodel
# components of an FGT_X_Y slug).
_VocabEntry = Tuple[re.Pattern, Union[str, Callable[[re.Match], str]]]


def _compile(entries):
    out: List[_VocabEntry] = []
    for pattern, replacement in entries:
        out.append((re.compile(pattern), replacement))
    return out


# ---------------------------------------------------------------------------
# Vocabulary -- most specific first; first match wins.
# ---------------------------------------------------------------------------
#
# FortiOS underscore-slug convention (observed live on FG-40F):
#
#     FGT_<model>_<submodel>      ->  FortiGate <model>-<submodel>
#     FGT_<model>                 ->  FortiGate <model>
#
# v1.0 lab matrix coverage: FG-40F (the only live FortiGate today).
# Forward compatibility: 60F / 100F / 200F / 600F shapes plus the
# already-canonical passthrough so a future FortiOS version that
# returns the marketing form directly doesn't trip the fall-through.
#
# Submodel slug uses [A-Z0-9]+ to capture FortiGate's uppercase-and-
# digit suffixes (3G4G, POE, BDSL, DSL, etc.). If a real lab device
# surfaces a lowercase-letter or hyphen-bearing submodel, extend the
# character class + add a test case in the same change.
# ---------------------------------------------------------------------------
FORTINET_CHASSIS_VOCAB: List[_VocabEntry] = _compile([
    # Underscore-slug with submodel suffix:
    #   FGT_40F_3G4G -> FortiGate 40F-3G4G
    #   FGT_100F_POE -> FortiGate 100F-POE
    (r"^FGT_(\d+[A-Z]+)_([A-Z0-9]+)$",
     lambda m: f"FortiGate {m.group(1)}-{m.group(2)}"),

    # Underscore-slug without submodel suffix:
    #   FGT_60F -> FortiGate 60F
    (r"^FGT_(\d+[A-Z]+)$",
     lambda m: f"FortiGate {m.group(1)}"),

    # Already-canonical marketing form passes through unchanged.
    # Forward-compat for any future FortiOS firmware that returns
    # the library-canonical form directly via entPhysicalModelName.
    (r"^FortiGate \d+[A-Z]+(-[A-Z0-9]+)?$",
     lambda m: m.group(0)),
])


def normalize_chassis_model(raw):
    """Normalize a FortiGate chassis_model string to library hyphenated form.

    Args:
        raw: The string returned by ENTITY-MIB::entPhysicalModelName
            on the chassis row. May be ``str``, ``None``, or
            ``bytes`` -- bytes are decoded UTF-8 with errors=replace.

    Returns:
        The canonical hyphenated form (e.g., ``"FortiGate 40F-3G4G"``)
        when the vocabulary matches. The input unchanged (after
        whitespace strip and bytes decode) when no entry matches.
        ``None`` if the input is ``None``.

    Examples:
        >>> normalize_chassis_model("FGT_40F_3G4G")
        'FortiGate 40F-3G4G'
        >>> normalize_chassis_model("FGT_60F")
        'FortiGate 60F'
        >>> normalize_chassis_model("FortiGate 80F-DSL")
        'FortiGate 80F-DSL'
        >>> normalize_chassis_model("Some Future FortiGate Format")
        'Some Future FortiGate Format'
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

    for pattern, replacement in FORTINET_CHASSIS_VOCAB:
        match = pattern.match(text)
        if match is None:
            continue
        if callable(replacement):
            return replacement(match)
        return match.expand(replacement)

    return text
