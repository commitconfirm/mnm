# MNM Nautobot Plugin (`mnm-plugin`)

The MNM Nautobot plugin (`mnm-plugin`) adds the data models
Nautobot lacks for network-state inspection — Endpoint, ARP
entry, MAC entry, LLDP neighbor, route, BGP neighbor, and
fingerprint records — and exposes them as first-class Nautobot
views.

This plugin is part of MNM's "Documentation Is a Primary Output"
architectural rule (`CLAUDE.md` Rule 12). The MNM controller
collects data; the plugin makes Nautobot the operator-facing
surface for inspecting that data.

## Status (v1.0)

Block E in the MNM v1.0 roadmap. Implementation lands in six
prompts:

| # | Scope | Status |
|---|---|---|
| E1 | Plugin scaffold + `Endpoint` model + cross-vendor naming helper | ✅ shipped |
| E2 | `ArpEntry`, `MacEntry`, `LldpNeighbor` models | 🚧 |
| E3 | `Route`, `BgpNeighbor`, `Fingerprint` models | 🚧 |
| E4 | Detail views + cross-system Recent Events panel | 🚧 |
| E5 | `dcim.Interface` detail extension | 🚧 |
| E6 | Filter framework + saved filters + export | 🚧 |

See `mnm-dev-claude:design/mnm_plugin_design.md` for the
architectural design.

## What the plugin provides

After E1 + E2 ship, the plugin surfaces four models as
Nautobot-native list and detail views, all reachable from a
top-level **MNM** navigation tab.

### Endpoint (E1)

- `/plugins/mnm/endpoints/` — list view with filtering, sortable
  columns, pagination. Cross-links from `current_switch` to the
  Nautobot Device, from `current_port` to the Interface (via
  the cross-vendor naming helper), and from `current_ip` to the
  IPAddress.
- `/plugins/mnm/endpoints/<uuid>/` — detail view with the
  primary-fields panel, an "All locations seen" history panel
  (every `(switch, port, vlan)` row this MAC has been observed
  on), and an "All IPs ever seen" panel.
- `/api/plugins/mnm/endpoints/` — REST API.

### ARP entries (E2)

- `/plugins/mnm/arp-entries/` — per-node ARP table snapshots
  (one row per `(node, ip, mac, vrf)` tuple). Source: SNMP
  `ipNetToMediaTable` walked by `controller/app/arp_snmp.py`.
- `/plugins/mnm/arp-entries/<uuid>/` — detail view with an
  "Other ARP entries for this IP" sidebar surfacing other MAC
  bindings the network has recorded for the same address (e.g.,
  the same IP on multiple VRFs or a recent MAC change).
- `/api/plugins/mnm/arp-entries/` — REST API.

### MAC entries (E2)

- `/plugins/mnm/mac-entries/` — per-node MAC/FDB snapshots
  (`(node, mac, interface, vlan)` tuples). The `Type` column
  renders a green **Static** chip for administratively-set
  entries (Junos `entry_status` of `self`/`mgmt` per Block C
  P4 remap) and a grey **Dynamic** chip for everything else.
- `/plugins/mnm/mac-entries/<uuid>/` — detail with "Other
  locations for this MAC on the same node" sidebar (catches
  MACs that roam within one switch).
- `/api/plugins/mnm/mac-entries/` — REST API.

### LLDP neighbors (E2)

- `/plugins/mnm/lldp-neighbors/` — per-node LLDP neighbor
  snapshots (`(node, local_interface, remote_system_name,
  remote_port)` tuples). Default columns show the minimal
  identity set; the column-chooser unlocks the five Block C P2
  expansion fields (local ifIndex/ifName, chassis-ID subtype,
  port-ID subtype, remote sysDescr).
- `/plugins/mnm/lldp-neighbors/<uuid>/` — detail with "Other
  neighbors on the same local interface" sidebar (rare in
  well-formed networks; useful for daisy-chained phones).
- `/api/plugins/mnm/lldp-neighbors/` — REST API.

### Routes (E3)

