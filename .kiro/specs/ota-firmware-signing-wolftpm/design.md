# Design — OTA Firmware Signing + On-Device Verification (wolfTPM software TPM)

> Status: **draft design** for review. Grounded in the current OTA/build/cert
> code (see "Current-state anchors"). Two items are explicit **feasibility gates**
> resolved before implementation locks: (G1) wolfTPM-software on ESP32-S3, and
> (G2) producing a PCA-chained firmware signature the device can verify. The
> firmware version progression and the trusted-vs-untrusted split are locked.

## Locked design decisions

1. **Verifier ships in ALL variants; signature algorithm migrates.** Every
   firmware variant carries the OTA verifier. 2.0.0-pqc and 3.0.0-mldsa OTAs are
   signed with a **classical** signature (classical CA); 4.0.0-signer cuts over to
   **ML-DSA-44** (ML-DSA CA); 5.0.0+ stay ML-DSA-44. The classical USB flash
   (1.0.0) is not verified against itself (trusted factory) but still ships the
   verifier for the first OTA it receives.
2. **Detached signature artifact alongside the firmware.** The build produces
   `firmware.bin.sig` under the same `s3://<bucket>/<serial>/<version>/` prefix as
   the existing artifacts. The OTA command carries the signature (or its presigned
   URL) + algorithm + signer-cert reference.
3. **PCA issues a SIGNER certificate; signing is done locally.** ACM PCA signs
   CSRs, not blobs, so we issue a signer cert (reusing `issue_cert.py`) and sign
   `firmware.bin` locally with the signer private key (OpenSSL stock ECDSA /
   OpenSSL 3.5 ML-DSA-44). The device verifies the signature against the signer
   public cert, whose chain roots in the PCA the device already trusts.
4. **Verify BEFORE boot switch.** Verification happens after download completes
   (`esp_ota_end`) and BEFORE `esp_ota_set_boot_partition`. On failure:
   `esp_ota_abort`, do not switch boot, report — running firmware untouched.
5. **Also enforce the already-transmitted SHA-256.** The device will check the
   `sha256` field (sent today, currently ignored) as a cheap pre-check before the
   signature verify.

## Feasibility gate G1 — wolfTPM software TPM on ESP32-S3 (spike task 1.x)

- **wolfTPM is NOT in the repo today.** wolfSSL IS, and already has
  `WOLFSSL_DILITHIUM_VERIFY` (ML-DSA-44 verify) and `HAVE_ECC_VERIFY`+`HAVE_ECC256`
  (ECDSA-P256 verify) + SHA-256 compiled in (`wolfssl_user_settings_arduino.h`).
  So the raw verify capability already exists on-device.
- **Spike must answer:** does wolfTPM's software/simulator backend
  (`WOLFTPM2_*`, TPM software mode / `--enable-swtpm`, or the `wolfTPM` Arduino
  library) build and link in the arduino-cli / ESP32-S3 wolfSSL setup, and what is
  its incremental flash + heap cost on top of ML-KEM + ML-DSA (fw3 min-heap was
  already ~82 KB)? Must stay under the 0x380000 (3.5 MB) per-slot ceiling and keep
  a safe handshake+verify min-heap.
