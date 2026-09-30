# Requirements — OTA Firmware Signing + On-Device Verification (wolfTPM software TPM)

## Overview

This feature adds **integrity + authenticity to over-the-air (OTA) firmware
updates**: firmware delivered to a fielded device is **signed in the cloud**, and
the device **verifies that signature before applying the update** (before
`esp_ota_set_boot_partition` / reboot). It is an absolute workshop requirement
and the terminal chapter of the PQC migration story: after Step 1 (ML-KEM key
exchange) and Step 2 (ML-DSA-44 certificate), Step 3 makes the **firmware supply
chain** itself quantum-safe by migrating the OTA signature from a classical
algorithm to ML-DSA-44.

The trust model is split by delivery environment:

- **Builder-Pi initial flash = TRUSTED (factory) environment.** The operator
  physically controls the board and flashes over USB. The initial classical
  flash (`1.0.0-classical`) is therefore **not signed and not verified**.
- **OTA (device in the field) = UNTRUSTED delivery path.** Every OTA update MUST
  be signed, and the device MUST verify the signature before applying it.

### Grounding in the current code (what exists today)

- **The cloud already computes and transmits a SHA-256** of `firmware.bin` in the
  `cmd/<thing>/ota` payload (`raw_device_builder/lambda/publish-ota/index.py`:
  `{"action":"ota-update","url","version","size","sha256","expiresIn"}`), **but
  the device never reads or checks it.** There is a latent integrity gap even
  before signatures.
- **The device OTA path does NO verification.** `otaTask` (in all three sketches)
  downloads over TLS with `WOLFSSL_VERIFY_NONE` and streams bytes straight into
  the inactive OTA partition via `esp_ota_begin/write/end`, then
  `esp_ota_set_boot_partition` + reboot. The only checks are byte-count
  completeness and `esp_ota_end`'s built-in image CRC.
- **wolfSSL already has the verify primitives compiled in.** The firmware's
  `wolfssl_user_settings_arduino.h` enables **ML-DSA-44 verify**
  (`HAVE_DILITHIUM`, `WOLFSSL_DILITHIUM_VERIFY`) AND **ECDSA-P256 verify**
  (`HAVE_ECC_VERIFY`, `HAVE_ECC256`), plus SHA-256. So both signature algorithms
  in the progression can be verified on-device today with wolfSSL.
- **wolfTPM is NOT in the codebase** (only referenced in the backlog). It is a
  proposed *framing/mechanism* for the verifier, to be evaluated against the
  ESP32-S3 memory budget vs. using wolfSSL's verify primitives directly.
- **ACM PCA signs CSRs (issues certificates), not arbitrary blobs.** Both CAs
  exist: classical `EC_prime256v1` (`CA_ARN`) and `ML_DSA_44` (`MLDSA_CA_ARN`).
  Firmware signing therefore means: issue a *signer certificate* from the PCA for
  a signing keypair, sign the firmware locally with that key (OpenSSL — stock for
  ECDSA, OpenSSL 3.5 for ML-DSA-44, exactly as `shared/cert-issuer/issue_cert.py`
  already does keygen), and have the device verify against the signer public cert
  whose chain roots in the PCA it already trusts.
- **No durable key/value store is used on the device today.** No NVS/Preferences
  code exists; the partition table (`esp32-flash/partitions_pqc_workshop.csv`)
  has a 20 KB `nvs` partition, dual 3.5 MB OTA app slots, no SPIFFS/LittleFS.
  Durable signer-cert storage is net-new.

## Design intent (governs all requirements)

- **Verifier ships in ALL firmware variants.** Every variant (classical, pqc,
  pqc-mldsa, and the new signer/verified variants) carries the software-TPM /
  wolfSSL verifier from here on. Only the signature ALGORITHM it checks changes
  as the migration progresses. The classical initial flash is exempt from
  *verification of itself* (trusted USB flash) but still ships the verifier so it
  can verify the FIRST OTA it receives.
- **Signature-algorithm progression mirrors the crypto-migration narrative**
  (classical signature first, ML-DSA-44 later), running in parallel — a device is
  migrated over the existing OTA path, one step at a time, reversibly.
- **Reuse existing machinery.** Extend the migration Step Function, `publish-ota`,
  the per-device CodeBuild, `shared/cert-issuer/issue_cert.py`, and the device
  `otaTask` — do NOT build parallel infrastructure.

### Firmware version map (extends classical/pqc/pqc-mldsa)

```
1.0.0-classical  unsigned, USB-flashed on the trusted builder-Pi (no verify of self)
2.0.0-pqc        ML-KEM, classical cert, OTA signed with a CLASSICAL signature (classical CA)
3.0.0-mldsa      ML-KEM + ML-DSA cert, OTA signed with a CLASSICAL signature (classical CA)
4.0.0-signer     signer cutover: OTA signed with an ML-DSA-44 signature (ML-DSA CA)
5.0.0-verified   ML-DSA-44-signed; its successful OTA is the PROOF the device verifies ML-DSA signatures
>= 4.0.0         all subsequent updates use ML-DSA-44 signatures
```

