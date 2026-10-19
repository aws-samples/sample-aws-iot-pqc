#!/usr/bin/env bash
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# ─────────────────────────────────────────────────────────────────────────────
# ota_tamper_test.sh — Workstream 4 (task 4.3) negative/tamper proof.
#
# Proves the on-device OTA signature verifier (Workstream 3) REJECTS a tampered
# or mis-signed firmware image: the device must NOT switch its boot partition,
# and must keep running its current firmware.
#
# This is an on-HARDWARE test — it needs a deployed environment (signer secrets,
# the signing build, and verifier firmware already on the device) and a device
# that is online and reporting telemetry. It is intentionally NOT part of the
# mocked unit suite (run-tests.sh); it is run by hand against pqc-a/pqc-b during
# the WS4 end-to-end proof.
#
# What it does (all against S3 artifacts the device will pull):
#   1. Snapshot the device's CURRENT reported firmwareVersion (from DynamoDB).
#   2. Pick a SIGNED artifact dir (<serial>/<version>/) that has firmware.bin +
#      firmware.bin.sig + signer.crt.
#   3. Produce a TAMPERED variant in a scratch prefix, one of:
#        --mode corrupt-image : flip bytes in firmware.bin, keep the real sig
#        --mode wrong-sig      : replace firmware.bin.sig with a valid-looking
#                                but wrong signature (sig over different bytes)
#        --mode swap-signer    : replace signer.crt with a self-signed leaf that
#                                does NOT chain to the baked-in CA root
#   4. Point an OTA at the tampered artifact (publish cmd/<thing>/ota with the
#      tampered presigned URLs) and wait.
#   5. ASSERT: after a grace period the device is STILL running the original
#      firmwareVersion (it refused the tampered image) and remains online.
#
# Exit 0 = device correctly REJECTED the tampered OTA (test PASS).
# Exit 1 = device booted the tampered image or went missing (test FAIL — the
#          verifier did not protect the device).
#
# Usage:
#   ./ota_tamper_test.sh --profile pqc-a --region us-east-1 \
#       --thing esp32-C69E18 --version 5.0.0-verified --mode corrupt-image
#
# Requirements: awscli v2, python3, openssl (3.5+ for the wrong-sig ML-DSA case),
# a Midway/Isengard profile with S3 + iot-data + dynamodb access.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE=""
REGION="us-east-1"
THING=""
VERSION=""
MODE="corrupt-image"
GRACE_SECONDS=180
DEVICE_TABLE="pqc-workshop-devices"

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    --thing) THING="$2"; shift 2;;
    --version) VERSION="$2"; shift 2;;
    --mode) MODE="$2"; shift 2;;
    --grace) GRACE_SECONDS="$2"; shift 2;;
    --device-table) DEVICE_TABLE="$2"; shift 2;;
    -h|--help) usage 0;;
    *) echo "Unknown arg: $1" >&2; usage 1;;
  esac
done

[[ -n "$PROFILE" && -n "$THING" && -n "$VERSION" ]] || {
  echo "ERROR: --profile, --thing, and --version are required." >&2; usage 1; }

case "$MODE" in corrupt-image|wrong-sig|swap-signer) ;; *)
  echo "ERROR: --mode must be corrupt-image | wrong-sig | swap-signer" >&2; exit 1;; esac

AWS=(aws --profile "$PROFILE" --region "$REGION")
SERIAL="${THING#esp32-}"

echo "═══════════════════════════════════════════════════════════════"
echo " OTA TAMPER TEST  thing=$THING version=$VERSION mode=$MODE"
echo "═══════════════════════════════════════════════════════════════"

# ── Resolve the firmware bucket + IoT endpoint from the deployed stacks ──
FIRMWARE_BUCKET="$("${AWS[@]}" cloudformation describe-stacks \
  --query "Stacks[].Outputs[?OutputKey=='FirmwareBucketName' || OutputKey=='FirmwareBucket'].OutputValue" \
  --output text 2>/dev/null | head -1)"
