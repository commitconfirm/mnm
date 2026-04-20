# Changelog

All notable changes to MNM are documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **Phase 1 direct-REST onboarding orchestrator (Junos only, v1.0 Prompt 4).** New `controller/app/onboarding/orchestrator.py` implements the 6-step Nautobot creation sequence from the reality-check doc (§1.3) with proper rollback for the interface-cascades-but-IP-doesn't asymmetry (§4.7). Strict-new refusal (operator Q2) prevents accidental overwrite of existing devices. Step G (polling seed) failure marks the device `Onboarding Incomplete` rather than rolling back a valid device. Vendor probe modules in `controller/app/onboarding/probes/`; `probes/junos.py` collects hostname / serial / chassis model / os_version via SNMP (JUNIPER-MIB scalars with ENTITY-MIB fallback). New operator CLI: `python -m app.scripts.onboard_probe --ip <addr> --community <community> --location-id <uuid> --secrets-group-id <uuid>`. Replaces the nautobot-device-onboarding plugin path for Junos devices invoked via `onboard_probe.py`; plugin path remains the default for the Discover UI until Prompt 8 integrates the new path.

### Changed
- Replaced `nautobot_client.get_status_by_slug` with `get_status_by_name`. `natural_slug` parsing removed — Nautobot's auto-generated `natural_slug` format is internal and not a stable contract; exact name match via the documented `?name=` filter is the correct identifier. `ensure_custom_statuses` now resolves each desired Status via `get_status_by_name(name, content_type="dcim.device")` instead of paging the full Status list client-side.
- Extracted vendor classification into `controller/app/onboarding/classifier.py`. Added `sysObjectID` prefix fallback as a second independent signal; added two-stage Cisco IOS / IOS-XE platform discrimination (matches `IOSXE`, `IOS-XE`, and `IOS XE` variants); added Arista classifier rule (validated against live vEOS 172.21.140.16 on 2026-04-20). `app.discovery.classify_endpoint` now delegates to the new module so sweep-consumer behaviour is unchanged. `sysDescr` and `sysObjectID` added to the `snmp_collector.OIDS` registry. New operator probe script: `python -m app.scripts.classify_probe --ip <addr> --community <community>`.
- `nautobot_client.create_device` accepts optional `serial=` and a new helper `find_device_at_location(name, location_id)` returns the existing device record or None. `device_exists_at_location` and `find_device_at_location` filter with `?location=<uuid>` — the Nautobot 3.x filter name (`location_id=` returns 400). Added OIDs to the registry: `SNMPv2-MIB::sysName`, `JUNIPER-MIB::jnxBoxDescr`, `JUNIPER-MIB::jnxBoxSerialNo`, `ENTITY-MIB::entPhysicalClass`, `ENTITY-MIB::entPhysicalSerialNum`, `ENTITY-MIB::entPhysicalModelName`.

## [0.9.0] - 2026-04-19

Pre-v1.0 baseline tag. No code changes from prior commit — this tag marks the reference point for the v1.0 architectural rework documented in CLAUDE.md.

This release includes all completed work through Phase 2.9 (see CLAUDE.md "Completed Phases" section for the full list).

## [0.9.1] - 2026-04-19

### Fixed
- **Controller-DB race on cold start** — controller now retries DB connection for up to 5 minutes (5s intervals) instead of failing immediately and falling back to JSON mode. Eliminates the need to manually restart the controller after bootstrap completes. If the DB is unreachable for the full timeout, a prominent warning banner appears on the dashboard.
- **Degraded-mode dashboard banner** — dashboard now shows a visible warning when `db_connected` is false, explaining the impact and recovery steps.

### Added
- Added `docs/CODING_STANDARDS.md` establishing project coding standards. Pre-commit self-check checklist will be applied to all future code-touching work.

