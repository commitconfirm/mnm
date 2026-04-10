# Controller API Reference

Base URL: `http://<host>:9090`

All `/api/*` endpoints (except health and auth) require authentication via the `mnm_token` cookie.

## Authentication

### POST /api/auth/login
Authenticate with the admin password.

**Request:**
```json
{"password": "your-admin-password"}
```

**Response:** `200 OK` with `Set-Cookie: mnm_token=...`
```json
{"status": "ok"}
```

### GET /api/auth/check
Check if the current session is authenticated.

**Response:**
```json
{"authenticated": true}
```

### POST /api/auth/logout
Clear the session cookie.

## Health

### GET /api/health
No authentication required.

**Response:**
```json
{"status": "ok"}
```

## Status

### GET /api/status
Returns health status of all MNM containers on the mnm-network.

**Response:**
```json
{
  "containers": [
    {
      "name": "mnm-nautobot",
      "status": "running",
      "health": "healthy",
      "image": "mnm-nautobot:latest",
      "ports": ["8443->8080/tcp"]
    }
  ]
}
```

## Discovery

### POST /api/discover/sweep
Start a network sweep.

**Request:**
```json
{
  "cidr_ranges": ["192.0.2.0/24"],
  "location_id": "uuid",
  "secrets_group_id": "uuid",
  "snmp_community": "public"
}
```

**Response:** `200 OK`
```json
{"status": "started"}
```

### GET /api/discover/status
Poll sweep progress. Returns per-host enriched data.

**Response:**
```json
{
  "running": true,
  "hosts": {
    "192.0.2.1": {
      "ip": "192.0.2.1",
      "status": "known",
      "ports_open": [22, 161, 830],
      "mac_address": "aa:bb:cc:dd:ee:ff",
      "mac_vendor": "Juniper Networks",
      "dns_name": "firewall-01.example.com",
      "snmp": {"sysName": "firewall-01", "sysDescr": "..."},
      "classification": "network_device",
      "first_seen": "2026-04-05T14:30:00Z",
      "last_seen": "2026-04-06T02:00:00Z"
    }
  },
  "summary": null
}
```

### GET /api/discover/neighbors
Returns LLDP neighbors not matching known devices.

**Response:**
```json
{
  "neighbors": [
    {
      "neighbor_name": "ap-lobby-01",
      "connected_to": "ge-0/0/5 (core-switch-01)"
    }
  ]
}
```

### POST /api/discover/onboard
Onboard a single device (from LLDP advisory).

**Request:**
```json
{
  "ip": "192.0.2.50",
  "location_id": "uuid",
  "secrets_group_id": "uuid"
}
```

### GET /api/discover/schedule
Returns configured sweep schedules.

### POST /api/discover/schedule
Save a sweep schedule.

**Request:**
```json
{
  "cidr_ranges": ["192.0.2.0/24"],
  "location_id": "uuid",
  "secrets_group_id": "uuid",
  "interval_hours": 24
}
```

## Endpoints (Infrastructure Collection)

### GET /api/endpoints
Returns all collected endpoint records. Supports query parameters: `?vlan=100`, `?switch=core-switch-01`, `?mac_vendor=Apple`, `?source=infrastructure`

### GET /api/endpoints/summary
Returns summary stats: total endpoints, VLANs active, vendors seen, last collection time.

**Response:**
```json
{
  "total_endpoints": 147,
  "vlans_active": 12,
  "vendors_seen": 45,
  "switches": 2,
  "last_collection": "2026-04-06T14:00:00Z",
  "running": false
}
```

### POST /api/endpoints/collect
Manually trigger an endpoint collection run.

## Nodes

Nodes are onboarded infrastructure devices that MNM authenticates to and actively polls.

### GET /api/nodes
List all onboarded nodes with poll health status.