The verifier is present in every variant; the signature it checks is classical
for 2.0.0/3.0.0 and ML-DSA-44 from 4.0.0 onward.

## Requirements

### 1. Cloud-side firmware signing

**User story:** As the workshop operator, I want each OTA firmware artifact
signed by a key whose certificate is issued by the appropriate ACM Private CA, so
that a fielded device can prove the firmware came from a trusted source.

Acceptance criteria:
1. WHEN the per-device firmware build produces `firmware.bin` for an OTA-delivered
   variant THEN the build SHALL produce a detached signature over that exact
   artifact and upload it alongside the firmware under the same S3 prefix
   (`<serial>/<version>/`), e.g. `firmware.bin.sig`.
2. The signature SHALL be produced with a **signer private key whose signer
   certificate is issued by an ACM Private CA** — the CLASSICAL CA (`CA_ARN`) for
   `2.0.0-pqc` and `3.0.0-mldsa`, and the **ML-DSA-44 CA (`MLDSA_CA_ARN`)** for
   `4.0.0-signer` and all later variants.
3. The signing key SHALL be generated and used within the build/signing
   environment only (never shipped to the device); only the signer *public*
   certificate is provisioned to devices. (Mirrors the existing device-cert
   pattern where the private key never leaves the build.)
4. WHERE the initial classical flash (`1.0.0-classical`) is built for USB
   flashing on the builder-Pi, the system SHALL NOT require a signature (trusted
   environment) — signing applies to OTA-delivered variants.
5. The signature scheme and signer-cert reference SHALL be conveyed to the device
   (see requirement 3) so the device knows which algorithm to verify with.

### 2. OTA command carries the signature

**User story:** As a fielded device, I want the OTA command to tell me the
signature (and how to verify it), so I can fetch and check it before applying the
update.

Acceptance criteria:
1. WHEN `publish-ota` issues the `cmd/<thing>/ota` command THEN the payload SHALL
   include the signature material — the signature (or a presigned URL to
   `firmware.bin.sig`), the signature algorithm (classical vs ML-DSA-44), and
   enough to select/locate the signer public certificate.
2. The existing `sha256`, `url`, `version`, `size`, `expiresIn` fields SHALL be
   preserved (backward-compatible superset).
3. IF the signature material is delivered by URL THEN it SHALL use the same
   short-lived presigned-URL mechanism as the firmware, with a lifetime covering
   a device reconnect + download.

### 3. Signer public certificate provisioned & durable on-device

**User story:** As a fielded device, I need the signer's public certificate
available and surviving reboots, so I can verify OTA signatures offline.

Acceptance criteria:
1. The device SHALL have the signer public certificate available to the verifier
   at OTA time, and it SHALL **survive a power cycle / reboot** (PSRAM alone is
   volatile and insufficient).
2. The system SHALL define how the signer cert is delivered and stored durably
   (candidates: baked into firmware at build, stored in NVS, a dedicated flash
   region; possibly cached into PSRAM at boot for the verify path). The design
   SHALL choose one and justify it against the 20 KB `nvs` partition / partition
   layout constraints.
3. The device SHALL verify that the signer certificate **chains to an ACM Private
   CA it trusts** (the classical CA for 2.0.0/3.0.0, the ML-DSA-44 CA from 4.0.0),
   not merely trust a bare public key.
4. WHEN the signer algorithm cuts over classical → ML-DSA-44 at `4.0.0-signer`
   THEN the ML-DSA-44 signer public cert SHALL be in place on the device BEFORE it
   must verify a 4.0.0+ update. The design SHALL specify the cutover sequencing so
   there is no chicken-and-egg (e.g. the ML-DSA signer cert is delivered on/with
   the last classically-signed update, or provisioned ahead of the cutover).

### 4. On-device signature verification before apply (wolfTPM software TPM)

**User story:** As a fielded device, I want to verify the firmware signature
before I boot it, so a tampered or untrusted image is rejected.

Acceptance criteria:
1. WHEN a device receives an `ota-update` command and downloads the firmware THEN
   it SHALL verify the signature over the downloaded image BEFORE calling
   `esp_ota_set_boot_partition` (i.e. after `esp_ota_end`/download-complete and
   before the boot switch).
2. IF the signature is invalid, missing, or does not verify against the trusted
   signer cert THEN the device SHALL **abort the OTA** (`esp_ota_abort`), NOT set
   the boot partition, and report the failure — the running firmware stays intact.
3. The verifier SHALL support the classical signature (for 2.0.0/3.0.0) AND the
   ML-DSA-44 signature (for 4.0.0+), selecting the algorithm per the OTA command /
   signer cert.
4. The verifier SHALL be implemented via the **wolfTPM software TPM** where
   feasible on the ESP32-S3; IF the memory/build research shows wolfTPM-software
   is infeasible within the per-slot / heap budget THEN the design SHALL fall back
   to wolfSSL's already-compiled verify primitives under a documented "software
   TPM" framing, and record the tradeoff. (The user's intended mechanism is the
   wolfTPM software TPM; feasibility is an explicit design gate.)