### Added
- **Node/endpoint terminology rework** — introduces the distinction between "nodes" (onboarded infrastructure that feeds MNM data) and "endpoints" (passively-discovered devices). New `GET /api/nodes` endpoint returns onboarded devices with poll health status. `GET /api/devices` now returns a `301` redirect to `/api/nodes` (deprecated).
- **Nodes page** (`/nodes`) — dedicated page listing all onboarded infrastructure with health dots (green/yellow/red/gray), platform, primary IP, role, location, per-job poll status dots, last polled time, and Poll Now button. Uses DataTable component with search and filtering.
- **`GET /api/nodes/macs`** — returns MAC addresses belonging to onboarded nodes, used by the endpoints page to filter out infrastructure devices.
- **UI navigation reorganization** — nav bar grouped into sections: Intelligence (Nodes, Endpoints, Events), Operations (Discovery, Jobs, Logs), with visual separators and group labels. Discovery moved from top-level to Operations. Updated on all 8 pages.
- **Endpoints page node filtering** — endpoints page now excludes MACs belonging to onboarded nodes, showing only passively-discovered endpoints.
- **Multi-IP endpoint correlation fix** — builds cross-device MAC→IPs map from ALL nodes' ARP tables and sweep observations before upserting endpoints. Devices like SRX320 with multiple IPs across different interfaces now get all IPs merged into `additional_ips`. New `get_mac_ip_map_from_observations()` helper supplements ARP data with sweep-discovered IP→MAC associations.
- **Routing collection** — three new polling job types: `collect_routes` (NAPALM `get_route_to`), `collect_bgp` (NAPALM `get_bgp_neighbors`), and VRF context via `get_network_instances`. Routes and BGP neighbors stored in controller PostgreSQL (`routes` and `bgp_neighbors` tables) with upsert-on-conflict semantics. Default intervals: `MNM_POLL_ROUTES_INTERVAL=3600`, `MNM_POLL_BGP_INTERVAL=3600`.
- **Route and BGP API endpoints** — `GET /api/routes`, `GET /api/routes/{node}`, `GET /api/routes/advisories`, `GET /api/bgp`, `GET /api/bgp/{node}`. Route advisories surface next-hops not in IPAM as discovery candidates.
- **Route advisories dashboard card** — shows routes pointing to unknown next-hop gateways with "Add to sweep" action. Similar pattern to subnet and LLDP advisories.
- **Dashboard polling table** — added Routes and BGP columns to polling status card. Maintenance card shows route/BGP row counts.
- **Prune integration** — daily prune job now cleans old `routes` and `bgp_neighbors` rows past retention window. Preview endpoint includes route/BGP counts.
- **Raw network data persistence** — ARP, MAC, and LLDP collection now persists per-node to `node_arp_entries`, `node_mac_entries`, `node_lldp_entries` tables (in addition to feeding the correlation engine). Per-node API endpoints: `GET /api/nodes/{name}/arp`, `/mac-table`, `/lldp`, `/fib`. FIB populated from SNMP route data. All tables included in daily prune.
- **Node detail page** (`/nodes/{name}`) — tabbed view with ARP Table, MAC Address Table, Routing Table, LLDP Neighbors, BGP Peers, Poll History. Each section uses DataTable with search/filter and count badges. Replaces Nautobot link on nodes list.
- **Global network search** (`/search`) — auto-detecting search bar: MAC, IP, CIDR prefix, or hostname. Cross-node queries via `GET /api/search?q={query}`. Search icon added to nav on all pages. Results grouped by type with links to node detail and endpoint pages.
- **Hop-limited auto-discovery** — opt-in LLDP neighbor walk during sweep or manual trigger. New `auto_discover_hops` parameter (0–5, default 0) on sweep form and API. Sequential onboarding with BFS traversal, visited-set loop prevention, exclusion list respect, `MNM_AUTO_DISCOVER_MAX` hard cap (default 10). IP resolved from LLDP chassis ID, Nautobot lookup, or DNS. New module `auto_discover.py`.
- **Auto-discovery API** — `POST /api/discover/auto` (manual trigger), `GET /api/discover/auto/history`, `GET /api/discover/auto/recent` (dashboard advisory). Progress appears in sweep scan log.
- **Recently Auto-Discovered dashboard card** — shows nodes auto-discovered in the last 24 hours with parent node, hop depth, and onboarding status. Rule 6 compliance visibility.
- **Discovery page auto-discover field** — number input for hop limit (0–5) in the sweep form.
- **Modular per-device polling** — replaces monolithic endpoint collector with independent `collect_arp()`, `collect_mac()`, `collect_dhcp()`, `collect_lldp()` job functions per device. New `device_polls` table tracks per-device/per-job-type state (last success, last error, next due, interval, enabled). Per-device sequential execution avoids overlapping NAPALM sessions; cross-device parallel dispatch with configurable concurrency. 10% random jitter on `next_due` prevents thundering herd. Seeds from Nautobot inventory on first startup. Legacy monolithic collector gated behind `MNM_LEGACY_COLLECTOR=true`. Default intervals: `MNM_POLL_ARP_INTERVAL=300`, `MNM_POLL_MAC_INTERVAL=300`, `MNM_POLL_DHCP_INTERVAL=600`, `MNM_POLL_LLDP_INTERVAL=3600`.
- **Jobs page** (`/jobs`) — consolidated view of all background tasks: Sweep Scheduler, Modular Poller, Proxmox Collector, Database Prune, and legacy Endpoint Collector. Shows status, schedule interval, last run, next run, duration, result summary, and Run Now button per job. `GET /api/jobs` consolidated endpoint.
- **Polling API endpoints** — `GET /api/polling/status`, `GET /api/polling/status/{device}`, `POST /api/polling/trigger/{device}`, `POST /api/polling/trigger/{device}/{job_type}`, `PUT /api/polling/config/{device}/{job_type}`. Trigger endpoints return 202 Accepted.
- **Dashboard Polling Status card** — per-device rows with green/yellow/red/gray status dots for ARP, MAC, DHCP, LLDP collection. "Poll Now" button per device. Auto-refreshes with dashboard interval.
- **Onboarding progress tracker** — in-memory per-IP state machine (`submitting → queued → running → succeeded | failed | timeout`) surfaced via `GET /api/discover/onboarding` and in the sweep host Details panel.
- **Sweep-scheduled trigger** — `POST /api/discover/sweep-scheduled` re-runs the first saved sweep schedule without requiring a request body (used by Jobs page Run Now).
- **Add-to-sweep prefill** — dashboard "Discovered Subnets" → "Add to sweep" now pre-fills the CIDR textarea on the discovery page, shows an instructional banner, and scrolls to the form.
- **Device interface subnet auto-expansion** — sweep schedule loop auto-adds subnets from onboarded device interface IPs to the sweep scope. Subnet advisory filters them out.
- **Nautobot Patch 4** — `patches/patch_processor_schema_logging.py` promotes schema validation errors from DEBUG to WARNING so the actual missing field is visible in JobResult logs without enabling debug mode.
- **Documentation index** — new `docs/INDEX.md` with role-based navigation.