**Response:**
```json
{
  "nodes": [
    {
      "name": "core-switch-01",
      "id": "uuid",
      "platform": "junos",
      "primary_ip": "198.51.100.1/32",
      "role": "Network Device",
      "location": "Main Site",
      "health": "green",
      "health_label": "All polls healthy",
      "last_polled": "2026-04-09T22:05:00Z",
      "jobs": {
        "arp": {"last_success": "...", "interval_sec": 300, "enabled": true},
        "mac": {"last_success": "...", "interval_sec": 300, "enabled": true}
      },
      "interface_count": null,
      "nautobot_url": "..."
    }
  ]
}
```

### GET /api/nodes/{node_name}
Returns details for a single node including poll status and Nautobot device data.

### GET /api/nodes/macs
Returns MAC addresses belonging to onboarded nodes (used to filter endpoints page).

**Response:**
```json
{"macs": ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"]}
```

### GET /api/devices *(deprecated)*
Returns `301 Moved Permanently` redirect to `/api/nodes`. Use `/api/nodes` instead.

## Nautobot Proxy

### GET /api/nautobot/devices
Returns all devices from Nautobot (raw Nautobot API proxy, unfiltered).

### GET /api/nautobot/devices/{id}
Returns a single device from Nautobot.

### GET /api/nautobot/secrets-groups
Returns available credential sets.

### GET /api/nautobot/locations
Returns available locations.

## Configuration

### GET /api/config
Returns controller configuration.

### POST /api/config
Update controller configuration (merge).

## Example: curl

```bash
# Login
curl -c cookies.txt -X POST http://localhost:9090/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password":"test123"}'

# Check status
curl -b cookies.txt http://localhost:9090/api/status

# Start sweep
curl -b cookies.txt -X POST http://localhost:9090/api/discover/sweep \
  -H "Content-Type: application/json" \
  -d '{"cidr_ranges":["192.0.2.0/29"],"location_id":"uuid","secrets_group_id":"uuid","snmp_community":"public"}'

# Poll progress
curl -b cookies.txt http://localhost:9090/api/discover/status
```

## Jobs

### GET /api/jobs
Consolidated view of all background tasks: sweep scheduler, modular poller, Proxmox collector, database prune, and legacy endpoint collector.

**Response:**
```json
{
  "jobs": [
    {
      "id": "sweep",
      "name": "Sweep Scheduler",
      "status": "idle",
      "running": false,
      "schedule_interval": "1h",
      "last_run": "2026-04-09T22:10:00Z",
      "duration_seconds": 285.3,
      "summary": {"total": 254, "alive": 10, "known": 3},
      "enabled": true
    }
  ]
}
```

### POST /api/discover/sweep-scheduled
Re-run the first saved sweep schedule. Used by the Jobs page "Run Now" button.

**Response:** `200 OK`
```json
{"status": "started", "cidr_ranges": ["198.51.100.0/24"]}
```

## Modular Polling

Per-device, per-job-type collection tracking. Replaces the monolithic endpoint collector.

### GET /api/polling/status
All devices, all job types, grouped by device.

**Response:**
```json
{
  "devices": [
    {
      "device_name": "core-switch-01",
      "jobs": {
        "arp": {"last_success": "2026-04-09T22:05:00Z", "interval_sec": 300, "enabled": true},
        "mac": {"last_success": "2026-04-09T22:05:06Z", "interval_sec": 300, "enabled": true},
        "dhcp": {"last_success": null, "interval_sec": 600, "enabled": true},
        "lldp": {"last_success": "2026-04-09T21:30:00Z", "interval_sec": 3600, "enabled": true}
      }
    }
  ]
}
```

### GET /api/polling/status/{device_name}
Single device, all job types.

### POST /api/polling/trigger/{device_name}
Trigger immediate poll of all enabled job types for a device. Returns `202 Accepted`.

### POST /api/polling/trigger/{device_name}/{job_type}
Trigger a single job type (`arp`, `mac`, `dhcp`, `lldp`, `routes`, or `bgp`). Returns `202 Accepted`.

### PUT /api/polling/config/{device_name}/{job_type}
Update interval or enabled flag for a specific device/job type.

**Request:**
```json
{"interval_sec": 600, "enabled": false}
```

**Response:** Updated poll row.

## Routes

Routing table data collected from onboarded nodes via NAPALM. Stored in the controller database (Nautobot has no native routing model).

