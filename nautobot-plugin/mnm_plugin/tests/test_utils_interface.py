"""Tests for ``mnm_plugin.utils.interface``.

This is the **lab-fidelity contract** for the cross-vendor
naming helper (E0 §2d). If a test passes, the helper handles
that vendor form. If a vendor form not in this file surfaces
during E2-E6 or live deployment, extend the helper AND add the
test in the same change.

Test posture: pure-Python tests (no Django ORM). The
``get_interface`` lookup against the Nautobot DB is exercised
in ``test_views.py``.
"""

from __future__ import annotations

import pytest

from mnm_plugin.utils.interface import (
    expand_for_lookup,
    is_sentinel,
    normalize,
)


# ---------------------------------------------------------------------------
# Sentinel detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ifindex:7", True),
        ("ifindex:0", True),
        ("ifindex:42", True),
        ("ifindex:999999", True),
        # Not sentinels:
        ("Ethernet1", False),
        ("ge-0/0/12", False),
        ("ifindex:", False),
        ("ifindex:abc", False),
        ("ifindex", False),
        ("", False),
        (None, False),
    ],
)
def test_is_sentinel(name, expected):
    assert is_sentinel(name) is expected


# ---------------------------------------------------------------------------
# Junos forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_canonical",
    [
        # Junos slot/port physical interfaces (no .unit suffix
        # to strip — already canonical).
        ("ge-0/0/12", "ge-0/0/12"),
        ("xe-0/2/0", "xe-0/2/0"),
        ("ae0", "ae0"),
        ("lo0", "lo0"),
        # Junos logical-unit form: stripped to physical
        ("ge-0/0/12.0", "ge-0/0/12"),
        ("xe-0/2/0.100", "xe-0/2/0"),
        ("ae0.0", "ae0"),
        ("lo0.0", "lo0"),
        # Junos logical interfaces — preserve unit
        ("irb.0", "irb.0"),
        ("irb.140", "irb.140"),
        ("vlan.100", "vlan.100"),
    ],
)
def test_normalize_junos(name, expected_canonical):
    canonical, original = normalize(name)
    assert canonical == expected_canonical
    assert original == name


# ---------------------------------------------------------------------------
# Arista forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_canonical",
    [
        # Arista numeric — already long form per Cisco-style
        # mapping; passes through, but the .unit form (rare on
        # Arista) gets stripped.
        ("Ethernet1", "Ethernet1"),
        ("Ethernet1/1", "Ethernet1/1"),
        ("Management1", "Management1"),
        ("Ethernet1.100", "Ethernet1"),
    ],
)
def test_normalize_arista(name, expected_canonical):
    canonical, _ = normalize(name)
    assert canonical == expected_canonical


# ---------------------------------------------------------------------------
# Fortinet aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "wan",
        "lan1",
        "fortilink",
        "port1",
        "internal",
        "wan1",
        "wan2",
    ],
)
def test_normalize_fortinet(name):
    """Fortinet aliases pass through — no transformation
    applies (they don't match Cisco short prefixes after the
    capitalization filter)."""
    canonical, original = normalize(name)
    assert canonical == name
    assert original == name


# ---------------------------------------------------------------------------
# Cisco short → long expansion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "short,long_",
    [
        ("Gi1", "GigabitEthernet1"),
        ("Gi0/0", "GigabitEthernet0/0"),
        ("Gi0/0/1", "GigabitEthernet0/0/1"),
        ("Te0/1", "TenGigabitEthernet0/1"),
        ("Fo0/1", "FortyGigabitEthernet0/1"),
        ("Hu0/1", "HundredGigE0/1"),
        ("Lo0", "Loopback0"),
        ("Vl100", "Vlan100"),
        ("Nu0", "Null0"),
        ("Tu0", "Tunnel0"),
        ("Po10", "Port-channel10"),
    ],
)
def test_normalize_cisco_short_to_long(short, long_):
    canonical, original = normalize(short)
    assert canonical == long_
    assert original == short


@pytest.mark.parametrize(
    "name",
    [
        "GigabitEthernet1",
        "GigabitEthernet0/0",
        "TenGigabitEthernet0/1",
        "Loopback0",
        "Vlan100",
        "Null0",
    ],
)
def test_normalize_cisco_long_form_idempotent(name):
    canonical, _ = normalize(name)
    assert canonical == name


# ---------------------------------------------------------------------------
# Sentinel pass-through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentinel",
    ["ifindex:7", "ifindex:0", "ifindex:42"],
)
def test_normalize_sentinel_passthrough(sentinel):
    canonical, original = normalize(sentinel)
    assert canonical == sentinel
    assert original == sentinel


# ---------------------------------------------------------------------------
# Cisco short prefix does NOT misfire on non-Cisco names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # Junos slot/port names start with lowercase — never
        # match the Cisco capitalized-short prefix table.
        "ge-0/0/0",
        "xe-0/0/0",
        "ae0",
        # Fortinet aliases — lowercase.
        "lan1",
        "wan",
        # IRB/VLAN preserve unit — must not be misread as Cisco
        # short-form.
        "irb.0",
        "vlan.100",
    ],
)
def test_cisco_short_does_not_misfire(name):
    canonical, _ = normalize(name)
    # The canonical should NOT be a Cisco long-form expansion of
    # one of these (e.g., "irb.0" must not become
    # "irbXxx0" or anything weird).
    assert canonical == name or canonical == _strip_check(name)


