#!/usr/bin/env bash
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────
# wifi-survey.sh — survey the 2.4 GHz WiFi around the Builder-Pi and recommend
# the channel for the workshop access point (and a pair, for two APs).
#
# Run it at the venue, with the Pi where it will sit during the event, just
# before the access point is built (setup-builder-pi-remote.sh). Read-only: it
# only scans, using the Pi's station-mode radios; a running workshop AP is left
# alone and excluded from the results.
#
# Two ways to run it:
#   From the deploy host (the Pi is registered with SSM):
#     ./raw_device_builder/builder-pi/wifi-survey.sh --region us-east-1 [--profile P]
#   On the Pi itself (console/KVM, e.g. before SSM registration; it is in the
#   Builder-Pi bundle):
#     sudo ./wifi-survey.sh
#
# Options:
#   --region R        run on the registered Builder-Pi over SSM (deploy-host mode)
#   --profile P       AWS CLI profile (deploy-host mode)
#   --instance-id ID  target this managed instance (default: the Online Builder-Pi)
#   --scans N         scans per radio (default 5); scans miss networks, so repeat
#   --interval S      seconds between scans (default 6)
#   --local           force on-Pi mode (what the SSM run uses)
#
# Apply the recommendation with:
#   ./raw_device_builder/setup-builder-pi-remote.sh --region R --only configure-wifi-ap --channel N
# or let provisioning do it: setup-builder-pi-remote.sh --channel auto
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="" PROFILE="" INSTANCE_ID="" SCANS=5 INTERVAL=6 LOCAL="false"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --instance-id) INSTANCE_ID="$2"; shift 2 ;;
        --scans) SCANS="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --local) LOCAL="true"; shift ;;
        -h|--help) sed -n '4,31p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "[ERROR] Unknown arg: $1 (see --help)"; exit 2 ;;
    esac
done
[[ "$SCANS" =~ ^[1-9][0-9]?$ && "$INTERVAL" =~ ^[0-9]{1,2}$ ]] || {
    echo "[ERROR] --scans must be 1-99 and --interval 0-99"; exit 2; }

