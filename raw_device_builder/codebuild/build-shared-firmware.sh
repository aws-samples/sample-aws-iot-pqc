#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build-shared-firmware.sh — build every firmware variant ONCE for this
# deployment, into s3://<firmware-bucket>/shared/<version>/.
#
# Firmware images carry no device identity (keys, certs, CA roots and WiFi are
# in each device's "certs" NVS partition), so one signed image per variant
# serves every device: Register & Flash uses shared/1.0.0-classical/, and every
# dashboard migration publishes shared/<version>/ without building anything.
#
# Runs in two places, same code:
#   - automatically, at the end of the firmware-builder image build
#     (buildspec-image.yml post_build, with --wait), so the shared images build
#     in parallel with Builder-Pi provisioning (workshop 99.4 / 99.6);
#   - by hand, to rebuild after changing a sketch or buildspec.yml (e.g. after
#     `deploy-workshop.sh --stage-only`).
#
# The variant list comes from variants.json — the same file prepare-build uses —
# so the builds and the migrations can never disagree.
#
# Usage:
#   raw_device_builder/codebuild/build-shared-firmware.sh [--region R] [--profile P]
#       [--variant KEY ...] [--wait] [--variants-file PATH]
#
#   --variant KEY     build only this variant (repeatable); default: all
#   --wait            wait for all builds; exit non-zero if any fails
#   --variants-file   default: raw_device_builder/lambda/prepare-build/variants.json
#                     next to this repo, else ./variants.json (CodeBuild)
#
# Env (set in CodeBuild; derived otherwise): FIRMWARE_BUILD_PROJECT
# (default pqc-workshop-firmware-build), BROKER_ENDPOINT (default: SSM
# /pqc-workshop/broker/endpoint, else the project's own default).
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
WAIT="false"
ONLY=()
VARIANTS_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --profile) export AWS_PROFILE="$2"; shift 2 ;;
        --variant) ONLY+=("$2"); shift 2 ;;
        --wait) WAIT="true"; shift ;;
        --variants-file) VARIANTS_FILE="$2"; shift 2 ;;
        -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done
export AWS_DEFAULT_REGION="$REGION"

if [[ -z "$VARIANTS_FILE" ]]; then
    for f in "$SCRIPT_DIR/../lambda/prepare-build/variants.json" "./variants.json" "$SCRIPT_DIR/variants.json"; do
        [[ -f "$f" ]] && { VARIANTS_FILE="$f"; break; }
    done
fi
[[ -f "${VARIANTS_FILE:-}" ]] || { echo "[ERROR] variants.json not found (use --variants-file)"; exit 1; }

# "key version build_variant signer_era" per line (signer_era "-" = unsigned).
# Parsed BEFORE any AWS call, via command substitution so a bad --variant fails
# the script (bash 3.2-compatible: no mapfile / associative arrays — runs on macOS).
ROWS_TEXT=$(python3 - "$VARIANTS_FILE" "${ONLY[@]+"${ONLY[@]}"}" <<'PY'
import json, sys
variants = json.load(open(sys.argv[1]))["variants"]
only = sys.argv[2:]
unknown = [k for k in only if k not in variants]
if unknown:
    sys.exit(f"[ERROR] unknown variant(s): {', '.join(unknown)}; expected {', '.join(variants)}")
for key, v in variants.items():
    if not only or key in only:
        print(key, v["version"], v["build_variant"], v.get("signer_era") or "-")
PY
)
ROWS=()
while IFS= read -r _row; do [[ -n "$_row" ]] && ROWS+=("$_row"); done <<<"$ROWS_TEXT"
(( ${#ROWS[@]} > 0 )) || { echo "[ERROR] no variants selected"; exit 1; }

PROJECT="${FIRMWARE_BUILD_PROJECT:-pqc-workshop-firmware-build}"
if [[ -z "${BROKER_ENDPOINT:-}" ]]; then
    BROKER_ENDPOINT=$(aws ssm get-parameter --name /pqc-workshop/broker/endpoint \
        --query Parameter.Value --output text 2>/dev/null || true)
fi
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "Shared firmware build — account $ACCOUNT_ID, project $PROJECT"
echo "  broker endpoint: ${BROKER_ENDPOINT:-<project default>}"
echo "  variants: ${#ROWS[@]}  (from $VARIANTS_FILE)"

start_build() {
    # $1 version  $2 build_variant  $3 signer_era ("-" = unsigned)
    local version="$1" bvar="$2" era="$3" overrides id attempt
    [[ "$era" == "-" ]] && era=""
    overrides="name=FIRMWARE_VERSION,value=$version,type=PLAINTEXT"
    overrides+=" name=FIRMWARE_VARIANT,value=$bvar,type=PLAINTEXT"
    overrides+=" name=SIGNER_ERA,value=$era,type=PLAINTEXT"
    [[ -n "${BROKER_ENDPOINT:-}" && "$BROKER_ENDPOINT" != "None" ]] && \
        overrides+=" name=BROKER_ENDPOINT,value=$BROKER_ENDPOINT,type=PLAINTEXT"
    # Retry when the account's CodeBuild concurrency quota is momentarily full.
    for attempt in $(seq 1 20); do
        # shellcheck disable=SC2086
        if id=$(aws codebuild start-build --project-name "$PROJECT" \
                --environment-variables-override $overrides \
                --query build.id --output text 2>/tmp/_sfw_err); then
            echo "$id"; return 0
        fi
        if grep -q "AccountLimitExceeded" /tmp/_sfw_err; then
            echo "    (concurrency limit reached; retrying in 30 s)" >&2
            sleep 30
        else
            cat /tmp/_sfw_err >&2; return 1
        fi
    done
    echo "[ERROR] could not start build for $version (concurrency limit)" >&2
    return 1
}

IDS=(); NAMES=()
for row in "${ROWS[@]}"; do
    read -r key version bvar era <<<"$row"
    id=$(start_build "$version" "$bvar" "$era")
    echo "  - $key -> shared/$version/  ($bvar, signer ${era/-/unsigned})  build $id"
    IDS+=("$id"); NAMES+=("$version")
done

[[ "$WAIT" == "true" ]] || { echo "Started ${#IDS[@]} build(s); not waiting (--wait to block)."; exit 0; }

echo "Waiting for ${#IDS[@]} build(s)..."
failed=0
DONE=(); for i in "${!IDS[@]}"; do DONE[$i]=0; done
remaining=${#IDS[@]}
while (( remaining > 0 )); do
    sleep 20
    for i in "${!IDS[@]}"; do
        (( DONE[i] )) && continue
        st=$(aws codebuild batch-get-builds --ids "${IDS[$i]}" --query 'builds[0].buildStatus' --output text)
        case "$st" in
            SUCCEEDED) DONE[$i]=1; remaining=$((remaining - 1)); echo "  ✓ ${NAMES[$i]} SUCCEEDED" ;;
            FAILED|FAULT|TIMED_OUT|STOPPED) DONE[$i]=1; remaining=$((remaining - 1)); failed=1
                echo "  ✗ ${NAMES[$i]} $st (${IDS[$i]})" ;;
        esac
    done
done
if (( failed )); then
    echo "[ERROR] one or more shared firmware builds failed"; exit 1
fi
echo "All shared firmware images built: s3://pqc-workshop-firmware-${ACCOUNT_ID}/shared/"
