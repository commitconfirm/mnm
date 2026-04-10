# Configuration

All MNM configuration is via the `.env` file. Copy `.env.example` to `.env` and fill in values.

## Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MNM_ADMIN_USER` | `mnm-admin` | Nautobot superuser and controller login username |
| `MNM_ADMIN_PASSWORD` | *(required)* | Nautobot superuser and controller login password |
| `MNM_ADMIN_EMAIL` | `admin@example.com` | Nautobot superuser email |
| `MNM_TIMEZONE` | `America/Boise` | System timezone |
| `MNM_DOMAIN` | `localhost` | FQDN for Traefik |

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `NAUTOBOT_SECRET_KEY` | *(required)* | Django secret key |
| `POSTGRES_USER` | `nautobot` | PostgreSQL username |
| `POSTGRES_PASSWORD` | *(required)* | PostgreSQL password |
| `POSTGRES_DB` | `nautobot` | Database name |
| `REDIS_PASSWORD` | *(required)* | Redis password |

## Device Credentials

| Variable | Default | Description |
|----------|---------|-------------|
| `NAUTOBOT_NAPALM_USERNAME` | | SSH/NETCONF username for device access |
| `NAUTOBOT_NAPALM_PASSWORD` | | SSH/NETCONF password |
| `NAPALM_USERNAME` | | Same value — Nornir's `CredentialsEnvVars` reads the unprefixed form |
| `NAPALM_PASSWORD` | | Same value |

