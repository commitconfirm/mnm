# Monitoring

MNM uses Prometheus for metric storage and Grafana for visualization.

```
Network Devices
    │
    ├── SNMP (universal) ──► SNMP Exporter ──► Prometheus ──► Grafana
    │                                               │
    └── gNMI (modern) ──────► gnmic ───────────────►│
                                                     │
                                                     ▼
                                              365-day retention
                                              10 GB size cap
```

## Components

**Prometheus** — central metric store, scrapes SNMP exporter and gnmic every 30-60 seconds. Retention defaults to 365 days with a 10 GB size cap. Accessible at `:8080/prometheus/`. See [Prometheus documentation](https://prometheus.io/docs/).

**Grafana** — dashboard UI with two pre-built dashboards. Anonymous read-only access enabled (appliance model). Accessible at `:8080/grafana/` or `:3000` direct. See [Grafana documentation](https://grafana.com/docs/).

**SNMP Exporter** — Prometheus-native SNMP poller. Polls IF-MIB (interface counters, status, errors) from configured devices. Read-only: SNMP GET/WALK only. See [snmp_exporter documentation](https://github.com/prometheus/snmp_exporter).

**gnmic** — gNMI streaming telemetry collector. Subscribes to OpenConfig paths (interface counters, oper-status, system state). Ready when devices have gNMI enabled. See [gnmic documentation](https://gnmic.openconfig.net/).

## Dashboards

**Device Dashboard** (home) — per-device view with hostname dropdown. Shows interface count, up/down status, uptime, inbound/outbound traffic rates, errors, discards, and interface status table.

**Network Overview** — fleet-wide view showing device count, total interfaces, top talkers by traffic, and error rates across all monitored devices.

**Proxmox Overview** — populated by the [Proxmox connector](CONNECTORS.md#proxmox-ve). Hypervisor node CPU/memory, top VMs by CPU/memory, full VM inventory table, ZFS pool usage with thresholds, ZFS pool health, ZFS growth over time, and physical disk SMART health. Only renders data when `PROXMOX_HOST` is configured.

## Adding Devices to Monitoring

SNMP targets are defined in `config/prometheus/prometheus.yml` under the `snmp_exporter` job. Each target needs:
- IP address
- `device_name` label (for Grafana dropdown)

Example:
```yaml
static_configs:
  - targets: ["192.0.2.5"]
    labels:
      device_name: "switch-01"
```

After editing, restart Prometheus: `docker compose restart prometheus`

## Enabling gNMI on Devices

gNMI targets are configured in `config/gnmic/gnmic.yml`. MNM subscribes in read-only mode (subscribe, never SetRequest).

**Juniper Junos** — enable gNMI with:
```
set system services extension-service request-response grpc ssl address 0.0.0.0 port 32767
```
See [Junos gNMI documentation](https://www.juniper.net/documentation/us/en/software/junos/grpc-network-services/).

**Cisco IOS-XE** — see [IOS-XE gNMI configuration](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/prog/configuration/1710/b_1710_programmability_cg/gnmi.html).

## Prometheus Retention

Default: 365 days with 10 GB size cap. Configurable via `PROMETHEUS_RETENTION_DAYS` in `.env`. The size cap prevents disk exhaustion if device count grows beyond estimates. Prometheus automatically removes oldest data when either limit is reached.
