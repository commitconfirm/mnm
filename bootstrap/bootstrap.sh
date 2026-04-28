#!/usr/bin/env bash
# MNM Bootstrap Script
# Creates the Nautobot superuser and pre-populates sensible defaults so the user
# can immediately start onboarding devices without manual setup.
# Idempotent — safe to run multiple times.

set -euo pipefail

# Load .env from project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

CONTAINER="mnm-nautobot"
API_BASE="http://localhost:8080/api"
SUPERUSER_NAME="${MNM_ADMIN_USER:-mnm-admin}"
SUPERUSER_EMAIL="${MNM_ADMIN_EMAIL:-admin@example.com}"
SUPERUSER_PASSWORD="${MNM_ADMIN_PASSWORD:?MNM_ADMIN_PASSWORD must be set in .env}"

NAPALM_USER="${NAUTOBOT_NAPALM_USERNAME:-}"
NAPALM_PASS="${NAUTOBOT_NAPALM_PASSWORD:-}"

# Counters for summary (use temp files because api_create runs in subshells)
COUNTER_FILE=$(mktemp)
echo "0 0" > "$COUNTER_FILE"
trap 'rm -f "$COUNTER_FILE"' EXIT

inc_created() { read -r c s < "$COUNTER_FILE"; echo "$((c + 1)) $s" > "$COUNTER_FILE"; }
inc_skipped() { read -r c s < "$COUNTER_FILE"; echo "$c $((s + 1))" > "$COUNTER_FILE"; }

# ---------------------------------------------------------------------------
# Wait for Nautobot container to be healthy
# ---------------------------------------------------------------------------
echo "Waiting for $CONTAINER to be healthy..."

MAX_ATTEMPTS=30
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    HEALTH=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "not_found")

    if [ "$HEALTH" = "healthy" ]; then
        echo "$CONTAINER is healthy."
        break
    fi

    ATTEMPT=$((ATTEMPT + 1))
    WAIT=$((ATTEMPT < 5 ? 5 : 10))
    echo "  Attempt $ATTEMPT/$MAX_ATTEMPTS — status: $HEALTH — retrying in ${WAIT}s..."
    sleep $WAIT
done

if [ "$HEALTH" != "healthy" ]; then
    echo "ERROR: $CONTAINER did not become healthy after $MAX_ATTEMPTS attempts."
    exit 1
fi

# ---------------------------------------------------------------------------
# Create mnm_controller database (Phase 2.7 — controller storage)
# ---------------------------------------------------------------------------
echo ""
echo "--- Controller Database ---"
PG_CONTAINER="mnm-postgres"
PG_USER="${POSTGRES_USER:-nautobot}"
CTRL_DB="${MNM_CONTROLLER_DB:-mnm_controller}"

if docker exec "$PG_CONTAINER" psql -U "$PG_USER" -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw "$CTRL_DB"; then
    echo "  Database '$CTRL_DB': already exists (skipped)"
else
    if docker exec "$PG_CONTAINER" psql -U "$PG_USER" -c "CREATE DATABASE $CTRL_DB;" >/dev/null 2>&1; then
        echo "  Database '$CTRL_DB': created"
    else
        echo "  WARNING: failed to create database '$CTRL_DB' — controller will use JSON fallback"
    fi
fi

# ---------------------------------------------------------------------------
# Create superuser (idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "--- Superuser ---"
OUTPUT=$(docker exec -e DJANGO_SUPERUSER_PASSWORD="$SUPERUSER_PASSWORD" \
            "$CONTAINER" \
            nautobot-server createsuperuser --noinput \
                --username "$SUPERUSER_NAME" \
                --email "$SUPERUSER_EMAIL" 2>&1) || true

if echo "$OUTPUT" | grep -q "already taken"; then
    echo "  Superuser '$SUPERUSER_NAME': already exists (skipped)"
    inc_skipped
else
    echo "  Superuser '$SUPERUSER_NAME': created"
    inc_created
fi

