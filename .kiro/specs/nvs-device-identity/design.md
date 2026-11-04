# Design — Device identity in a dedicated NVS partition

## Overview

Identity moves from compile time to flash time. Registration runs the shared
cert-issuer (no firmware compile) to mint both device certificates and package
them, with the CA roots and WiFi credentials, into a `certs` NVS partition image.
The Builder-Pi flashes a shared app image plus that identity image. Firmware
reads identity from NVS at boot.

## Current flow (for reference)

1. Builder-Pi reads the serial: `esptool read_mac`, last 6 hex characters
   (`builder-pi/device_manager.py` `get_serial_number`).
2. Pi calls `POST /register` (SigV4). The registrar creates the Thing and policy,
   writes the registry and dashboard row, and starts the firmware CodeBuild.
3. CodeBuild issues the cert via `issue_cert.py`, writes `secrets.h`, compiles,
   and uploads `<serial>/<version>/firmware.merged.bin`.
4. Pi polls the build, downloads `firmware.merged.bin`, and runs
   `esptool write_flash 0x0 firmware.merged.bin` (`device_manager.py` `flash_firmware`).

## New flow

1. Unchanged: Pi reads the serial and calls `POST /register`.
2. The registrar does the same IoT, registry and dashboard work, but starts a new
   **device-identity** CodeBuild project instead of the firmware build.
3. The identity job (cert-issuer image):
   - issues the classical cert (`--key-mode ec-p256`, classical CA);
   - issues the ML-DSA-44 cert (ML-DSA CA), then exports the key as a 54-byte
     seed-only PKCS#8 (OpenSSL keeps the seed for a randomly generated key);
   - fetches both CA roots;
   - checks each private key matches its certificate, converts everything to
     DER and builds `certs.bin` with `esp-idf-nvs-partition-gen`;
   - uploads `s3://<firmware-bucket>/identity/<serial>/certs.bin` (one
     top-level prefix, so a single lifecycle rule can expire them);
   - deletes its working directory.
4. The Pi polls the build, downloads the shared app pieces and `certs.bin`, and
   flashes them in one call:
   `esptool write_flash 0x0 bootloader.bin 0x8000 partitions.bin 0xe000 boot_app0.bin 0x10000 app.bin 0x721000 certs.bin`.
   It must NOT use `firmware.merged.bin`: arduino-cli pads that to the full
   8 MB flash, so it overlaps `0x721000` and would overwrite the identity
   (found in spike 0.1). The pieces are about 0.6 MB, so flashing is also much
   faster than today's 8 MB write. `boot_app0.bin` at `0xe000` resets
   `otadata` so the device boots `ota_0`.
   After a successful flash it deletes `identity/<serial>/certs.bin`, using
   the exact version it downloaded (`DeleteObjectVersion`).
5. The device boots, loads identity from NVS, and connects.

## Partition table

`esp32-flash/partitions_pqc_workshop.csv` (the copy deploy-workshop.sh stages
for both the image build and the Builder-Pi). Add one line:

```
certs,        data, nvs,      0x721000,  0x10000,
```

Flash is 8 MB. `coredump` ends at `0x721000`; `certs` ends at `0x731000`, leaving
about 830 KB free. No existing partition moves, so app sizes are unchanged.
Partition tables cannot change over OTA, so every device is re-flashed over USB
(already the plan).

## NVS layout

Partition `certs`, namespace `devid`. NVS key names are at most 15 characters.
Blobs are used for anything that may exceed the 4,000-byte NVS string limit
(the ML-DSA-44 certificate is 4.1 KB).

| Key | Type | Content | Approx size |
|---|---|---|---|
| `fmt` | u8 | Layout version: `2` (`1` = legacy, no signing roots) | 1 B |
| `serial` | string | Device serial (e.g. `FC22E0`) | 6 B |
| `wifi_ssid` | string | WiFi SSID | <33 B |
| `wifi_psk` | string | WiFi password | <64 B |
| `ca_cls` | blob | Classical CA root, DER | 471 B |
| `ca_mldsa` | blob | ML-DSA-44 CA root, DER | 4,089 B |
| `sig_cls` | blob | ECDSA P-256 firmware-signing root, DER (OTA trust only) | ~480 B |
| `sig_mldsa` | blob | ML-DSA-44 firmware-signing root, DER (OTA trust only) | ~4,100 B |
| `crt_cls` | blob | Classical device cert, DER | ~550 B |
| `key_cls` | blob | P-256 private key, DER | ~140 B |
| `crt_mldsa` | blob | ML-DSA-44 device cert, DER | ~4,170 B |
| `key_mldsa` | blob | ML-DSA-44 private key as seed-only PKCS#8 DER | 54 B |

Total is about 14.2 KB in a 64 KB partition (9.6 KB before the signing roots). CA root sizes are measured from pqc-a; device cert sizes are
taken from the broker certs issued by the same CAs; key sizes are estimates.

Firmware opens the partition read-only (`Preferences.begin("devid", true,
"certs")`, or `nvs_flash_init_partition("certs")`) and loads DER with
`WOLFSSL_FILETYPE_ASN1`.

## ML-DSA-44 seed