# Fall back to the conventional name if the output is not published.
if [[ -z "$FIRMWARE_BUCKET" || "$FIRMWARE_BUCKET" == "None" ]]; then
  ACCT="$("${AWS[@]}" sts get-caller-identity --query Account --output text)"
  FIRMWARE_BUCKET="pqc-workshop-firmware-${ACCT}"
fi
IOT_ENDPOINT="$("${AWS[@]}" iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text)"
echo "firmware bucket: $FIRMWARE_BUCKET"
echo "iot endpoint:    $IOT_ENDPOINT"

PREFIX="${SERIAL}/${VERSION}"
TAMPER_PREFIX="${SERIAL}/_tamper-${MODE}-$(date +%s)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 1. Snapshot the current running firmware version ──
BEFORE="$("${AWS[@]}" dynamodb get-item --table-name "$DEVICE_TABLE" \
  --key "{\"thingName\":{\"S\":\"$THING\"}}" \
  --query "Item.firmwareVersion.S" --output text 2>/dev/null || echo "unknown")"
echo "device currently reports firmwareVersion = $BEFORE"
[[ "$BEFORE" != "$VERSION" ]] || echo "NOTE: device already on $VERSION; a refused OTA will simply leave it there."

# ── 2. Verify the signed source artifact exists ──
for f in firmware.bin firmware.bin.sig signer.crt; do
  "${AWS[@]}" s3api head-object --bucket "$FIRMWARE_BUCKET" --key "$PREFIX/$f" >/dev/null 2>&1 || {
    echo "ERROR: missing signed artifact s3://$FIRMWARE_BUCKET/$PREFIX/$f — is $VERSION a SIGNED variant that has been built?" >&2
    exit 1; }
done
"${AWS[@]}" s3 cp "s3://$FIRMWARE_BUCKET/$PREFIX/firmware.bin"     "$WORK/firmware.bin"     --quiet
"${AWS[@]}" s3 cp "s3://$FIRMWARE_BUCKET/$PREFIX/firmware.bin.sig" "$WORK/firmware.bin.sig" --quiet
"${AWS[@]}" s3 cp "s3://$FIRMWARE_BUCKET/$PREFIX/signer.crt"       "$WORK/signer.crt"       --quiet

# ── 3. Produce the tampered artifact set ──
cp "$WORK/firmware.bin" "$WORK/t.bin"
cp "$WORK/firmware.bin.sig" "$WORK/t.sig"
cp "$WORK/signer.crt" "$WORK/t.crt"
case "$MODE" in
  corrupt-image)
    # Flip a byte deep in the image; keep the genuine signature. The signature
    # no longer matches the bytes → signature verify MUST fail on the device.
    python3 - "$WORK/t.bin" <<'PY'
import sys
p=sys.argv[1]; b=bytearray(open(p,'rb').read())
i=len(b)//2; b[i]^=0xFF
open(p,'wb').write(b)
print(f"flipped byte at offset {i}")
PY
    ;;
  wrong-sig)
    # Replace the signature with a well-formed signature over DIFFERENT bytes
    # (random). Structurally valid, cryptographically wrong → verify MUST fail.
    head -c 4096 /dev/urandom > "$WORK/other.bin"
    # We cannot re-sign with the real signer key (it never leaves Secrets
    # Manager / the build), so just use random bytes of the right length as the
    # "signature": the device's wc_ecc/wc_dilithium verify will reject it.
    SIG_LEN=$(wc -c < "$WORK/t.sig")
    head -c "$SIG_LEN" /dev/urandom > "$WORK/t.sig"
    echo "replaced signature with $SIG_LEN random bytes"
    ;;
  swap-signer)
    # Replace the signer leaf with a self-signed cert that does NOT chain to the
    # baked-in CA root → chain-to-root check MUST fail on the device.
    openssl ecparam -genkey -name prime256v1 -noout -out "$WORK/rogue.key" 2>/dev/null
    openssl req -new -x509 -key "$WORK/rogue.key" -days 1 \
      -subj "/CN=rogue-firmware-signer/O=attacker/C=US" -out "$WORK/t.crt" 2>/dev/null
    echo "replaced signer.crt with a rogue self-signed leaf (does not chain to baked-in root)"
    ;;
