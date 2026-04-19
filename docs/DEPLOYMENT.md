# MNM Deployment Guide

## Audience

Network engineers deploying MNM on a fresh Linux host. This guide assumes familiarity with networking concepts (SNMP, LLDP, ARP, VLANs) but not necessarily with Docker, Python, or REST APIs. Every command in this guide was run verbatim during v0.9.0 installation validation on a clean Ubuntu 24.04 VM.

---

## Prerequisites

### Hardware / VM

Validated on: Ubuntu 24.04 LTS, 8 vCPUs, 32 GB RAM, 127 GB disk.

**Minimum (testing and evaluation):**
- 4 vCPUs
- 16 GB RAM
- 50 GB disk

**Recommended (small-to-medium production deployment):**
- 8 vCPUs
- 32 GB RAM
- 160 GB disk (Prometheus retains metrics for 365 days by default; larger environments generate more time-series data)

**Network requirements:**
- Reachability to devices you want to monitor (ICMP, TCP/22, TCP/161, TCP/830 as applicable)
- No internet access required after initial Docker image pull

### OS

Ubuntu 24.04 LTS. Other Linux distributions that support Docker should work but are untested.

### Required packages

Git is the only Linux package required that isn't Docker itself. On Ubuntu 24.04 it's already installed. Verify:

```bash
git --version
# git version 2.43.0
```

If missing:
```bash
sudo apt-get install -y git
```

---

## Phase 1 — Install Docker

MNM requires Docker Engine and the Compose plugin. Do not use the `docker.io` package from Ubuntu's default repositories — it is outdated. Use Docker's official repository.

```bash
# Install prerequisites
sudo apt-get update
sudo apt-get install -y ca-certificates curl

# Add Docker's GPG key
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) \
  signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine and Compose plugin
sudo apt-get update
sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
```

**Versions validated:** Docker 29.4.0, Docker Compose v5.1.3.

Verify:
```bash
docker --version
docker compose version
```

### Allow your user to run Docker without sudo

```bash
sudo usermod -aG docker $USER
```

Log out and back in (or open a new SSH session) for the group change to take effect. Verify:

```bash
docker ps
# Should return an empty table, not a permission error
```

---

## Phase 2 — Clone and Configure

### Clone

```bash
git clone https://github.com/commitconfirm/mnm.git
cd mnm
```

To deploy a specific release:

```bash
git checkout v0.9.0
git describe --tags
# v0.9.0
```

### Create your `.env`

```bash
cp .env.example .env
```

Open `.env` in your editor. The required fields are documented below. Optional fields that aren't set are disabled — you can add them later.

#### Required fields

| Variable | What it does | What to set |
|----------|-------------|------------|
| `MNM_TIMEZONE` | System timezone for timestamps | Your local timezone, e.g. `America/New_York` |
| `MNM_ADMIN_USER` | Nautobot superuser login name | e.g. `mnm-admin` |
| `MNM_ADMIN_PASSWORD` | Nautobot + controller dashboard password | A strong password — **required, cannot be blank** |
| `MNM_ADMIN_EMAIL` | Nautobot superuser email | Any valid email format |
| `NAUTOBOT_SECRET_KEY` | Django cryptographic secret | A 50+ character random string — **required, cannot be blank** |
| `POSTGRES_PASSWORD` | PostgreSQL password | A strong password |
| `REDIS_PASSWORD` | Redis password | A strong password |

Generate a secret key:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

> **Important:** Use distinct passwords for each field in production. Reusing passwords between `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, and `MNM_ADMIN_PASSWORD` creates unnecessary blast radius if any one is compromised.

#### Monitoring fields (set these if you have SNMP-monitored devices)

| Variable | What it does | What to set |
|----------|-------------|------------|
| `SNMP_COMMUNITY` | SNMP read-only community string | Your network's RO community string |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin login | A password for Grafana |

#### Device collection fields (set these to onboard network devices)

| Variable | What it does | What to set |
|----------|-------------|------------|
| `NAUTOBOT_NAPALM_USERNAME` | SSH/NETCONF username for network devices | Read-only service account |
| `NAUTOBOT_NAPALM_PASSWORD` | SSH/NETCONF password | Service account password |

If you don't set these now, the bootstrap will skip creating a default credential set and print a reminder. You can re-run bootstrap after adding them.

#### Optional fields

| Variable | Default | Notes |
|----------|---------|-------|
| `MNM_DOMAIN` | `localhost` | Hostname of the MNM host — used in log messages |
| `PROMETHEUS_RETENTION_DAYS` | 365 | How long Prometheus keeps metrics |
| `MNM_RETENTION_DAYS` | 365 | How long the controller keeps endpoint history |
| `MNM_SWEEP_CONCURRENCY` | 50 | Max concurrent TCP probes during a sweep |
| `PROXMOX_HOST` | *(unset)* | Enable Proxmox connector, e.g. `https://198.51.100.10:8006` |
| `PROXMOX_TOKEN_ID` | *(unset)* | Proxmox API token, e.g. `mnm-monitor@pve!mnm-token` |
| `PROXMOX_TOKEN_SECRET` | *(unset)* | Proxmox token secret UUID |

