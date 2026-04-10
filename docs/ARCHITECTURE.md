# Architecture

```
Internet/LAN
    │
    ├── :9090  → Controller (dashboard, discovery, management)
    ├── :8080  → Traefik ─┬─ /           → Nautobot (inventory + API)
    │                      ├─ /grafana    → Grafana (dashboards)
    │                      └─ /prometheus → Prometheus (metrics)
    ├── :8443  → Nautobot (direct fallback)
    └── :3000  → Grafana (direct fallback)

Internal (mnm-network):
    PostgreSQL 15       ← Nautobot database
    Redis 7             ← Nautobot cache + Celery broker
    Celery Worker       ← Runs onboarding/sync jobs
    Celery Scheduler    ← Periodic tasks
    SNMP Exporter       ← Prometheus scrapes → polls devices via SNMP
    gnmic               ← gNMI streaming telemetry → Prometheus
    Prometheus          ← Metric storage, 365-day retention
    Grafana             ← Visualizes Prometheus data
    Controller          ← Management UI, discovery engine
```

## Container Inventory

MNM runs as 11 Docker containers on a shared `mnm-network` bridge.

| Container | Image | Purpose | Port |
|-----------|-------|---------|------|
| mnm-controller | mnm-controller:latest | Management UI, discovery engine | :9090 |
| mnm-traefik | traefik:v2.11 | Reverse proxy, Docker label routing | :8080 |
| mnm-nautobot | mnm-nautobot:latest | Network inventory, REST API | :8443 |
| mnm-nautobot-worker | mnm-nautobot:latest | Celery task worker (onboarding jobs) | — |
| mnm-nautobot-scheduler | mnm-nautobot:latest | Celery beat scheduler | — |
| mnm-postgres | postgres:15 | Nautobot database | — |
| mnm-redis | redis:7-alpine | Cache + Celery message broker | — |
| mnm-prometheus | prom/prometheus:v3.4.0 | Metric storage (365-day retention) | via Traefik |
| mnm-grafana | grafana/grafana-oss:12.0.1 | Dashboard visualization | :3000 |
| mnm-snmp-exporter | prom/snmp-exporter:v0.29.0 | SNMP polling for Prometheus | — |
| mnm-gnmic | ghcr.io/openconfig/gnmic:latest | gNMI streaming telemetry | — |

## Port Assignments

| Port | Service | Access |
|------|---------|--------|
| 9090 | Controller | Direct — management UI and API |
| 8080 | Traefik | Proxied — routes to Nautobot (`/`), Grafana (`/grafana`), Prometheus (`/prometheus`) |
| 8443 | Nautobot | Direct fallback |
| 3000 | Grafana | Direct fallback |

## Data Retention

| Data | Location | Retention | Notes |
|------|----------|-----------|-------|
| SNMP metrics | Prometheus | 365 days (configurable via `PROMETHEUS_RETENTION_DAYS`, 10 GB cap) | |
| Discovery records | Nautobot IPAM | Indefinite | Never deleted, `first_seen`/`last_seen` tracked |
| Device inventory | Nautobot | Indefinite | Devices, interfaces, cables, LLDP |
| Grafana dashboards | Grafana volume | Indefinite | |

## Security Model

MNM collects sensitive network intelligence. If compromised, this data could be used to map and attack the monitored network. The following documents MNM's security posture, trust boundaries, and hardening recommendations.

### Access Control

- **Controller UI**: Password-gated via `MNM_ADMIN_PASSWORD`. All management operations (discovery, sweep configuration, container control) require authentication.
- **Nautobot**: Has its own authentication system using `MNM_ADMIN_USER` / `MNM_ADMIN_PASSWORD`. API access requires a token, obtained by the controller at startup via `docker exec` into the Nautobot container.
- **Grafana**: Anonymous read-only access is enabled by default to support the appliance model. In sensitive environments, disable anonymous access by setting `GF_AUTH_ANONYMOUS_ENABLED=false` in docker-compose.yml and using Grafana's built-in auth.
- **Internal services**: PostgreSQL, Redis, Prometheus, SNMP exporter, and gnmic are not exposed to the host. They communicate only within the `mnm-network` Docker bridge and are unreachable from outside the host.
- **Docker socket mount**: The controller mounts `/var/run/docker.sock` for container management (health checks, restart, bootstrap). This grants root-equivalent access to the host. The controller container is the trust boundary — anyone who can execute code inside it effectively has root on the host. Future mitigation: consider [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) to restrict the API surface.

### Data at Rest

- **PostgreSQL** stores all Nautobot data including device inventories, IP addresses, topology, and Secrets (SSH/NETCONF credentials). The `mnm-postgres-data` volume should reside on encrypted storage.
- **Prometheus** stores all collected metrics (SNMP, gNMI, discovery telemetry). The `mnm-prometheus-data` volume should reside on encrypted storage.
- **Controller config** (`config.json`) stores sweep configuration including IP ranges and scheduling. The `mnm-controller-data` volume should reside on encrypted storage.
- **Recommendation**: Deploy MNM on a host or VM with full-disk encryption enabled. This protects all volumes, logs, and temporary files at rest.
- **Snapshots**: Proxmox or hypervisor snapshots containing MNM data include credentials and network intelligence. Treat all snapshots and backups as sensitive.

