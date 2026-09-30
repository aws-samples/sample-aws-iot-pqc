# Tasks — OTA Firmware Signing + On-Device Verification (wolfSSL)

Implementation plan. **Spikes (Workstream 0) gate everything** — do not lock the
signing mechanism until G2 is resolved. Order
follows design.md "Sequencing".

## Workstream 0 — Feasibility spike (GATING)

> Note: the former G1 (wolfTPM-software feasibility) is REMOVED — the design uses
> no TPM (the device holds no private key; verify with wolfSSL primitives already
> compiled in). Only G2 remains.

- [ ] 0.2 **G2 — PCA-chained firmware signature round trip.** Using
      `shared/cert-issuer/issue_cert.py`, mint a signer cert (ec-p256 from
      classical CA; mldsa44 from ML-DSA CA). Sign a sample `firmware.bin` locally
      (OpenSSL stock ECDSA; OpenSSL 3.5 ML-DSA). Verify it on-device (or in a
      wolfSSL test harness) against the signer public cert, and confirm the leaf
      chains to the PCA root. Nail down: signature encoding (raw/DER; ML-DSA ~2.4 KB),
      verify input format, and signer-key lifecycle (stable per-era vs per-build).
      Update design.md G2 with the outcome.

## Workstream 1 — Cloud signing (build + signer cert)

- [ ] 1.1 Provision a **stable per-era signer** cert+key: classical signer (classical
      CA) and ML-DSA-44 signer (ML-DSA CA) via `issue_cert.py`, stored in Secrets
      Manager (pattern: the broker cert). CFN + IAM for issue + read.
- [ ] 1.2 Add a **local signing step** to `buildspec.yml` post_build for OTA
      variants: fetch the era-appropriate signer key, sign `firmware.bin`
      (`openssl dgst -sign` / OpenSSL 3.5 ML-DSA), upload `firmware.bin.sig` to
      `<serial>/<version>/`. Skip signing for `1.0.0-classical` (USB/trusted).
- [ ] 1.3 Extend `prepare-build` VARIANT_CONFIG + buildspec variant map with the
      variant → signer-era + sigAlg mapping (2.0.0/3.0.0 → classical; 4.0.0/5.0.0/
      >=4.0.0 → ML-DSA-44).
- [ ] 1.4 Publish the signer public cert(s) so the device can obtain them (S3 and/
      or bake-into-firmware per WS3 decision).

## Workstream 2 — OTA command + publish-ota

- [ ] 2.1 Extend `publish-ota` `cmd/<thing>/ota` payload with `sigUrl` (presigned
      `firmware.bin.sig`), `sigAlg` (`ecdsa-p256`|`mldsa44`), and a signer-cert
      locator; preserve `url/version/size/sha256/expiresIn`.
- [ ] 2.2 Presign `firmware.bin.sig` with the same lifetime as the firmware; wait
      out the POST_BUILD upload lag (reuse the existing `_firmware_metadata`
      polling pattern for the `.sig`).

## Workstream 3 — Device verifier + durable signer cert + SHA-256

- [ ] 3.1 Implement the OTA image hash: incremental SHA-256 during the
      `esp_ota_write` loop (preferred) so the image is never fully in RAM; fallback
      re-read the written partition. Enforce the transmitted `sha256` as a
      pre-check (close the current latent gap).
- [ ] 3.2 Implement signature verification with wolfSSL
      (`wc_ecc_verify_hash` / `wc_dilithium_verify_msg`) against the signer leaf's
      public key; validate the signer leaf chains to the baked-in CA root anchor.
- [ ] 3.3 Wire verify INTO `otaTask` between `esp_ota_end` and
      `esp_ota_set_boot_partition`: on success set boot + reboot; on failure
      `esp_ota_abort`, keep running firmware, report the failure (telemetry/log).
      Apply to ALL three sketches (identical handlers).
- [ ] 3.4 Bake the CA trust ANCHOR (root cert) into the firmware image: classical
      CA root in the classical-verifying variants; ML-DSA-44 CA root baked into
      3.0.0-mldsa onward so it is present before the 4.0.0 cutover. (The signer
      LEAF cert is NOT stored on-device — it travels with the OTA.) No NVS or
      partition change required for the anchor; NVS storage of a leaf is an
      optional later optimization only.
- [ ] 3.5 Parse `sigUrl`/`sigAlg` + the signer leaf cert in `mqttMessageHandler`
      (currently ignores `sha256` too) and fetch the `.sig`/leaf in `otaTask`.

## Workstream 4 — New variants + proof

- [ ] 4.1 Add `4.0.0-signer` variant (ML-DSA-signed OTA; carries/ensures the
      ML-DSA signer cert) to VARIANT_CONFIG + buildspec + dashboard picker.
- [ ] 4.2 Add `5.0.0-verified` variant (ML-DSA-signed OTA) — the acceptance-proof
      variant.
- [ ] 4.3 Negative test: craft a bad/mis-signed OTA and confirm the device
      REJECTS it (no boot switch, running firmware intact).

## Workstream 5 — Cross-cutting

- [ ] 5.1 Extend `check_setup_status.sh` to verify signer certs/secrets present.
- [ ] 5.2 Confirm clean from-scratch deploy + teardown in BOTH accounts with the
      signing infra; teardown cleans up signer secrets.
- [ ] 5.3 Validate ESP32-S3 memory budget with the verifier added (flash + min-heap).
- [ ] 5.4 End-to-end proof: 1.0.0 (USB) → 2.0.0 → 3.0.0 → 4.0.0-signer →
      5.0.0-verified, each OTA signature verified before apply; bad-signature
      rejected. Capture for the workshop docs.
- [ ] 5.5 Update DEPLOYMENT.md + attendee content.

## Notes

- Spec-driven: this tasks list follows requirements.md + design.md. Update the
  design's G2 section with the spike outcome BEFORE starting Workstream 1+.
- Reuse over rebuild: `issue_cert.py`, `publish-ota`, the migration Step Function,
  the per-device CodeBuild, and the shared `otaTask` are extended, not replaced.
