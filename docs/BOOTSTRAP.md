# MNM Bootstrap Guide

How `bootstrap/bootstrap.sh` populates Nautobot with the reference data
MNM needs, and how to extend it when a new vendor / model surfaces.

## Audience

- **Operators**: how to re-run bootstrap, how to fix
  `MissingReferenceError` from onboarding, how to add a new vendor.
- **Engineers**: how the script is structured, where to add entries.

## What bootstrap does

`bootstrap/bootstrap.sh` runs once after `docker compose up -d` brings
Nautobot up. It is **idempotent** — every step checks if the target
already exists and skips it cleanly. Safe to re-run.

The script creates, in order:

1. **`mnm_controller` PostgreSQL database** on the existing
   `mnm-postgres` instance (controller's persistent storage).
2. **Nautobot superuser** from `MNM_ADMIN_*` env vars.
3. **API token** for that superuser.
4. **Location Types** (Region → Site) and **default Region + Site**.
5. **Device Roles**: Router, Switch, Firewall, Access Point, Endpoint,
   Unknown.
6. **Manufacturers**: Juniper, Cisco, Cisco Meraki, Fortinet, Arista,
   Palo Alto Networks, Aruba, Extreme Networks, MikroTik, Ubiquiti,
   Huawei.
7. **Platforms** — one per `(network_driver, napalm_driver)` combo
   (see *Adding a Platform* below).
8. **Devicetype Library Git repo** registration for the welcome-wizard
   plugin, plus a sync trigger.
9. **Bulk DeviceType import** from the synced devicetype-library
   (~5,200 device types from the netbox-community library).
10. **Lab-only virtual DeviceTypes** that the community library does not
    ship — currently `vEOS-lab` (Arista) and `C8000V` (Cisco).
    See *Adding a DeviceType* below.
11. **NAPALM credential Secrets + Secrets Group** (only if
    `NAUTOBOT_NAPALM_USERNAME` / `_PASSWORD` are set).
12. **Discovery custom fields** on the IP Address model (sweep +
    endpoint enrichment).

## Re-running bootstrap

```bash
bash bootstrap/bootstrap.sh
```

The script fast-skips if Nautobot already has > 5,000 device types
**and** the `endpoint_data_source` custom field exists. Force a full
re-run with:

```bash
MNM_BOOTSTRAP_SKIP_CHECK=1 bash bootstrap/bootstrap.sh
```

Use the force flag after extending the script (e.g. adding a Platform
or DeviceType row) so the new entries land on an already-bootstrapped
install.

## When onboarding fails with `MissingReferenceError`

Direct-REST onboarding (see [docs/ONBOARDING.md](ONBOARDING.md))
validates every Nautobot reference (Role, DeviceType, Platform, Active
Status) **before** Step A. If any are missing, the orchestrator returns
a structured error like:

```
MissingReferenceError: Cannot onboard: required Nautobot reference
data missing.
Vendor: cisco; Chassis model: C8000V

Missing entries:
  - DeviceType: C8000V
    Fix: POST /api/dcim/device-types/ {"model": "C8000V",
    "manufacturer": "<Cisco UUID>", "u_height": 0}
    (or add to LAB_DEVICETYPES in bootstrap/bootstrap.sh)
  - Platform: cisco_iosxe
    Fix: POST /api/dcim/platforms/ {"name": "cisco_iosxe",
    "network_driver": "cisco_iosxe", "napalm_driver": "<ios|junos|...>"}
    (or add to PLATFORMS in bootstrap/bootstrap.sh)

After adding the missing entries, retry onboarding. To make the fix
permanent, add the entry to bootstrap/bootstrap.sh (see
docs/BOOTSTRAP.md).
```

Each missing entry includes a one-line REST command you can paste into
`curl` (or the Nautobot API browser) to fix immediately, plus the
bootstrap section to extend for a permanent fix.

**Why no auto-create?** MNM Rule 5 (Pre-load all reference data). Auto-
creating missing references on-the-fly would hide real data-quality
issues — you'd never know the bootstrap was incomplete, and every
fresh install would silently fix itself differently. Explicit failure
+ one-line fix is better than implicit silent recovery.

## Adding a Platform (new vendor / new network_driver)

Every Platform pre-loaded by bootstrap appears in section 4b of
`bootstrap/bootstrap.sh` as a `network_driver|napalm_driver|display|
manufacturer` row in the `PLATFORMS` array.

To add a new one:

1. Confirm the NAPALM driver is installed in
   `nautobot/Dockerfile` (every PLATFORMS row's `napalm_driver` must
   resolve to a driver class that loads, otherwise Polling raises at
   first use).
2. Append a row to the `PLATFORMS` array. Format:

   ```
   "<network_driver>|<napalm_driver>|<display name>|<manufacturer>"
   ```

3. The `network_driver` value must match what
   `controller/app/onboarding/classifier.py` returns as
   `ClassifierResult.platform` (e.g. `cisco_iosxe`, `juniper_junos`).
4. The `manufacturer` must already exist in the `MFG_NAMES` array
   earlier in the script.
5. Re-run with `MNM_BOOTSTRAP_SKIP_CHECK=1`.

Example: adding Aruba AOS-CX (already in the array as a reference):

```
"aruba_aoscx|ios|Aruba AOS-CX|Aruba"
```

## Adding a DeviceType (new chassis model)

There are two paths depending on whether the model is in the
[netbox-community/devicetype-library](https://github.com/netbox-community/devicetype-library):

### Path 1 — In the community library

The bulk import (`bootstrap/import_devicetypes.py`) picks it up
automatically on the next bootstrap run. If your model isn't appearing,
re-trigger the Git repo sync:

```bash
# Inside Nautobot UI: Extensibility > Git Repositories >
# "Devicetype Library" > Sync
```

then re-run bootstrap with `MNM_BOOTSTRAP_SKIP_CHECK=1`.

### Path 2 — Not in the community library (virtual platforms, unusual hardware)

Append to the `LAB_DEVICETYPES` array in section 6b of
`bootstrap/bootstrap.sh`. Format:

```
"<model>|<manufacturer>"
```

The script creates the DeviceType with `u_height=0` (treats it as
virtual / nominal-spec). Phase 2 of onboarding walks `ifTable` via
SNMP to populate the actual interface set, so the DeviceType record
itself only needs to satisfy the foreign-key constraint on Device
creation.

Example (already shipped in v1.0):

```
"vEOS-lab|Arista"
"C8000V|Cisco"
```

Re-run with `MNM_BOOTSTRAP_SKIP_CHECK=1`.

If you need richer DeviceType records (specific port templates, power
draw, console ports), use the Nautobot UI to create them and let the
operator workflow document the addition manually. The bootstrap script
focuses on the minimum viable record so onboarding doesn't 400.

## Adding a Manufacturer or Role

Both are simple `for ... do api_create ...` loops earlier in the
script. Add to the relevant array, re-run.

## Why this lives in a shell script (not a Python module)

Bootstrap runs once on first install, then occasionally after schema
extensions. It needs to:

- Wait for Nautobot's container to become healthy.
- Talk to Nautobot's API from outside the container (different network
  namespace from the controller).
- Be runnable on a fresh box that has Docker but not yet a Python
  virtualenv set up.

A shell script with `docker exec ... curl ...` calls fits these
constraints cleanly. The DeviceType bulk-import is the only Python
piece (`bootstrap/import_devicetypes.py`) and runs inside the Nautobot
container via `nautobot-server nbshell` to use the welcome-wizard
plugin's models directly.