# ─── Deploy-host mode: ship this script + the analyser to the Pi over SSM ────
if [[ "$LOCAL" != "true" && -n "$REGION" ]]; then
    command -v python3 >/dev/null || { echo "[ERROR] python3 is required on the deploy host"; exit 1; }
    AWS=(aws --region "$REGION")
    [[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")
    if [[ -z "$INSTANCE_ID" ]]; then
        INSTANCE_ID=$("${AWS[@]}" ssm describe-instance-information \
            --query "InstanceInformationList[?PingStatus=='Online' && Name=='pqc-workshop-builder-pi'].InstanceId | [0]" \
            --output text 2>/dev/null)
        if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
            echo "[ERROR] No Online Builder-Pi in $REGION. Register it first (setup-ssm.sh),"
            echo "        or run 'sudo ./wifi-survey.sh' on the Pi itself."
            exit 1
        fi
    fi
    echo "[*] Surveying 2.4 GHz WiFi on $INSTANCE_ID ($SCANS scans per radio, ${INTERVAL}s apart)..." >&2
    PARAMS=$(python3 - "$SCRIPT_DIR/wifi-survey.sh" "$SCRIPT_DIR/wifi_survey.py" "$SCANS" "$INTERVAL" <<'PY'
import json, sys
sh, py, scans, interval = sys.argv[1:5]
cmd = "\n".join([
    "D=$(mktemp -d)",
    "cat > \"$D/wifi_survey.py\" <<'WIFI_SURVEY_PY_EOF'", open(py).read().rstrip("\n"), "WIFI_SURVEY_PY_EOF",
    "cat > \"$D/wifi-survey.sh\" <<'WIFI_SURVEY_SH_EOF'", open(sh).read().rstrip("\n"), "WIFI_SURVEY_SH_EOF",
    f"bash \"$D/wifi-survey.sh\" --local --scans {int(scans)} --interval {int(interval)}",
    "rc=$?", "rm -rf \"$D\"", "exit $rc",
])
print(json.dumps({"commands": [cmd], "executionTimeout": ["900"]}))
PY
)
    CID=$("${AWS[@]}" ssm send-command --instance-ids "$INSTANCE_ID" \
        --document-name AWS-RunShellScript --comment "read-only: 2.4 GHz WiFi survey" \
        --parameters "$PARAMS" --query Command.CommandId --output text) || {
        echo "[ERROR] send-command failed"; exit 1; }
    STATUS=""
    for _ in $(seq 1 120); do
        sleep 5
        STATUS=$("${AWS[@]}" ssm get-command-invocation --command-id "$CID" --instance-id "$INSTANCE_ID" \
            --query Status --output text 2>/dev/null || echo Pending)
        case "$STATUS" in Success|Failed|Cancelled|TimedOut) break ;; esac
    done
    "${AWS[@]}" ssm get-command-invocation --command-id "$CID" --instance-id "$INSTANCE_ID" \
        --query StandardOutputContent --output text
    if [[ "$STATUS" != "Success" ]]; then
        echo "[ERROR] survey $STATUS on $INSTANCE_ID:" >&2
        "${AWS[@]}" ssm get-command-invocation --command-id "$CID" --instance-id "$INSTANCE_ID" \
            --query StandardErrorContent --output text | tail -20 >&2
        exit 1
    fi
    echo "Apply: ./raw_device_builder/setup-builder-pi-remote.sh --region $REGION${PROFILE:+ --profile $PROFILE} --only configure-wifi-ap --channel <N>"
    exit 0
fi

# ─── On-Pi mode: scan with every station-mode radio ─────────────────────────
command -v iw >/dev/null || { echo "[ERROR] 'iw' not found. Run this on the Builder-Pi, or pass --region."; exit 1; }
[[ "$(id -u)" -eq 0 ]] || { echo "[ERROR] Scanning needs root: sudo $0"; exit 1; }
ANALYSER="$SCRIPT_DIR/wifi_survey.py"
[[ -f "$ANALYSER" ]] || { echo "[ERROR] $ANALYSER is missing (keep it next to this script)."; exit 1; }

IFACES=() BROUGHT_UP=()
while read -r ifc type; do
    [[ "$type" == "managed" ]] && IFACES+=("$ifc")
done < <(iw dev | awk '/Interface/{i=$2} /type/{print i, $2}')
[[ ${#IFACES[@]} -gt 0 ]] || { echo "[ERROR] No station-mode WiFi radio to scan with."; exit 1; }
restore() { for ifc in "${BROUGHT_UP[@]}"; do ip link set "$ifc" down 2>/dev/null; done; }
trap restore EXIT
for ifc in "${IFACES[@]}"; do
    if ! ip link show "$ifc" | grep -q 'state UP\|,UP'; then
        ip link set "$ifc" up 2>/dev/null && BROUGHT_UP+=("$ifc")
    fi
done

# Exclude this Pi's own AP: its SSID (if the workshop AP profile exists) and
# every local WiFi MAC (an AP's BSSID is normally its interface address).
EXCL=()
OWN_SSID=$(nmcli -g 802-11-wireless.ssid connection show workshop-ap 2>/dev/null || true)
[[ -n "$OWN_SSID" ]] && EXCL+=(--exclude-ssid "$OWN_SSID")
for addr in /sys/class/net/wlan*/address; do
    [[ -r "$addr" ]] && EXCL+=(--exclude-bssid "$(cat "$addr")")
done

echo "Radios: ${IFACES[*]}   scans: $SCANS x ${INTERVAL}s   own AP excluded: ${OWN_SSID:-none}" >&2
RAW=$(mktemp)
for n in $(seq 1 "$SCANS"); do
    for ifc in "${IFACES[@]}"; do
        echo "### SCAN $ifc" >> "$RAW"
        # A scan already in progress (e.g. NetworkManager's) returns EBUSY: retry.
        for _ in 1 2 3; do iw dev "$ifc" scan >> "$RAW" 2>/dev/null && break; sleep 2; done
    done
    [[ "$n" -lt "$SCANS" ]] && sleep "$INTERVAL"
done
for ifc in "${IFACES[@]}"; do
    # Airtime per channel, where the driver reports it (mt76 does; brcmfmac does not).
    echo "### SURVEY $ifc" >> "$RAW"
    iw dev "$ifc" survey dump >> "$RAW" 2>/dev/null || true
done
python3 "$ANALYSER" "${EXCL[@]}" < "$RAW"
rc=$?
rm -f "$RAW"
exit $rc
