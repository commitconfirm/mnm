# MNM — Modular Network Monitor

MNM is a self-contained, Docker-based network discovery and monitoring appliance. Point it at a few seed devices and credentials, and it builds a complete inventory of every device, endpoint, and IP it can see — across vendors, across VLANs, across hypervisors. It then watches that network over time, tracking what changes.

It's built for network engineers who need to quickly understand what's on a network: during onboarding at a new job, during M&A integration, after inheriting an undocumented environment, or as a standing monitoring platform for small-to-medium environments.

This is **v0.1.0**. The discovery, monitoring, and endpoint correlation pieces are working and tested on real hardware. The AI assistant and config-backup pieces are on the roadmap (see below).

## Principles

- **Read-only.** MNM never writes configuration to network devices or cloud services. Every API call, SNMP operation, and SSH session is read-only. This is enforced architecturally — there are no create/update/delete code paths in the tool layer.
- **Self-contained.** Runs on a single host with no external subscriptions or cloud dependencies. Designed to function fully on an isolated network.
- **Privacy-first.** No telemetry, no phone-home, no usage tracking. Network data stays on the box.
- **Modular.** Discovery and inventory are always on. Monitoring, hypervisor connectors, and (eventually) AI assistance are opt-in.
- **Open source.** MIT licensed. Built in the open.

## What's in v0.1.0

### Discovery & Inventory
- **Nautobot 3.0** as the source of truth, with the device-onboarding plugin and 5,200+ pre-loaded community device types ready on first boot
- **Seed-and-sweep** discovery — give MNM CIDR ranges and credentials, it scans, fingerprints every IP it can reach (open ports, banners, TLS certs, HTTP titles, SSH versions, SNMP system data, MAC vendor lookup), classifies the host, and onboards anything that looks like a network device
- **Periodic re-sweeps** on a configurable schedule, so you can watch a network evolve over weeks and months
- **LLDP neighbor advisory** — newly discovered neighbors are surfaced for the operator to approve. MNM never recursively crawls (Rule 6: human-in-the-loop for scope decisions)

### Endpoint Correlation Engine
- **MAC-keyed identity records** with composite-key tracking of every (switch, port, VLAN) location a MAC has occupied
- **Movement detection** — emits `appeared`, `moved_port`, `moved_switch`, `ip_changed`, and `hostname_changed` events as endpoints move around the network
- **Multi-source correlation** — joins switch ARP tables, MAC tables, DHCP bindings, sweep results, and Proxmox guest data into one unified record per MAC
- **Multi-IP support** — `additional_ips` tracking for dual-stack and multi-NIC endpoints
- **Watchlist** — flag MACs of interest; their movement events get highlighted across the activity feed
- **Anomaly detection** — surfaces IP conflicts, MACs active on multiple switches, endpoints with no IP, unclassified hosts, and stale endpoints (configurable threshold)
- **Historical timeline** — every (mac, switch, port, vlan) row ever recorded is preserved for forensics

### Infrastructure Collection
Once a device is onboarded, MNM polls it on a schedule for forwarding-plane data:
- **ARP tables** via SNMP / NAPALM
- **MAC address tables** with VLAN context
- **DHCP server bindings** (Junos via PyEZ; other vendors as drivers mature)
- **Uplink detection** with three-tier fallback: cables → NAPALM LLDP neighbors → access-port heuristic. Uplink ports are excluded from endpoint location to avoid mistaking trunk-transit MACs for endpoints

### Hypervisor Connector — Proxmox VE
- **Read-only** API client (PVEAuditor role) collecting nodes, VMs, LXC containers, host status, ZFS pools, storage, and physical disk SMART health
- **VM IP enrichment** with three sources: QEMU Guest Agent for VMs that have it, `/lxc/{vmid}/interfaces` for containers, and cross-MAC propagation from switch ARP tables for everything else
- **VMs and containers become endpoints** in the same unified store, joined to their on-the-wire identity by MAC
- **Prometheus exposition** of 30+ host and guest metrics for the Grafana dashboard
- **ZFS pool monitoring** with usage thresholds, fragmentation, dedup ratio, and per-disk SMART status

