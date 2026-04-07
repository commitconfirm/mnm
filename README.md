# MNM — Modular Network Monitor

MNM is a self-contained, Docker-based network discovery and monitoring appliance. Deploy it on a network, give it seed devices and credentials, and it discovers, inventories, and monitors your infrastructure — across vendors, across sites.

MNM is designed for network engineers who need to quickly understand what's on a network: during onboarding at a new job, during M&A integration, or as a standing monitoring platform for small-to-medium environments.

## Principles

- **Read-only.** MNM never writes configuration changes to network devices or cloud services. It observes. It does not modify.
- **Self-contained.** MNM runs entirely on a single host with no external subscriptions or cloud dependencies. Everything — including the AI assistant — runs locally.
- **Privacy-first.** No telemetry, no phone-home, no data leaves the box.
- **Modular.** Enable only the components you need. Discovery and inventory are always on. Monitoring, config backup, log aggregation, and cloud connectors are optional.
- **Open source.** MIT licensed. Built in the open.

## What It Does

**Discovery & Inventory** — Hand MNM a few seed IP addresses and SNMP/SSH credentials. It connects to each device, identifies the vendor and platform, pulls interface data, routing tables, ARP/MAC tables, and LLDP/CDP neighbor information, then follows those neighbors to map the full topology. The result is a populated [Nautobot](https://github.com/nautobot/nautobot) instance with your complete network inventory.

**AI-Powered Querying** — MNM includes an embedded AI assistant (powered by a local LLM via [Ollama](https://ollama.ai)) that can answer natural language questions about your network: *"What Juniper devices are on the network?"*, *"Which interfaces are down?"*, *"Show me the routing table for the core switch."* The assistant is scoped exclusively to MNM's data — it cannot answer general questions, and it cannot make changes.

**MCP Server** — MNM exposes a [Model Context Protocol](https://modelcontextprotocol.io/) server, allowing external AI tools like Claude Desktop or Claude Code to query MNM's data directly. This gives you the option of using a more capable external model for complex analysis while MNM provides the data access layer.

**Monitoring & Telemetry** — Prometheus and Grafana provide metric collection and dashboards for device health, interface utilization, and alerting. MNM supports both SNMP polling (universal fallback) and gNMI/gRPC streaming telemetry (preferred when devices support it) via gnmic. Both paths feed into Prometheus for a unified view.

**Cloud Connectors** — Optional read-only integrations with cloud-managed networking platforms (Juniper Mist, with more planned) pull inventory and status data into MNM's unified view.

**Config Backup** — Oxidized performs automated read-only configuration backups, pulling its device list directly from Nautobot's inventory.

## Quick Start

### Prerequisites

- Ubuntu 24.04 LTS (VM recommended, x86_64)
- Docker CE and Docker Compose v2
- Minimum 16 GB RAM, 4 CPU cores (32 GB recommended if using larger LLM models)
- Network access to target devices (SNMP and/or SSH)

### Deploy

```bash
git clone https://github.com/commitconfirm/mnm.git
cd mnm
cp .env.example .env
# Edit .env with your timezone, passwords, etc.
docker compose up -d
```

MNM will start Nautobot and its dependencies. Once healthy, access the web interface via your browser.

### Discover Your Network

1. Open Nautobot at `http://<server-ip>:8443`
2. Add your SNMP communities and/or SSH credentials
3. Seed your first device IPs
4. MNM discovers neighbors and builds your inventory

### Connect Claude Desktop (Optional)

Add MNM's MCP server to your Claude Desktop configuration to query your network data with Claude:

```json
{
  "mcpServers": {
    "mnm": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://<server-ip>:8444/mcp"
      ]
    }
  }
}
```

## Architecture

MNM is built on proven open-source tools, composed via Docker:

| Component | Tool | Purpose |
|-----------|------|---------|
| Inventory & Discovery | Nautobot + NAPALM | Multi-vendor device onboarding, topology mapping |
| Monitoring | Prometheus + Grafana | Metrics, dashboards, alerting |
| Network Telemetry | gnmic + snmp_exporter | gNMI streaming (preferred) + SNMP polling (fallback) |
| Config Backup | Oxidized | Read-only configuration archival |
| Log Aggregation | Loki | Centralized syslog and log search |
| AI Assistant | Ollama | Local LLM for natural language queries |
| Reverse Proxy | Traefik | Automatic service discovery and TLS |
| Cloud Connectors | Custom (Python) | Read-only ingestion from Mist, etc. |

## Supported Vendors

MNM uses NAPALM for multi-vendor device communication. Tested platforms:

| Vendor | Status | Notes |
|--------|--------|-------|
| Juniper (Junos) | Primary test target | Excellent NAPALM support |
| Cisco (IOS/IOS-XE) | Tested | Strong NAPALM support |
| Fortinet (FortiOS) | In progress | Community NAPALM driver + direct API |

NAPALM supports [many additional platforms](https://napalm.readthedocs.io/en/latest/support/). If a device responds to SNMP or SSH, MNM can likely discover it.

## Project Status

MNM is in active early development. See [CLAUDE.md](CLAUDE.md) for architectural decisions, phasing plan, and development context.

| Phase | Status | Scope |
|-------|--------|-------|
| Phase 1 — Discovery | **In progress** | Nautobot, device onboarding, bootstrap |
| Phase 2 — Monitoring | Planned | Prometheus, Grafana, SNMP exporter |
| Phase 3 — AI Layer | Planned | Ollama, tool layer, MCP server, chat UI |
| Phase 4 — Connectors & Ops | Planned | Mist, Oxidized, Loki |

## Predecessor

MNM is a ground-up rewrite of [nts-server](https://github.com/commitconfirm/nts-server), a bash-driven Docker deployment of network monitoring tools (Netdisco, LibreNMS, Oxidized). MNM reimagines the concept with a modern stack, AI-native querying, and a self-contained architecture.

## Security

MNM collects sensitive network intelligence including device inventories, IP ranges, service fingerprints, SNMP data, LLDP topology, and credentials. This data, if compromised, could be used to map and attack the monitored network. Deploy MNM on trusted infrastructure with appropriate access controls. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#security-model) for the full security model.

## Contributing

Contributions welcome once the project reaches public release. For now, issues and ideas can be discussed via GitHub Issues.

## License

MIT License. See [LICENSE](LICENSE) for details.

## Built With

This project is developed with [Claude Code](https://claude.ai) following documented architectural decisions in [CLAUDE.md](CLAUDE.md).