**Note:** Both `NAUTOBOT_NAPALM_*` and `NAPALM_*` variants must be set. The Nautobot web layer reads the prefixed form; the Nornir credential plugin (used by "Sync Network Data" jobs) reads the unprefixed form. See [nautobot-plugin-nornir CredentialsEnvVars source](https://github.com/nautobot/nautobot-plugin-nornir).

## Monitoring

| Variable | Default | Description |
|----------|---------|-------------|
| `SNMP_COMMUNITY` | `public` | SNMPv2c read-only community string for discovery and SNMP exporter |
| `GNMI_USERNAME` | | gNMI username for streaming telemetry |
| `GNMI_PASSWORD` | | gNMI password |
| `GRAFANA_ADMIN_PASSWORD` | `mnm-admin` | Grafana admin password |
| `PROMETHEUS_RETENTION_DAYS` | `365` | Prometheus metric retention in days |
| `ANOMALY_STALE_DAYS` | `7` | Endpoints not seen in this many days are flagged as `stale` by `/api/endpoints/anomalies`. Can be overridden at runtime by writing the `anomaly_stale_days` key to `kv_config` via `POST /api/config`. |
| `MNM_RETENTION_DAYS` | `365` | Daily prune deletes endpoint events, IP observations, and stale sentinel rows older than this. Set this to match (or exceed) `PROMETHEUS_RETENTION_DAYS` so cross-referencing time ranges between Prometheus and the endpoint store always works. |
| `MNM_PRUNE_INTERVAL_HOURS` | `24` | How often the background prune task runs. The first run happens 2 minutes after controller startup; subsequent runs are spaced by this interval. Operators can also trigger a prune on demand from the **Database Maintenance** card on the dashboard. |

## Database Maintenance

MNM's controller database (`mnm_controller`) accumulates rows over time as
endpoints move, IPs are observed, and events fire. A daily background task
(`_scheduled_prune_loop`) evicts old data based on `MNM_RETENTION_DAYS`:

- **`endpoint_events`** — movement, IP-change, hostname-change events older
  than the retention window
- **`ip_observations`** — per-sweep IP snapshots older than the retention
  window
- **`endpoint_watches`** — watchlist entries whose target MAC no longer
  appears in any endpoint row (orphaned)
- **`endpoints`** — sentinel rows (`current_switch = '(none)'`,
  `current_port = '(none)'`) older than the retention window. Sentinels
  are sweep-only endpoints that never got correlated with infrastructure
  ARP/MAC table data, and they accumulate forever without this cleanup.

Operators can preview or trigger a prune on demand from the **Database
Maintenance** card on the dashboard, or via the REST API:

```bash
# Preview what a prune would remove without committing
curl -b cookies.txt http://localhost:9090/api/admin/prune/preview

# Run an immediate prune
curl -b cookies.txt -X POST http://localhost:9090/api/admin/prune
```

Both endpoints require authentication and use the current value of
`MNM_RETENTION_DAYS`.

## Nautobot Retention

Nautobot has its own retention controls for the data it stores. These are
separate from MNM's controller-database pruning and need to be set in
`nautobot/nautobot_config.py` (or via the `NAUTOBOT_*` environment variables
the base image accepts). For busy MNM deployments with frequent onboarding /
sync runs, the defaults can consume significant database space inside the
shared `mnm-postgres` instance:

| Setting | Default | What it controls |
|---------|---------|------------------|
| `JOB_RESULT_RETENTION` | 90 days | How long Nautobot keeps `JobResult` records (every onboarding run, sync run, custom job execution). MNM submits these on every Sync All button press, every periodic sync, and every endpoint collection cycle that touches Nautobot. |
| `CHANGELOG_RETENTION` | 90 days | How long Nautobot keeps `ObjectChange` records — the audit trail of every IPAM/DCIM mutation. |

For typical MNM deployments the 90-day defaults are reasonable. If you have
a high job-execution rate (sub-hourly syncs against many devices) consider
lowering `JOB_RESULT_RETENTION` to 30 days to keep the database compact.
See the [Nautobot configuration documentation](https://docs.nautobot.com/projects/core/en/stable/user-guide/administration/configuration/optional-settings/)
for the full set of retention knobs.

> **Note:** Nautobot's retention is enforced by Celery beat tasks running
> in the `nautobot-scheduler` container — they're independent of MNM's
> prune loop. If the scheduler container is unhealthy, neither retention
> system runs.

## Connectors

Connectors are optional read-only integrations with external systems. See
[CONNECTORS.md](CONNECTORS.md) for the full list and setup guides.

### Proxmox VE

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXMOX_HOST` | (unset) | Base URL of the Proxmox API, e.g. `https://192.0.2.10:8006`. Connector is disabled when empty. |
| `PROXMOX_TOKEN_ID` | (unset) | Full token ID: `user@realm!tokenid` |
| `PROXMOX_TOKEN_SECRET` | (unset) | The UUID secret printed once when the token was created |
| `PROXMOX_VERIFY_SSL` | `false` | Verify the Proxmox TLS cert (Proxmox ships self-signed by default) |
| `PROXMOX_INTERVAL_SECONDS` | `300` | Collection interval in seconds |

## Storage Estimates

| Component | Estimate | Notes |
|-----------|----------|-------|
| Prometheus | ~1 GB per 100 devices per year | Interface counters at 60s scrape interval |
| Nautobot (PostgreSQL) | ~500 MB base + minimal per device | Devices, IPAM, discovery custom fields |
| Grafana | < 100 MB | Dashboard configs |
| Docker images | ~3 GB total | All 11 container images |

The default 365-day retention with 10 GB size cap accommodates most deployments. Adjust `PROMETHEUS_RETENTION_DAYS` and the `--storage.tsdb.retention.size` flag in `docker-compose.yml` based on your device count and polling frequency.

## Sweep Schedules

Configured via the Controller UI at `:9090/discover` and persisted in `/data/config.json` inside the controller container.

## Modular Polling

Per-device, per-job-type collection intervals. These are defaults for new devices — per-device overrides are stored in the `device_polls` table and changeable via `PUT /api/polling/config/{device}/{job_type}`.

| Variable | Default | Description |
|----------|---------|-------------|
| `MNM_POLL_ARP_INTERVAL` | `300` | ARP table collection interval in seconds |
| `MNM_POLL_MAC_INTERVAL` | `300` | MAC address table collection interval in seconds |
| `MNM_POLL_DHCP_INTERVAL` | `600` | DHCP bindings collection interval in seconds |
| `MNM_POLL_LLDP_INTERVAL` | `3600` | LLDP neighbor collection interval in seconds |
| `MNM_POLL_ROUTES_INTERVAL` | `3600` | Routing table collection interval in seconds |
| `MNM_POLL_BGP_INTERVAL` | `3600` | BGP neighbor collection interval in seconds |
| `MNM_POLL_CHECK_INTERVAL` | `30` | How often the poll loop checks for due jobs (seconds) |
| `MNM_AUTO_DISCOVER_HOPS` | `0` | Default hop limit for LLDP auto-discovery (0 = disabled) |
| `MNM_AUTO_DISCOVER_MAX` | `10` | Hard cap on nodes per auto-discovery run |
| `MNM_LEGACY_COLLECTOR` | `false` | Set to `true` to re-enable the old monolithic endpoint collector during transition |

## Performance Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `MNM_SWEEP_CONCURRENCY` | `50` | Max concurrent TCP probes during network sweep |
| `MNM_COLLECTION_CONCURRENCY` | `20` | Max concurrent device polls (shared by modular poller and legacy collector) |
| `MNM_API_CONCURRENCY` | `10` | Max concurrent Nautobot API calls |

These control how aggressively MNM probes the network. Higher values complete faster but generate more concurrent connections. On constrained networks or slow devices, reduce these values.
