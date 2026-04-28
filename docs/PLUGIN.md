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

After `Endpoint` ships in E1:

- `/plugins/mnm/endpoints/` — list view with filtering, sortable
  columns, and pagination. Cross-links from `current_switch` to
  the Nautobot Device, from `current_port` to the Interface
  (using a cross-vendor naming helper), and from `current_ip` to
  the IPAddress when one matches.
- `/plugins/mnm/endpoints/<uuid>/` — detail view with the
  primary fields panel and an "All locations seen" history
  panel showing every `(switch, port, vlan)` row this MAC has
  been observed on.
- `/api/plugins/mnm/endpoints/` — REST API with filtering and
  pagination matching the list view.

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
   `bootstrap.sh`'s output. A WARNING line indicates the plugin
   isn't installed or migrations didn't run.
2. **HTTP smoke test:** `curl -sI
   http://localhost:8443/plugins/mnm/endpoints/` returns 200
   (after authentication via the Nautobot session cookie or
   API token) or 302 (redirecting to the login page).
3. **Migration introspection:**
   `docker exec mnm-nautobot nautobot-server showmigrations mnm_plugin`
   should list `[X] 0001_initial`.

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