### GET /api/routes
Query collected routes with optional filters.

**Query params:** `node_name`, `vrf`, `protocol`, `prefix` (substring search)

**Response:**
```json
{
  "routes": [
    {
      "id": 1,
      "node_name": "core-switch-01",
      "prefix": "198.51.100.0/24",
      "next_hop": "198.51.100.1",
      "protocol": "ospf",
      "vrf": "default",
      "metric": 10,
      "preference": 110,
      "outgoing_interface": "ge-0/0/0",
      "active": true,
      "collected_at": "2026-04-10T12:00:00Z"
    }
  ],
  "count": 1
}
```

### GET /api/routes/{node_name}
All routes for a specific node. Query params: `vrf`, `protocol`.

### GET /api/routes/advisories
Routes with next-hops that don't match any known IP in endpoint data or Nautobot IPAM. These are discovery candidates.

**Response:**
```json
{
  "advisories": [
    {
      "node_name": "core-switch-01",
      "prefix": "203.0.113.0/24",
      "next_hop": "198.51.100.254",
      "protocol": "static",
      "vrf": "default"
    }
  ],
  "count": 1
}
```

## BGP

BGP neighbor state collected from onboarded nodes via NAPALM.

### GET /api/bgp
Query collected BGP neighbors with optional filters.

**Query params:** `node_name`, `state`, `vrf`

**Response:**
```json
{
  "neighbors": [
    {
      "id": 1,
      "node_name": "core-router-01",
      "neighbor_ip": "198.51.100.2",
      "remote_asn": 65001,
      "local_asn": 65000,
      "state": "Established",
      "prefixes_received": 150,
      "prefixes_sent": 75,
      "uptime_seconds": 86400,
      "vrf": "default",
      "address_family": "ipv4 unicast",
      "collected_at": "2026-04-10T12:00:00Z"
    }
  ],
  "count": 1
}
```

### GET /api/bgp/{node_name}
All BGP neighbors for a specific node. Query params: `vrf`.

## Onboarding Progress

### GET /api/discover/onboarding
All tracked onboarding job states.

### GET /api/discover/onboarding/{ip}
Detailed onboarding progress for a single host. Stages: `submitting`, `queued`, `running`, `succeeded`, `failed`, `timeout`.

**Response:**
```json
{
  "ip": "198.51.100.7",
  "stage": "succeeded",
  "message": "Device onboarded (core-switch-02)",
  "job_result_id": "uuid",
  "device_id": "uuid"
}
```

## Phase 2.7 — Endpoint correlation endpoints

These endpoints expose the MAC-keyed endpoint store backed by the
`mnm_controller` PostgreSQL database. They return empty results if the database
is unreachable; the controller will fall back to JSON for `/api/config` only.

### `GET /api/endpoints/{mac}`
Return the current identity record for one MAC.

### `GET /api/endpoints/{mac}/history`
Return all `endpoint_events` rows for a MAC, newest first.
Response: `{"mac": "...", "events": [{"event_type": "moved_port", "old_value": "ge-0/0/12", "new_value": "ge-0/0/24", "timestamp": "..."}, ...]}`

### `GET /api/endpoints/{mac}/timeline`
Return a chronological narrative for a MAC. Each entry has a human-readable
`text` describing the event, plus the underlying `event_type` and `timestamp`.
Response also includes the current `endpoint` record.

### `GET /api/endpoints/events`
Recent network activity feed.
Query params:
- `type` (optional) — `appeared`, `moved_port`, `moved_switch`, `ip_changed`, `hostname_changed`
- `since` (default `24h`) — duration string: `1h`, `24h`, `7d`, `30d`
- `limit` (default `200`)

### `GET /api/endpoints/conflicts`
Return IPs currently claimed by more than one MAC. Each entry includes the
list of conflicting endpoints with their switch/port context.

```bash
# Last day of port moves
curl -b cookies.txt 'http://localhost:9090/api/endpoints/events?type=moved_port&since=24h'

# Full timeline for one endpoint
curl -b cookies.txt http://localhost:9090/api/endpoints/AA:BB:CC:DD:EE:FF/timeline
```

