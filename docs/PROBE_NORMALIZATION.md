# Probe-Side Chassis Model Normalization

When MNM onboards a network device, it walks SNMP for `chassis_model`
and uses that value to bind the device to a DeviceType in Nautobot.
Nautobot's DeviceType records (imported at bootstrap from the
[netbox-community device-type-library](https://github.com/netbox-community/devicetype-library))
are indexed by canonical short names like `EX2300-24P` or
`FortiGate 40F-3G4G` — but the underlying vendor MIBs return
descriptive long forms that don't match.

This document explains the normalization pattern MNM uses to bridge
that gap, and how operators extend it when a new device model
surfaces a string the vocabulary doesn't recognize.

## Why normalization exists

Direct example from the v1.0 lab:

| Device | `JUNIPER-MIB::jnxBoxDescr` returns | Library indexes by |
| --- | --- | --- |
| Juniper EX2300-24P | `"Juniper EX2300-24P Switch"` | `EX2300-24P` |
| Juniper EX4300-48T | `"Juniper EX4300-48T Ethernet Switch"` | `EX4300-48T` |
| Juniper SRX320 | `"Juniper SRX320 Internet Router"` | `SRX320` |

Without normalization, `_resolve_devicetype_id` looks up the long
form, finds nothing, and the orchestrator surfaces a
`MissingReferenceError` even though the canonical DeviceType is
already in the library. Operators see this as "onboarding fails on
every Junos device after a clean install" — a per-vendor gap that
F1 (Junos) and F2 (FortiGate) close permanently.

## Where the vocabulary lives

Each vendor with a normalization need gets a dedicated module under
`controller/app/onboarding/probes/`:

- `_junos_vocab.py` — Junos chassis_model vocabulary (Block F1)
- `_fortinet_vocab.py` — FortiGate chassis_model vocabulary (Block F2; planned)

Each module exports:

- A constant `<VENDOR>_CHASSIS_VOCAB`: a list of
  `(compiled_regex, replacement)` tuples. Most-specific patterns
  first; first-match wins. Replacement is either a backreference
  template (`r"EX2300-\1P"`) or a callable that takes the regex
  match and returns the canonical short form (used for transforms
  like uppercase-the-lowercase-model-name).
- A function `normalize_chassis_model(raw)`: applies the vocabulary
  to a raw chassis_model string. Returns the input unchanged when
  no entry matches.

The vendor's probe module imports `normalize_chassis_model` and
calls it on the SNMP-derived `chassis_model` value before returning
it from `probe_device_facts`. Both the primary path
(vendor-specific MIB scalar) and any fallback path (ENTITY-MIB
walk, etc.) get normalized.

## How to extend when a new model fails to onboard

When you onboard a device for the first time and the orchestrator
reports `MissingReferenceError: DeviceType '<some long string>' not
found`, the fix is usually one new vocabulary entry plus one test
case.

### 1. Capture the actual chassis_model string

Two ways to get it:

- **From the orchestrator log.** `probes/junos.py` logs a debug
  line on every probe that returns chassis_model. When the
  vocabulary doesn't match, it logs the passthrough event:

  ```
  junos_probe_chassis_passthrough value='Juniper EX9999-NEW Switch' \
  (no vocabulary match — extend _junos_vocab.py if onboarding fails)
  ```

  Copy the value from the log line.

- **From `snmpget` against the device** (operator-side, RFC 5737
  example IP):

  ```
  snmpget -v2c -c <community> 192.0.2.1 1.3.6.1.4.1.2636.3.1.2.0
  ```

  For other vendors, look up the equivalent OID — for FortiGate,
  the FortiOS chassis_model probe uses the FortiOS REST `/api/v2/
  monitor/system/status` response field; for Cisco / Arista, use
  ENTITY-MIB `entPhysicalModelName` on the chassis row.

### 2. Add one tuple to the vocabulary

Open the relevant `_<vendor>_vocab.py` and add a tuple to the
`<VENDOR>_CHASSIS_VOCAB` list. Examples:

```python
# A new EX-series model with a different suffix shape
(r"^Juniper EX9999-(\d+)X Switch$", r"EX9999-\1X"),

# A vendor-specific transform that needs more than back-substitution
(r"^Juniper Networks,\s*Inc\.\s+(qfx\d+[a-z\d-]*)\b.*$",
 lambda m: m.group(1).upper()),
```

Most-specific patterns first; first-match wins. If your new pattern
is more specific than an existing one, place it earlier in the list.

### 3. Add a test case

Open `tests/unit/test_<vendor>_probe.py` and add one parametrized
case to the `ChassisModelNormalizeTests` block (or whichever block
holds the marketing-form / long-form lists):

```python
@pytest.mark.parametrize("raw, expected", [
    # ... existing cases ...
    ("Juniper EX9999-12X Switch", "EX9999-12X"),
])
def test_normalize_chassis_model_marketing_forms(raw, expected):
    assert normalize_chassis_model(raw) == expected
```

### 4. Run the test suite

```
docker exec mnm-controller python -m pytest \
    tests/unit/test_junos_probe.py -q
```

All cases should pass — both your new one and every prior case. If
a prior case starts failing, your new pattern is too greedy and is
matching a string an earlier pattern should have caught. Fix by
either making your pattern more specific or moving it later in
the list.

### 5. Confirm the DeviceType exists in Nautobot

Normalization translates the SNMP string to the library key. The
library still has to ship a record matching that key. If the
device is a virtual or otherwise non-mainstream variant, the
DeviceType record may be missing from the netbox-community
library. In that case, extend the bootstrap library per
`docs/BOOTSTRAP.md` instead of (or in addition to) the
normalization vocabulary. See D3 / Block F's lessons in
`mnm-dev-claude` for prior cases.

## What happens when normalization fails

`normalize_chassis_model` returns the input unchanged when no
vocabulary entry matches. The orchestrator's `_resolve_devicetype_id`
then fails to find a matching DeviceType, and the orchestrator
surfaces a `MissingReferenceError` with operator-actionable text
naming both the missing DeviceType and a one-line REST `POST` to
extend the bootstrap library if appropriate.

This is **deliberate** per Rule 5 (Pre-load all reference data).
Auto-creating a DeviceType from a probe-derived string would hide
real data-quality issues — operators would never know bootstrap was
incomplete, and every install would self-fix differently. The
normalization vocabulary is the supported extension surface;
auto-creation is explicitly rejected. See D3 lesson in
`mnm-dev-claude` `CLAUDE.md`.