See [CONFIGURATION.md](CONFIGURATION.md) for the full variable reference.

---

## Phase 3 — Start the Stack

From the repo root:

```bash
docker compose up -d
```

This creates and starts 11 containers. The command returns quickly — containers start in the background. Watch startup progress:

```bash
docker compose ps
```

**Expected startup sequence and timing:**

1. `mnm-postgres`, `mnm-redis` start first (~30 seconds to healthy)
2. `mnm-prometheus`, `mnm-snmp-exporter`, `mnm-gnmic`, `mnm-traefik`, `mnm-grafana` start next (most are healthy within 1–2 minutes)
3. `mnm-nautobot` starts after postgres and redis are healthy — **takes 4–5 minutes** on first boot while Django runs migrations
4. `mnm-nautobot-worker` and `mnm-nautobot-scheduler` start after nautobot is healthy (~1 minute more)
5. `mnm-controller` starts last (~1 minute after nautobot-worker)

**Total time from `docker compose up -d` to all containers running: 6–7 minutes.**

Wait until nautobot is healthy before running bootstrap:

```bash
# Wait for nautobot to be healthy (runs once, exits when healthy)
until [ "$(docker inspect --format='{{.State.Health.Status}}' mnm-nautobot)" = "healthy" ]; do
  echo "$(date +%H:%M:%S) waiting for nautobot..."
  sleep 15
done
echo "nautobot is healthy"
```

### Expected final state

```
NAME                     STATUS
mnm-controller           Up X minutes (healthy)
mnm-gnmic                Up X minutes
mnm-grafana              Up X minutes (healthy)
mnm-nautobot             Up X minutes (healthy)
mnm-nautobot-scheduler   Up X minutes (healthy)
mnm-nautobot-worker      Up X minutes (healthy)
mnm-postgres             Up X minutes (healthy)
mnm-prometheus           Up X minutes (healthy)
mnm-redis                Up X minutes (healthy)
mnm-snmp-exporter        Up X minutes (healthy)
mnm-traefik              Up X minutes
```

`mnm-gnmic` and `mnm-traefik` show no health status — this is expected. They don't have Docker healthchecks configured but are functioning normally.

---

## Phase 4 — Run Bootstrap

Bootstrap creates the Nautobot superuser, pre-loads all reference data (manufacturers, platforms, device roles, location types, 5,200+ community device types), and initializes the controller's PostgreSQL database.

```bash
bash bootstrap/bootstrap.sh
```

**This takes approximately 9–10 minutes**, most of which is waiting for the community device type library to sync from GitHub and import 5,000+ records.

Expected output summary:
```
--- Device Type Library Import ---
  Git repo synced — 5510 device types available for import
  Importing manufacturers and device types (this takes a few minutes)...
  Manufacturers: 271 created, 8 skipped
  Device Types:  5320 created, 0 skipped, 190 failed
  (Failed types have invalid data in community library — not a problem)

============================================
  MNM Bootstrap Complete
============================================
  Created: 64 objects
  Skipped: 0 objects (already existed)
```

The 190 failed device types are entries in the community library with schema validation errors — they don't affect MNM's ability to identify devices. The 5,320+ successful imports cover all common vendors.

Bootstrap is idempotent. Safe to re-run. If you add NAPALM credentials to `.env` later, re-running bootstrap will create the credential set.

### Restart the controller after bootstrap

**This step is required.** The controller starts before bootstrap creates its database. If you don't restart it, the controller runs in a degraded JSON fallback mode and polling features won't work.

```bash
docker restart mnm-controller
```

Wait for it to come back healthy:

```bash
until [ "$(docker inspect --format='{{.State.Health.Status}}' mnm-controller)" = "healthy" ]; do
  sleep 5
done
echo "controller ready"
```

Then verify the health endpoint shows `db_connected: true`:

```bash
curl -s http://localhost:9090/api/health | python3 -m json.tool
# "db_connected": true
```

---

## Phase 5 — Verify Services

All services are accessible from the host. Service URLs use the machine's IP or hostname:

| Service | URL | Notes |
|---------|-----|-------|
| **MNM Controller** | `http://<host>:9090` | Start here — primary operator UI |
| Nautobot | `http://<host>:8443` | Source-of-truth inventory |
| Grafana | `http://<host>:8080/grafana/` | Dashboards in the `MNM` folder |
| Prometheus | `http://<host>:8080/prometheus/` | Raw metrics, target status |

Quick smoke test from the host:

```bash
# Controller — expect 200
curl -s -o /dev/null -w "%{http_code}" http://localhost:9090/

# Nautobot — expect 302 (redirect to login)
curl -s -o /dev/null -w "%{http_code}" http://localhost:8443/

# Grafana — expect 200
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/grafana/

# Prometheus — expect 302 (redirect to /query)
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/prometheus/
```

### Nautobot login

- URL: `http://<host>:8443`
- Username: value of `MNM_ADMIN_USER` from your `.env`
- Password: value of `MNM_ADMIN_PASSWORD` from your `.env`

### Controller dashboard login

