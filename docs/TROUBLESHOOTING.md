# Troubleshooting

Issues organized by symptom.

## Device Onboarding

### "show configuration interfaces" times out
**Symptom:** "Sync Devices From Network" job fails with `Pattern not detected` error on `show configuration interfaces | display json`.

**Cause:** Netmiko's `read_timeout` was too short for large Junos configs, or the user lacks permission.

**Fix:** MNM patches `read_timeout` to 120s and sets `read_timeout_override: 120` in nornir config. If still failing, the NAPALM user needs `view configuration` permission on Junos — standard read-only users cannot view configuration data.

### "No authentication methods available"
**Symptom:** "Sync Network Data" fails with paramiko SSHException.

**Cause:** The nornir `CredentialsEnvVars` plugin reads `NAPALM_USERNAME` / `NAPALM_PASSWORD` (no `NAUTOBOT_` prefix). These weren't set.

**Fix:** Both `NAUTOBOT_NAPALM_*` and `NAPALM_*` env vars must be set in `docker-compose.yml`. They're mapped from the same `.env` values.

### tcp_ping crashes with int(None)
**Symptom:** "Sync Network Data" fails with `TypeError: int() argument must be a string... not 'NoneType'` on tcp_ping.

**Cause:** `NautobotORMInventory` doesn't set `Host.port`, so `task.host.port` is None.

**Fix:** MNM patches `tcp_ping(task.host.hostname, task.host.port or 22)` in the Dockerfile.

### Juniper SRX series SSH connection timeout
**Symptom:** Onboarding fails with `'No existing session' error: try increasing 'conn_timeout'`.

**Cause:** Default 10s SSH timeout too short for slower devices.

**Fix:** MNM sets `conn_timeout: 30` in nornir connection_options.

### Jobs stuck in "Pending" state
**Symptom:** Nautobot Jobs page shows jobs as "Pending" indefinitely.

**Cause:** The Celery worker was down or restarting when the job was submitted.

**Fix:** Delete stuck job results via API or Nautobot admin UI, then restart the worker: `docker compose restart nautobot-worker`.

## Grafana

### Dashboards show "No data"
**Symptom:** Grafana panels are empty even though Prometheus has data.

**Possible causes:**
1. Missing `refId` on panel targets (Grafana 12 requires it)
2. Datasource UID mismatch (empty UID doesn't resolve to default in Grafana 12)
3. Template variable not loading (needs `query` object with `qryType: 1`)

**Fix:** MNM's provisioned dashboards include all required fields. If editing dashboards manually, ensure every target has `"refId": "A"` and datasource uses `"uid": "mnm-prometheus"`.

### Device dropdown is blank
**Symptom:** The Device Dashboard shows no devices in the dropdown.

**Cause:** Grafana 12 changed how template variables work. The provisioned dashboard needs both `definition` and `query` fields.

**Fix:** Dashboard JSON includes both formats. Clear browser cache (Ctrl+Shift+R).

## Traefik

### Traefik v3 Docker API error
**Symptom:** Traefik logs show `client version 1.24 is too old`.

**Cause:** Traefik v3.3/v3.4 has a Docker API negotiation bug.

**Fix:** MNM uses Traefik v2.11 which works correctly.

## Container Health

### Checking container status
```bash
docker compose ps
```

### Viewing container logs
```bash
docker compose logs <service> --tail 50
```

### Restarting a service
```bash
docker compose restart <service>
```

### Full stack restart
```bash
docker compose down && docker compose up -d
```

### Bootstrap re-run (idempotent)
```bash
./bootstrap/bootstrap.sh
```

## Performance

### Endpoint collection is slow
**Symptom:** Collection takes >30 seconds for a few devices.

**Possible causes:**
1. SNMP not responding — collector falls back to NAPALM proxy (15-30s per device)
2. SNMP community string mismatch
3. Firewall blocking SNMP (UDP 161)

**Fix:** Ensure `SNMP_COMMUNITY` in `.env` matches your device SNMP community. Verify SNMP is reachable: `snmpwalk -v2c -c <community> <device-ip> 1.3.6.1.2.1.1.5.0`

### Discovery sweep is slow on large ranges
**Symptom:** Sweep of a /24 takes >60 seconds.

**Possible causes:**
1. Too many concurrent probes overwhelming the network
2. Many alive hosts with slow SNMP responses
3. Banner grabbing timeouts on unresponsive services

**Fix:** Tune `MNM_SWEEP_CONCURRENCY` (default 50). Reduce if network is constrained. The sweep logs show per-host timing — look for outliers.

### Bootstrap runs every time
**Symptom:** Bootstrap takes minutes on every `docker compose up`.

**Fix:** Bootstrap auto-skips when >5000 device types and custom fields exist. If it's still running, check if custom fields are being deleted between runs.
