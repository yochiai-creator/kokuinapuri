#!/bin/bash
# gas-autopilot runner for lpg-inventory-app.
#
# Adapted from the gas-autopilot skill template to avoid touching the live
# production Web App deployment: instead of a doGet ?fn= router, this calls
# GAS functions directly via the Apps Script Execution API (scripts.run),
# and "deploy" only updates a separate test-only deployment (access: MYSELF),
# never productionDeploymentId in .gas-autopilot.json.
#
# Usage:
#   ./gas-run.sh <functionName> ['[params json array]']  — Run a function
#   ./gas-run.sh deploy                                    — push + version + update test deployment
#   ./gas-run.sh deploy <functionName> ['[params]']        — deploy then run a function
#
# Requires ~/.clasprc.json to hold a token with at least the spreadsheets,
# script.projects, script.deployments and script.external_request scopes
# (see gas-auth.py in the gas-autopilot skill).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/.gas-autopilot.json"
CLASPRC="$HOME/.clasprc.json"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Error: $CONFIG_FILE not found. Run gas-autopilot setup first." >&2
  exit 1
fi

SCRIPT_ID=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE'))['scriptId'])")
TEST_DEPLOY_ID=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE'))['testDeploymentId'])")

get_token() {
  python3 -c "
import json, urllib.request, urllib.parse
with open('$CLASPRC') as f:
    t = json.load(f)['tokens']['default']
data = urllib.parse.urlencode({
    'client_id': t['client_id'], 'client_secret': t['client_secret'],
    'refresh_token': t['refresh_token'], 'grant_type': 'refresh_token'
}).encode()
r = json.loads(urllib.request.urlopen(urllib.request.Request(
    'https://oauth2.googleapis.com/token', data=data)).read())
print(r['access_token'])
"
}

run_function() {
  local fn="$1"
  local params_json="${2:-[]}"
  local token="$3"
  curl -s -X POST "https://script.googleapis.com/v1/scripts/${SCRIPT_ID}:run" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d "{\"function\":\"${fn}\",\"parameters\":${params_json}}" \
    | python3 -m json.tool
}

do_deploy() {
  echo "=== clasp push --force ===" >&2
  (cd "$SCRIPT_DIR" && clasp push --force) >&2

  echo "" >&2
  echo "=== clasp version ===" >&2
  local version_output version_num
  version_output=$(cd "$SCRIPT_DIR" && clasp version "gas-autopilot auto-deploy $(date +%Y-%m-%d_%H:%M)")
  version_num=$(echo "$version_output" | grep -oE '[0-9]+' | head -1)
  echo "$version_output" >&2

  if [ -z "$version_num" ]; then
    echo "Error: Could not get version number" >&2
    exit 1
  fi

  echo "" >&2
  echo "=== Updating TEST deployment to version ${version_num} (production untouched) ===" >&2
  local token resp
  token=$(get_token)
  resp=$(curl -s -X PUT "https://script.googleapis.com/v1/projects/${SCRIPT_ID}/deployments/${TEST_DEPLOY_ID}" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d "{\"deploymentConfig\":{\"versionNumber\":${version_num},\"manifestFileName\":\"appsscript\",\"description\":\"gas-autopilot test deployment\"}}")

  if ! echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'deploymentId' in d else 1)" 2>/dev/null; then
    echo "Error: Test deployment update failed" >&2
    echo "$resp" | python3 -m json.tool >&2 || echo "$resp" >&2
    exit 1
  fi
  echo "Test deployment updated to v${version_num}" >&2
  echo "$token"
}

CMD="${1:-}"
if [ -z "$CMD" ]; then
  echo "Usage:"
  echo "  $0 <functionName> ['[params]']  — Run a function via Execution API"
  echo "  $0 deploy                        — push + version + update test deployment"
  echo "  $0 deploy <functionName> ['[params]']"
  exit 1
fi

if [ "$CMD" = "deploy" ]; then
  TOKEN=$(do_deploy)
  FN="${2:-}"
  if [ -n "$FN" ]; then
    echo ""
    echo "=== Running ${FN} ==="
    run_function "$FN" "${3:-[]}" "$TOKEN"
  fi
else
  TOKEN=$(get_token)
  run_function "$CMD" "${2:-[]}" "$TOKEN"
fi