### Data in Transit

- **Internal container traffic**: Communication between containers on the `mnm-network` Docker bridge is unencrypted. This is acceptable for same-host deployments where all containers share a single machine.
- **Traefik (HTTP)**: Traefik serves HTTP on port 8080 by default. To add TLS: mount certificate files into the Traefik container, add a TLS entrypoint in `config/traefik/traefik.yml`, and update router rules to use the HTTPS entrypoint. Self-signed certificates are sufficient for management access; use CA-signed certificates if exposing to a broader network.
- **SNMP v2c**: Community strings are sent in plaintext over the network. This is an inherent protocol limitation. Use SNMPv3 with authentication and encryption where devices support it. Restrict SNMP access to the management VLAN.
- **SSH/NETCONF**: Connections to network devices via NAPALM use SSH (encrypted by protocol). NETCONF runs over SSH. These are secure in transit.
- **Network access**: All device communication is strictly read-only (Inviolable Rule 1). SNMP GET/WALK only, TCP connect-then-close for probes, SSH/NETCONF via NAPALM read-only drivers. MNM never sends SET, configuration, or write operations.

### Credential Handling

- **NAPALM/SSH credentials** are stored in Nautobot Secrets, backed by PostgreSQL. They are not stored as plaintext files on disk (beyond the initial `.env` used for bootstrap).
- **SNMP community strings** appear in `.env` and in `config/prometheus/prometheus.yml` (or generated scrape configs). Restrict file permissions on both.
- **`.env` file**: Should be `chmod 600` and owned by the deploying user. Contains database passwords, admin credentials, SNMP communities, and API keys. Already listed in `.gitignore` — never commit this file.
- **Grafana admin password**: Set via `GRAFANA_ADMIN_PASSWORD` in `.env`. If unset, Grafana uses its default (`admin`), which should be changed immediately.

### Recommendations

- Deploy MNM on a dedicated VM with full-disk encryption (LUKS on Linux, or hypervisor-level encryption).
- Restrict network access to exposed ports (8080, 8443, 9090, 3000) to authorized management stations only.
- Use host firewall rules or network ACLs to limit who can reach the MNM UI.
- Place MNM on a dedicated management VLAN with access to device management interfaces.
- Rotate credentials (NAPALM, SNMP, admin passwords) periodically.
- Back up MNM data (PostgreSQL dumps, Prometheus snapshots) to encrypted storage.
- Treat all snapshots and backups containing MNM data as sensitive — they include credentials and full network topology.
- Review Grafana anonymous access settings before deploying in environments where dashboard data is sensitive.
- Consider upgrading from SNMP v2c to v3 where device support allows.

## Design Decisions

Key architectural choices and rationale are documented in the Design Decisions Log in the project's internal architecture document. Notable decisions:

- **Nautobot over NetBox**: SSoT plugin framework and device onboarding plugin
- **Traefik v2.11 over v3**: Docker API incompatibility in v3 (see Phase 2 Lessons Learned)
- **Seed-and-sweep over recursive crawl**: Prevents runaway discovery (Rule 6)
- **Vanilla JS over frameworks**: Minimal dependencies, appliance model
- **365-day retention**: Enables historical network intelligence queries

## Performance Architecture

**Endpoint collection** uses SNMP for speed:
- ARP table: SNMP walk of `ipNetToMediaTable` (~1-2 seconds per device)
- MAC table: SNMP walk of `dot1dTpFdbTable` / Q-BRIDGE-MIB (~1-2 seconds per device)
- DHCP bindings: NETCONF RPC (Junos only, ~5 seconds)
- NAPALM proxy: fallback only when SNMP fails (~15-30 seconds per device)

**Discovery sweep** optimizations:
- Pre-fetch all known IPs from Nautobot in one bulk call (eliminates N per-host API calls)
- Enrichment steps (SNMP, DNS, banners, ARP) run in parallel per host via `asyncio.gather`
- IPAM writes batched after all hosts complete
- Concurrency tunable via `MNM_SWEEP_CONCURRENCY` env var

**Bootstrap** skips entirely on subsequent runs when device types (>5000) and custom fields are already present.

## Controller Database (Phase 2.7)

The controller persists its state to a dedicated `mnm_controller` database on
the same `mnm-postgres` instance that hosts Nautobot. No additional containers
are introduced — the database is created idempotently by `bootstrap.sh`.

**Why a separate database, not Nautobot's?** Nautobot's IPAM model is the
source of truth for IP records and device inventory. The controller needs
relational queries over time-series data (per-MAC event histories, sweep runs,
collection runs, IP observation snapshots) that don't fit Nautobot's schema and
shouldn't pollute it.

Tables:
- `endpoints` — MAC-keyed identity, one row per unique MAC ever seen.
  Tracks current IP, switch, port, VLAN, hostname, vendor, classification,
  first/last seen, and the data source (`sweep`, `infrastructure`, or `both`).