**Decision (spike 0.2, passed on hardware):** store the ML-DSA-44 key as the
54-byte seed-only PKCS#8 DER (`openssl pkey -outform DER -provparam
ml-dsa.output_formats=seed-only`) under `key_mldsa`, and load it with
`wolfSSL_CTX_use_PrivateKey_buffer(..., WOLFSSL_FILETYPE_ASN1)`, exactly like the
classical key. wolfSSL 5.8.4 expands the seed itself. No derivation code is
needed in the firmware. The notes below are the original analysis.

**wolfSSL support (checked).** The Arduino wolfSSL library is 5.8.4
(`Arduino-wolfSSL` `library.properties`). Its `dilithium.h` declares
`wc_dilithium_make_key_from_seed(dilithium_key*, const byte* seed)`, and the
wolfSSL 5.8.4 ChangeLog lists "New ML-KEM / ML-DSA APIs and seed/import PKCS8
support". The device build (`wolfssl_user_settings_arduino.h`) enables
`HAVE_DILITHIUM`, `WOLFSSL_WC_DILITHIUM` and `WOLFSSL_DILITHIUM_SIGN`, and does
not define `WOLFSSL_DILITHIUM_NO_MAKE_KEY` or `WOLFSSL_DILITHIUM_VERIFY_ONLY`.

**Issuer side.** Generate 32 random bytes as the FIPS 204 seed (ξ) and derive the
key from it with OpenSSL 3.5, then build the CSR from that key. Planned command:
`openssl genpkey -algorithm ML-DSA-44 -pkeyopt hexseed:<64 hex chars>`.
**To verify in task 0.2**: that OpenSSL 3.5 accepts this option.

**Device side, option A (preferred).** At boot, call
`wc_dilithium_make_key_from_seed()` with the stored seed, export the key to DER
(`wc_Dilithium_KeyToDer`) into RAM, and load it into the TLS context. The key
derived from ξ is deterministic in FIPS 204, so it must match the certificate.

**Device side, option B.** Store the seed as a seed-only PKCS#8 DER and load it
directly, using the 5.8.4 seed PKCS#8 import.

**Fallback.** If neither works, store the full ML-DSA private key DER
(`key_mldsa`, about 2.6 KB). It still fits comfortably.

**Risks to check in the spike.** Key derivation time and peak RAM on the
ESP32-S3; that OpenSSL and wolfSSL agree on the seed-to-key derivation (both
target FIPS 204 final); and that the derived public key equals the
certificate's.

## Identity CodeBuild project

- Image: `pqc-workshop-cert-issuer` (already has OpenSSL 3.5 and
  `issue_cert.py`). Add a pinned `esp-idf-nvs-partition-gen` from pip.
- Source: none. The buildspec is inline in the project (`python3
  /var/task/issue_device_identity.py`); the script is baked into the image, so
  nothing extra is staged by deploy-workshop.sh.
- Environment: `DEVICE_SERIAL` (override), both CA ARNs, `FIRMWARE_BUCKET`,
  and the WiFi SSID and password as `PARAMETER_STORE` values from
  `/pqc-workshop/wifi/*`.
- Role: `acm-pca:IssueCertificate`/`GetCertificate`/
  `GetCertificateAuthorityCertificate` on both CAs, `ssm:GetParameters` plus
  `kms:Decrypt` via SSM on the two WiFi parameters, `s3:PutObject` on
  `<firmware-bucket>/identity/*`, logs scoped to the project.
- Output: `identity/<serial>/certs.bin` only. No firmware compile.
- The registrar also still starts the per-device firmware build; shared
  images (workstream 4) are out of scope for now.

The Pi needs `s3:DeleteObject` and `s3:DeleteObjectVersion` on `identity/*`.
An S3 lifecycle rule on the `identity/` prefix expires current and noncurrent
versions after 1 day as a backstop.

## Firmware loading

Shared helper in each sketch:
1. Open `certs`/`devid` read-only; read `fmt` and refuse an unknown version.
   Log with ESP_LOG/printf, not `Serial` (on this board `Serial` is UART0).
2. Read the WiFi strings and the blobs for the active variant: the classical CA
   root, cert and key for classical and PQC; the ML-DSA CA root, cert and
   seed-derived key for ML-DSA. Load both firmware-signing roots for OTA
   verification; the OTA verifier uses only the one matching the image's
   sigAlg and requires the codeSigning EKU, and never the device CA roots
   (T024). A format-1 identity loads for TLS and WiFi but refuses every signed
   OTA until the device is reflashed.
3. Load into wolfSSL with `WOLFSSL_FILETYPE_ASN1`.
4. On any failure, log `[Setup] identity missing/invalid: <reason>` and stay
   offline (no connect attempts).

`secrets.h` keeps only non-secret build settings (broker endpoint, firmware
version, OTA signature label, log level). The broker endpoint can move to NVS
later.

## Shared images (workstream 4)

With identity out of the image, each variant needs one build per deployment:
`s3://<firmware-bucket>/shared/<version>/` holding `firmware.bin`,
`firmware.merged.bin`, the signature and the signer cert. Migration skips
`WaitForBuild` and publishes the shared image. The registrar's first flash uses
the shared classical image.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Where identity lives | Separate `certs` NVS partition | Default `nvs` is too small, and WiFi/PHY data would compete |
| Encoding | DER blobs | Fits; avoids the NVS string limit; halves PEM size |
| ML-DSA key | 32-byte seed (if spike passes) | Smallest; wolfSSL 5.8.4 supports it |
| When identity is created | At registration on plug-in | No serial list to maintain |
| Who creates it | Shared cert-issuer in CodeBuild | Same tested path as today; OpenSSL 3.5 already there |
| Encryption | None this phase | Flash readout accepted for the workshop (T005) |

## Threat-model effect

Removes per-device keys and the WiFi password from firmware images in S3
(T003, T011) and removes the plaintext WiFi password from the build
environment (part of T013). Identity images exist in S3 only between issuance
and flashing. Update the threat model when this lands.
