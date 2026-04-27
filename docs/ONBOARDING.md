# Onboarding a Device

MNM's v1.0 onboarding path creates a Nautobot Device for each
infrastructure node (switch, router, firewall) in two automated phases:

- **Phase 1** — synchronous, seconds. Classify the device via SNMP,
  probe its facts (hostname, serial, chassis model), create the
  Nautobot Device record with a management interface and primary IP,
  seed the polling-loop schedule.
- **Phase 2** — asynchronous, up to ~30 seconds after Phase 1. Walk
  the device's `ifTable` and `ipAddressTable` and bulk-populate all
  remaining interfaces and IPs.

## Supported vendors (v1.0)

Juniper Junos, Arista EOS, Palo Alto PAN-OS, Fortinet FortiOS. Cisco
IOS / IOS-XE and everything else is deferred to v1.1.

## Two UI flows

### 1. Discover → Onboard (bulk, after a sweep)

1. Open **Discovery**.
2. Fill in **Location**, **Credentials (SecretsGroup)**, **SNMP
   Community**, and the **CIDR range(s)** to scan.
3. Click **Run Sweep**. Eligible network devices (Juniper / Arista /
   PAN-OS / Fortinet) are onboarded automatically as they're
   discovered.
4. Watch the **Onboarding Status** block inline on each sweep-result
   row. Successful devices appear on the **Nodes** page with a
   green **Active** badge.
5. If onboarding fails, click **Retry** on the row — it reuses the
   sweep's credentials to re-run the direct-REST orchestrator.

### 2. Nodes → Add Node (single device, manual)

1. Open **Nodes**.
2. Click **+ Add Node** at the top of the list.
3. Fill in:
   - **IP address** (IPv4).
   - **SNMP community** (read-only).
   - **Location** (from the Nautobot dropdown).
   - **SecretsGroup** (from the Nautobot dropdown — used by the
     polling loop for NAPALM calls after onboarding).
4. Click **Onboard**. Phase 1 completes in seconds; Phase 2 runs in
   the background and the Nodes list auto-refreshes once it's done.

The Add Node form is the right flow when you already know the IP of
the device you want to onboard and don't need a sweep first.

## Status badges

The Nodes list shows a per-device status derived from Nautobot:

| Badge | Meaning |
|---|---|
| 🟢 **Active** | Fully onboarded. Phase 1 + Phase 2 both succeeded. |
| 🟡 **Incomplete** | Phase 2 failed (or is still retrying). Polling loop retries every 5 min. Click **Retry Phase 2** to re-run immediately. |
| 🔴 **Failed** | Phase 1 failed partway through. Manual cleanup may be required. |
| ⚪ **—** | Other Nautobot statuses (Staged, Inventory, etc.), or legacy plugin-onboarded devices. |

An hourglass next to the badge means Phase 2 is actively running.

## When Retry Phase 2 helps

If a device shows **Incomplete**, click **Retry Phase 2** on its row
in the Nodes list. The polling loop picks up the re-enabled row within
~30 seconds. Common reasons Phase 2 retries succeed:

- **Stale Nautobot device cache** — the first Phase 2 dispatch
  immediately after Phase 1 sometimes sees a cached device list that
  doesn't yet include the new device. Retry clears the race.
- **Transient SNMP timeout** — the device was briefly unreachable
  during the first attempt.
- **Upstream IPAM collision** — a sweep-recorded IP still held a
  through-model link from a previous state. Retry after manual IPAM
  cleanup.

If Retry Phase 2 fails repeatedly, check:

1. The device is reachable via SNMP from the controller.
2. The SNMP community in SecretsGroup is correct.
3. The Nautobot **DeviceType** exists for this device's chassis model
   (vEOS and FortiGate may need manual DeviceType creation — see
   the CLAUDE.md "Nautobot device-type-library gaps" lesson).

## What about the old plugin path?

The `nautobot-device-onboarding` plugin is still installed in v1.0
(per operator Q1 decision) and reachable via the API endpoints
`POST /api/discover/onboard` and `POST /api/nautobot/sync-network-data`.
The UI does not call these; all UI-initiated onboarding routes
through the direct-REST orchestrator at `/api/onboarding/direct-rest`.
The plugin path is removed entirely in v1.0.x or v1.1.

## API (if you want to script onboarding)

```
POST /api/onboarding/direct-rest
Content-Type: application/json
{
  "ip": "172.21.140.99",
  "snmp_community": "public",
  "secrets_group_id": "<uuid>",
  "location_id": "<uuid>"
}
```

Returns `{success, device_id, device_name, phase1_steps_completed,
error, error_type}`. Poll `GET /api/onboarding/phase2-status/{device_name}`
for Phase 2 progress.

```
POST /api/onboarding/retry-phase2/{device_name}
```

Re-enables the `phase2_populate` polling row. Returns immediately;
the polling loop runs Phase 2 within ~30 seconds.
