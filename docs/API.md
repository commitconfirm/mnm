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

## Nautobot Proxy

### GET /api/nautobot/devices
Returns all devices from Nautobot.

### GET /api/nautobot/devices/{id}
Returns a single device.

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