- **Decision rule (records the user's intent + the fallback):** the intended
  mechanism is the wolfTPM software TPM. IF the spike shows it links and fits with
  acceptable headroom, implement the verifier through wolfTPM. IF it is infeasible
  or costs too much memory on the S3, fall back to wolfSSL's already-compiled
  verify primitives (`wc_ecc_verify_hash` / `wc_dilithium_verify_msg`) under a
  documented "software TPM" framing, and record the tradeoff in this doc. Either
  way the on-device behaviour (verify signature against PCA-chained signer cert
  before boot) is identical.

## Feasibility gate G2 — PCA-chained firmware signature (spike task 1.x)

- **Mechanism:** generate a signer keypair + CSR and issue a **signer certificate**
  from the appropriate PCA using the EXISTING `shared/cert-issuer/issue_cert.py`
  path (`ec-p256` → classical CA / `SHA256WITHECDSA`; `mldsa44` → ML-DSA CA /
  `ML_DSA_44`). Then sign `firmware.bin` LOCALLY with the signer private key
  (`openssl dgst -sign` for ECDSA; OpenSSL 3.5 ML-DSA sign for mldsa44). Upload the
  detached signature + the signer public cert.
- **Spike must answer / confirm:**
  - Signature encoding the device verifier can parse (raw vs DER for ECDSA;
    ML-DSA-44 signature is ~2.4 KB — confirm wolfSSL/wolfTPM verify input format).
  - Whether the device verifies against the signer LEAF cert's public key and
    separately validates the leaf chains to the trusted PCA root, or verifies the
    chain in one step. (wolfSSL cert-chain verify exists; ML-DSA leaf under a root
    is the Step-2 pattern already proven for TLS certs.)
  - Signer key lifecycle: a **stable per-era signer** (one classical signer for the
    2.0.0/3.0.0 era, one ML-DSA signer for the 4.0.0+ era) is preferable to a
    per-build ephemeral key, so the device only needs ONE signer cert per era
    durably stored. Confirm where the signer key lives between builds (e.g.
    Secrets Manager, like the broker cert) vs regenerated per build.

## Guiding principles

- **Trusted factory vs untrusted field.** USB flash on the builder-Pi is trusted
  and unsigned; every OTA is signed and verified. This is the security spine of
  the feature.
- **Parallel, reversible, per-device.** The signature era migrates over the
  existing OTA path one step at a time, like the ML-KEM and cert migrations.
- **Reuse, don't duplicate.** Extend `publish-ota`, the migration Step Function,
  `buildspec.yml` post_build, `issue_cert.py`, and the device `otaTask`.

## Current-state anchors (what exists today)

- **Cloud OTA (`raw_device_builder/lambda/publish-ota/index.py`):** computes
  `sha256` of `firmware.bin` and publishes
  `{"action":"ota-update","url","version","size","sha256","expiresIn"}` to
  `cmd/<thing>/ota` (QoS 1), 15-min presigned URL, marks registry `ota-pending`.
  **The signature field is added here.**
- **Device OTA (`aws_iot_pqc_esp32*/...ino` `otaTask` + `mqttMessageHandler`):**
  parses url/version/size (IGNORES `sha256`), downloads over TLS with
  `WOLFSSL_VERIFY_NONE`, streams 4 KB chunks via `esp_ota_begin/write/end` into
  `esp_ota_get_next_update_partition`, then `esp_ota_set_boot_partition` +
  `esp_restart`. **No integrity or signature check today.** The three sketches'
  OTA handlers are functionally identical. **The verify step is inserted between
  `esp_ota_end` and `esp_ota_set_boot_partition`.**
- **Cert issuer (`shared/cert-issuer/issue_cert.py`):** `KEY_MODES` = {ec-p256 →
  SHA256WITHECDSA, mldsa44 → ML_DSA_44}; generates key+CSR (stock openssl / OpenSSL
  3.5), `acm_pca.issue_certificate`, returns/writes ca.crt/cert.crt/key.pem.
  **Reused to mint the signer cert; local signing is a new step next to it.**
- **Build (`raw_device_builder/codebuild/buildspec.yml`):** variant case block maps
  FIRMWARE_VARIANT → sketch/CA/key-mode/port; issues device cert in-build; post_build
  uploads `firmware.bin`, `firmware.merged.bin`, `bootloader.bin`, `partitions.bin`,
  `boot_app0.bin`, `metadata.json` to `<serial>/<version>/`. **`firmware.bin.sig`
  (+ signer cert) join here.**
- **wolfSSL config (`raw_device_builder/codebuild/wolfssl_user_settings_arduino.h`):**
  ML-KEM + ML-DSA sign/verify + ECDSA verify + SHA-256 already enabled;
  `MAX_CERTIFICATE_SZ=16384`. **wolfTPM absent.**
- **Two ACM PCAs (`broker-container/cloudformation/broker-container-stack.yaml`):**
  classical `PrivateCA` (EC_prime256v1) + `MlDsaPrivateCA` (ML_DSA_44), exported as
  `PrivateCAArn`/`MlDsaPrivateCAArn`, wired into the build as `CA_ARN`/`MLDSA_CA_ARN`.
- **Partitions (`esp32-flash/partitions_pqc_workshop.csv`):** 20 KB `nvs`, dual
  3.5 MB OTA slots, `nvs_key`, `coredump`. No SPIFFS/LittleFS. No NVS/Preferences
  code in the firmware. **Durable signer-cert storage is net-new.**

## Workstream 1 — Cloud signing (build + signer cert)

- Add a signer-cert + local-signing step. Prefer a **stable per-era signer**: at
  deploy (or first use), mint a classical signer cert (classical CA) and an
  ML-DSA-44 signer cert (ML-DSA CA) via `issue_cert.py`, store the signer key+cert
  in Secrets Manager (like the broker cert). The per-device build fetches the
  era-appropriate signer key, signs `firmware.bin` locally (OpenSSL stock ECDSA /
  OpenSSL 3.5 ML-DSA), and uploads `firmware.bin.sig`.
  - Variant → signer-era mapping (data, in `prepare-build` VARIANT_CONFIG +
    buildspec): 2.0.0-pqc, 3.0.0-mldsa → classical signer; 4.0.0-signer,
    5.0.0-verified, >=4.0.0 → ML-DSA-44 signer.
- Confirm signature encoding + size; publish the signer public cert to S3 (and/or
  bake into firmware per WS3).

## Workstream 2 — OTA command + publish-ota

- Extend the `cmd/<thing>/ota` payload with `signature` (or `sigUrl`), `sigAlg`
  (`ecdsa-p256` | `mldsa44`), and a signer-cert locator; keep all existing fields.
- `publish-ota` presigns `firmware.bin.sig` (same mechanism/expiry as the firmware)
  or inlines the signature if small enough for the 4 KB MQTT buffer (ML-DSA sig
  ~2.4 KB — borderline; prefer a presigned URL). Keep the existing `sha256`.

## Workstream 3 — Device verifier + durable signer cert

- **Verifier (G1 outcome):** wolfTPM-software or wolfSSL verify primitives. In
  `otaTask`, after `esp_ota_end`: (a) check `sha256`; (b) obtain the image hash
  (incremental hash during the `esp_ota_write` loop is preferred — the image is
  never fully in RAM; re-reading the partition is the fallback); (c) verify the
  signature with the era's signer public cert; (d) only on success
  `esp_ota_set_boot_partition`; on failure `esp_ota_abort` + report.
- **Durable signer cert (requirement 3) — MUST be in flash, NOT PSRAM.** PSRAM on
  the ESP32-S3 is volatile external RAM and is wiped by the `esp_restart()` the
  OTA flow performs, so it cannot hold the signer cert across the very reboot the
  verify protects; it may only serve as a boot-time working cache loaded FROM
  flash. Decide among (i) bake-into-firmware (simplest; in flash as part of the
  app; rotation = rebuild — acceptable, one signer per era), (ii) NVS (survives
  reboots AND is writable at runtime, so a fielded device can RECEIVE the ML-DSA
  signer cert over the air; fits the 20 KB partition but tight next to
  nvs_key/coredump), (iii) new/enlarged data partition (shrinks the 3.5 MB app
  slots — risky, avoid). Lean **bake-into-firmware for the classical-era cert**
  (it ships in 1.0.0/2.0.0/3.0.0 anyway) and store the **ML-DSA signer cert in NVS,
  delivered on/with the last classically-signed update** so it is present before
  the 4.0.0 cutover (requirement 3.4). Confirm in design detailing.
- **Cutover sequencing (requirement 3.4):** 3.0.0-mldsa (classically-signed OTA,
  verified with the classical signer cert baked in) ALSO carries/receives the
  ML-DSA-44 signer public cert and stores it durably; THEN 4.0.0-signer arrives
  ML-DSA-signed and is verified with that now-present ML-DSA signer cert. No
  chicken-and-egg.

## Workstream 4 — New variants 4.0.0-signer & 5.0.0-verified

- Extend `prepare-build` VARIANT_CONFIG + buildspec variant map with
  `4.0.0-signer` and `5.0.0-verified` (both ML-DSA-signed OTA). They reuse the
  pqc-mldsa sketch/transport (ML-KEM + ML-DSA cert on 8884); the NEW thing is the
  OTA-signature era + carrying the ML-DSA signer cert.
- Dashboard picker gains the new variants (consistent with the existing picker).
- Negative test: a deliberately bad/mis-signed OTA must be REJECTED (requirement
  5.3) — validate the verifier actually gates.

## Cross-cutting

- **check_setup_status.sh / deploy / teardown:** signer certs/secrets provisioned
  on a clean from-scratch deploy in both accounts; teardown cleans up signer
  secrets. Memory budget validated (WS gate).
- **Docs:** DEPLOYMENT.md + attendee content updated once implemented.

## Sequencing (proposed)

1. **Spikes first (G1 + G2):** wolfTPM-software feasibility on ESP32-S3, and the
   PCA-signer-cert + local-signing + on-device-verify round trip (prove one signed
   blob verifies on a device) — these gate the rest.
2. Workstream 1 (cloud signing + signer certs) — foundational.
3. Workstream 2 (OTA command + publish-ota) — needs the signature artifact.
4. Workstream 3 (device verifier + durable signer cert + SHA-256 check) — the core.
5. Workstream 4 (4.0.0-signer, 5.0.0-verified, negative test) — the proof.
6. Cross-cutting (dashboard, validation, docs) — last.

## Risks / to confirm in detailing

- wolfTPM-software build/link + memory on ESP32-S3 (G1) — may fall back to wolfSSL
  verify primitives.
- ML-DSA-44 signature size (~2.4 KB) vs MQTT buffer — likely deliver by presigned
  URL, not inline.
- Durable signer-cert storage vs the tight 20 KB nvs / 3.5 MB slot budget
  (flash only — PSRAM is volatile and ruled out as the store; cache-only).
- Classical→ML-DSA signer-cert cutover ordering (the trickiest bit).
- Incremental hashing of the streamed-to-flash image without holding it in RAM.
- Signer key lifecycle (stable per-era in Secrets Manager vs per-build).