- `/plugins/mnm/routes/` — per-node routing table snapshots
  (`(node, prefix, next_hop, vrf)` tuples). Source: NAPALM
  `get_route_to` Tier 2 in v1.0; the SNMP-routes collector is a
  v1.1 workstream. Default columns hide `metric` /
  `preference` / `outgoing_interface` (toggle via column
  chooser). `protocol` renders as a color-coded chip
  (BGP=blue, OSPF/IS-IS=info, static=warning, connected/local=
  green). FortiGate (NAPALM-fortios broken) and vEOS (NAPALM
  via Nautobot proxy fragile) have
  `device_polls.routes.enabled = False` per the Block C
  close-out; their rows stay empty until v1.1 SNMP-routes.
- `/plugins/mnm/routes/<uuid>/` — detail with "Same prefix on
  other nodes" sidebar (surfaces ECMP fan-out and
  cross-node visibility).
- `/api/plugins/mnm/routes/` — REST API.

### BGP neighbors (E3)

- `/plugins/mnm/bgp-neighbors/` — per-node BGP neighbor
  snapshots (`(node, neighbor_ip, vrf, address_family)`
  tuples). Source: NAPALM `get_bgp_neighbors_detail` Tier 2 in
  v1.0; v1.1 SNMP-BGP. `state` renders as a chip — green for
  Established/Up, red for Idle/Active/Down/Connect/OpenSent/
  OpenConfirm, grey for Unknown. `uptime_seconds` renders
  humanized as `Nd Nh` / `Nh Nm` / `Nm Ns`. Per the Block C
  close-out, FortiGate and vEOS have
  `device_polls.bgp.enabled = False` — their rows stay empty
  until v1.1 SNMP-BGP.
- `/plugins/mnm/bgp-neighbors/<uuid>/` — detail with "Other
  neighbors on this node" sidebar.
- `/api/plugins/mnm/bgp-neighbors/` — REST API.

### Fingerprints (E3, schema-only in v1.0)

- `/plugins/mnm/fingerprints/` — identity fingerprints per
  endpoint MAC. **Schema is in place; no signal collection is
  wired in v1.0.** The list view renders an empty-state
  callout pointing at the v1.1 fingerprinting workstream
  until rows land. Each `(target_mac, signal_type,
  signal_value)` tuple is unique; `seen_count` increments on
  conflict (the "I've seen this signal again" semantic).
  Cross-host correlation ("same device, moved") falls out
  naturally from the schema once collectors land — see the
  detail view's "Same signal value on other MACs" panel.
- `/plugins/mnm/fingerprints/<uuid>/` — detail with
  cross-MAC and cross-signal correlation sidebars.
- `/api/plugins/mnm/fingerprints/` — REST API.

### Sentinel rendering

Any `interface` / `local_interface` value matching `ifindex:N`
indicates the controller's bridge-port → ifIndex resolution
failed during collection (Block C P3/P4/P5 fallback). Such
values render with a yellow warning badge and a tooltip; data
is preserved per Rule 7 but no link to a Nautobot Interface is
possible.

## Prerequisites

A running MNM stack from this repository's
`docker-compose.yml`. Nautobot 3.0 is required; the plugin
pins to `>=3.0,<3.1` per the v1.0 stability discipline.

## Installation

Installed automatically when you bring up the MNM stack. The
`nautobot/Dockerfile` `pip install`s the plugin during the
container build, and `nautobot/nautobot_config.py` registers
`mnm_plugin` in `PLUGINS`.

After `docker compose up -d`, plugin migrations run alongside
Nautobot's standard migrations. Bootstrap (`bootstrap/bootstrap.sh`)
verifies the migrations applied and prints a confirmation line.

## Verification

After the stack comes up:

1. **Bootstrap output:** look for the line
   `mnm_plugin: <N> migration(s) applied` near the end of
   `bootstrap.sh`'s output. After E3, expect `3 migration(s)
   applied`. A WARNING line indicates the plugin isn't installed
   or migrations didn't run.
2. **HTTP smoke test:** all seven model list views serve HTTP
   200 (or 302 redirect to login) for the unauthenticated path:
   - `/plugins/mnm/endpoints/`
   - `/plugins/mnm/arp-entries/`
   - `/plugins/mnm/mac-entries/`
   - `/plugins/mnm/lldp-neighbors/`
   - `/plugins/mnm/routes/`
   - `/plugins/mnm/bgp-neighbors/`
   - `/plugins/mnm/fingerprints/`
3. **Migration introspection:**
   `docker exec mnm-nautobot nautobot-server showmigrations mnm_plugin`
   should list `[X] 0001_initial`, `[X] 0002_arp_mac_lldp`,
   and `[X] 0003_route_bgp_fingerprint`.

## Permissions

- View permissions on plugin models are granted to all
  authenticated Nautobot users by default per E0 §7 Q3
  ("authenticated-all"). Operators with stricter requirements
  can tighten via Nautobot's standard RBAC.
- Add / change / delete permissions are restricted to admin
  users in v1.0. Plugin data is written by the controller's
  service account via the controller-side `plugin_writer.py` —
  not via the plugin's UI.

## Cross-vendor interface naming

The plugin stores interface names exactly as the controller's
SNMP collectors write them (vendor-native form: Junos
slot/port, Junos logical-unit, Arista numeric, Fortinet alias,
Cisco short, Cisco long). To resolve these consistently to
Nautobot `dcim.Interface` records, the plugin uses
`mnm_plugin.utils.interface.get_interface()`, which tries
multiple candidate forms in order:

1. Literal match
2. Normalized (logical-unit-stripped, Cisco short → long)
3. Plain logical-unit-stripped form
4. Cisco short → long expansion

Sentinel values like `ifindex:7` (produced by SNMP collectors
when bridge-port → ifIndex resolution fails) render with a
styled badge and a tooltip ("ifindex resolution failed; raw
bridge port shown"). They never link to a Nautobot Interface.

If a vendor naming form not handled by the helper surfaces
(plugin views show "no Nautobot interface match" for legitimate
ports), file an issue with the form name and an example. The
helper's test file
(`nautobot-plugin/mnm_plugin/tests/test_utils_interface.py`)
is the contract — extending the helper means adding the test.

## How data lands in the plugin

Per E0 §5 (controller-to-plugin write path), the MNM controller
writes plugin rows directly to Nautobot's Postgres database
using SQLAlchemy with reflection. There is no HTTP API mediation
in v1.0 — operationally simple, and the plugin schema co-versions
with the controller in this same repo.

The polling pipeline (`controller/app/polling.py
::_correlate_and_record`) writes endpoints to **two** locations
each cycle:

1. The controller's `mnm_controller.endpoints` table
   (authoritative for v1.0 — operational pages depend on it).
2. The plugin's `mnm_plugin_endpoint` table (the mirror; what
   you see in `/plugins/mnm/endpoints/`).

Plugin write failures are logged and swallowed — the polling
cycle continues regardless. This is the **two-tier write
discipline** of E0 §5d.

## Interface detail extension

Every Nautobot `dcim.Interface` detail page (e.g.,
`/dcim/interfaces/<uuid>/`) renders four inline MNM panels in
the right column:

- **Endpoints currently on this port** — active rows from the
  plugin's `Endpoint` table, sorted by `last_seen` descending.
- **Endpoints historically on this port** — 90-day window;
  includes active rows by design so the operator sees the
  full timeline. If the 90-day window is empty but older rows
  exist, a footer link reads "Show endpoints beyond 90 days →".
- **LLDP neighbors on this port** — rows from `LldpNeighbor`
  keyed on `(node_name, local_interface)`, sorted by
  `collected_at` descending.
- **MAC entries on this port** — rows from `MacEntry` keyed on
  `(node_name, interface)`, sorted by `collected_at`
  descending.

Each panel paginates to the 25 most-recent rows. A "Show all"
footer link drops the operator into the relevant model's
existing list view (E1+E2+E3) filtered by `(device,
interface)`.

The cross-vendor naming helper
(`mnm_plugin.utils.interface.expand_for_lookup`) drives every
panel's query. A Nautobot `dcim.Interface.name` like
`ge-0/0/0` expands to candidates `["ge-0/0/0", "ge-0/0/0.0"]`
so plugin rows stored under either form (depending on which
collection path produced them) are caught. Examples:

| Nautobot interface name | Storage candidates |
|---|---|
| `ge-0/0/0` | `ge-0/0/0`, `ge-0/0/0.0` |
| `ge-0/0/0.0` | `ge-0/0/0.0`, `ge-0/0/0` |
| `Gi1` | `Gi1`, `GigabitEthernet1` |
| `GigabitEthernet1` | `GigabitEthernet1`, `Gi1` |
| `GigabitEthernet1.100` | `GigabitEthernet1.100`, `GigabitEthernet1`, `Gi1.100`, `Gi1` |
| `Ethernet1` | `Ethernet1` |
| `wan` | `wan` |
| `irb.140` | `irb.140` (logical interface — no expansion) |
| `ifindex:7` | `ifindex:7` (sentinel passthrough) |

The panels are **fail-soft**. Each panel's ORM query runs
inside a `try/except BLE001` wrapper; if one panel raises, an
error notice replaces that panel's body and the host
Interface detail page continues rendering normally. This
mirrors the controller↔plugin write path's degraded-mode
posture documented elsewhere in this guide.

The rendering uses Nautobot's `TemplateExtension.right_page()`
hook in `mnm_plugin/template_content.py`. No tab — operators
see the panels inline alongside Nautobot's native interface
metadata (cables, IP addresses, etc.) per the locked design
decision in E0 §7 Q5.

## Detail-page panels

Each model's detail view (`/plugins/mnm/<model>/<pk>/`) renders
the row's own fields plus three classes of context panel
introduced in v1.0 Block E4. The panels are read-only and never
trigger upstream queries — every panel except Recent Events
(Endpoint only) is a single indexed query against the plugin's
own Postgres tables.

**Cross-row history.** A "Prior observations" panel on every
detail view lists prior rows for the same logical record,
filtered on the model's `unique_together` key with the current
row's PK excluded. Use cases per model:

- **Endpoint** — every other `(switch, port, vlan)` ever seen
  for this MAC, including inactive history.
- **ArpEntry** — prior observations of the same `(node, ip,
  mac, vrf)` quadruple. Mostly empty in steady state because
  upserts replace the row in place; populates when interface
  changes (MAC moves between physical interfaces on the same
  node).
- **MacEntry** — re-observations of the same MAC on the same
  `(node, interface, vlan)`.
- **LldpNeighbor** — prior observations of the same neighbor on
  the same `(node, local_interface, remote_system_name,
  remote_port)`. Surfaces neighbor turnover.
- **Route** — next-hop changes / route convergence churn on the
  same `(node, prefix, next_hop, vrf)`.
- **BgpNeighbor** — state-flap history (Established → Idle →
  Established) on the same `(node, neighbor_ip, vrf,
  address_family)`.
- **Fingerprint** — repeat observations of the same `(target_mac,
  signal_type, signal_value)`. Mostly empty by design — v1.1
  collectors increment `seen_count` in place rather than insert.

**Cross-model identity panels.** MAC-keyed lookups across the
plugin's tables. Each panel renders a small table with the most
relevant 4-5 columns, paginated to 25 rows, with a "Show all"
link to the related model's list view filtered by the same MAC:

- **Endpoint detail** — sibling panels for "MAC table
  observations", "ARP observations", and "Fingerprint signals"
  for the same MAC.
- **ArpEntry detail** — "Endpoint records for this MAC" and
  "MAC table observations" for the same MAC.
- **MacEntry detail** — "Endpoint records for this MAC" and
  "ARP observations" for the same MAC.
- **LldpNeighbor detail** — `remote_system_name` resolves to a
  Nautobot `dcim.Device` link when one matches; the
  `remote_port` resolves to a `dcim.Interface` link via the
  cross-vendor naming helper. Renders inline in the primary
  fields block, not as a separate panel.
- **Fingerprint detail** — "Endpoint records for this MAC".

`mac_address` on Endpoint, `mac` on ArpEntry / MacEntry,
`target_mac` on Fingerprint, and `remote_system_name` on
LldpNeighbor are all indexed (E1 / E2 / E3 migrations); these
queries hit indexes.

**Recent Events read-through.** The Endpoint detail view
additionally surfaces a "Recent events" panel powered by a
cross-system query to the controller's
`/api/endpoints/{mac}/history` endpoint. This is the **only**
cross-process query in the v1.0 plugin — every other panel
reads the plugin's own database.

The read-through is deliberately fail-soft. The panel renders
one of three states based on the controller's response:

- **Controller unavailable** — the controller didn't respond
  within the 2-second timeout, returned a non-2xx status, or the
  response was malformed. The panel renders
  `Controller unavailable; recent events temporarily inaccessible.`
  and the rest of the page renders normally. WARN-deduplicated
  in the Nautobot log so a controller outage doesn't flood the
  log with one line per detail-page hit.
- **No recent events** — the controller responded successfully
  but had no events for this MAC. The panel renders
  `No recent events for this MAC.`
- **Populated** — the panel renders a table of timestamp /
  event type chip / source / description. Description is derived
  from the controller's event payload (e.g., `appeared` →
  "First seen on switch X port Y (IP Z)"; `moved_port` →
  "Moved from port X to port Y on switch Z").

The client caches each MAC's response for 30 seconds, so two
operators viewing the same Endpoint within that window share one
HTTP call to the controller. Auth uses the same
`NAUTOBOT_SECRET_KEY`-signed token scheme the controller's
operational UI uses; no new shared secret is configured.

If you see "Controller unavailable" persistently, check that
`mnm-controller` is running and reachable on the
`mnm-network` Docker bridge:

```
docker ps --filter name=mnm-controller
docker exec mnm-nautobot \
    curl -s -o /dev/null -w "%{http_code}\n" \
    http://mnm-controller:9090/api/health