# ---------------------------------------------------------------------------
# Create API token
# ---------------------------------------------------------------------------
echo ""
echo "--- API Token ---"
TOKEN_OUTPUT=$(docker exec "$CONTAINER" bash -c 'echo "
from nautobot.users.models import Token
from django.contrib.auth import get_user_model
User = get_user_model()
user = User.objects.get(username=\"'"${SUPERUSER_NAME}"'\")
token, created = Token.objects.get_or_create(user=user)
import sys; sys.stderr.write(token.key + chr(10))
" | nautobot-server nbshell' 2>&1)
# Extract the 40-char hex token from the mixed output
TOKEN=$(echo "$TOKEN_OUTPUT" | grep -oE '[0-9a-f]{40}')
echo "  API token ready"

# ---------------------------------------------------------------------------
# Fast skip if already fully initialized
# ---------------------------------------------------------------------------
if [ "${MNM_BOOTSTRAP_SKIP_CHECK:-}" != "1" ] && [ -n "$TOKEN" ]; then
    DT_CHECK=$(docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/dcim/device-types/?limit=1" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || DT_CHECK=0

    CF_CHECK=$(docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/extras/custom-fields/?name=endpoint_data_source&limit=1" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || CF_CHECK=0

    if [ "$DT_CHECK" -gt 5000 ] && [ "$CF_CHECK" -gt 0 ]; then
        echo ""
        echo "============================================"
        echo "  MNM already initialized"
        echo "  $DT_CHECK device types, custom fields present"
        echo "  Skipping. Set MNM_BOOTSTRAP_SKIP_CHECK=1 to force."
        echo "============================================"
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Helper: create or skip an object via the REST API
# Uses the container's localhost since we exec curl inside the container
# ---------------------------------------------------------------------------
api_create() {
    local endpoint="$1"
    local name="$2"
    local filter_field="${3:-name}"
    local payload="$4"
    local label="${5:-$name}"

    # Check if object already exists (URL-encode the name for query param)
    local encoded_name
    encoded_name=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${name}'))")
    local existing
    existing=$(docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/${endpoint}/?${filter_field}=${encoded_name}" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null) || existing='{"count":0}'

    local count
    count=$(echo "$existing" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || count=0

    if [ "$count" -gt 0 ]; then
        echo "  $label: already exists (skipped)" >&2
        inc_skipped
        # Return the existing object's ID (stdout only)
        echo "$existing" | python3 -c "import sys,json; print(json.load(sys.stdin)['results'][0]['id'])"
        return 0
    fi

    # Create the object
    local result
    result=$(docker exec "$CONTAINER" \
        curl -sf -X POST "${API_BASE}/${endpoint}/" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json" \
            -d "$payload" 2>&1)

    local obj_id
    obj_id=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null) || true

    if [ -n "$obj_id" ]; then
        echo "  $label: created" >&2
        inc_created
        echo "$obj_id"
    else
        echo "  $label: FAILED — $result" >&2
        return 1
    fi
}

# Helper to get an object's ID by name
api_get_id() {
    local endpoint="$1"
    local name="$2"
    local filter_field="${3:-name}"

    local encoded_name
    encoded_name=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${name}'))")

    docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/${endpoint}/?${filter_field}=${encoded_name}" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null \
    | python3 -c "import sys,json; r=json.load(sys.stdin)['results']; print(r[0]['id'] if r else '')"
}

# ---------------------------------------------------------------------------
# 1. Location Types
# ---------------------------------------------------------------------------
echo ""
echo "--- Location Types ---"

REGION_TYPE_ID=$(api_create "dcim/location-types" "Region" "name" \
    '{"name":"Region","description":"Geographic region","nestable":false}' \
    "Location Type 'Region'")

SITE_TYPE_ID=$(api_create "dcim/location-types" "Site" "name" \
    "{\"name\":\"Site\",\"description\":\"Physical site or facility\",\"nestable\":false,\"parent\":\"${REGION_TYPE_ID}\",\"content_types\":[\"dcim.device\"]}" \
    "Location Type 'Site'")

# ---------------------------------------------------------------------------
# 2. Locations
# ---------------------------------------------------------------------------
echo ""
echo "--- Locations ---"

# Get Active status ID
ACTIVE_STATUS_ID=$(api_get_id "extras/statuses" "Active")

DEFAULT_REGION_ID=$(api_create "dcim/locations" "Default Region" "name" \
    "{\"name\":\"Default Region\",\"location_type\":\"${REGION_TYPE_ID}\",\"status\":\"${ACTIVE_STATUS_ID}\"}" \
    "Location 'Default Region'")

DEFAULT_SITE_ID=$(api_create "dcim/locations" "Default Site" "name" \
    "{\"name\":\"Default Site\",\"location_type\":\"${SITE_TYPE_ID}\",\"status\":\"${ACTIVE_STATUS_ID}\",\"parent\":\"${DEFAULT_REGION_ID}\"}" \
    "Location 'Default Site'")

# ---------------------------------------------------------------------------
# 3. Device Roles
# ---------------------------------------------------------------------------
echo ""
echo "--- Device Roles ---"

for ROLE_NAME in "Router" "Switch" "Firewall" "Access Point" "Unknown"; do
    api_create "extras/roles" "$ROLE_NAME" "name" \
        "{\"name\":\"${ROLE_NAME}\",\"content_types\":[\"dcim.device\"]}" \
        "Role '${ROLE_NAME}'" >/dev/null
done

# ---------------------------------------------------------------------------
# 4. Manufacturers
# ---------------------------------------------------------------------------
echo ""
echo "--- Manufacturers ---"

for MFG_NAME in "Juniper" "Cisco" "Cisco Meraki" "Fortinet" "Arista" "Palo Alto Networks" "Aruba" "Extreme Networks" "MikroTik" "Ubiquiti" "Huawei"; do
    api_create "dcim/manufacturers" "$MFG_NAME" "name" \
        "{\"name\":\"${MFG_NAME}\"}" \
        "Manufacturer '${MFG_NAME}'" >/dev/null
done

# ---------------------------------------------------------------------------
# 4b. Platforms — NAPALM driver mappings for all supported vendors
# Every vendor must have a platform pre-loaded so onboarding never fails
# with "no specified NAPALM driver". The network_driver field is what the
# onboarding plugin uses to match SSH auto-detection results to a platform.
# ---------------------------------------------------------------------------
echo ""
echo "--- Platforms ---"

# Format: network_driver|napalm_driver|display_name|manufacturer
# Every NAPALM-supported vendor must be here so onboarding never fails.
# network_driver = what the onboarding plugin uses to find the platform
# napalm_driver = which NAPALM driver class to load (must be installed)
PLATFORMS=(
    # Juniper (built-in junos driver)
    "juniper_junos|junos|Juniper Junos|Juniper"
    # Cisco (built-in ios, nxos, iosxr drivers)
    # cisco_ios = classic IOS; cisco_iosxe = IOS-XE (separate Platform so the
    # MNM classifier's two-stage Cisco discrimination can target the right
    # network_driver). Both use the NAPALM ``ios`` driver — IOS-XE's CLI is
    # close enough that the ios driver works (lab-validated against c8000v
    # 17.16.1a). Discovered during Block C.5 live onboarding when the
    # community welcome-wizard library only ships ``cisco_ios``; without
    # this row, IOS-XE devices land with platform=null.
    "cisco_ios|ios|Cisco IOS|Cisco"
    "cisco_iosxe|ios|Cisco IOS-XE|Cisco"
    "cisco_nxos|nxos|Cisco NX-OS|Cisco"
    "cisco_nxos_ssh|nxos_ssh|Cisco NX-OS SSH|Cisco"
    "cisco_iosxr|iosxr|Cisco IOS-XR|Cisco"
    # Arista (built-in eos driver)
    "arista_eos|eos|Arista EOS|Arista"
    # Palo Alto (community napalm-panos)
    "paloalto_panos|panos|Palo Alto PAN-OS|Palo Alto Networks"
    # Fortinet (community napalm-fortios has NAPALM 5.x compat issues — use ios/SSH)
    "fortinet_fortios|ios|Fortinet FortiOS|Fortinet"
    # MikroTik (community napalm-ros has dep issues — use ios/SSH)
    "mikrotik_routeros|ios|MikroTik RouterOS|MikroTik"
    # Aruba / HPE (use ios driver — AOS-CX has similar CLI to Cisco)
    "aruba_aoscx|ios|Aruba AOS-CX|Aruba"
    # Extreme (use ios driver as best-effort)
    "extreme_exos|ios|Extreme EXOS|Extreme Networks"
    # Ubiquiti (EdgeOS — Vyatta-based, use ios driver as fallback)
    "ubiquiti_edgeos|ios|Ubiquiti EdgeOS|Ubiquiti"
    # Huawei (community napalm-ce has NAPALM 5.x compat issues — use ios/SSH)
    "huawei_vrp|ios|Huawei VRP|Huawei"
)

for entry in "${PLATFORMS[@]}"; do
    IFS='|' read -r NET_DRV NAPALM_DRV DISPLAY MFG_NAME <<< "$entry"
    MFG_ID=$(api_get_id "dcim/manufacturers" "$MFG_NAME" 2>/dev/null) || MFG_ID=""
    if [ -n "$MFG_ID" ]; then
        PAYLOAD="{\"name\":\"${NET_DRV}\",\"napalm_driver\":\"${NAPALM_DRV}\",\"network_driver\":\"${NET_DRV}\",\"manufacturer\":\"${MFG_ID}\"}"
    else
        PAYLOAD="{\"name\":\"${NET_DRV}\",\"napalm_driver\":\"${NAPALM_DRV}\",\"network_driver\":\"${NET_DRV}\"}"
    fi
    api_create "dcim/platforms" "$NET_DRV" "name" \
        "$PAYLOAD" \
        "Platform '${DISPLAY}' (${NET_DRV} → ${NAPALM_DRV})" >/dev/null
done

# ---------------------------------------------------------------------------
# 5. Devicetype Library Git Repository (for Welcome Wizard)
# ---------------------------------------------------------------------------
echo ""
echo "--- Git Repositories ---"

api_create "extras/git-repositories" "Devicetype Library" "name" \
    '{"name":"Devicetype Library","remote_url":"https://github.com/netbox-community/devicetype-library.git","branch":"master","provided_contents":["welcome_wizard.import_wizard"]}' \
    "Git Repo 'Devicetype Library'" >/dev/null

# ---------------------------------------------------------------------------
# 6. Bulk import manufacturers and device types from Welcome Wizard library
# ---------------------------------------------------------------------------
echo ""
echo "--- Device Type Library Import ---"

# Check if device types are already imported (idempotent skip)
DT_COUNT=$(docker exec "$CONTAINER" \
    curl -sf "${API_BASE}/dcim/device-types/?limit=1" \
        -H "Authorization: Token ${TOKEN}" \
        -H "Accept: application/json" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || DT_COUNT=0

if [ "$DT_COUNT" -gt 1000 ]; then
    echo "  $DT_COUNT device types already exist — skipping bulk import" >&2
else
    # Wait for the Git repo sync to complete (worker clones the repo).
    # On a fresh deploy the worker needs to run its own migrations first,
    # then clone ~100MB of YAML files, then parse them all. This can take
    # 5+ minutes. We trigger a sync explicitly and poll for completion.
    echo "  Waiting for Devicetype Library Git repo sync..."

    # Get the repo ID and trigger a sync (idempotent if already queued)
    REPO_ID=$(docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/extras/git-repositories/?name=Devicetype%20Library" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null \
        | python3 -c "import sys,json; r=json.load(sys.stdin)['results']; print(r[0]['id'] if r else '')" 2>/dev/null) || REPO_ID=""

    if [ -n "$REPO_ID" ]; then
        docker exec "$CONTAINER" \
            curl -sf -X POST "${API_BASE}/extras/git-repositories/${REPO_ID}/sync/" \
                -H "Authorization: Token ${TOKEN}" \
                -H "Accept: application/json" 2>/dev/null >/dev/null || true
    fi

    SYNC_ATTEMPTS=0
    MAX_SYNC_ATTEMPTS=60
    WIZARD_DT=0
    while [ $SYNC_ATTEMPTS -lt $MAX_SYNC_ATTEMPTS ]; do
        WIZARD_DT=$(docker exec "$CONTAINER" \
            curl -sf "${API_BASE}/plugins/welcome_wizard/devicetypeimport/?limit=1" \
                -H "Authorization: Token ${TOKEN}" \
                -H "Accept: application/json" 2>/dev/null \
            | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || WIZARD_DT=0

        if [ "$WIZARD_DT" -gt 0 ]; then
            echo "  Git repo synced — $WIZARD_DT device types available for import"
            break
        fi

        SYNC_ATTEMPTS=$((SYNC_ATTEMPTS + 1))
        echo "  Attempt $SYNC_ATTEMPTS/$MAX_SYNC_ATTEMPTS — waiting for sync (10s)..."
        sleep 10
    done

    if [ "$WIZARD_DT" -eq 0 ]; then
        echo "  WARNING: Git repo sync did not complete — skipping device type import"
        echo "  (Re-run bootstrap after the worker finishes syncing)"
    else
        # Copy and run the import script
        docker cp "$SCRIPT_DIR/import_devicetypes.py" "$CONTAINER:/tmp/import_devicetypes.py" 2>/dev/null
        echo "  Importing manufacturers and device types (this takes a few minutes)..."
        docker exec "$CONTAINER" bash -c 'nautobot-server shell < /tmp/import_devicetypes.py' 2>/dev/null

        # Read results
        IMPORT_RESULTS=$(docker exec "$CONTAINER" cat /tmp/import_results.json 2>/dev/null) || IMPORT_RESULTS='{}'
        MFG_CREATED=$(echo "$IMPORT_RESULTS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mfg_created',0))" 2>/dev/null) || MFG_CREATED=0
        MFG_SKIPPED=$(echo "$IMPORT_RESULTS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mfg_skipped',0))" 2>/dev/null) || MFG_SKIPPED=0
        DT_CREATED=$(echo "$IMPORT_RESULTS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('dt_created',0))" 2>/dev/null) || DT_CREATED=0
        DT_SKIPPED=$(echo "$IMPORT_RESULTS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('dt_skipped',0))" 2>/dev/null) || DT_SKIPPED=0
        DT_FAILED=$(echo "$IMPORT_RESULTS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('dt_failed',0))" 2>/dev/null) || DT_FAILED=0

        echo "  Manufacturers: $MFG_CREATED created, $MFG_SKIPPED skipped"
        echo "  Device Types:  $DT_CREATED created, $DT_SKIPPED skipped, $DT_FAILED failed"
        if [ "$DT_FAILED" -gt 0 ]; then
            echo "  (Failed types have invalid data in community library — not a problem)"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 6b. Lab-only virtual DeviceTypes not present in the community library
# ---------------------------------------------------------------------------
# The welcome-wizard / netbox-community devicetype-library does not ship
# definitions for several common virtual lab platforms. Without these rows
# pre-populated, onboarding a virtual device fails with a cryptic 400 from
# Step A because get_devicetype_by_model returns None. Block D3 closes the
# three known gaps (vEOS-lab, C8000V, cisco_iosxe Platform — see section
# 4b for the Platform). Operators adding new virtual devices should extend
# this section rather than creating DeviceTypes by hand each time.
#
# u_height=0 because these are virtual; physical specs (port count, etc.)
# come from the device-type templates Phase 2 walks via SNMP, so the
# DeviceType record itself only needs to satisfy the Nautobot foreign-key
# constraint.
echo ""
echo "--- Lab Virtual DeviceTypes ---"

# Format: model|manufacturer
LAB_DEVICETYPES=(
    # Arista vEOS-lab — virtual EOS image used in lab environments. Closes
    # the v0.9.x vEOS onboarding blocker discovered during Prompt 5.
    "vEOS-lab|Arista"
    # Cisco C8000V — virtual Catalyst 8000V (IOS-XE on x86). Lab-validated
    # at 17.16.1a during Block C.5; community library has C8000K (the
    # physical chassis) but not the virtual variant.
    "C8000V|Cisco"
)

for entry in "${LAB_DEVICETYPES[@]}"; do
    IFS='|' read -r MODEL MFG_NAME <<< "$entry"
    MFG_ID=$(api_get_id "dcim/manufacturers" "$MFG_NAME" 2>/dev/null) || MFG_ID=""
    if [ -z "$MFG_ID" ]; then
        echo "  DeviceType '${MODEL}': skipped — manufacturer '${MFG_NAME}' missing" >&2
        continue
    fi
    PAYLOAD="{\"model\":\"${MODEL}\",\"manufacturer\":\"${MFG_ID}\",\"u_height\":0}"
    api_create "dcim/device-types" "$MODEL" "model" \
        "$PAYLOAD" \
        "DeviceType '${MODEL}' (${MFG_NAME})" >/dev/null
done

# ---------------------------------------------------------------------------
# 7. Secrets and Secrets Group (only if NAPALM credentials are set)
# ---------------------------------------------------------------------------
echo ""
echo "--- Secrets ---"

if [ -n "$NAPALM_USER" ] && [ -n "$NAPALM_PASS" ]; then
    USERNAME_SECRET_ID=$(api_create "extras/secrets" "NAPALM Username" "name" \
        '{"name":"NAPALM Username","description":"NAPALM device username from env","provider":"environment-variable","parameters":{"variable":"NAUTOBOT_NAPALM_USERNAME"}}' \
        "Secret 'NAPALM Username'")

    PASSWORD_SECRET_ID=$(api_create "extras/secrets" "NAPALM Password" "name" \
        '{"name":"NAPALM Password","description":"NAPALM device password from env","provider":"environment-variable","parameters":{"variable":"NAUTOBOT_NAPALM_PASSWORD"}}' \
        "Secret 'NAPALM Password'")

    # Create Secrets Group
    SECRETS_GROUP_ID=$(api_create "extras/secrets-groups" "Default Credentials" "name" \
        '{"name":"Default Credentials","description":"Default NAPALM credentials for device onboarding"}' \
        "Secrets Group 'Default Credentials'")

    # Add associations (username + password) — check if they exist first
    for ASSOC in "username:${USERNAME_SECRET_ID}" "password:${PASSWORD_SECRET_ID}"; do
        SECRET_TYPE="${ASSOC%%:*}"
        SECRET_ID="${ASSOC##*:}"

        EXISTING=$(docker exec "$CONTAINER" \
            curl -sf "${API_BASE}/extras/secrets-groups-associations/?secrets_group=${SECRETS_GROUP_ID}&secret_type=${SECRET_TYPE}" \
                -H "Authorization: Token ${TOKEN}" \
                -H "Accept: application/json" 2>/dev/null) || EXISTING='{"count":0}'
        COUNT=$(echo "$EXISTING" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || COUNT=0

        if [ "$COUNT" -gt 0 ]; then
            echo "  Association '${SECRET_TYPE}': already exists (skipped)"
            inc_skipped
        else
            RESULT=$(docker exec "$CONTAINER" \
                curl -sf -X POST "${API_BASE}/extras/secrets-groups-associations/" \
                    -H "Authorization: Token ${TOKEN}" \
                    -H "Content-Type: application/json" \
                    -H "Accept: application/json" \
                    -d "{\"secrets_group\":\"${SECRETS_GROUP_ID}\",\"secret\":\"${SECRET_ID}\",\"access_type\":\"Generic\",\"secret_type\":\"${SECRET_TYPE}\"}" 2>&1)

            if echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" >/dev/null 2>&1; then
                echo "  Association '${SECRET_TYPE}': created"
                inc_created
            else
                echo "  Association '${SECRET_TYPE}': FAILED — $RESULT"
            fi
        fi
    done
else
    echo "  NAUTOBOT_NAPALM_USERNAME or NAUTOBOT_NAPALM_PASSWORD not set — skipping secrets"
    echo "  (Set them in .env and re-run to create default credentials)"
fi

# ---------------------------------------------------------------------------
# 8. Discovery custom fields on IP Address model
# ---------------------------------------------------------------------------
echo ""
echo "--- Discovery Custom Fields ---"

DISCOVERY_FIELDS=(
    "discovery_classification:text:Device classification from sweep"
    "discovery_ports_open:text:Comma-separated open ports"
    "discovery_mac_address:text:MAC address"
    "discovery_mac_vendor:text:MAC OUI manufacturer"
    "discovery_dns_name:text:Reverse DNS name"
    "discovery_snmp_sysname:text:SNMP sysName"
    "discovery_snmp_sysdescr:text:SNMP sysDescr"
    "discovery_snmp_syslocation:text:SNMP sysLocation"
    "discovery_first_seen:text:First time seen alive"
    "discovery_last_seen:text:Most recent time seen alive"
    "discovery_method:text:Discovery method (sweep/lldp/manual)"
    "discovery_banners:text:Service banners by port (JSON)"
    "discovery_http_headers:text:HTTP response headers (JSON)"
    "discovery_http_title:text:HTML title tag content"
    "discovery_tls_subject:text:TLS certificate subject CN"
    "discovery_tls_issuer:text:TLS certificate issuer"
    "discovery_tls_expiry:text:TLS certificate expiration date"
    "discovery_tls_sans:text:TLS Subject Alternative Names"
    "discovery_ssh_banner:text:SSH version banner"
    "endpoint_mac_address:text:MAC address from ARP table"
    "endpoint_mac_vendor:text:OUI vendor from infrastructure"
    "endpoint_switch:text:Switch where MAC was learned"
    "endpoint_port:text:Physical port on switch"
    "endpoint_vlan:text:VLAN ID"
    "endpoint_dhcp_hostname:text:DHCP client hostname"
    "endpoint_dhcp_server:text:DHCP server device"
    "endpoint_dhcp_lease_start:text:DHCP lease start time"
    "endpoint_dhcp_lease_expiry:text:DHCP lease expiry time"
    "endpoint_data_source:text:Data source (sweep/infrastructure/both)"
)

for FIELD_DEF in "${DISCOVERY_FIELDS[@]}"; do
    FIELD_NAME="${FIELD_DEF%%:*}"
    REMAINDER="${FIELD_DEF#*:}"
    FIELD_TYPE="${REMAINDER%%:*}"
    FIELD_LABEL="${REMAINDER#*:}"

    # Check if custom field exists
    ENCODED_NAME=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${FIELD_NAME}'))")
    EXISTING=$(docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/extras/custom-fields/?name=${ENCODED_NAME}" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null) || EXISTING='{"count":0}'
    COUNT=$(echo "$EXISTING" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || COUNT=0

    if [ "$COUNT" -gt 0 ]; then
        echo "  Custom field '${FIELD_NAME}': already exists (skipped)" >&2
        inc_skipped
    else
        RESULT=$(docker exec "$CONTAINER" \
            curl -sf -X POST "${API_BASE}/extras/custom-fields/" \
                -H "Authorization: Token ${TOKEN}" \
                -H "Content-Type: application/json" \
                -H "Accept: application/json" \
                -d "{\"name\":\"${FIELD_NAME}\",\"label\":\"${FIELD_LABEL}\",\"type\":\"${FIELD_TYPE}\",\"content_types\":[\"ipam.ipaddress\"],\"filter_logic\":\"loose\"}" 2>&1)

        if echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" >/dev/null 2>&1; then
            echo "  Custom field '${FIELD_NAME}': created" >&2
            inc_created
        else
            echo "  Custom field '${FIELD_NAME}': FAILED — $RESULT" >&2
        fi
    fi
done

# ---------------------------------------------------------------------------
# Section 6c — verify mnm-plugin migrations applied
# ---------------------------------------------------------------------------
# v1.0 Block E adds a Nautobot Django app (mnm-plugin) providing
# the Endpoint, ARP, MAC, LLDP, Route, BGP, and Fingerprint
# models. Plugin migrations run as part of Nautobot's startup.
# This section asserts they actually applied — catches the case
# where Nautobot started without picking up the plugin (config
# error, install failure during build, etc.).
echo ""
echo "Verifying mnm_plugin migrations..." >&2
PLUGIN_MIGRATIONS=$(docker exec "$CONTAINER" \
    nautobot-server showmigrations mnm_plugin 2>/dev/null | \
    grep -c '\[X\]' || true)

if [ "${PLUGIN_MIGRATIONS:-0}" -lt 1 ]; then
    echo "  WARNING: mnm_plugin migrations not detected." >&2
    echo "  Run: docker exec ${CONTAINER} nautobot-server migrate mnm_plugin" >&2
    echo "  If the plugin isn't installed, rebuild nautobot:" >&2
    echo "    docker compose build nautobot && docker compose up -d --force-recreate nautobot" >&2
else
    echo "  mnm_plugin: ${PLUGIN_MIGRATIONS} migration(s) applied" >&2
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo "  MNM Bootstrap Complete"
echo "============================================"
read -r CREATED SKIPPED < "$COUNTER_FILE"
echo "  Created: $CREATED objects"
echo "  Skipped: $SKIPPED objects (already existed)"
echo ""
echo "  Nautobot URL:  http://localhost:8443"
echo "  Username:      $SUPERUSER_NAME"
echo "  Password:      $SUPERUSER_PASSWORD"
echo ""
echo "  Next steps:"
echo "    1. Log in to Nautobot"
echo "    2. Go to Jobs > Sync Devices From Network"
echo "    3. Enter a device IP, select 'Default Site'"
if [ -n "$NAPALM_USER" ] && [ -n "$NAPALM_PASS" ]; then
echo "    4. Select 'Default Credentials' and run the job"
else
echo "    4. Create credentials in Secrets, then run the job"
fi
echo "============================================"