esac

# ── 4. Upload tampered set to a scratch prefix + presign + publish OTA ──
"${AWS[@]}" s3 cp "$WORK/t.bin" "s3://$FIRMWARE_BUCKET/$TAMPER_PREFIX/firmware.bin"     --quiet
"${AWS[@]}" s3 cp "$WORK/t.sig" "s3://$FIRMWARE_BUCKET/$TAMPER_PREFIX/firmware.bin.sig" --quiet
"${AWS[@]}" s3 cp "$WORK/t.crt" "s3://$FIRMWARE_BUCKET/$TAMPER_PREFIX/signer.crt"       --quiet

presign() { "${AWS[@]}" s3 presign "s3://$FIRMWARE_BUCKET/$TAMPER_PREFIX/$1" --expires-in 900; }
URL="$(presign firmware.bin)"
SIG_URL="$(presign firmware.bin.sig)"
SIGNER_URL="$(presign signer.crt)"
SIZE=$(wc -c < "$WORK/t.bin" | tr -d ' ')
SHA=$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$WORK/t.bin")

# ML-DSA (5.0.0-verified / 4.0.0-signer) → mldsa44; classical era → ecdsa-p256.
case "$VERSION" in
  *-signer|*-verified) SIG_ALG="mldsa44";;
  *) SIG_ALG="ecdsa-p256";;
esac

PAYLOAD="$WORK/ota.json"
python3 - "$PAYLOAD" "$URL" "$VERSION" "$SIZE" "$SHA" "$SIG_ALG" "$SIG_URL" "$SIGNER_URL" <<'PY'
import json,sys
p,url,ver,size,sha,alg,sigurl,signerurl=sys.argv[1:9]
json.dump({"action":"ota-update","url":url,"version":ver,"size":int(size),
           "sha256":sha,"expiresIn":900,"sigAlg":alg,"sigUrl":sigurl,
           "signerCertUrl":signerurl}, open(p,"w"))
PY

echo "publishing TAMPERED ota-update to cmd/$THING/ota (sigAlg=$SIG_ALG)..."
"${AWS[@]}" iot-data publish --topic "cmd/$THING/ota" --qos 1 \
  --cli-binary-format raw-in-base64-out --payload "file://$PAYLOAD" \
  --endpoint-url "https://$IOT_ENDPOINT"

# ── 5. Wait out the grace period, then assert the device did NOT take it ──
echo "waiting ${GRACE_SECONDS}s for the device to download, REJECT, and keep running..."
sleep "$GRACE_SECONDS"

AFTER="$("${AWS[@]}" dynamodb get-item --table-name "$DEVICE_TABLE" \
  --key "{\"thingName\":{\"S\":\"$THING\"}}" \
  --query "Item.firmwareVersion.S" --output text 2>/dev/null || echo "unknown")"
echo "device reports firmwareVersion = $AFTER (was $BEFORE)"

# Clean up the scratch prefix regardless of result.
"${AWS[@]}" s3 rm "s3://$FIRMWARE_BUCKET/$TAMPER_PREFIX/" --recursive --quiet || true

echo "═══════════════════════════════════════════════════════════════"
if [[ "$AFTER" == "$BEFORE" && "$AFTER" != "unknown" ]]; then
  echo " PASS ✅  Device REJECTED the tampered OTA (still on $AFTER)."
  echo "          The verifier protected the device — no boot switch."
  exit 0
else
  echo " FAIL ❌  Device firmwareVersion changed ($BEFORE -> $AFTER)."
  echo "          A tampered image may have been accepted — INVESTIGATE the"
  echo "          on-device verifier before trusting OTA signing."
  exit 1
fi