```

A `200` response confirms the read-through path is healthy at
the network layer; a `401` means the plugin's token wasn't
recognized (typically `NAUTOBOT_SECRET_KEY` mismatch between
nautobot and controller containers — verify both pulled from
the same `.env`).

## Troubleshooting

### Plugin doesn't load

Check Nautobot's logs for plugin-import errors:

```
docker logs mnm-nautobot 2>&1 | grep -i "mnm_plugin\|plugin" | head -30
```

If `mnm_plugin` isn't found, rebuild the Nautobot container:

```
docker compose build nautobot
docker compose up -d --force-recreate nautobot nautobot-worker nautobot-scheduler
```

### Endpoint list is empty

Three causes, in order of likelihood:

1. **Polling hasn't run yet.** Endpoints land after the next
   ARP/MAC/DHCP poll cycle (default: 5-minute intervals). Watch
   `docker logs mnm-controller` for `plugin_endpoint_upsert`
   events — those confirm the plugin write path is live.
2. **Plugin migrations didn't run.** Check
   `bootstrap.sh` output for the section 6c verification line,
   or run `docker exec mnm-nautobot nautobot-server
   showmigrations mnm_plugin` directly.
3. **Plugin reflection failed.** The controller's
   `plugin_writer.py` reflects the plugin table on first use.
   If the connection to Nautobot's Postgres failed, you'll see
   a `plugin_reflection_failed` warning in controller logs.
   Subsequent polling cycles silently skip plugin writes until
   the next process restart.

### Sentinel badges everywhere

The cross-vendor naming helper is conservative — it only
expands the forms in its test matrix. A vendor like HPE
Procurve or Dell that uses a naming form not yet in the helper
will surface as "no Nautobot interface match" for legitimate
ports.

If you see this, file an issue with the form name (e.g.,
`Trk1`, `1/g1`) and a representative `ifName` value from the
device. The helper extends in the same change as the test file
— see `nautobot-plugin/mnm_plugin/tests/test_utils_interface.py`.

## Permissions reference

| Action | Default RBAC |
|---|---|
| View Endpoint records | All authenticated users |
| Create / edit / delete | Admin only |
| Access REST API endpoints | All authenticated users (read), admin (write) |

Tighten via Nautobot's standard "Permissions" UI under
**Admin → Users / Groups → Permissions**.

## Architecture references

- **Design doc:** `mnm-dev-claude:design/mnm_plugin_design.md`
  (private repo; the architectural memory)
- **Roadmap:** `CHANGELOG.md` and `mnm-dev-claude:CLAUDE.md`
  Block E entries
- **Source:** `nautobot-plugin/` in this repo
- **Controller-side write path:** `controller/app/plugin_writer.py`
