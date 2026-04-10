# Network Discovery

MNM uses a **seed-and-sweep** discovery model. The operator defines CIDR ranges and credential sets; MNM probes the ranges, enriches what it finds, and records everything in Nautobot's IPAM.

```
Operator defines CIDR ranges + credentials
    │
    ▼
Controller Sweep Engine
    ├── TCP probe (ports 22,23,80,161,443,830,8080,8443,9100)
    ├── ARP table lookup → MAC address → OUI vendor
    ├── DNS reverse lookup → PTR record
    └── SNMP GET (if port 161 open) → sysName, sysDescr, etc.
    │
    ▼
Classify: network_device | server | web_service | printer | access_point | endpoint
    │
    ├──► Nautobot IPAM (ALL alive IPs recorded with custom fields)
    │
    └──► network_device → Nautobot Onboarding Job → Device record
              │
              ▼
         LLDP Neighbor Advisory → Operator reviews → Onboard / Ignore
```

## Discovery Modes

**Manual sweep** — triggered via the Controller UI at `:9090/discover`. Enter CIDR ranges, select a location and credential set, optionally provide an SNMP community string, and click Start Sweep. The Stop button cancels a running sweep within seconds.

**Scheduled re-sweep** — configure automatic re-sweeps at intervals (1h to 7d) via the Controller UI. The controller runs sweeps in the background and updates all records.

**Add-to-sweep from dashboard** — the Discovered Subnets advisory on the dashboard includes "Add to sweep" buttons. Clicking one navigates to the discovery page with the CIDR pre-filled in the textarea.

**Automatic subnet expansion** — when onboarded devices have interface IPs on subnets not yet in the sweep schedule, those subnets are automatically added to the first schedule. The sweep loop checks for new device-interface subnets every 5 minutes. This follows the principle that the operator implicitly approved those subnets by onboarding the devices that live on them.

## Enriched Collection

For every IP in the sweep range, MNM passively collects:

