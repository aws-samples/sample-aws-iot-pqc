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

### G2 spike RESULTS (proven in pqc-a, 2026-10-02 — gate CLEARED)

Both signature eras were proven end-to-end against the live pqc-a PCAs
(classical `3334d1fc…`, ML-DSA `8823ea83…`): issue signer cert from the PCA →
sign a 256 KB sample `fw.bin` with the signer private key → verify signature with
the signer leaf's public key → verify the leaf chains to the baked-in CA root.
All four checks (sign, sig-verify, chain-verify ×2) passed.

- **Classical era (2.0.0 / 3.0.0 — ECDSA-P256 / SHA-256):**
  - Sign over the **SHA-256 digest** (`openssl dgst -sha256 -sign`).
  - Signature is **DER ASN.1 `SEQUENCE { INTEGER r, INTEGER s }`, ~70–72 bytes**
    (71 B observed). **No DER→raw r‖s conversion needed on-device** — wolfSSL
    `wc_ecc_verify_hash` accepts the DER signature directly. (Contrast the KMS COSE
    case, which needs raw r‖s; this PCA/OpenSSL path does not.)
  - Signer leaf PEM ~814 B; EC public key DER 91 B.
- **ML-DSA-44 era (4.0.0+ — pure PQC signature):**
  - Sign over the **raw firmware bytes** (`openssl pkeyutl -sign -rawin`), NOT a
    pre-hash — ML-DSA is a pure scheme that hashes internally (SHAKE). The device
    passes the FULL image to `wc_dilithium_verify_msg`, not a SHA-256 digest. This
    is a real divergence from the ECDSA path: the two eras feed the verifier
    different inputs (digest vs message). (Image is never fully in RAM on-device,
    so verify must stream the partition content to the ML-DSA verifier.)
  - Signature is **fixed 2420 bytes** → too large for the 4 KB MQTT buffer margin;
    **deliver the signature via a presigned S3 URL**, not inline in the OTA payload.
  - Signer leaf PEM ~5657 B; ML-DSA public key DER 1334 B.
- **Device trust-anchor (CA root) sizes to bake into flash:** classical CA root
  PEM **696 B**; ML-DSA CA root PEM **5592 B** (~5.5 KB). Baking both into every
  variant from 3.0.0 onward costs ~6.3 KB of app image — trivial.
- **EKU caveat (flagged open question — resolved for the workshop):** the default
  ACM PCA `EndEntityCertificate/V1` template stamps **TLS EKUs** (Web Server/Client
  Auth) + KeyUsage `Digital Signature` (EC cert also `Key Agreement`), NOT
  `id-kp-codeSigning`. This does **not** block the workshop: the device's manual
  chain-to-baked-root + signature-verify is the security property that matters, and
  wolfSSL's raw verify primitives + an opt-in chain check do not enforce EKU. A
  proper code-signing EKU would require a custom PCA template via `ApiPassthrough`
  extensions (not a built-in PCA template) — deferred, not required.
- **OpenSSL availability:** the host/toolchain OpenSSL (3.6.3) has **native
  ML-DSA-44** support — the dedicated `/opt/openssl-3.5` build used by the
  cert-issuer image is sufficient but not strictly required for signing; any
  OpenSSL ≥3.5 works.
- **Signer lifecycle (confirmed):** stable **per-era** signer (one classical, one
  ML-DSA-44), key stored in **Secrets Manager** like the broker cert; the per-build
  step fetches the era-appropriate signer key rather than regenerating per build.

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
- `publish-ota` presigns `firmware.bin.sig` (same mechanism/expiry as the firmware).
  **G2 settled this: always use a presigned URL**, not inline — the ML-DSA-44
  signature is a fixed 2420 B, which does not fit the 4 KB MQTT buffer with headroom
  (the ECDSA sig is ~71 B and would fit, but use ONE delivery mechanism for both
  eras). Keep the existing `sha256`.

## Workstream 3 — Device verifier + baked-in CA trust anchor

