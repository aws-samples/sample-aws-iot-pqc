#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-builder-pi-remote.sh — provision the builder-Pi remotely via SSM.
#
# Runs on the DEPLOY HOST (the machine with AWS credentials), not on the Pi.
# After the Pi has registered as an SSM managed instance (bundle -> setup-ssm.sh),
# this orchestrator waits for it to come Online, then runs the CFN-owned SSM
# documents in order:
#
#   1. install-base         apt: AWS CLI, python3 + pip, curl, udev (fast)
#   2. deploy-app           GUI app from S3 + pip deps (esptool, pyserial), config.json
#   3. configure-service    udev + systemd (GUI auto-starts) + CloudWatch
#   4. configure-wifi-ap    bring up the 2.4GHz workshop WiFi AP on the Pi's
#                           USB MediaTek radio (wlan1) so devices can join
#
# The Pi does not compile firmware (the cloud builds the images; the Pi only
# flashes them), so there is no arduino-cli / ESP32 core install.
#
# Each document is run with send-command and polled to completion. On any
# non-Success the orchestrator prints the command output + a console URL and
# stops — so you can fix the cause and re-run JUST that document (they are
# independently re-runnable).
#
# Usage:
#   ./setup-builder-pi-remote.sh [--region REGION] [--instance-id mi-XXXX] \
#       [--only install-base|deploy-app|configure-service|configure-wifi-ap] \
#       [--channel N|auto]
#
#   --region       AWS region (default: $AWS_DEFAULT_REGION or us-east-1)
#   --instance-id  Target managed instance. Omit to auto-discover the single
#                  Online pqc-workshop-builder-pi instance.
#   --only         Run just ONE document (for isolated retry): install-base
#                  (alias: awscli), deploy-app, configure-service, or
#                  configure-wifi-ap.
#   --profile      AWS CLI profile (optional; else ambient credentials)
#   --channel      2.4 GHz channel for the workshop AP (configure-wifi-ap). A
#                  number (1-11) sets it; 'auto' first runs the read-only
#                  builder-pi/wifi-survey.sh on the Pi and uses its recommended
#                  channel. Omit to keep the document's default (11). Use auto
#                  at a venue, with the Pi where it will sit during the event.
#
# NOTE: configure-wifi-ap brings up the workshop 2.4GHz WiFi AP on the Pi's
# MediaTek USB radio (wlan1). The workshop runs on controlled, consistent Pi
# hardware, so this is part of the DEFAULT provisioning chain — the Pi comes up
# fully networked with no manual step. The document detects the AP radio by its
# mt76* driver, so it does not hardcode the wlan1 name.
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
INSTANCE_ID=""
ONLY=""
CHANNEL=""
PROFILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --instance-id) INSTANCE_ID="$2"; shift 2 ;;
        --only) ONLY="$2"; shift 2 ;;
        --channel) CHANNEL="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done

# Thread --profile/--region into every aws call. Using an array avoids the
# quoting pitfalls of a flattened string.
if [[ -n "$CHANNEL" && "$CHANNEL" != auto && ! "$CHANNEL" =~ ^([1-9]|1[01])$ ]]; then
    echo "[ERROR] --channel must be 1-11 or auto (got '$CHANNEL')"; exit 1
fi

AWS=(aws --region "$REGION")
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

ACCOUNT_ID=$("${AWS[@]}" sts get-caller-identity --query Account --output text 2>/dev/null) || {
    echo "[ERROR] No usable AWS credentials for region $REGION."; exit 1; }

echo "═══════════════════════════════════════════════════════════════"
echo "  Builder-Pi remote provisioning (via SSM)"
echo "═══════════════════════════════════════════════════════════════"
echo "  Account: $ACCOUNT_ID    Region: $REGION"

# ─── Resolve the SSM document names from the master stack outputs ────────────
# The documents are auto-named by CloudFormation (no fixed Name, so their
# content can be updated in place without a replacement), so we MUST read the
# generated names from the stack outputs — there is no stable literal to fall
# back to. A missing output is a hard error.
get_out() {
    "${AWS[@]}" cloudformation describe-stacks --stack-name pqc-workshop \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
        --output text 2>/dev/null || echo ""
}
DOC_AWSCLI=$(get_out InstallAwscliDocumentName)
DOC_DEPLOYAPP=$(get_out DeployAppDocumentName)
DOC_CONFIGURE=$(get_out ConfigureServiceDocumentName)
DOC_WIFIAP=$(get_out ConfigureWifiApDocumentName)
for pair in "InstallAwscliDocumentName:$DOC_AWSCLI" \
            "DeployAppDocumentName:$DOC_DEPLOYAPP" "ConfigureServiceDocumentName:$DOC_CONFIGURE" \
            "ConfigureWifiApDocumentName:$DOC_WIFIAP"; do
    if [[ -z "${pair#*:}" || "${pair#*:}" == "None" ]]; then
        echo "[ERROR] Could not resolve SSM document name '${pair%%:*}' from the"
        echo "        'pqc-workshop' stack outputs. Is the workshop stack deployed"
        echo "        in this account/region? (profile ${PROFILE:-<ambient>}, region $REGION)"
        exit 1
    fi