### Changed
- **Table styling consistency** — removed `full-width-table` edge-bleed wrapper from all pages; all tables now use card-padded style matching Sweep History.
- **Discovery page UX** — moved CIDR instruction into textarea label, moved pipeline description under progress bar, scan log box vertically resizable (80px–500px).
- **Container Status** moved to bottom of dashboard page.
- **Jobs nav link** added to all pages (Dashboard, Discovery, Endpoints, Events, Logs, Jobs).

### Fixed
- **Sweep stop button** — now cancels within seconds instead of hanging for minutes. Cancel check added inside onboarding poll loop (every iteration) and port probe queue (`semaphore.acquire` with 2s timeout).
- **Five discovery pipeline bugs** causing every onboarding to fail silently:
  - Location type: saved schedule pointed at Region (no `dcim.device` content type); startup migration auto-repoints to Site.
  - JobResult stuck PENDING: controller reads `celery-task-meta-*` from Redis db 1 as fallback when DB row stays PENDING (upstream plugin bug).
  - Status string case: poll loop checked `"completed"`/`"failed"` but Nautobot 3.x uses `SUCCESS`/`FAILURE`.
  - IPAM IntegrityError: `delete_standalone_ip()` removes unattached IP records before onboarding to prevent duplicate key crash.
  - `find_device_by_ip`: rewrote to use `IPAddress → Interface → Device` lookup chain (Nautobot 3.x rejects `?primary_ip4__host=`).
- **Container Status crash** — `ImageNotFound` when Docker prunes old images after rebuild; falls back to `Config.Image` from container attrs.

### Previous additions
- **Database maintenance — daily prune + on-demand admin endpoints**
  - New helpers in `controller/app/endpoint_store.py`: `prune_old_events`, `prune_old_observations`, `prune_orphaned_watches`, `prune_stale_sentinels`, `prune_all`, `prune_preview`, `maintenance_stats`. Each operation is structured-logged under the dedicated `prune` module.
  - Background scheduled prune task in the controller, configurable via `MNM_RETENTION_DAYS` (default 365) and `MNM_PRUNE_INTERVAL_HOURS` (default 24). First run is delayed 2 minutes after startup to let the system settle.
  - New API endpoints: `GET /api/admin/maintenance` (row counts + last prune summary + retention setting), `GET /api/admin/prune/preview` (dry-run row counts), `POST /api/admin/prune` (run prune now)
  - New "Database Maintenance" card on the dashboard with row counts (endpoints, events, observations, watches), oldest-event/observation timestamps, retention setting, "Preview Prune" + "Run Prune Now" buttons (with confirm dialog per Rule 9), and last-run summary
  - Sentinel-row reaping: sweep-only endpoints with `current_switch = '(none)'` that age past the retention window are deleted
  - Orphaned watchlist entries (watches whose target MAC has been purged) are cleaned up
- **Docker log rotation on every service** — added `x-default-logging` YAML anchor and applied `logging: *default-logging` (json-file driver, 10MB × 3 files = 30MB cap per container) to all 11 services in `docker-compose.yml`. Caps unbounded container log growth.
- **Nautobot retention documentation** — new section in `docs/CONFIGURATION.md` covering `JOB_RESULT_RETENTION` and `CHANGELOG_RETENTION`, where to set them, and how they interact with MNM's own prune loop.
- **Patch 3 in `nautobot/Dockerfile`** — tolerate missing chassis serials in Sync Network Data. Some devices (factory-reset, virtual chassis members, mid-RMA, certain EX2300 states) return command-getter results without a usable `serial` key, which crashed the entire upstream sync run with `KeyError 'serial'`. The patch defaults to `""` and falls back to hostname-only matching when no non-empty serials were collected. Implemented as a small Python script (`nautobot/patches/patch_diffsync_utils.py`) since the multi-line replacement isn't sed-friendly. Tracking upstream.
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