### Monitoring
- **Prometheus** with 365-day retention by default
- **Grafana** with three pre-built dashboards in the `MNM` folder: Device Dashboard, Network Overview, and Proxmox Overview
- **Dynamic SNMP target discovery** — Prometheus pulls the SNMP scrape target list from the controller via `http_sd`, so newly onboarded devices automatically appear in monitoring with no config edits
- **gnmic** for gNMI streaming telemetry (active when devices have gNMI enabled)
- **snmp_exporter** for the universal SNMP-polling fallback

### Controller UI
A FastAPI + vanilla-JS web app on port 9090 that's the primary operator entry point:
- **Dashboard** — container health, device count, IPs tracked, recent events, Proxmox node status, advisory cards (incomplete devices, discovered subnets, LLDP neighbors)
- **Discovery** — trigger sweeps, view live progress, manage CIDR ranges and schedules. Includes an **Unsupported vendors / unclassified hosts** panel that surfaces sweep-discovered IPs MNM didn't onboard, with raw signals (sysDescr, OUI vendor, open ports) and CSV/JSON export, so operators can see which vendors to prioritise for future support.
- **Endpoints** — sortable / filterable table, per-MAC detail page with full movement timeline
- **Events** — network activity feed with filtering, IP conflict detection, anomaly buckets
- **Logs** — structured log viewer with level/module filters and a one-click export bundle for GitHub issue reports
- **Watchlist** — add/remove MACs of interest, see flagged events
- **One-click sync** for incomplete devices (devices that exist in Nautobot but lack IPs)

### Backend
- **PostgreSQL** for both Nautobot and the controller's endpoint store
- **Redis** for Nautobot's cache and Celery broker
- **Celery worker + scheduler** for Nautobot background jobs
- **Traefik** as the reverse proxy
- **Structured JSON logging** with secret masking and an in-memory ring buffer for the log viewer

## Quick Start

### Prerequisites
- Ubuntu 24.04 LTS or any Linux host capable of running Docker
- Docker Engine and Docker Compose v2
- 16 GB RAM minimum, 4 vCPUs
- Network reachability to the devices you want to monitor (typically port 161 SNMP, port 22 SSH, port 830 NETCONF, ICMP)

### Deploy

```bash
git clone https://github.com/commitconfirm/mnm.git
cd mnm
cp .env.example .env
# Edit .env — see the file itself for a guided tour. Required values
# are clearly marked [REQUIRED]; secrets are marked [SECRET].
docker compose up -d
bash bootstrap/bootstrap.sh
```

The bootstrap script is idempotent — it creates the Nautobot superuser, locations, roles, manufacturers, platforms, ~5,200 community device types, the controller's PostgreSQL database, and (when NAPALM credentials are set in `.env`) a default Nautobot SecretsGroup. Safe to re-run. See [docs/BOOTSTRAP.md](docs/BOOTSTRAP.md) for what gets created and how to extend the bootstrap library when new vendors enter your network.

#### Two compose-file variants

MNM ships two functionally-identical Compose files:

- **`docker-compose.yml`** (default) — production-grade ops manual. Top-of-file orientation, per-service comment blocks (purpose, dependencies, exposed ports, persistent storage, failure modes), inline annotations on every non-obvious choice. Recommended for first-deploy operators and as ongoing reference.
- **`docker-compose.expert.yml`** — same YAML, lean inline-only commentary. For operators already familiar with the stack who don't want the full annotation pass.

Both files produce identical `docker compose config` output. Use the expert variant by passing `-f docker-compose.expert.yml` to every Compose command. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full first-deploy walkthrough.

### First Login

Once `docker compose ps` shows everything healthy, the **controller** at **`http://<host>:9090`** is your primary entry point. Log in with the password you set in `.env`. From there:

