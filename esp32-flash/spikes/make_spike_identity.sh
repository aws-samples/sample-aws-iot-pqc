#!/usr/bin/env bash
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# make_spike_identity.sh — generate a THROWAWAY device identity for the NVS
# identity spike (.kiro/specs/nvs-device-identity, tasks 0.2/0.3).
#
# Creates two local test CAs (classical P-256 and ML-DSA-44), a device cert from
# each, the ML-DSA-44 device key from a supplied 32-byte seed, and the DER blobs
# plus NVS CSV the spike sketch reads. It never touches the workshop Private CAs.
#
# Run it inside the firmware-builder image so the ML-DSA steps use the
# production OpenSSL 3.5 (falls back to `openssl` on PATH):
#
#   docker run --rm -v "$PWD:/work" -w /work pqc-fw-builder:local \
#       bash esp32-flash/spikes/make_spike_identity.sh /work/spike-out
#
# Then build the partition image on a host with esp-idf-nvs-partition-gen:
#
#   cd spike-out && python -m esp_idf_nvs_partition_gen generate \
#       identity.csv certs.bin 0x10000
set -euo pipefail

OUT="${1:-./spike-out}"
O35="${OPENSSL35_BIN:-/opt/openssl-3.5/bin/openssl}"
[ -x "$O35" ] || O35="$(command -v openssl)"
OSSL="$(command -v openssl)"

mkdir -p "$OUT"
cd "$OUT"
rm -f ./*.pem ./*.der ./*.key ./*.csr ./*.srl ./*.bin identity.csv

echo "ML-DSA OpenSSL: $("$O35" version)"

# ── Classical (P-256) test CA + device ────────────────────────────────────────
"$OSSL" ecparam -name prime256v1 -genkey -noout -out ca_cls.key
"$OSSL" req -x509 -new -key ca_cls.key -subj "/CN=Spike Classical Test CA" \
    -days 30 -out ca_cls.pem
"$OSSL" ecparam -name prime256v1 -genkey -noout -out dev_cls.key   # SEC1
"$OSSL" req -new -key dev_cls.key -subj "/CN=esp32-SPIKE1/OU=IoT Device" -out dev_cls.csr
"$OSSL" x509 -req -in dev_cls.csr -CA ca_cls.pem -CAkey ca_cls.key -CAcreateserial \
    -days 30 -out dev_cls.pem 2>/dev/null

# ── ML-DSA-44 test CA + device key from a 32-byte seed ───────────────────────
"$O35" genpkey -algorithm ML-DSA-44 -out ca_mldsa.key
"$O35" req -x509 -new -key ca_mldsa.key -subj "/CN=Spike ML-DSA-44 Test CA" \
    -days 30 -out ca_mldsa.pem
SEED_HEX="$("$OSSL" rand -hex 32)"
"$O35" genpkey -algorithm ML-DSA-44 -pkeyopt "hexseed:${SEED_HEX}" -out dev_mldsa.key
"$O35" req -new -key dev_mldsa.key -subj "/CN=esp32-SPIKE1/OU=IoT Device PQC ML-DSA" \
    -out dev_mldsa.csr
"$O35" x509 -req -in dev_mldsa.csr -CA ca_mldsa.pem -CAkey ca_mldsa.key -CAcreateserial \
    -days 30 -out dev_mldsa.pem 2>/dev/null

# Prove the seed regenerates the same key on the issuer side too.
"$O35" genpkey -algorithm ML-DSA-44 -pkeyopt "hexseed:${SEED_HEX}" -out regen.key
cmp <("$O35" pkey -in dev_mldsa.key -pubout -outform DER) \
    <("$O35" pkey -in regen.key -pubout -outform DER) \
    && echo "OpenSSL: seed regenerates the same ML-DSA-44 key"
rm -f regen.key

# ── DER blobs for NVS ────────────────────────────────────────────────────────
"$OSSL" x509 -in ca_cls.pem    -outform DER -out ca_cls.der
"$OSSL" x509 -in dev_cls.pem   -outform DER -out crt_cls.der
"$OSSL" ec   -in dev_cls.key   -outform DER -out key_cls.der 2>/dev/null
"$O35"  x509 -in ca_mldsa.pem  -outform DER -out ca_mldsa.der
"$O35"  x509 -in dev_mldsa.pem -outform DER -out crt_mldsa.der
python3 -c "import sys;open('seed_mldsa.bin','wb').write(bytes.fromhex(sys.argv[1]))" "$SEED_HEX"
"$O35" pkey -in dev_mldsa.key -outform DER \
    -provparam ml-dsa.output_formats=seed-only -out p8_mldsa.der

cat > identity.csv <<'CSV'
key,type,encoding,value
devid,namespace,,
fmt,data,u8,1
serial,data,string,SPIKE1
wifi_ssid,data,string,spike-test-ssid
wifi_psk,data,string,spike-test-password
ca_cls,file,binary,ca_cls.der
ca_mldsa,file,binary,ca_mldsa.der
crt_cls,file,binary,crt_cls.der
key_cls,file,binary,key_cls.der
crt_mldsa,file,binary,crt_mldsa.der
seed_mldsa,file,binary,seed_mldsa.bin
p8_mldsa,file,binary,p8_mldsa.der
CSV

# Private keys of the test CAs are no longer needed; keep only what NVS needs.
rm -f ./*.key ./*.csr ./*.srl

echo "Expected NVS entries (compare with the device's [Spike] entry lines):"
for f in ca_cls.der ca_mldsa.der crt_cls.der key_cls.der crt_mldsa.der seed_mldsa.bin p8_mldsa.der; do
    printf "  %-15s len=%5s sha256=%s\n" "$f" "$(wc -c < "$f" | tr -d ' ')" \
        "$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$f" | cut -d' ' -f1)"
done