def _strip_check(name: str) -> str:
    """Helper: what the strip-logical-unit step would produce."""
    import re

    if re.match(r"^(irb|vlan)\.\d+$", name, re.IGNORECASE):
        return name
    return re.sub(r"\.\d+$", "", name)


# ---------------------------------------------------------------------------
# Empty / None handling
# ---------------------------------------------------------------------------


def test_normalize_empty():
    assert normalize("") == ("", "")
    assert normalize(None) == ("", "")


# ---------------------------------------------------------------------------
# expand_for_lookup — E5 multi-candidate form for panel queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_name,expected",
    [
        # Junos slot/port ↔ logical-unit form (both directions).
        ("ge-0/0/0", ["ge-0/0/0", "ge-0/0/0.0"]),
        ("ge-0/0/12", ["ge-0/0/12", "ge-0/0/12.0"]),
        ("ge-0/0/0.0", ["ge-0/0/0.0", "ge-0/0/0"]),
        ("ge-0/0/12.100", ["ge-0/0/12.100", "ge-0/0/12"]),
        ("xe-0/2/0", ["xe-0/2/0", "xe-0/2/0.0"]),
        ("xe-0/2/0.100", ["xe-0/2/0.100", "xe-0/2/0"]),
        # Junos logical interfaces — never expand; the .N is the unit.
        ("irb.140", ["irb.140"]),
        ("irb.0", ["irb.0"]),
        ("vlan.100", ["vlan.100"]),
        ("vlan.0", ["vlan.0"]),
        # Junos aggregated ethernet
        ("ae0", ["ae0"]),
        ("ae0.0", ["ae0.0", "ae0"]),
        # Cisco short ↔ long form (both directions).
        ("Gi1", ["Gi1", "GigabitEthernet1"]),
        ("Gi0/0", ["Gi0/0", "GigabitEthernet0/0"]),
        ("GigabitEthernet1", ["GigabitEthernet1", "Gi1"]),
        ("GigabitEthernet0/0", ["GigabitEthernet0/0", "Gi0/0"]),
        ("Te0/1", ["Te0/1", "TenGigabitEthernet0/1"]),
        ("TenGigabitEthernet0/1", ["TenGigabitEthernet0/1", "Te0/1"]),
        ("Vl100", ["Vl100", "Vlan100"]),
        ("Vlan100", ["Vlan100", "Vl100"]),
        ("Lo0", ["Lo0", "Loopback0"]),
        ("Loopback0", ["Loopback0", "Lo0"]),
        ("Po10", ["Po10", "Port-channel10"]),
        ("Port-channel10", ["Port-channel10", "Po10"]),
        ("Nu0", ["Nu0", "Null0"]),
        ("Null0", ["Null0", "Nu0"]),
        # Arista numeric — no expansion needed.
        ("Ethernet1", ["Ethernet1"]),
        ("Ethernet1/1", ["Ethernet1/1"]),
        ("Management1", ["Management1"]),
        # Arista subinterface — strip to parent.
        ("Ethernet1.100", ["Ethernet1.100", "Ethernet1"]),
        # Cisco subinterface long → contracted form + parent.
        ("GigabitEthernet1.100", [
            "GigabitEthernet1.100", "GigabitEthernet1", "Gi1.100", "Gi1",
        ]),
        # Fortinet aliases — never expand.
        ("wan", ["wan"]),
        ("lan1", ["lan1"]),
        ("fortilink", ["fortilink"]),
        ("port1", ["port1"]),
        ("internal", ["internal"]),
        # Sentinel — passthrough; never expands.
        ("ifindex:7", ["ifindex:7"]),
        ("ifindex:42", ["ifindex:42"]),
        ("ifindex:999999", ["ifindex:999999"]),
    ],
)
def test_expand_for_lookup_matrix(input_name, expected):
    """Order of the first entry MUST be the input verbatim. Order
    beyond that is best-effort — the panel query uses ``__in=``
    which doesn't care about order. Test against sets for the
    tail."""
    result = expand_for_lookup(input_name)
    assert result[0] == input_name, f"first entry must be input verbatim: {result}"
    assert set(result) == set(expected), (
        f"expand_for_lookup({input_name!r}) returned {result!r}, "
        f"expected {expected!r}"
    )


def test_expand_for_lookup_empty_returns_empty_list():
    assert expand_for_lookup("") == []
    assert expand_for_lookup(None) == []


def test_expand_for_lookup_first_entry_always_input():
    """The verbatim input is always the first candidate so the
    most-likely match is tried first when the panel query backend
    cares about order."""
    for name in ["ge-0/0/0", "Gi1", "Ethernet1", "wan"]:
        assert expand_for_lookup(name)[0] == name
