# Connectors

A **connector** is a read-only client for an external system. It collects
inventory and metrics on a schedule, optionally feeds MAC-keyed records into
the controller's endpoint store, and exposes Prometheus metrics for Grafana.

> **Inviolable Rule 1 applies.** Connectors only ever GET. They never write
> configuration changes back to the remote system. See [the project rules in
> CLAUDE.md](../.claude/CLAUDE.md) (operator-only doc).

The Proxmox VE connector is the first concrete example of this pattern.
Future connectors (Mist, Meraki, FortiCloud) will follow the same shape:

| Layer | Where it lives |
|------|----------------|
| API client | `controller/app/connectors/<vendor>.py` |
| State + scheduler | module-level state, `scheduled_loop()` coroutine |
| API endpoints | `/api/<vendor>/status`, `/api/<vendor>/metrics`, `/api/<vendor>/collect` (in `main.py`) |
| Endpoint store integration | `endpoint_store.upsert_endpoint(..., source="<vendor>")` |
| Prometheus | `render_metrics()` returns exposition format; scrape job in `config/prometheus/prometheus.yml` |
| Grafana | `config/grafana/dashboards/<vendor>-overview.json` |

---

## Proxmox VE

The Proxmox connector collects:

- **Hypervisor nodes** — CPU, memory, uptime, kernel, PVE version
- **VMs (qemu)** — status, CPU, memory, disk I/O, network I/O, MAC addresses,
  bridge/VLAN attachments
- **Containers (LXC)** — same fields as VMs
- **Storage pools** — used / total / available bytes per pool
- **ZFS pools** — size, allocated, free, fragmentation, health, dedup ratio
- **Physical disks** — model, serial, size, type (SSD/HDD/NVMe), SMART health

Every VM and container with a MAC address in its config becomes an entry in
the controller's `endpoints` table with `data_source="proxmox"`. If the same
MAC also shows up in switch ARP/MAC tables (infrastructure collection), the
endpoint's `data_source` is upgraded to `both` — corroborating evidence that
this VM is actually reachable on the network and where.

### Setting up the API token in Proxmox

The connector authenticates with a Proxmox API token. Tokens are tied to a
user and inherit that user's permissions, scoped to a role.

1. **Create a dedicated user** (Datacenter → Permissions → Users):
   - User name: `mnm-monitor`
   - Realm: `pve` (or your preferred realm)
2. **Create the API token** (Datacenter → Permissions → API Tokens):
   - User: `mnm-monitor@pve`
   - Token ID: `mnm-token`
   - Privilege Separation: **enabled** (recommended — the token can have
     fewer permissions than the user)
   - Save the secret value — it is shown only once.
3. **Grant read-only access** (Datacenter → Permissions → Add → API Token Permission):
   - Path: `/`
   - API Token: `mnm-monitor@pve!mnm-token`
   - Role: **PVEAuditor** (built-in read-only role)
   - Propagate: ✅
4. **Verify** by curl:
   ```bash
   curl -k -H 'Authorization: PVEAPIToken=mnm-monitor@pve!mnm-token=<secret>' \
        https://pve.example.com:8006/api2/json/nodes
   ```

> ### ⚠ Privilege Separation gotcha
>
> When **Privilege Separation** is enabled on a token (the Proxmox default for
> new tokens), the token does **not** inherit its parent user's permissions —
> it has its own ACL, which starts empty. Granting PVEAuditor to the user
> `mnm-monitor@pve` is not enough; you must grant it to the **token**
> `mnm-monitor@pve!mnm-token` directly. Without this the connector reaches
> the API but every collection call returns
> `Permission check failed (/, Sys.Audit)` and `/api2/json/access/permissions`
> returns `{}`.
>
> The dashboard card will show this as a red banner with a fix link, but you
> can also detect it from the CLI:
> ```bash
> # Should return permission entries; an empty {} means the token has no ACL.
> curl -k -H 'Authorization: PVEAPIToken=mnm-monitor@pve!mnm-token=<secret>' \
>      https://pve.example.com:8006/api2/json/access/permissions
> ```
>
> **Fix from the Proxmox shell:**
> ```bash
> pveum acl modify / --tokens 'mnm-monitor@pve!mnm-token' --roles PVEAuditor
> ```
> Equivalent to step 3 above performed in the Web UI, but explicitly targeting
> the token rather than the user. No controller restart is required — the
> next 5-minute collection cycle will pick up the new permissions.

