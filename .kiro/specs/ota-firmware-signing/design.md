# Design — OTA Firmware Signing + On-Device Verification (wolfSSL)

> Status: **draft design** for review. Grounded in the current OTA/build/cert
> code (see "Current-state anchors"). One remaining **feasibility gate** before
> implementation locks: (G2) producing a PCA-chained firmware signature the device
> can verify. (G1 — wolfTPM — is CLOSED BY DECISION: no TPM, verify with wolfSSL;
> see below.) The firmware version progression and the trusted-vs-untrusted split
> are locked.

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

## Decision — no TPM; verify with wolfSSL (was feasibility gate G1)

- **wolfTPM is dropped from the design.** A TPM's purpose is to hold and operate a
  *private key* without exposing it. This feature keeps NO private key on the
  device: the signing key lives in the cloud, and the device only performs
  **public-key verification** (against the baked-in CA root + the signer leaf that
  arrives with the OTA). With no secret to protect, a TPM (hardware or software /
  wolfTPM) adds no security — only flash/heap cost on the constrained ESP32-S3, and
  the memory-feasibility risk that G1 existed to probe. So G1 is closed by
  decision, not by a spike.
- **The verifier is plain wolfSSL, already compiled in.** The firmware's
  `wolfssl_user_settings_arduino.h` already enables `WOLFSSL_DILITHIUM_VERIFY`
  (ML-DSA-44 verify), `HAVE_ECC_VERIFY`+`HAVE_ECC256` (ECDSA-P256 verify), and
  SHA-256. Verification uses `wc_dilithium_verify_msg` / `wc_ecc_verify_hash`
  plus wolfSSL X.509 chain validation against the baked-in CA root — no new
  library, no toolchain change.
- **Security is unchanged by dropping the TPM:** the integrity/authenticity
  guarantee comes from the signature + the CA trust chain; whether wolfSSL or a
  TPM does the arithmetic is irrelevant when there is no private key to guard.

## Feasibility gate G2 — PCA-chained firmware signature (spike task 1.x)

- **Mechanism:** generate a signer keypair + CSR and issue a **signer certificate**
  from the appropriate PCA using the EXISTING `shared/cert-issuer/issue_cert.py`
  path (`ec-p256` → classical CA / `SHA256WITHECDSA`; `mldsa44` → ML-DSA CA /
  `ML_DSA_44`). Then sign `firmware.bin` LOCALLY with the signer private key
  (`openssl dgst -sign` for ECDSA; OpenSSL 3.5 ML-DSA sign for mldsa44). Upload the
  detached signature + the signer public cert.
- **Spike must answer / confirm:**
  - Signature encoding the device verifier can parse (raw vs DER for ECDSA;
    ML-DSA-44 signature is ~2.4 KB — confirm wolfSSL verify input format).
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

## Workstream 3 — Device verifier + baked-in CA trust anchor

- **Verifier (wolfSSL, no TPM):** use the already-compiled wolfSSL primitives
  (`wc_ecc_verify_hash` / `wc_dilithium_verify_msg`) + X.509 chain validation. In
  `otaTask`, after `esp_ota_end`: (a) check `sha256`; (b) obtain the image hash
  (incremental hash during the `esp_ota_write` loop is preferred — the image is
  never fully in RAM; re-reading the partition is the fallback); (c) verify the
  signature using the signer LEAF cert delivered WITH the OTA, and validate that
  leaf chains to the **baked-in CA root** for this era; (d) only on success
  `esp_ota_set_boot_partition`; on failure `esp_ota_abort` + report.
- **Trust anchor on device, signer leaf with the OTA (requirement 3).** What the
  device MUST hold durably is the **CA trust anchor** (the ACM Private CA root),
  not the per-signer leaf. The signer's private key signs the firmware; the device
  verifies with the signer leaf's public key AND checks that leaf chains to the CA
  root it trusts. Because the CA roots are known at deploy time and never change,
  **bake the CA root(s) into the firmware image** — in flash, un-substitutable, no
  runtime provisioning. The signer **leaf cert travels WITH the OTA** (in the
  signature bundle), validated against the baked-in root at verify time.
  - **MUST be flash, NOT PSRAM.** PSRAM is volatile external RAM, wiped by the
    `esp_restart()` the OTA performs; it may only be a boot-time cache loaded from
    flash. Baking the root into the app image satisfies this for free.
  - Storing the leaf in NVS is an OPTIONAL optimization; not required since the
    root is the anchor and the leaf ships per-OTA.
- **Cutover sequencing (requirement 3.5) — solved by baking the ROOT ahead.**
  Because the anchor is a baked-in CA root (not a runtime-pushed leaf), the
  chicken-and-egg dissolves: ship the **ML-DSA-44 CA root** baked into
  `3.0.0-mldsa` (which is itself classically-signed and verifiable with the
  baked-in CLASSICAL root). Once a device is on 3.0.0 it already trusts the ML-DSA
  root, so the ML-DSA-signed `4.0.0-signer` update verifies with no runtime cert
  push. (Both CA roots can simply be baked into every variant from 3.0.0 onward —
  they are ~1–4 KB each and known at build time.)

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

1. **Spike first (G2 only):** the PCA-signer-cert + local-signing +
   on-device-verify round trip (prove one signed blob verifies on a device with
   wolfSSL) — gates the rest. (G1/wolfTPM removed — no TPM.)
2. Workstream 1 (cloud signing + signer certs) — foundational.
3. Workstream 2 (OTA command + publish-ota) — needs the signature artifact.
4. Workstream 3 (device verifier + baked-in CA anchor + SHA-256 check) — the core.
5. Workstream 4 (4.0.0-signer, 5.0.0-verified, negative test) — the proof.
6. Cross-cutting (dashboard, validation, docs) — last.

## Risks / to confirm in detailing

- ML-DSA-44 signature size (~2.4 KB) vs MQTT buffer — likely deliver by presigned
  URL, not inline.
- Trust anchor = CA root baked into firmware (flash); PSRAM ruled out (volatile).
  Signer leaf travels with the OTA — no durable leaf storage needed.
- Classical→ML-DSA cutover: bake the ML-DSA CA root into 3.0.0-mldsa so it is
  present before the 4.0.0 ML-DSA-signed update arrives.
- Incremental hashing of the streamed-to-flash image without holding it in RAM.
- Signer key lifecycle (stable per-era in Secrets Manager vs per-build).
