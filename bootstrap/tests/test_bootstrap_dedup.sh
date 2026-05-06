#!/usr/bin/env bash
# Tier 1 test for the F3 manufacturer dedup pass in bootstrap/bootstrap.sh.
#
# Creates synthetic manufacturer records and a DeviceType via the live
# Nautobot API, runs the same dedup logic, asserts outcomes, and cleans up.
# Validates: happy-path (reassign + delete), idempotency (skip when already
# absent), and canonical-missing edge case (skip when canonical not found).
#
# Requirements: Nautobot container healthy, bootstrap.sh already run once
# (superuser + token exist), .env in project root.
#
# Usage (from project root):
#   bash bootstrap/tests/test_bootstrap_dedup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

CONTAINER="mnm-nautobot"
API_BASE="http://localhost:8080/api"
SUPERUSER_NAME="${MNM_ADMIN_USER:-mnm-admin}"
SUPERUSER_PASSWORD="${MNM_ADMIN_PASSWORD:?MNM_ADMIN_PASSWORD must be set in .env}"

echo "=== F3 Manufacturer Dedup — Tier 1 Test ==="
echo ""

# ---------------------------------------------------------------------------
# Get API token (same method as bootstrap.sh)
# ---------------------------------------------------------------------------
TOKEN_OUTPUT=$(docker exec "$CONTAINER" bash -c 'echo "
from nautobot.users.models import Token
from django.contrib.auth import get_user_model
User = get_user_model()
user = User.objects.get(username=\"'"${SUPERUSER_NAME}"'\")
token, created = Token.objects.get_or_create(user=user)
import sys; sys.stderr.write(token.key + chr(10))
" | nautobot-server nbshell' 2>&1)
TOKEN=$(echo "$TOKEN_OUTPUT" | grep -oE '[0-9a-f]{40}')
if [ -z "$TOKEN" ]; then
    echo "FAIL: Could not obtain API token — is the Nautobot container healthy?" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Cleanup on exit
# ---------------------------------------------------------------------------
_CLEANUP_MFG_IDS=()
_CLEANUP_DT_IDS=()

_cleanup() {
    echo ""
    echo "--- Cleanup ---"
    for _dt_id in "${_CLEANUP_DT_IDS[@]:-}"; do
        [ -z "$_dt_id" ] && continue
        docker exec "$CONTAINER" \
            curl -sf -X DELETE "${API_BASE}/dcim/device-types/${_dt_id}/" \
                -H "Authorization: Token ${TOKEN}" 2>/dev/null >/dev/null || true
    done
    for _mfg_id in "${_CLEANUP_MFG_IDS[@]:-}"; do
        [ -z "$_mfg_id" ] && continue
        docker exec "$CONTAINER" \
            curl -sf -X DELETE "${API_BASE}/dcim/manufacturers/${_mfg_id}/" \
                -H "Authorization: Token ${TOKEN}" 2>/dev/null >/dev/null || true
    done
    echo "  Done."
}
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------
_PASS=0
_FAIL=0

_assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS: $desc"
        _PASS=$((_PASS + 1))
    else
        echo "  FAIL: $desc — expected='$expected' got='$actual'"
        _FAIL=$((_FAIL + 1))
    fi
}

_assert_empty() {
    local desc="$1" actual="$2"
    if [ -z "$actual" ]; then
        echo "  PASS: $desc (empty as expected)"
        _PASS=$((_PASS + 1))
    else
        echo "  FAIL: $desc — expected empty, got '$actual'"
        _FAIL=$((_FAIL + 1))
    fi
}

_assert_nonempty() {
    local desc="$1" actual="$2"
    if [ -n "$actual" ]; then
        echo "  PASS: $desc (non-empty)"
        _PASS=$((_PASS + 1))
    else
        echo "  FAIL: $desc — expected non-empty, got empty"
        _FAIL=$((_FAIL + 1))
    fi
}

# ---------------------------------------------------------------------------
# Local API helpers
# ---------------------------------------------------------------------------
_api_get_id() {
    local endpoint="$1" name="$2"
    local encoded
    encoded=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${name}'))")
    docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/${endpoint}/?name=${encoded}" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null \
    | python3 -c "
import sys, json
r = json.load(sys.stdin)['results']
print(r[0]['id'] if r else '')
" 2>/dev/null || echo ""
}

_api_post() {
    local endpoint="$1" payload="$2"
    docker exec "$CONTAINER" \
        curl -sf -X POST "${API_BASE}/${endpoint}/" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json" \
            -d "$payload" 2>/dev/null \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('id', ''))
" 2>/dev/null || echo ""
}

_dt_count_for_mfg_name() {
    local mfg_name="$1"
    local enc
    enc=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${mfg_name}'))")
    docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/dcim/device-types/?manufacturer=${enc}&limit=1" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null \
    | python3 -c "import sys, json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null || echo "0"
}

# ---------------------------------------------------------------------------
# Dedup function (mirrors bootstrap.sh section 6a verbatim)
# ---------------------------------------------------------------------------
_dedup_manufacturer() {
    local dup_name="$1"
    local can_name="$2"

    local dup_id
    dup_id=$(_api_get_id "dcim/manufacturers" "$dup_name")
    local can_id
    can_id=$(_api_get_id "dcim/manufacturers" "$can_name")

    if [ -z "$dup_id" ]; then
        echo "  Dedup '$dup_name' → '$can_name': duplicate not present (skipped)"
        return 0
    fi
    if [ -z "$can_id" ]; then
        echo "  Dedup '$dup_name' → '$can_name': canonical '$can_name' not found — skipping"
        return 0
    fi
    if [ "$dup_id" = "$can_id" ]; then
        echo "  Dedup '$dup_name' → '$can_name': same record (skipped)"
        return 0
    fi

    local dup_name_enc
    dup_name_enc=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${dup_name}'))")

    local reassigned=0
    local limit=50
    local offset=0
    while true; do
        local page
        page=$(docker exec "$CONTAINER" \
            curl -sf "${API_BASE}/dcim/device-types/?manufacturer=${dup_name_enc}&limit=${limit}&offset=${offset}" \
                -H "Authorization: Token ${TOKEN}" \
                -H "Accept: application/json" 2>/dev/null) || page='{"count":0,"results":[]}'

        local ids
        ids=$(echo "$page" | python3 -c "
import sys, json
for r in json.load(sys.stdin).get('results', []):
    print(r['id'])
" 2>/dev/null) || ids=""
        [ -z "$ids" ] && break

        while IFS= read -r dt_id; do
            [ -z "$dt_id" ] && continue
            docker exec "$CONTAINER" \
                curl -sf -X PATCH "${API_BASE}/dcim/device-types/${dt_id}/" \
                    -H "Authorization: Token ${TOKEN}" \
                    -H "Content-Type: application/json" \
                    -H "Accept: application/json" \
                    -d "{\"manufacturer\":\"${can_id}\"}" 2>/dev/null >/dev/null || true
            reassigned=$((reassigned + 1))
        done <<< "$ids"

        local page_count
        page_count=$(echo "$page" | python3 -c "
import sys, json
print(len(json.load(sys.stdin).get('results', [])))
" 2>/dev/null) || page_count=0
        [ "$page_count" -lt "$limit" ] && break
        offset=$((offset + limit))
    done

    local remaining
    remaining=$(docker exec "$CONTAINER" \
        curl -sf "${API_BASE}/dcim/device-types/?manufacturer=${dup_name_enc}&limit=1" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null \
        | python3 -c "import sys, json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null) || remaining=1

    if [ "${remaining:-1}" -gt 0 ]; then
        echo "  Dedup '$dup_name': ${remaining} DeviceType(s) still attached — NOT deleting"
        return 1
    fi

    docker exec "$CONTAINER" \
        curl -sf -X DELETE "${API_BASE}/dcim/manufacturers/${dup_id}/" \
            -H "Authorization: Token ${TOKEN}" \
            -H "Accept: application/json" 2>/dev/null >/dev/null || true

    if [ "$reassigned" -gt 0 ]; then
        echo "  Dedup '$dup_name' → '$can_name': $reassigned DeviceType(s) reassigned, duplicate deleted"
    else
        echo "  Dedup '$dup_name' → '$can_name': no DeviceTypes to reassign, duplicate deleted"
    fi
}

# ===========================================================================
# Test 1 — Happy path: duplicate + canonical exist, DeviceType reassigned
# ===========================================================================
echo "--- Test 1: Happy path ---"

_CAN_ID=$(_api_post "dcim/manufacturers" '{"name":"TestF3Canon"}')
_assert_nonempty "TestF3Canon created" "$_CAN_ID"
_CLEANUP_MFG_IDS+=("$_CAN_ID")

_DUP_ID=$(_api_post "dcim/manufacturers" '{"name":"TestF3Dup"}')
_assert_nonempty "TestF3Dup created" "$_DUP_ID"
_CLEANUP_MFG_IDS+=("$_DUP_ID")

_DT_ID=$(_api_post "dcim/device-types" "{\"model\":\"TestF3Model\",\"manufacturer\":\"${_DUP_ID}\"}")
_assert_nonempty "TestF3Model DeviceType created under duplicate" "$_DT_ID"
_CLEANUP_DT_IDS+=("$_DT_ID")

_assert_eq "DeviceType count under duplicate before dedup" "1" "$(_dt_count_for_mfg_name "TestF3Dup")"
_assert_eq "DeviceType count under canonical before dedup" "0" "$(_dt_count_for_mfg_name "TestF3Canon")"

_dedup_manufacturer "TestF3Dup" "TestF3Canon"

_assert_empty "Duplicate manufacturer deleted" "$(_api_get_id "dcim/manufacturers" "TestF3Dup")"
_assert_eq "DeviceType count under canonical after dedup" "1" "$(_dt_count_for_mfg_name "TestF3Canon")"

# ===========================================================================
# Test 2 — Idempotency: running again when duplicate is already absent
# ===========================================================================
echo ""
echo "--- Test 2: Idempotency ---"

_IDEM_OUT=$(_dedup_manufacturer "TestF3Dup" "TestF3Canon" 2>&1 || true)
_assert_eq "Idempotent skip message" \
    "  Dedup 'TestF3Dup' → 'TestF3Canon': duplicate not present (skipped)" \
    "$_IDEM_OUT"

# ===========================================================================
# Test 3 — Canonical missing: duplicate not deleted when canonical absent
# ===========================================================================
echo ""
echo "--- Test 3: Canonical missing ---"

_DUP2_ID=$(_api_post "dcim/manufacturers" '{"name":"TestF3Dup2"}')
_assert_nonempty "TestF3Dup2 created" "$_DUP2_ID"
_CLEANUP_MFG_IDS+=("$_DUP2_ID")

_dedup_manufacturer "TestF3Dup2" "TestF3NonExistent" || true

_assert_nonempty "Duplicate not deleted when canonical absent" \
    "$(_api_get_id "dcim/manufacturers" "TestF3Dup2")"

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "============================================"
if [ "$_FAIL" -eq 0 ]; then
    echo "  RESULT: $_PASS passed, 0 failed — OK"
else
    echo "  RESULT: $_PASS passed, $_FAIL failed — FAIL"
fi
echo "============================================"

[ "$_FAIL" -eq 0 ]