- URL: `http://<host>:9090`
- Password: value of `MNM_ADMIN_PASSWORD` from your `.env`

---

## Phase 6 — First Sweep

The Discovery page at `http://<host>:9090` → **Operations → Discovery** is where you configure and run sweeps.

To verify the sweep pipeline works end-to-end before pointing it at real devices:

1. Log in to the controller dashboard
2. In the **Discovery** page, enter a CIDR range and click **Start Sweep**
3. For a no-device smoke test, use `198.51.100.0/24` (RFC 5737 documentation range — guaranteed empty)
4. The progress tracker updates in real time. A /24 sweep completes in under 30 seconds when no hosts respond
5. Sweep history at the bottom of the page shows results

**Expected result on an empty range:** `total: 254, alive: 0` — all /24 addresses probed, none alive.

To run via the API directly:

```bash
# Get location ID from Nautobot
LOCATION_ID=$(curl -s -u <user>:<pass> http://localhost:8443/api/dcim/locations/?depth=1 \
  | python3 -c "import sys,json; locs=json.load(sys.stdin)['results']; \
  site=[l for l in locs if l['location_type']['display']=='Region → Site']; \
  print(site[0]['id'] if site else 'no-site')")

# Trigger sweep (no credentials needed for sweep-only, no onboarding will occur)
curl -s -X POST http://localhost:9090/api/discover/sweep \
  -H "Content-Type: application/json" \
  -H "Cookie: session=<your session cookie>" \
  -d "{\"cidr_ranges\":[\"198.51.100.0/24\"],\"location_id\":\"$LOCATION_ID\",\"secrets_group_id\":\"\"}"
```

For API authentication, log in first:

```bash
curl -c /tmp/mnm-cookies.txt -X POST http://localhost:9090/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password":"<your MNM_ADMIN_PASSWORD>"}'
# {"status":"ok"}
```

Then use `-b /tmp/mnm-cookies.txt` on subsequent API calls.

---

## Next Steps

After a successful installation:

1. **Configure device credentials** — Add your network device SSH/NETCONF credentials to `.env` (`NAUTOBOT_NAPALM_USERNAME`, `NAUTOBOT_NAPALM_PASSWORD`), re-run `bootstrap/bootstrap.sh` to create the credential set in Nautobot
2. **Run a real sweep** — Add a real CIDR range on the Discovery page, select the credential set, start a sweep
3. **Review LLDP advisories** — After onboarding seed devices, the dashboard advisory cards show newly discovered LLDP neighbors for your review
4. **Configure Proxmox** (optional) — Set `PROXMOX_HOST`, `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET` in `.env`, recreate the controller container: `docker compose up -d --force-recreate controller`

See [DISCOVERY.md](DISCOVERY.md) for the sweep pipeline in detail, and [CONNECTORS.md](CONNECTORS.md) for the Proxmox connector setup.

---

## Troubleshooting

### Controller shows `db_connected: false`

The controller started before bootstrap created its database. Run bootstrap, then restart:

```bash
bash bootstrap/bootstrap.sh
docker restart mnm-controller
```

### `nautobot-worker` or `nautobot-scheduler` crash-looping

Both depend on nautobot being healthy. If they're restarting, check that nautobot itself is healthy first. Usually resolves once nautobot finishes first-boot migrations.

### Bootstrap fails with "MNM_ADMIN_PASSWORD must be set"

`MNM_ADMIN_PASSWORD` is not optional. Open `.env` and set it before running bootstrap again.

### Bootstrap hangs at "Waiting for Devicetype Library Git repo sync..."

The device type library sync requires the nautobot container to reach GitHub. If you're on an isolated network, this step will wait until it times out (up to 10 minutes), then skip. Bootstrap will continue — you'll have 0 community device types. On air-gapped deployments, pre-seed the device type library manually.

### Controller `/api/health` shows `containers_healthy: 9` when all 11 are running

`mnm-gnmic` and `mnm-traefik` don't have Docker healthchecks configured. The controller only counts containers with a `(healthy)` status. All 11 containers running is the expected state — 9 healthy + 2 running-without-healthcheck.

### Nautobot not accessible at `http://<host>:8443`

Check if the container is healthy: `docker inspect --format='{{.State.Health.Status}}' mnm-nautobot`. If still starting, give it a few more minutes. If unhealthy, check logs: `docker logs mnm-nautobot 2>&1 | tail -50`.

### Slow request warnings flooding controller logs

The controller's health endpoint (`/api/health`) calls multiple downstream services and consistently takes 1–2 seconds. This is a known issue in v0.9.0. The `slow_request` warnings are cosmetic — they don't affect functionality but make it harder to spot real errors. See [v0.9-install-issues.md](v0.9-install-issues.md).

### Changing `.env` values after first deploy

Docker Compose bakes `.env` into containers at creation time — `docker compose restart` does NOT pick up `.env` changes. After changing any value:

```bash
docker compose up -d --force-recreate <service-name>
```

For password changes affecting multiple services (e.g., `POSTGRES_PASSWORD`), recreate all affected containers. See [CONFIGURATION.md](CONFIGURATION.md) for which variables affect which services.