done

# ─── Resolve the target instance (auto-discover the Online builder-Pi) ───────
if [[ -z "$INSTANCE_ID" ]]; then
    echo ""
    echo "[*] Auto-discovering the Online builder-Pi managed instance..."
    INSTANCE_ID=$("${AWS[@]}" ssm describe-instance-information \
        --query "InstanceInformationList[?PingStatus=='Online' && Name=='pqc-workshop-builder-pi'].InstanceId | [0]" \
        --output text 2>/dev/null || echo "")
    if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
        echo "[ERROR] No Online 'pqc-workshop-builder-pi' managed instance found."
        echo "        Register the Pi first (copy the bundle, run: sudo ./setup-ssm.sh),"
        echo "        wait ~30s for it to come Online, then re-run — or pass --instance-id."
        exit 1
    fi
fi
echo "    target instance: $INSTANCE_ID"

# ─── Wait for the instance to be Online (in case it just registered) ─────────
echo ""
echo "[*] Waiting for $INSTANCE_ID to be Online..."
ONLINE=""
for _ in $(seq 1 30); do
    PING=$("${AWS[@]}" ssm describe-instance-information \
        --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
        --query "InstanceInformationList[0].PingStatus" --output text 2>/dev/null || echo "")
    if [[ "$PING" == "Online" ]]; then ONLINE="yes"; break; fi
    echo "    ping status: ${PING:-<none>} (waiting)"
    sleep 10
done
if [[ -z "$ONLINE" ]]; then
    echo "[ERROR] $INSTANCE_ID did not reach Online. Check the SSM agent on the Pi."
    exit 1
fi
echo "    Online."

# ─── run_doc <document-name> <friendly-label> ────────────────────────────────
# Sends the document to the instance and polls to completion, streaming the
# invocation status. On failure prints stderr + a console deep link and returns
# non-zero so the caller stops (each doc is independently re-runnable).
run_doc() {
    local doc="$1" label="$2" onlykey="${3:-}"
    local params=()
    # The AP channel is the only document parameter this orchestrator sets.
    if [[ "$onlykey" == configure-wifi-ap && -n "$CHANNEL" ]]; then
        params=(--parameters "Channel=$CHANNEL")
    fi
    echo ""
    echo "───────────────────────────────────────────────────────────────"
    echo "  Running: $label  ($doc)"
    echo "───────────────────────────────────────────────────────────────"
    local cid
    cid=$("${AWS[@]}" ssm send-command \
        --instance-ids "$INSTANCE_ID" \
        --document-name "$doc" \
        ${params[@]+"${params[@]}"} \
        --query 'Command.CommandId' --output text 2>/dev/null) || {
            echo "[ERROR] send-command failed for $doc"; return 1; }
    echo "    command: $cid"

    # Poll. No step is slow any more (the longest is a fresh Pi's apt update +
    # pip install, a few minutes), so 80 * 15s = 20 min covers the longest
    # document timeout (deploy-app, 15 min) with margin.
    local status="" i
    for i in $(seq 1 80); do
        status=$("${AWS[@]}" ssm get-command-invocation \
            --command-id "$cid" --instance-id "$INSTANCE_ID" \
            --query 'Status' --output text 2>/dev/null || echo "Pending")
        case "$status" in
            Success) echo "    ✅ $label: Success"; return 0 ;;
            Failed|Cancelled|TimedOut) break ;;
            *) printf '    %s status=%s\n' "$(date +%H:%M:%S)" "$status"; sleep 15 ;;
        esac
    done

    echo "    ❌ $label: $status"
    echo "    ── stderr (tail) ─────────────────────────────────────────"
    "${AWS[@]}" ssm get-command-invocation \
        --command-id "$cid" --instance-id "$INSTANCE_ID" \
        --query 'StandardErrorContent' --output text 2>/dev/null | tail -30 || true
    echo "    ── stdout (tail) ─────────────────────────────────────────"
    "${AWS[@]}" ssm get-command-invocation \
        --command-id "$cid" --instance-id "$INSTANCE_ID" \
        --query 'StandardOutputContent' --output text 2>/dev/null | tail -30 || true
    echo "    ──────────────────────────────────────────────────────────"
    echo "    Console: https://${REGION}.console.aws.amazon.com/systems-manager/run-command/${cid}/${INSTANCE_ID}?region=${REGION}"
    if [[ -n "$onlykey" ]]; then
        echo "    Re-run just this step with: $0 --region $REGION --instance-id $INSTANCE_ID${PROFILE:+ --profile $PROFILE} --only $onlykey"
    fi
    return 1
}