| Data | Method | Notes |
|------|--------|-------|
| Alive/dead | TCP connect (ports 22, 23, 80, 161, 443, 830, 8080, 8443, 9100) | 2s timeout, 10 concurrent probes |
| Open ports | Same TCP probes | Recorded as comma-separated list |
| MAC address | Local ARP table (`ip neigh show`) | Only works on same L2 segment |
| MAC vendor | OUI lookup | Built-in database of common network vendor prefixes |
| DNS name | Reverse DNS (PTR) | Uses system resolver |
| SNMP system info | SNMPv2c GET | sysName, sysDescr, sysObjectID, sysUpTime, sysContact, sysLocation. Requires community string. See [RFC 3418](https://www.rfc-editor.org/rfc/rfc3418) for OIDs. |

All collection is read-only. SNMP uses GET only (never SET). TCP probes connect and immediately close. MNM never tries credentials against unrecognized services.

## Device Classification

Each host is classified based on available data:

| Classification | Criteria |
|---------------|----------|
| `network_device` | SNMP responds with recognizable sysDescr, OR port 830 (NETCONF) open, OR ports 22+161 open with known network vendor MAC |
| `server` | Port 22 open, no SNMP, no NETCONF |
| `web_service` | Ports 80 or 443 open |
| `printer` | Port 9100 open |
| `access_point` | Known AP vendor MAC (Aruba, Ubiquiti, Ruckus, Meraki) |
| `virtual_machine` | Proxmox VM (from Proxmox connector) |
| `container` | Proxmox LXC container (from Proxmox connector) |
| `hypervisor` | Proxmox host node (from Proxmox connector) |
| `endpoint` | Responds to probe but no interesting ports |
| `unknown` | Responsive but doesn't match any classification |

## Nautobot IPAM Integration

Every alive IP from a sweep is recorded in Nautobot IPAM as an IP Address object with custom fields:

- `discovery_first_seen` / `discovery_last_seen` — temporal brackets for when this IP was active
- `discovery_classification` — device classification result
- `discovery_ports_open` — open ports
- `discovery_mac_address` / `discovery_mac_vendor` — L2 identity
- `discovery_dns_name` — reverse DNS
- `discovery_snmp_sysname` / `discovery_snmp_sysdescr` / `discovery_snmp_syslocation` — SNMP data
- `discovery_method` — how the IP was discovered (sweep, lldp, manual)

Records are never deleted. IPs that stop responding retain their last known data with `discovery_last_seen` frozen.

## LLDP Neighbor Advisory

After device onboarding, LLDP/CDP neighbor data is surfaced in the Controller dashboard. Neighbors not matching known devices appear as advisories with "Onboard" and "Ignore" buttons.

## Hop-Limited Auto-Discovery

**Default: disabled (hops=0).** Auto-discovery never happens unless the operator explicitly requests it (Rule 6 — Human-in-the-Loop).

When running a sweep or manually onboarding, the operator can set an "Auto-discover hops" value (1–5). After each successful onboarding, MNM reads the new node's LLDP neighbors and attempts to onboard each unseen neighbor, recursing up to the specified depth.

**Example:** Onboarding a core switch with `auto_discover_hops=2`:
- Hop 1: auto-onboard directly connected IDF switches discovered via LLDP
- Hop 2: auto-onboard devices connected to those IDF switches
- Devices already onboarded are skipped (no duplicate onboarding)

### Guard Rails

| Guard | Description |
|-------|-------------|
| Default 0 | Auto-discovery disabled unless explicitly enabled per-operation |
| Hop limit | Maximum depth 5, enforced server-side |
| Hard cap | `MNM_AUTO_DISCOVER_MAX` (default 10) nodes per auto-discovery run |
| Loop prevention | Visited set tracks all attempted nodes by name and IP |
| Exclusion list | Respects both IP and device_name exclusions |
| Sequential | One onboarding at a time — no parallel attempts |
| Credential inheritance | Same credentials and location used for all auto-discovered nodes |
| Visibility | All auto-discovered nodes appear in a dashboard advisory card |

### IP Resolution from LLDP

LLDP neighbor data does not always include a management IP address. MNM extracts an IP using this priority:

1. **Chassis ID** — some devices advertise their management IP as the LLDP chassis ID
2. **Hostname field** — may contain an IP address directly
3. **Nautobot lookup** — if the system name matches an existing device, use its primary IP
4. **DNS resolution** — resolve the LLDP system name via DNS

If no IP can be resolved, the neighbor is skipped with a log entry noting the chassis ID for manual investigation.

### API

- `POST /api/discover/auto` — manually trigger auto-discovery from a specific node
- `GET /api/discover/auto/history` — past auto-discovery run summaries
- `GET /api/discover/auto/recent?hours=24` — recently auto-discovered nodes (dashboard advisory)

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MNM_AUTO_DISCOVER_HOPS` | `0` | Default hop limit when not specified per-operation |
| `MNM_AUTO_DISCOVER_MAX` | `10` | Hard cap on nodes per auto-discovery run |

## Re-sweep and Data Lifecycle

- Re-sweeps update `discovery_last_seen` and detect changes (new hosts, changed ports/services)
- `discovery_first_seen` is never overwritten
- Prefixes are auto-created in Nautobot IPAM when CIDR ranges are swept
- To query historical data: filter IP Addresses in Nautobot by `cf_discovery_*` custom fields

## Infrastructure Endpoint Collection

In addition to active sweeps, MNM passively collects endpoint data from onboarded network devices' forwarding tables.

### Data Sources

| Source | Method | Data Returned |
|--------|--------|---------------|
| ARP table | NAPALM `get_arp_table()` via Nautobot proxy | IP, MAC, interface |
| MAC address table | NAPALM `get_mac_address_table()` via Nautobot proxy | MAC, VLAN, port, static/dynamic |
| DHCP server bindings | Junos NETCONF RPC `get-dhcp-server-binding-information` | IP, MAC, hostname, lease times |
| DHCP snooping | Junos NETCONF RPC `get-dhcp-snooping-binding-information` | IP, MAC, VLAN, interface |

See [NAPALM documentation](https://napalm.readthedocs.io/en/latest/support/) for supported getters by platform. Junos DHCP RPCs are documented in the [Junos XML API Explorer](https://apps.juniper.net/xmlapi/).

### Correlation

The collector merges data from multiple tables by joining on MAC address:
- ARP → IP-to-MAC mapping
- MAC table → MAC-to-port and MAC-to-VLAN mapping
- DHCP → MAC-to-hostname and lease timing

When the same MAC appears from multiple devices, MNM prefers the entry from the device where it's learned on an access port (not an uplink/trunk).

### Merge with Sweep Data

IPs found by both sweep and infrastructure collection are merged:
- `endpoint_data_source` is set to "both"
- Sweep custom fields (`discovery_*`) and infrastructure fields (`endpoint_*`) coexist on the same IP Address record
- `discovery_first_seen` is never overwritten

### Modular Polling (replaces monolithic collector)

The monolithic collection task has been replaced by independent per-device, per-job-type polling. Each job type (ARP, MAC, DHCP, LLDP) runs on its own schedule per device, tracked in the `device_polls` table.

**Default intervals (configurable via environment variables):**

| Job Type | Env Var | Default |
|----------|---------|---------|
| ARP | `MNM_POLL_ARP_INTERVAL` | 300s (5 min) |
| MAC | `MNM_POLL_MAC_INTERVAL` | 300s (5 min) |
| DHCP | `MNM_POLL_DHCP_INTERVAL` | 600s (10 min) |
| LLDP | `MNM_POLL_LLDP_INTERVAL` | 3600s (1 hour) |

Per-device overrides are stored in the `device_polls` table and can be changed via `PUT /api/polling/config/{device}/{job_type}`. The poll loop checks for due jobs every 30 seconds (`MNM_POLL_CHECK_INTERVAL`).

**Key behaviors:**
- Jobs for the same device run sequentially to avoid overlapping NAPALM sessions
- Cross-device jobs run in parallel, bounded by `MNM_COLLECTION_CONCURRENCY`
- 10% random jitter on `next_due` prevents thundering herd after restarts
- On failure, retry after half the interval
- New devices are automatically seeded when first discovered in Nautobot

**Dashboard:** The Polling Status card on the dashboard shows per-device rows with green/yellow/red/gray status indicators and a "Poll Now" button. The Jobs page (`/jobs`) shows the Modular Poller as a background task.

**API:** See [API.md](API.md#modular-polling) for the 5 polling endpoints.

**Migration:** The legacy monolithic collector is disabled by default. Set `MNM_LEGACY_COLLECTOR=true` to re-enable it during transition.

### Platform Support

| Platform | ARP | MAC Table | DHCP Server | DHCP Snooping |
|----------|-----|-----------|-------------|---------------|
| Junos | Yes | Yes | Yes (NETCONF RPC) | Yes (NETCONF RPC) |
| Cisco IOS/IOS-XE | Yes | Yes | Future | Future |
| FortiOS | Yes | Limited | Future | N/A |
| Arista EOS | Yes | Yes | Future | Future |

The collector gracefully skips unsupported operations — a getter failure on one device never stops the collection run.

## Endpoint Correlation Engine (Phase 2.7)

Every collection run and sweep run feeds the controller's `mnm_controller`
PostgreSQL database (see [ARCHITECTURE.md](ARCHITECTURE.md#controller-database-phase-27)).
Endpoints are keyed on **MAC address**, the most stable identifier for an
endpoint, so the same device retains its identity even when its IP changes.

For each upsert, the controller diffs the incoming record against the prior
state and writes one or more rows to `endpoint_events`:

| Change | Event type |
|--------|-----------|
| MAC seen for the first time | `appeared` |
| MAC moved to a different port on the same switch | `moved_port` |
| MAC moved to a different switch | `moved_switch` |
| MAC's IP changed | `ip_changed` |
| Hostname (DHCP > DNS > sysName) changed | `hostname_changed` |

A MAC that's missing from a given run is **not** deleted. Its `last_seen`
timestamp simply stops advancing — historical queries still resolve, and the
endpoint can be flagged as stale by inspection.

When the same MAC is observed by both the sweep (active probe) and the
infrastructure collector (ARP/MAC table query), its `data_source` field is
upgraded to `both`, recording that we have corroborating evidence of the
endpoint.

### Querying endpoint history

The controller exposes the endpoint store via several REST endpoints (see
[API.md](API.md#phase-27--endpoint-correlation-endpoints)):

- `GET /api/endpoints/{mac}/timeline` — human-readable narrative of every
  movement, IP change, and hostname change for one MAC, in chronological order.
- `GET /api/endpoints/events?type=moved_port&since=24h` — filtered activity
  feed across the whole network.
- `GET /api/endpoints/conflicts` — IPs currently claimed by more than one MAC.

In the UI, click any MAC in the Endpoints table to open its detail page, or
visit `/events` for the network activity feed.