1. Open **Discovery**, add a CIDR range and select the credential set you configured during bootstrap, click **Start Sweep**
2. Watch the discovery table populate as MNM probes each IP
3. Onboarded devices automatically appear in **Prometheus** monitoring within ~60 seconds via dynamic SNMP target discovery
4. Open **Endpoints** to see what's been correlated; **Events** to see what's been changing

### Service URLs

| Service | URL | Notes |
|---------|-----|------|
| **MNM Controller** | `http://<host>:9090` | Primary operator UI — start here |
| Nautobot | `http://<host>:8443` | Source-of-truth inventory + IPAM |
| Grafana | `http://<host>:8080/grafana/` | Dashboards in the `MNM` folder |
| Prometheus | `http://<host>:8080/prometheus/` | Raw metrics + scrape target status |

For a step-by-step deployment walkthrough including credential setup, see [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Architecture

MNM ships as 11 Docker services:

| Service | Tool | Purpose |
|---------|------|---------|
| `controller` | FastAPI + vanilla JS | Primary operator UI, discovery engine, endpoint store, connectors, advisories |
| `nautobot` | Nautobot 3.0 + device-onboarding | Source-of-truth inventory, device records, IPAM |
| `nautobot-worker` | Celery worker | Runs Nautobot's background jobs (onboarding, sync) |
| `nautobot-scheduler` | Celery beat | Scheduled Nautobot tasks |
| `postgres` | PostgreSQL 15 | Backs both Nautobot and the controller's `mnm_controller` database |
| `redis` | Redis 7 | Nautobot cache + Celery broker |
| `traefik` | Traefik v2.11 | Reverse proxy routing `/`, `/grafana`, `/prometheus` to backend services |
| `prometheus` | Prometheus v3 | Metrics storage with 365-day retention |
| `grafana` | Grafana 12 | Dashboards (auto-provisioned into the `MNM` folder) |
| `snmp-exporter` | snmp_exporter | SNMP polling, scrape targets pulled dynamically from the controller |
| `gnmic` | gnmic | gNMI streaming telemetry collector |

The controller is the heart of MNM. It owns the discovery engine, the endpoint correlation store, the connector framework, and every operator-facing UI. Nautobot remains the source of truth for device records and IPAM; the controller's PostgreSQL store handles temporal data (movement events, sweep history, IP observations) that Nautobot's model doesn't represent natively.

For the full architectural breakdown including container map, port assignments, data flows, and security model, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

| Doc | What's in it |
|-----|--------------|
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every `.env` variable, what it does, defaults, and gotchas |
| [docs/DISCOVERY.md](docs/DISCOVERY.md) | How seed-and-sweep, fingerprinting, and the endpoint correlation engine work |
| [docs/MONITORING.md](docs/MONITORING.md) | Prometheus, Grafana, SNMP exporter, gnmic — what's collected and how to add devices |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Container map, network flows, port assignments, security model |
| [docs/CONNECTORS.md](docs/CONNECTORS.md) | Connector framework reference + Proxmox VE setup (API token, PVEAuditor role) |
| [docs/API.md](docs/API.md) | Controller REST API reference with curl examples |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common issues drawn from real deployment experience |
| [docs/PLUGIN.md](docs/PLUGIN.md) | The mnm-plugin Nautobot Django app — Endpoint, ARP, MAC, LLDP, Route, BGP, and Fingerprint models surfaced in Nautobot (v1.0 Block E) |

## Supported Vendors

MNM uses NAPALM for multi-vendor device communication. Tested:

| Vendor | Status | Notes |
|--------|--------|-------|
| Juniper (Junos) | Primary test target | Excellent NAPALM support via NETCONF/PyEZ. Requires `view configuration` permission for full onboarding |
| Cisco (IOS / IOS-XE) | Supported | Strong NAPALM SSH support, well-tested upstream |
| Fortinet (FortiOS) | Driver available | Community NAPALM driver — coverage is thinner than Juniper/Cisco |

NAPALM supports [many additional platforms](https://napalm.readthedocs.io/en/latest/support/). For SNMP-only monitoring, MNM works with anything that speaks SNMPv2c or SNMPv3.

## Project Status

| Phase | Status | Scope |
|-------|--------|-------|
| Phase 1 — Discovery foundation | ✅ Complete | Nautobot, device onboarding, bootstrap, idempotent setup, 5,200+ device types |
| Phase 2 — Monitoring stack | ✅ Complete | Prometheus, Grafana, snmp_exporter, gnmic, dynamic SNMP target discovery |
| Phase 2.5 — Controller + intelligence | ✅ Complete | Controller UI, seed-and-sweep, fingerprinting, IPAM integration, password-gated UI |
| Phase 2.7 — Endpoint correlation engine | ✅ Complete | PostgreSQL endpoint store, MAC-keyed movement tracking, anomaly detection, watchlist, advisories |
| Phase 2.7+ — Proxmox connector | ✅ Complete | Read-only Proxmox VE API client with ZFS, VM/CT, and SMART monitoring |
| Phase 3 — AI layer | 🔜 Planned | Embedded Ollama assistant, tool layer, MCP server, chat UI |
| Phase 4 — Connectors + ops | 🔜 Planned | Mist cloud connector, Oxidized config backup, Loki log aggregation |
| Phase 5 — Testing + CI | 🔜 Planned | Unit tests, integration tests, GitHub Actions pipeline |

## Roadmap

The following are designed and documented but **not yet built**:

- **Embedded AI assistant (Phase 3).** A local LLM via [Ollama](https://ollama.com), scoped exclusively to MNM's data via a tool layer. The assistant will answer natural-language questions like *"Which Juniper devices are on the network?"*, *"What changed on switch port ge-0/0/12 last week?"*, *"Show me endpoints that moved between switches today."* It will not be able to answer general questions, and it will not have any write capabilities.
- **MCP server (Phase 3).** A [Model Context Protocol](https://modelcontextprotocol.io/) server exposing the same tool layer as the embedded assistant, so external AI tools (Claude Desktop, Claude Code) can query MNM's data directly. This is the optional upgrade path for users who want a more capable model than the local LLM can run.
- **Cloud connectors (Phase 4).** Mist is the first planned cloud connector — read-only ingestion of inventory and status into Nautobot, following the connector framework pattern established by the Proxmox connector.
- **Config backup (Phase 4).** Oxidized for read-only configuration archival, with its device list pulled directly from Nautobot.
- **Log aggregation (Phase 4).** Loki for centralized device syslog and search.
- **Testing + CI (Phase 5).** pytest unit + integration test suite, mocked Nautobot/SNMP/SSH responses, GitHub Actions pipeline with lint + type-check + test on every push.

These will be built and shipped in subsequent releases. The phasing exists so each release stays small enough to test thoroughly on real hardware before moving on.

## Security

MNM collects sensitive network intelligence: device inventories, IP ranges, service fingerprints, TLS certificates, SNMP data, LLDP topology, MAC addresses, and operator credentials (stored in Nautobot Secrets and the host's `.env`). If compromised, this data is a roadmap for attacking the monitored network.

Deploy MNM on trusted infrastructure with appropriate access controls. The controller UI is password-gated; Nautobot has its own auth; Grafana defaults to anonymous read-only viewing because dashboards are intended to be shared. The Docker socket is mounted into the controller container so it can manage the stack, which is functionally root-equivalent — operators should treat the MNM host the same way they would any management/jump host.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the threat model, hardening recommendations, and data retention details.

## Predecessor

MNM is a ground-up rewrite of [nts-server](https://github.com/commitconfirm/nts-server), a bash-driven Docker deployment of network monitoring tools (Netdisco, LibreNMS, Oxidized, NGINX). MNM reimagines the same problem space with a modern stack, structured persistence, an operator UI built around the discovery and endpoint workflows, and a connector framework for hypervisor and cloud integrations.

## Contributing

Issues and ideas are welcome via GitHub Issues. Pull requests will be accepted once the project reaches a more stable feature set; for now, expect rapid iteration and occasional schema changes between releases.

## License

MIT. See [LICENSE](LICENSE).