See the [Proxmox API token documentation](https://pve.proxmox.com/pve-docs/pveum.1.html#pveum_tokens)
for full details.

### Environment variables

| Variable | Default | Description |
|---------|---------|-------------|
| `PROXMOX_HOST` | (unset) | Base URL, e.g. `https://192.0.2.10:8006`. The connector is disabled when this is empty. |
| `PROXMOX_TOKEN_ID` | (unset) | Full token ID: `user@realm!tokenid` |
| `PROXMOX_TOKEN_SECRET` | (unset) | The UUID secret printed once when the token was created |
| `PROXMOX_VERIFY_SSL` | `false` | Set to `true` if you have a real cert installed on the Proxmox API |
| `PROXMOX_INTERVAL_SECONDS` | `300` | Scheduled collection interval (5 minutes by default) |

### What appears in the UI

- **Dashboard card** — node count, VM count, container count, last
  collection timestamp, per-node CPU/memory/uptime table, ZFS storage usage
  summary, and a red alert banner if any pool or disk is unhealthy.
- **Endpoints table** — VMs and containers appear with classification
  `virtual_machine` / `container`, switch = node name, port = bridge,
  VLAN = tag from the net config.
- **Grafana → MNM - Proxmox Overview** — full dashboard with node health,
  VM inventory, top VMs by CPU/memory, ZFS pool usage with thresholds,
  pool health table, ZFS growth over time, and physical disk health.

### ZFS storage monitoring

The connector pulls ZFS pool data from `/api2/json/nodes/{node}/disks/zfs`,
storage usage from `/api2/json/nodes/{node}/storage`, and physical disk SMART
health from `/api2/json/nodes/{node}/disks/list`.

| Metric | Meaning |
|--------|--------|
| `mnm_proxmox_zfs_pool_size_bytes` | Total raw size of the pool |
| `mnm_proxmox_zfs_pool_used_bytes` | Currently allocated bytes |
| `mnm_proxmox_zfs_pool_free_bytes` | Free bytes |
| `mnm_proxmox_zfs_pool_fragmentation_percent` | Fragmentation (informational) |
| `mnm_proxmox_zfs_pool_health` | `1` if pool reports `ONLINE`, `0` otherwise |
| `mnm_proxmox_zfs_pool_dedup_ratio` | Deduplication ratio (1.0 = no dedup) |
| `mnm_proxmox_disk_size_bytes` | Disk capacity |
| `mnm_proxmox_disk_health` | `1` if SMART reports `PASSED`, `0` if failing |

The Grafana dashboard color-codes pool usage with thresholds (green <70%,
yellow 70–85%, red >85%) and renders pool/disk health as red/green cells.
Because Prometheus retains data for 365 days (see [CONFIGURATION.md](CONFIGURATION.md)),
the "ZFS Pool Used Bytes Over Time" panel is the right tool to predict when
to buy more disks.

> **MNM does not manage ZFS.** It does not run scrubs, take snapshots,
> import/export pools, or attempt repairs. It only monitors. See
> [ZFS administration](https://openzfs.github.io/openzfs-docs/) for the
> management side.

### Manual API endpoints

| Endpoint | Auth | Purpose |
|---------|------|---------|
| `GET /api/proxmox/status` | yes | Returns the latest snapshot for the dashboard card and inventory tables |
| `POST /api/proxmox/collect` | yes | Manually trigger an out-of-cycle collection run |
| `GET /api/proxmox/metrics` | **no** | Prometheus exposition for in-cluster scrape (same access model as Nautobot's `/metrics`) |