# ─── Resolve --channel (number, or 'auto' = survey the air first) ───────────
# Runs just before configure-wifi-ap, so the survey reflects the room as it is.
resolve_channel() {
    [[ "$CHANNEL" == auto ]] || return 0
    echo ""
    echo "[*] --channel auto: surveying the 2.4 GHz air from the Pi (read-only, ~1 min)..."
    local out rec
    out=$("$SCRIPT_DIR/builder-pi/wifi-survey.sh" --region "$REGION" --instance-id "$INSTANCE_ID" \
        ${PROFILE:+--profile "$PROFILE"}) || true
    printf '%s\n' "$out" | grep -vE '^(RECOMMENDED_|Apply:)' | sed 's/^/    /' || true
    rec=$(printf '%s\n' "$out" | sed -n 's/^RECOMMENDED_CHANNEL=\([0-9]*\)$/\1/p' | tail -1)
    if [[ -n "$rec" ]]; then
        CHANNEL="$rec"; echo "    -> using channel $CHANNEL"
    else
        CHANNEL=""; echo "    [WARN] survey gave no recommendation; using the document default (11)"
    fi
}

# ─── Run the requested document(s) ───────────────────────────────────────────
# Order: install-base (prerequisite) -> deploy-app -> configure-service ->
# configure-wifi-ap. install-base first because deploy-app's S3/SSM calls and
# pip installs need it; configure-wifi-ap last so the Pi ends fully networked.
case "$ONLY" in
    "")
        run_doc "$DOC_AWSCLI"    "1/4 install-base"       install-base
        run_doc "$DOC_DEPLOYAPP" "2/4 deploy-app"         deploy-app
        run_doc "$DOC_CONFIGURE" "3/4 configure-service"  configure-service
        resolve_channel
        run_doc "$DOC_WIFIAP"    "4/4 configure-wifi-ap"  configure-wifi-ap
        ;;
    install-base|awscli) run_doc "$DOC_AWSCLI"   "install-base"      install-base ;;
    deploy-app)         run_doc "$DOC_DEPLOYAPP" "deploy-app"        deploy-app ;;
    configure-service)  run_doc "$DOC_CONFIGURE" "configure-service" configure-service ;;
    configure-wifi-ap)  resolve_channel; run_doc "$DOC_WIFIAP" "configure-wifi-ap" configure-wifi-ap ;;
    install-toolchain)
        echo "[ERROR] install-toolchain was removed: the Pi no longer compiles firmware."
        echo "        Use install-base, deploy-app, configure-service or configure-wifi-ap."; exit 1 ;;
    *) echo "[ERROR] --only must be one of: install-base, deploy-app, configure-service, configure-wifi-ap"; exit 1 ;;
esac

# ─── Success: prune stale managed instances + print the GUI URL ──────────────
echo ""
echo "[*] Pruning stale (ConnectionLost) builder-Pi managed instances..."
STALE=$("${AWS[@]}" ssm describe-instance-information \
    --query "InstanceInformationList[?PingStatus=='ConnectionLost' && Name=='pqc-workshop-builder-pi'].InstanceId" \
    --output text 2>/dev/null || true)
for mi in $STALE; do
    [[ "$mi" == mi-* ]] || continue
    echo "    - deregistering $mi"
    "${AWS[@]}" ssm deregister-managed-instance --instance-id "$mi" >/dev/null 2>&1 \
        || echo "      (could not deregister $mi; ignoring)"
done

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Builder-Pi provisioned. The GUI auto-starts on boot, and the"
echo "     workshop WiFi AP is up on wlan1 for devices to join."
echo "═══════════════════════════════════════════════════════════════"
echo "  Open http://127.0.0.1:8080 in the Pi's own browser (its console or a KVM)."
echo "  The GUI listens on loopback only; it is not reachable over the network."
echo ""