5. The verifier SHALL ship in ALL firmware variants (uniform base capability).
6. WHERE the firmware is streamed directly to flash during download (current
   behaviour), the design SHALL specify how the image is hashed for verification
   (incremental hash during the write loop, or re-read the written partition) —
   the ESP32-S3 cannot hold the whole image in RAM.
7. The device SHALL also check the transmitted **SHA-256** (already sent today but
   ignored) as a cheap integrity pre-check, closing the existing latent gap.

### 5. New firmware variants: 4.0.0-signer and 5.0.0-verified

**User story:** As the workshop, I want a variant that cuts the OTA signature over
to ML-DSA-44 and a final variant whose successful signed OTA proves on-device
verification works.

Acceptance criteria:
1. The system SHALL add a `4.0.0-signer` variant: functionally the ML-DSA cert
   firmware, but whose OTA artifact is signed with an **ML-DSA-44** signature (from
   the ML-DSA-44 CA), and which carries the ML-DSA-44 signer public cert so it (and
   its successors) can verify ML-DSA-signed updates.
2. The system SHALL add a `5.0.0-verified` variant signed with ML-DSA-44 whose
   **successful OTA deployment is the acceptance proof** that on-device ML-DSA
   signature verification works end to end.
3. The system SHALL demonstrate the NEGATIVE case: an OTA with a bad/absent/
   wrong-algorithm signature is **REJECTED** by the device (does not boot the new
   slot) — proving the verifier actually gates the update.
4. The `prepare-build` `VARIANT_CONFIG` and `buildspec.yml` variant mapping SHALL
   be extended for the new variants (data-driven, consistent with the existing
   classical/pqc/pqc-mldsa mapping), including which CA signs each variant's OTA.

### 6. End-to-end, deploy/teardown, and docs

Acceptance criteria:
1. The full chain SHALL be demonstrable on a real device:
   1.0.0 (USB) → 2.0.0 (classically-signed OTA, verified) → 3.0.0 (classically-
   signed OTA, verified) → 4.0.0-signer (ML-DSA-signed OTA, signer cutover) →
   5.0.0-verified (ML-DSA-signed OTA, verified) — each step's signature verified
   before apply, and a bad-signature update rejected.
2. The workshop deploy/teardown, Builder-Pi provisioning, and
   `check_setup_status.sh` SHALL continue to pass from a clean from-scratch build
   in BOTH accounts (pqc-a and pqc-b), with signing infrastructure present.
3. The ESP32-S3 memory budget SHALL be validated with the verifier added on top of
   ML-KEM + ML-DSA (stay under the 0x380000 / 3.5 MB per-slot ceiling and keep a
   safe min-heap during the handshake + verify).
4. DEPLOYMENT.md and the attendee content SHALL be updated once implemented.

## Out of scope (this iteration)

- Rotating/revoking the signer certificate mid-fleet (single signer cert per
  algorithm era is sufficient for the workshop).
- Secure boot / flash encryption at the ESP32 hardware level (this feature is
  application-level OTA signature verification, not chip secure-boot).
- Signing the USB/builder-Pi initial flash (trusted environment, out of scope by
  design).
- A physical/discrete TPM (the ESP32-S3 has none; this is a software TPM).

## Open questions (to resolve in design)

- **wolfTPM-software feasibility on ESP32-S3:** does wolfTPM's software/simulator
  backend build + link in the Arduino/ESP32 wolfSSL setup, and what is its flash/
  heap cost on top of ML-KEM+ML-DSA? If too heavy, fall back to wolfSSL verify
  primitives (already compiled) under a software-TPM framing — decide in design.
- **Producing a PCA-verifiable firmware signature:** exact mechanism — issue a
  signer cert from the PCA (reusing `issue_cert.py` keygen+issue), sign the blob
  locally with OpenSSL (stock ECDSA / OpenSSL 3.5 ML-DSA), device verifies against
  the signer cert. Confirm the signature encoding the device can parse (raw vs
  DER; ML-DSA signature size ~2.4 KB).
- **Durable signer-cert store:** NVS (fits in 20 KB but tight) vs a new/enlarged
  data partition (shrinks the 3.5 MB app slots — already tight) vs bake-into-
  firmware (simplest, but rotation means a rebuild). Decide + justify.
- **Classical → ML-DSA signer cutover sequencing:** how the ML-DSA signer cert
  reaches the device before it must verify a 4.0.0+ image (ship-on-last-classical
  vs provision-ahead). This is the trickiest sequencing problem — spec it
  explicitly.
- **Hashing streamed-to-flash firmware:** incremental hash during
  `esp_ota_write` vs re-read the partition after `esp_ota_end`. Pick based on RAM
  budget and wolfSSL/wolfTPM API shape.
- **Where signing runs:** in the per-device `buildspec.yml` post_build (has the
  artifact + OpenSSL 3.5) vs a separate signing step in the Step Function. Confirm
  the signer key lifecycle (per-build ephemeral vs a stable per-era signer).
