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

## Performance Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `MNM_SWEEP_CONCURRENCY` | `50` | Max concurrent TCP probes during network sweep |
| `MNM_COLLECTION_CONCURRENCY` | `20` | Max concurrent SNMP walks during endpoint collection |
| `MNM_API_CONCURRENCY` | `10` | Max concurrent Nautobot API calls |

These control how aggressively MNM probes the network. Higher values complete faster but generate more concurrent connections. On constrained networks or slow devices, reduce these values.
