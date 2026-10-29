# Tasks — Device identity in a dedicated NVS partition

Branch: `feature/nvs-device-identity`. Workstream 0 spikes gate the rest: don't
change the production flow until 0.1–0.3 pass on a real board.

## Workstream 0 — Feasibility spikes (gating)

- [x] 0.1 **Partition table.** Add `certs, data, nvs, 0x721000, 0x10000,` to
      `esp32-flash/partitions_pqc_workshop.csv`. Generate the `.bin` with
      `gen_esp32part.py` and check `certs` is at `0x721000` and nothing else
      moved. Rebuild the firmware-builder image, compile the current classical
      sketch unchanged, flash a pqc-a device, and confirm it boots and connects
      as before. _Requirements: R1.1, R1.2_
      **Progress:** CSV and tracked `.bin` updated; compiled in the production
      firmware-builder image (core 3.3.10, wolfSSL 5.8.4) and the build's table
      has `certs` at 0x721000/64 KB with the first 6 entries byte-identical.
      Found: `firmware.merged.bin` is padded to 8 MB and overlaps `certs`, so
      flashing must use the pieces (see 2.1). **Done on hardware** (XIAO
      ESP32-S3, MAC ..:C6:9D:E0): flashed as pieces, `certs` reported at
      0x721000/0x10000 and the app ran from `ota_0`. The real firmware boot +
      broker connection is re-checked in workstream 3.
