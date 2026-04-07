# Changelog

All notable changes to MNM are documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **Connector framework — Proxmox VE (read-only)**
  - New `controller/app/connectors/` package establishing the connector pattern (read-only client + scheduler + state + Prometheus exposition + endpoint store integration)
  - `controller/app/connectors/proxmox.py` — Proxmox VE API client. Collects nodes, VMs, LXC containers, host status, storage pools, ZFS pools, and physical disks/SMART
  - Per-VM/CT MAC extraction from net0/net1/... config lines, upserted into the endpoint store with `data_source="proxmox"` (cross-correlates with infrastructure ARP/MAC table data)
  - Prometheus metrics endpoint `GET /api/proxmox/metrics` exposing `mnm_proxmox_node_*`, `mnm_proxmox_vm_*`, `mnm_proxmox_ct_*`, `mnm_proxmox_storage_*`, `mnm_proxmox_zfs_pool_*`, `mnm_proxmox_disk_*`
  - New Prometheus scrape job `proxmox` targeting `controller:9090`
  - New Grafana dashboard "MNM - Proxmox Overview" with node health, VM inventory, top VMs by CPU/memory, ZFS pool usage with thresholds (green <70%, yellow 70-85%, red >85%), pool health table, ZFS growth over time, and physical disk SMART health
  - Dashboard card on `/` showing per-node CPU/memory/uptime, ZFS storage usage summary, and red alert banner for unhealthy pools/disks
  - New env vars: `PROXMOX_HOST`, `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET`, `PROXMOX_VERIFY_SSL`, `PROXMOX_INTERVAL_SECONDS`
  - Connector is fully optional — disabled when `PROXMOX_HOST` is not set
  - New classifications added to discovery: `virtual_machine`, `container`, `hypervisor`
  - New doc: [docs/CONNECTORS.md](docs/CONNECTORS.md) — connector framework reference and Proxmox setup guide (API token, PVEAuditor role, env vars)
- **Phase 2.7 — Endpoint Correlation Engine**
  - Dedicated `mnm_controller` PostgreSQL database (on the existing `mnm-postgres` instance) for the controller's persistent state
  - SQLAlchemy + asyncpg async ORM layer (`controller/app/db.py`)
  - New tables: `endpoints` (MAC-keyed identity), `endpoint_events` (movement/IP/hostname change log), `sweep_runs`, `collection_runs`, `ip_observations`, `kv_config`
  - Automatic one-shot migration from `endpoints.json` and `config.json` on first startup; old files are renamed to `*.json.migrated`
  - Endpoint diff/event detection: `appeared`, `moved_port`, `moved_switch`, `ip_changed`, `hostname_changed`
  - New API endpoints: `GET /api/endpoints/{mac}`, `/{mac}/history`, `/{mac}/timeline`, `/api/endpoints/events`, `/api/endpoints/conflicts`
  - Endpoint detail page with full timeline narrative for any MAC
  - Network activity feed page (`/events`) — filter by event type and time window, with IP conflict detection
  - Dashboard "Recent Events" card showing the last 10 endpoint changes
  - Bootstrap script idempotently creates the controller database on `mnm-postgres`
- SNMP-based endpoint collection (replaces NAPALM proxy for ARP/MAC tables)
- DataTable component with pagination, sorting, column preferences
- User preferences panel (theme, page size, auto-refresh interval)
- Configurable concurrency: MNM_SWEEP_CONCURRENCY, MNM_COLLECTION_CONCURRENCY, MNM_API_CONCURRENCY
- Timing instrumentation on sweep and collection (per-host, per-device, elapsed)
- IEEE OUI database with 39,178 vendor prefixes
- Infrastructure endpoint collection from device ARP, MAC, and DHCP tables
- Endpoint correlation: ARP (IP→MAC) + MAC table (MAC→port→VLAN) + DHCP (MAC→hostname)
- Endpoints UI page with sortable/filterable table
- 10 new endpoint_* custom fields on IP Address for infrastructure data
- Scheduled endpoint collection every 15 minutes
- Junos DHCP server binding collection via NETCONF RPC
- Shodan-style service fingerprinting: banner grab, HTTP headers/title, TLS certs, SSH banners
- Enriched network sweep with SNMP, MAC, DNS, and port discovery
- Device classification engine (network_device, server, web_service, printer, access_point, endpoint)
- Nautobot IPAM integration — all discovered IPs recorded with 11 custom fields
- Periodic re-sweep scheduling via Controller UI
- 365-day Prometheus retention (up from 15 days) with 10 GB size cap
- Comprehensive documentation: DISCOVERY.md, MONITORING.md, TROUBLESHOOTING.md, API.md
- CHANGELOG.md

### Changed
- Endpoint collection uses SNMP walks instead of NAPALM proxy (10x faster)
- Discovery sweep: pre-fetches known IPs in bulk, parallel enrichment per host, batched IPAM writes
- Bootstrap auto-skips on subsequent runs when already initialized (>5000 device types + custom fields)
- All tables use full viewport width with pagination
- Prometheus retention configurable via `PROMETHEUS_RETENTION_DAYS` env var
- Discovery UI shows enriched data: DNS name, MAC vendor, open ports, SNMP name, classification
- Bootstrap creates 11 discovery custom fields on IP Address model (idempotent)

### Fixed
- first_seen preserved on endpoint updates (was being overwritten every collection)
- Hostname populated from DHCP, DNS, and SNMP sources (priority order)
- Cleared stuck Nautobot jobs in "Pending" state from previous sessions

## Phase 2.5 — Controller

### Added
- MNM Controller container (FastAPI + vanilla JS) on port 9090
- Password-gated dashboard with container status and service links
- Seed-and-sweep network discovery with operator-defined scope
- LLDP neighbor advisory panel (Human-in-the-Loop, Rule 6)
- Nautobot API proxy endpoints for devices, locations, secrets groups
- Docker SDK integration for container health monitoring

## Phase 2 — Monitoring + Telemetry

### Added
- Traefik v2.11 reverse proxy with Docker label routing
- Prometheus v3.4.0 metric storage
- SNMP Exporter v0.29.0 polling IF-MIB
- gnmic for gNMI streaming telemetry
- Grafana 12 with Device Dashboard and Network Overview
- Anonymous read-only Grafana access

### Fixed
- Worker healthcheck: `celery inspect ping`
- Scheduler healthcheck: `/proc/1/cmdline` check

## Phase 1 — Discovery + Foundation

### Added
- Custom Nautobot 3.0 image with device-onboarding, welcome-wizard, NAPALM
- Docker Compose: PostgreSQL 15, Redis 7, Nautobot, Celery worker/scheduler
- Idempotent bootstrap with 5,200+ community device types
- Juniper devices onboarded (EX series, SRX series)

### Fixed
- Netmiko read_timeout patched to 120s
- tcp_ping null port crash (default to 22)
- Nornir credential env var split (NAUTOBOT_NAPALM_* vs NAPALM_*)
- SSH conn_timeout to 30s for slower devices