- **Verifier (wolfSSL, no TPM):** use the already-compiled wolfSSL primitives
  (`wc_ecc_verify_hash` / `wc_dilithium_verify_msg`) + X.509 chain validation. In
  `otaTask`, after `esp_ota_end`: (a) check `sha256`; (b) verify the signature using
  the signer LEAF cert delivered WITH the OTA, and validate that leaf chains to the
  **baked-in CA root** for this era; (c) only on success
  `esp_ota_set_boot_partition`; on failure `esp_ota_abort` + report.
  - **Verifier input differs by era (G2 fact):** the ECDSA path verifies a
    **SHA-256 digest** of the image (`wc_ecc_verify_hash` over the 32-byte hash) —
    an incremental SHA-256 during the `esp_ota_write` loop is ideal since the image
    is never fully in RAM. The ML-DSA-44 path verifies the **full image message**
    (`wc_dilithium_verify_msg`), because ML-DSA hashes internally; the image must be
    streamed from the OTA partition into the verifier (re-read the just-written
    partition), as it cannot be held in RAM. Plan for both input shapes in the
    verify step.
  - **ECDSA signature is DER** (`SEQUENCE{r,s}`, ~71 B) — pass it straight to
    `wc_ecc_verify_hash`, no raw-r‖s conversion. **ML-DSA signature is a fixed
    2420 B** opaque blob.

### Workstream 3 implementation notes (device verifier — grounded)

The three sketches' `otaTask` bodies are byte-identical, so ONE patch applies to
all three (`aws_iot_pqc_esp32`, `aws_iot_pqc_esp32_v1_classical`,
`aws_iot_pqc_esp32_v3_mldsa`); only per-variant `FIRMWARE_VERSION` /
`CERT_SIGNATURE_ALG` differ and are untouched.

- **Where the image lives at verify time.** The 3.5 MB image is streamed to the
  OTA flash partition via `esp_ota_write` and is NEVER fully in RAM. The verify
  gate sits between `esp_ota_end` and `esp_ota_set_boot_partition` (so a bad
  signature means the boot partition is never switched; the running firmware
  stays intact and we `esp_ota_abort` was already past — we simply skip the
  set-boot and report).
- **ECDSA-P256 path (classical era):** compute SHA-256 **incrementally during the
  download loop** (`wc_Sha256_Update` on each 4 KB chunk, alongside
  `esp_ota_write`), finalize to a 32-byte digest after the loop, then
  `wc_ecc_verify_hash(sigDer, sigLen, digest, 32, &res, &eccKey)` — the DER
  signature is accepted directly. Cheap: a `wc_Sha256` ctx + a small `ecc_key`.
- **ML-DSA-44 path (4.0.0+ era):** ML-DSA is a PURE scheme — `wc_dilithium_verify_msg`
  (confirmed: no streaming/update variant exists) needs the FULL image as ONE
  contiguous buffer, and it hashes internally (SHAKE), so a pre-hash is useless.
  Resolution: after `esp_ota_end`, **memory-map the written OTA partition**
  read-only with `esp_partition_mmap(..., ESP_PARTITION_MMAP_DATA, &ptr, &handle)`
  to get a zero-copy contiguous pointer to the whole image, pass that pointer +
  `totalRead` to `wc_dilithium_verify_msg`, then `esp_partition_munmap`. The
  ESP32-S3 MMU has ample address space (32 MB theoretical) to map a 3.5 MB
  partition; this avoids copying the image into RAM. The `dilithium_key` struct
  is multi-KB — heap/PSRAM-allocate it (`heap_caps_malloc(..., MALLOC_CAP_SPIRAM)`),
  do NOT place it on the 32 KB OTA task stack.
- **Signer leaf → public key + chain-to-root.** The signer LEAF arrives per-OTA
  (`signerCertUrl`); fetch it into a RAM buffer (~5.7 KB PEM, under
  `MAX_CERTIFICATE_SZ`=16384). Extract its public key for the verify, AND validate
  the leaf chains to the **baked-in CA root** via a wolfSSL `WOLFSSL_CERT_MANAGER`
  (`wolfSSL_CertManagerLoadCABuffer(root)` + `wolfSSL_CertManagerVerifyBuffer(leaf)`).
  Both the signature-verify AND the chain-check must pass before set-boot.