- [x] 0.2 **ML-DSA-44 seed round trip.** In the cert-issuer image:
      - generate a 32-byte seed;
      - derive the key with OpenSSL 3.5 (`genpkey -algorithm ML-DSA-44
        -pkeyopt hexseed:...`; confirm the option exists);
      - issue a cert from the pqc-a ML-DSA CA.

      On a device, run a test sketch that:
      - calls `wc_dilithium_make_key_from_seed()`, exports the public key, and
        checks it equals the cert's public key;
      - completes a TLS handshake to the broker on 8884 with the derived key.

      Record the derivation time and peak heap. If it fails, try option B
      (seed-only PKCS#8), then fall back to a full key DER blob.
      _Requirements: R2.2_
      **Progress:** issuer side PASSED in the cert-issuer image (OpenSSL
      3.5.8): `genpkey -pkeyopt hexseed:` accepts a supplied seed, regenerates
      an identical key, and writes a 54-byte seed-only PKCS#8. The spike sketch
      (`esp32-flash/spikes/nvs_identity_spike/`) compiles against the device
      wolfSSL build, so `wc_dilithium_make_key_from_seed`, `KeyToDer` and
      `PrivateKeyDecode` are all present. **PASSED on hardware:**
      `wc_dilithium_make_key_from_seed` rebuilt the key in 13.7 ms (peak heap
      use ~40 KB of 336 KB free); its public key equals the cert's; it signs and
      the cert key verifies; it pairs with the cert in a TLS 1.3 context.
      **Option B also passed and is the chosen design:** wolfSSL loads the
      54-byte seed-only PKCS#8 directly with `wolfSSL_CTX_use_PrivateKey_buffer`
      (ASN1) and pairs it with the cert, so the firmware needs no derivation
      code. The TLS handshake to 8884 is checked in 3.3.
- [x] 0.3 **NVS image round trip.** Build a `certs.bin` with
      `nvs_partition_gen.py` from real DER blobs and the strings in the design
      layout. Flash it at `0x721000` alongside the app. A test sketch opens
      `certs`/`devid` read-only and prints each entry's length and SHA-256; check
      they match the source files. Confirm the ML-DSA cert (4.1 KB) reads back
      as a blob. _Requirements: R1.3, R2.1, R2.3_
      **Progress:** `esp32-flash/spikes/make_spike_identity.sh` builds a
      throwaway identity in the cert-issuer image; `esp-idf-nvs-partition-gen`
      0.3.0 produced a 64 KB `certs.bin` (V2, multipage blobs). **PASSED on
      hardware:** read-only open of `certs`/`devid`; all 7 blobs and 3 strings
      read back with lengths and SHA-256 identical to the host values,
      including the 4,013-byte ML-DSA cert as a multipage blob; both chains
      verify on the device.
      **Finding:** on the XIAO board, `CDCOnBoot=cdc` means CDC *disabled*, so
      `Serial` goes to UART0. Firmware output reaches USB only via ESP_LOG /
      printf (the IDF console, mirrored to USB-Serial/JTAG). New firmware code
      must log with ESP_LOG/printf, not `Serial.print`.
- [x] 0.4 **Pin wolfSSL.** Change `arduino-cli lib install "wolfssl"` to a pinned
      version (`wolfssl@5.8.4`, or the version 0.2 proves) in
      `raw_device_builder/codebuild/Dockerfile`. _Requirements: R5.3_
      **Done:** pinned to `wolfssl@5.8.4`, the version in the current pqc-a
      image. Takes effect on the next firmware-builder image build.

## Workstream 1 — Identity issuance at registration

- [x] 1.1 Add `esp-idf-nvs-partition-gen` (pinned) to the cert-issuer image.
      **Done:** `==0.3.0` in `shared/cert-issuer/Dockerfile` (runs on the image's
      Python 3.10) and in `requirements-dev.txt` for the tests.
- [x] 1.2 Add an identity mode to the issuer: write DER blobs, take a
      caller-supplied ML-DSA seed, and build `certs.bin` per the design layout.
      Add unit tests in `shared/cert-issuer/` (moto/mocked PCA).
      **Done:** `shared/cert-issuer/issue_device_identity.py`. No supplied seed
      is needed: a random key exports to the same 54-byte seed-only PKCS#8.
      Checks each key matches its cert before building. 23 tests with a fake
      PCA that really signs the CSR. Real run in the cert-issuer image against
      the pqc-a Private CAs produced a 64 KB image (blobs ~9.4 KB).
- [x] 1.3 Add `raw_device_builder/codebuild/buildspec-identity.yml`: issue both
      certs, fetch both CA roots, build `certs.bin`, upload to
      `identity/<serial>/certs.bin`, scrub the working directory.
      **Done differently:** the buildspec is inline in the CodeBuild project
      (one command), the script uploads with boto3 (the image has no AWS CLI),
      and the key is `identity/<serial>/certs.bin`. Work files live in a
      temporary directory.
- [x] 1.4 CloudFormation: a `pqc-workshop-device-identity` CodeBuild project
      (cert-issuer image) with a least-privilege role (both CAs, SSM WiFi params
      with KMS decrypt via SSM, S3 put on `*/identity/*`, scoped logs). Pass the
      WiFi values as `PARAMETER_STORE`, not `PLAINTEXT`. _Requirements: R3.4_
      **Done** in `device-builder-stack.yaml` (`DeviceIdentityRole`, auto-named;
      `DeviceIdentityProject`). cfn-lint clean.
- [x] 1.5 Firmware bucket: lifecycle rule to expire `*/identity/` after 1 day.
      Builder-Pi role: `s3:DeleteObject` on `*/identity/*`. _Requirements: R3.5_
      **Done** on the `identity/` prefix (current and noncurrent versions, 1
      day). Pi role: `DeleteObject` + `DeleteObjectVersion` on `identity/*` and
      `BatchGetBuilds` on the identity project.
- [x] 1.6 Registrar: start the identity project instead of the firmware build,
      and return its build id. Remove `wifiPassword` from the registry write.
      Update `device-registrar/test_index.py`. _Requirements: R3.1–R3.4_
      **Done, with one change:** the registrar starts the identity job AND
      still starts the firmware build (shared images are workstream 4, out of
      scope). Response adds `identityBuildId` and `identityKey`. Serial is
      validated (400 on bad input). `wifiPassword` removed from the registry,
      and `prepare-build` no longer falls back to it.

## Workstream 2 — Builder-Pi flashing

- [x] 2.1 `device_manager.py`: download the app pieces (bootloader,
      partitions, boot_app0, app) and `identity/<serial>/certs.bin`; flash them
      in one `esptool write_flash` call at 0x0 / 0x8000 / 0xe000 / 0x10000 /
      0x721000. Do not use `firmware.merged.bin` (8 MB, overlaps `certs`). The
      firmware build must upload the pieces (it already uploads bootloader,
      partitions and boot_app0).
      **Done:** `FLASH_LAYOUT` in `device_manager.py` is the single list of
      offsets/files. The GUI and CLI wait for BOTH builds
      (`operations.wait_for_builds`) before downloading. The identity image is
      read with `get_object` (records its `VersionId`, short retry on
      `NoSuchKey`) and written `0600`. All pieces are required; the buildspec
      upload of bootloader/partitions/boot_app0 no longer has `|| true`.
      `cli flash --bin` now takes `--offset` (default `0x0`).
- [x] 2.2 After a successful flash, delete `identity/<serial>/certs.bin`. On
      failure, keep it for a retry.
      **Done:** `DeviceManager.flash_device` = flash, then `delete_identity`
      (S3 `delete_object` with the downloaded `VersionId`, so no noncurrent copy
      or delete marker is left; plus the local file). Deletion is best effort;
      the 1-day lifecycle rule is the backstop.
- [x] 2.3 Reflash without re-registering: re-run identity issuance, then flash.
      _Requirements: R4.2_
      **Done:** `/register` takes `identityOnly: true`. The registrar checks the
      device is in the registry (404 if not) and starts only the identity job:
      no Thing/policy/registry writes and no firmware rebuild. GUI and CLI
      reflash use it, then flash the existing S3 firmware with the new identity.
      Note: each reflash issues new device certs; the old ones are not revoked
      (same as today's re-register).
- [x] 2.4 Update `builder-pi/tests/` (operations and CLI) for the two-image
      flash and the cleanup.
      **Done:** new `test_device_manager.py` (moto versioned bucket + mocked
      esptool: offsets, no merged image, version-exact delete, keep on failure,
      missing piece); operations (identity fields, `identityOnly`,
      `wait_for_builds`); CLI (reflash, wait-for-both, no flash on build
      failure); registrar (`identityOnly`, 404). Full suite: 110 passed.
      Hardware check of the Pi path happens with the pqc-a deploy after WS3.

## Workstream 3 — Firmware loads identity from NVS

- [ ] 3.1 Classical sketch: identity loader (version check, WiFi strings,
      classical CA root, cert and key as DER via `WOLFSSL_FILETYPE_ASN1`). Fail
      closed with a clear `[Setup]` log if anything is missing.
      _Requirements: R5.1, R5.2_
- [ ] 3.2 PQC sketch: same loader (classical cert, ML-KEM key exchange).
- [ ] 3.3 ML-DSA sketch: loader plus the seed-derived ML-DSA key from 0.2.
- [ ] 3.4 Load both CA roots into the OTA signer verifier from NVS, replacing
      the baked `OTA_SIGNER_CA_ROOT`. _Requirements: R2.4_
- [ ] 3.5 Remove identity and WiFi from `secrets.h` generation in
      `buildspec.yml`. Keep only non-secret build settings.

## Workstream 4 — Shared images and migration

- [ ] 4.1 Build each variant once per deployment with no per-device inputs, sign
      it, and upload to `shared/<version>/`. Wire it into `deploy-workshop.sh`
      after the firmware-builder image build. _Requirements: R6.1_
- [ ] 4.2 `prepare-build` / state machine: no per-device build for migrations;
      `publish-ota` presigns the shared image and signature. Update the ASL test.
      _Requirements: R6.2_
- [ ] 4.3 Registrar first flash uses the shared classical image (pairs with 2.1).

## Workstream 5 — Validation and docs

- [ ] 5.1 pqc-a end to end: plug in and register two devices; both online on the
      dashboard; each migrates classical → PQC → ML-DSA; identity intact after
      every OTA; `identity/<serial>/certs.bin` removed after flashing. _Requirements: R7.1_
- [ ] 5.2 Teardown: deregister removes the Thing and registry entry; lifecycle
      clears any stale identity objects; update `teardown-workshop.sh` if needed.
- [ ] 5.3 Update `cloudformation/DEPLOYMENT.md`, the README code walkthrough,
      and the workshop docs (flash layout and registration flow).
- [ ] 5.4 Update the threat model (T003, T011, T013, and the T005 note), re-run
      `add_tc_metadata.py`.
- [ ] 5.5 Definition of done: destroy and rebuild pqc-b, then pqc-a, from
      scratch, both fully working. _Requirements: R7.2_
