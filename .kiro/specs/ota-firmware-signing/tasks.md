# Tasks — OTA Firmware Signing + On-Device Verification (wolfSSL)

Implementation plan. **Spikes (Workstream 0) gate everything** — do not lock the
signing mechanism until G2 is resolved. Order
follows design.md "Sequencing".

## Workstream 0 — Feasibility spike (GATING)

> Note: the former G1 (wolfTPM-software feasibility) is REMOVED — the design uses
> no TPM (the device holds no private key; verify with wolfSSL primitives already
> compiled in). Only G2 remains.

- [x] 0.2 **G2 — PCA-chained firmware signature round trip. CLEARED (pqc-a,
      2026-10-02).** Minted signer certs from both live PCAs (classical ECDSA-P256,
      ML-DSA-44), signed a 256 KB sample blob, and verified signature + chain-to-root
      for BOTH eras — all passed. Findings recorded in design.md "G2 spike RESULTS":
      ECDSA sig = DER `SEQUENCE{r,s}` ~71 B over a SHA-256 digest, no raw-r‖s
      conversion needed; ML-DSA-44 sig = fixed 2420 B over the RAW image (pure
      scheme, hashes internally) → deliver via presigned URL, verifier streams the
      full image. CA roots to bake: classical 696 B, ML-DSA 5592 B. Default PCA
      template stamps TLS EKUs not code-signing (acceptable; chain+sig is the
      property). Signer lifecycle = stable per-era, key in Secrets Manager.

## Workstream 1 — Cloud signing (build + signer cert)

- [x] 1.1 Provision a **stable per-era signer** cert+key: classical signer (classical
      CA) and ML-DSA-44 signer (ML-DSA CA) via `issue_cert.py`, stored in Secrets
      Manager (pattern: the broker cert). CFN + IAM for issue + read. **DONE:**
      6 secrets `pqc-broker/signer-{classical,mldsa}-{ca,cert,key}` +
      `_provision_signer_certs()` in `issue_broker_certs.py` (gated on signer env
      vars; reuses the broker-cert provisioner CodeBuild + `CertProvisionerRole`);
      signer secret ARNs exported as broker-stack Outputs.
- [x] 1.2 Add a **local signing step** to `buildspec.yml` post_build for OTA
      variants: fetch the era-appropriate signer key, sign `firmware.bin`
      (`openssl dgst -sha256 -sign` / OpenSSL 3.5 `pkeyutl -sign -rawin`), upload
      `firmware.bin.sig` + `signer.crt` to `<serial>/<version>/`. Skips signing
      for `1.0.0-classical` (USB/trusted). Signer key read from Secrets Manager,
      scrubbed after signing; `signed`/`sigAlg` recorded in metadata.json. **DONE.**
- [x] 1.3 Extend `prepare-build` VARIANT_CONFIG + buildspec variant map with the
      variant → signer-era + sigAlg mapping (2.0.0/3.0.0 → classical;
      4.0.0+ → ML-DSA-44 in WS4). **DONE:** `signer_era`/`sig_alg` per variant,
      forwarded as `signerEra`/`sigAlg`; buildspec sets `SIGNER_ERA` per variant;
      device-builder stack wires signer secret ARNs + `secretsmanager:GetSecretValue`
      (scoped to `pqc-broker/signer-*`) + env vars; main stack passes the Outputs
      through. Tests updated.
- [x] 1.4 Publish the signer public cert(s) so the device can obtain them.
      **DONE (via WS1 build):** the build uploads `signer.crt` next to
      `firmware.bin.sig` — the signer LEAF travels with the OTA. Baking the CA
      ROOT into firmware is Workstream 3 (3.4).

## Workstream 2 — OTA command + publish-ota

- [x] 2.1 Extend `publish-ota` `cmd/<thing>/ota` payload with `sigUrl` (presigned
      `firmware.bin.sig`), `sigAlg` (`ecdsa-p256`|`mldsa44`), and `signerCertUrl`
      (presigned signer leaf); preserve `url/version/size/sha256/expiresIn`.
      **DONE:** signed variants add the three fields; unsigned variants (classical
      1.0.0) publish the exact legacy payload. `sigAlg` sourced from the event
      (prepare-build → state machine `$.prepare.sigAlg`).
- [x] 2.2 Presign `firmware.bin.sig` + `signer.crt` with the same lifetime as the
      firmware; wait out the POST_BUILD upload lag (new `_object_ready()` presence
      poll, same 403-or-404 handling as `_firmware_metadata`). Raises if a signed
      variant's `.sig`/`signer.crt` never lands. Both eras deliver the signature
      by presigned URL (ML-DSA sig is 2420 B, too large to inline). **DONE.**

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