- **Trust anchor delivery (correction to earlier note).** The CA root is NOT a
  single hardcoded `.ino` const like `S3_ROOT_CA`: the workshop PCA root differs
  per deployed account, so — like `AWS_CERT_CA` — it is injected by the build into
  `secrets.h`. WS1 added the signer CA-root secrets (`pqc-broker/signer-*-ca`);
  the build writes the era's root into `secrets.h` as a new PROGMEM const
  (`OTA_SIGNER_CA_ROOT`). The ML-DSA root is baked into `3.0.0-mldsa` onward so a
  device already trusts it before the first ML-DSA-signed `4.0.0` update arrives.
- **New OTA job globals + parsing.** `mqttMessageHandler`'s `ota-update` branch
  (hand-rolled `jsonGetString`) also reads `sigUrl`, `sigAlg`, `signerCertUrl`
  into new globals (`g_otaSigUrl`, `g_otaSignerCertUrl`, `g_otaSigAlg`). These are
  OPTIONAL: if absent (unsigned 1.0.0 push, or an unsigned legacy command) the
  device behaves exactly as today — but a signed-era device SHOULD refuse an
  unsigned image for its era (policy: require a signature when `sigAlg` is set;
  for the workshop, verify-when-present and log loudly when absent).
- **Download helper reuse.** Factor the URL-parse → TCP → `otaCtx` TLS (verify
  off, `S3_ROOT_CA` loaded) → HTTP GET → header/body read into
  `httpsGetToBuffer(url, out, cap, &outLen)` for the small `.sig` (≤2420 B) and
  `signer.crt` (~5.7 KB); the firmware itself keeps streaming to `esp_ota_write`
  (never buffered).

### WS3 verification status + open risks (MUST close in WS4 on real hardware)

The WS3 device code is implemented and statically checked (identical verifier
across all three sketches, balanced braces, wolfSSL API calls grounded against
the real `wolfssl/wolfcrypt/dilithium.h` header). It has NOT been compiled or
run on-device yet — there is no ESP32/wolfSSL toolchain in the dev environment,
so these are the explicit gates WS4 must clear with a real CodeBuild + flash:

- **Compile gate.** Confirm the firmware-builder image's wolfSSL exposes
  `wc_dilithium_verify_ctx_msg`, `wc_Dilithium_PublicKeyDecode` (needs
  `WOLFSSL_MLDSA_PUBLIC_KEY`, now set in the user settings), `wc_ecc_verify_hash`,
  `wc_EccPublicKeyDecode`, `DecodedCert`/`wc_ParseCert`, and that
  `esp_partition_mmap(..., ESP_PARTITION_MMAP_DATA, ...)` matches the installed
  ESP-IDF signature. First real build will surface any symbol/signature drift.
- **ECDSA cross-impl (LOW risk).** OpenSSL `dgst -sha256 -sign` DER sig →
  wolfSSL `wc_ecc_verify_hash` is standard and the encoding was proven in G2.
- **ML-DSA cross-impl (MODERATE risk — the real unknown).** G2 proved OpenSSL
  3.5 sign → OpenSSL verify, NOT OpenSSL → wolfSSL. FIPS 204 pure ML-DSA signs
  over a context+message; `pkeyutl -sign -rawin` uses an EMPTY context. The
  device uses `wc_dilithium_verify_ctx_msg(..., ctx=NULL, ctxLen=0, ...)` to
  match an empty context. WS4 MUST prove an OpenSSL-3.5-signed image verifies on
  the device; if it does not, the likely fixes are (a) the ctx variant/length,
  or (b) switching the build to a signing path whose context convention matches
  wolfSSL. Do NOT consider ML-DSA OTA signing done until a device accepts a good
  signature AND rejects a tampered one.
- **mmap size (LOW/MODERATE).** Mapping a ~3.5 MB OTA partition needs enough
  contiguous MMU vaddr; the S3 has ample, but confirm `esp_partition_mmap`
  succeeds for a full image on the XIAO ESP32-S3 under real heap pressure.
- **Memory (LOW/MODERATE).** `dilithium_key` is PSRAM-allocated; confirm free
  PSRAM at OTA time and that the 32 KB OTA task stack suffices with the verify
  added (bump `OTA_TASK_STACK_SIZE` if needed).
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