- `endpoint_events` — append-only log of changes: `appeared`, `moved_port`,
  `moved_switch`, `ip_changed`, `hostname_changed`. Each row has old/new
  values plus a JSONB `details` blob.
- `device_polls` — per-device, per-job-type poll tracking. Composite PK
  `(device_name, job_type)`. Tracks `last_success`, `last_attempt`,
  `last_error`, `last_duration`, `next_due`, `interval_sec`, `enabled`.
  Seeded from Nautobot inventory on first startup.
- `sweep_runs` — per-sweep summary (CIDR, duration, totals).
- `collection_runs` — per-collection-run summary (devices queried, endpoints
  found/new/updated/moved, duration).
- `ip_observations` — append-only sweep snapshot for an IP (open ports,
  banners, SNMP, HTTP headers, TLS data, classification).
- `kv_config` — replaces the legacy `config.json` file.
- `discovery_excludes` — operator-defined exclusion list (IP or device_name).

**Migration:** on first startup, if `endpoints` is empty and a
`/data/endpoints.json` (or `/data/config.json`) file exists, the controller
imports them once and renames the source files to `*.json.migrated`. If
Postgres is unreachable, the controller falls back to JSON for config only —
endpoint queries return empty until the database comes back.

The ORM layer uses SQLAlchemy 2.x async with the asyncpg driver. See
[../controller/app/db.py](../controller/app/db.py) and
[../controller/app/endpoint_store.py](../controller/app/endpoint_store.py).

## Controller UI Pages

The controller serves a vanilla JS frontend (no frameworks) at `:9090`.

### Navigation Structure

The UI nav bar is organized into grouped sections:

- **Intelligence:** Nodes, Endpoints, Events — primary data views
- **Operations:** Discovery, Jobs, Logs — operational tools
- **Monitoring:** Grafana, Prometheus, Proxmox — external service links (from dashboard)

The Dashboard is the landing page, accessible via the MNM logo.

### Terminology: Nodes vs Endpoints

MNM distinguishes between two categories:

- **Nodes** — onboarded infrastructure devices that are *sources of data* for MNM. They have credentials, poll schedules, and active NAPALM sessions. Listed on `/nodes`.
- **Endpoints** — everything else discovered passively through data collected *from* nodes and sweeps. Listed on `/endpoints`.

The `/endpoints` page filters out MACs belonging to onboarded nodes so only passively-discovered endpoints are shown.

### Page Routes

| Route | Page | Nav Section | Purpose |
|-------|------|-------------|---------|
| `/` | Dashboard | — | Container health, node count, sweep/collection stats, polling status, advisories |
| `/nodes` | Nodes | Intelligence | Onboarded infrastructure with poll health, platform, role, Poll Now |
| `/endpoints` | Endpoints | Intelligence | Passively-discovered endpoints (excludes node MACs) |
| `/endpoints/{mac}` | Endpoint Detail | Intelligence | Full timeline and identity dossier for a single MAC |
| `/events` | Events | Intelligence | Network activity feed filtered by event type and time window |
| `/discover` | Discovery | Operations | Sweep configuration, CIDR ranges, schedule, results table, exclusion list |
| `/jobs` | Jobs | Operations | Consolidated view of all background tasks with status, schedules, and Run Now |
| `/logs` | Logs | Operations | Structured log viewer with level/module filtering |

## Background Tasks

The controller runs these background tasks launched at startup:

| Task | Module | Schedule | Purpose |
|------|--------|----------|---------|
| Sweep Scheduler | `discovery.py` | Per-schedule interval (default 1h) | Network discovery sweeps |
| Modular Poller | `polling.py` | Per-device/per-job-type (ARP 5m, MAC 5m, DHCP 10m, LLDP 1h, Routes 1h, BGP 1h) | Independent device data collection |
| Proxmox Collector | `connectors/proxmox.py` | `PROXMOX_INTERVAL_SECONDS` (default 5m) | VM/container/storage inventory |
| Database Prune | `main.py` | `MNM_PRUNE_INTERVAL_HOURS` (default 24h) | Evict data past retention window |

The legacy monolithic Endpoint Collector (`endpoint_collector.py`) is disabled by default. Set `MNM_LEGACY_COLLECTOR=true` to re-enable during transition.

## Nautobot Dockerfile Patches

Four in-place patches are applied in `nautobot/Dockerfile` to work around upstream plugin issues. Check on plugin upgrades — these may become unnecessary.

| Patch | File | Issue |
|-------|------|-------|
| 1. Netmiko read_timeout | `command_getter.py` | Hardcoded 60s too short for large Junos configs; patched to 120s |
| 2. tcp_ping null port | `command_getter.py` | `NautobotORMInventory` never sets `Host.port`; patched to default 22 |
| 3. Missing serial KeyError | `diffsync_utils.py` | Devices without serial crash sync job; patched via `patches/patch_diffsync_utils.py` |
| 4. Schema validation logging | `processor.py` | Upstream gates ValidationError at DEBUG; patched to WARNING via `patches/patch_processor_schema_logging.py` |
