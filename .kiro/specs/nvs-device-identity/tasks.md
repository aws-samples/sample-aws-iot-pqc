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

- [x] 3.1 Classical sketch: identity loader (version check, WiFi strings,
      classical CA root, cert and key as DER via `WOLFSSL_FILETYPE_ASN1`). Fail
      closed with a clear `[Setup]` log if anything is missing.
      _Requirements: R5.1, R5.2_
      **Done:** `loadDeviceIdentity()` (same code in all three sketches, only
      `ID_TLS_CA/CERT/KEY` differ) runs in `setup()` before any task starts.
      It fails closed (repeating `[Setup] FATAL` log, no WiFi/MQTT) on a
      missing partition, `fmt` != 1, any missing/oversized entry, or an NVS
      `serial` that differs from the chip MAC. `setupWolfSSLContext` loads DER
      and now also calls `wolfSSL_CTX_check_private_key`. Logging is ESP_LOG.
      `idReadBlob` takes plain pointers because the Arduino build hoists
      prototypes above the `IdBlob` struct.
- [x] 3.2 PQC sketch: same loader (classical cert, ML-KEM key exchange).
- [x] 3.3 ML-DSA sketch: loader plus the seed-derived ML-DSA key from 0.2.
      **Done:** `key_mldsa` is the 54-byte seed-only PKCS#8 and loads directly
      with `WOLFSSL_FILETYPE_ASN1` (option B from 0.2); trusts `ca_mldsa` on 8884.
- [x] 3.4 Load both CA roots into the OTA signer verifier from NVS, replacing
      the baked `OTA_SIGNER_CA_ROOT`. _Requirements: R2.4_
      **Done:** confirmed the signer CA secrets come from the same two Private
      CAs (`issue_broker_certs.py` issues the signers from `CLASSICAL_CA_ARN` /
      `MLDSA_CA_ARN`). `otaVerifyChainToRoot` loads `ca_cls` + `ca_mldsa` (DER)
      into the cert manager; fails closed if neither loads. **Not yet run on
      hardware** — needs a signed OTA, i.e. the pqc-a migration test.
- [x] 3.5 Remove identity and WiFi from `secrets.h` generation in
      `buildspec.yml`. Keep only non-secret build settings.
      **Done:** `secrets.h` = FIRMWARE_VERSION, FIRMWARE_OTA_SIG_ALG, LOG_LEVEL,
      MQTT_BROKER_ENDPOINT/PORT. Removed from the firmware build: in-build cert
      issuance (`issue_cert.py` call, image COPY and staging), the signer CA
      root bake, `WIFI_*`/`IOT_ENDPOINT`/`CA_ARN`/`MLDSA_CA_ARN`/
      `SIGNER_*_CA_SECRET` env, and the CodeBuild role's acm-pca grant. WiFi is
      no longer a CloudFormation parameter anywhere (master, device-builder,
      deploy script) — only SSM, read by the identity job. Registrar and
      prepare-build no longer resolve WiFi; the registrar role lost kms:Decrypt
      and its SSM read is scoped to the broker endpoint. Both ASL copies drop
      the WiFi/IoT overrides. secrets.h templates rewritten (v3 now port 8884).
      **Hardware (board C69DE0, local builds of all three, real pqc-a identity
      issued with `--no-upload`):** wrong-serial identity → fail closed;
      classical → ECDHE-P256 on 8883; PQC → SECP256R1_MLKEM768 on 8883; ML-DSA →
      SECP256R1_MLKEM768 on 8884 with the ML-DSA cert. All three connected and
      subscribed from ONE `certs` partition; switching app images left it intact.
      Tests: 112 passed.

## Workstream 3b — End-to-end on a rebuilt pqc-a, and PSRAM (added during testing)

- [x] 3b.1 **From-scratch rebuild of pqc-a, following the workshop instructions**
      (teardown-workshop.sh, then workshop pages 99.3 → 99.8 with a reimaged
      Builder-Pi). Readiness check 40/40. Builder-Pi provisioning 11.5 min
      (toolchain 8 min 40 s). First device FC22E0: identity build ~15 s, firmware
      build ~2 min, 5-piece flash in 7 s, identity version deleted from S3.
- [x] 3b.2 **Migrations on hardware with one identity flash:** classical →
      2.0.0-pqc (ECDSA-signed OTA, chained to `ca_cls`) → 3.0.0-mldsa (8884, ML-DSA
      cert from NVS) → 4.0.0-signer (first **ML-DSA-signed** OTA, chained to
      `ca_mldsa`). This proves 3.4 for both NVS roots. Device cert and firmware
      signer share a CA per algorithm (verified with OpenSSL 3.5); see the
      backlog item on signer-cert scoping below.
- [x] 3b.3 **Builder-Pi boot check false negative fixed.** `verify_boot` looked
      for uppercase `MQTT`/`WiFi`; the firmware logs lowercase `[mqtt]`/`[wifi]`,
      so every flash reported "Boot verification timed out". New
      `classify_boot_line()` matches the exact success lines, ignores retry lines,
      and fails fast on a panic or `[Setup] FATAL` (no valid identity). Tests use
      real serial lines. Verified on four devices: `verifying → done`.
- [x] 3b.4 **PSRAM enabled.** The XIAO ESP32-S3 has 8 MB octal PSRAM
      (ESP32-S3R8, esptool: "Embedded PSRAM 8MB (AP_3v3)"), but the board menu
      defaulted to PSRAM disabled: the build linked the `qio_qspi` IDF libs and
      `ESP.getPsramSize()` reported 0. Added `PSRAM=opi` to the FQBN (defined
      once in buildspec.yml for the compile and metadata.json; Pi constant kept
      in sync), which selects `qio_opi` (`CONFIG_SPIRAM_MODE_OCT`). Note the
      `sdkconfig` copied into the build dir is the generic one and always says
      QUAD; the libs folder is what counts. App size +5 KB (1.21 MB, 33% of a
      slot). No code changes: `SPIRAM_USE_MALLOC` + `MALLOC_ALWAYSINTERNAL=4096`
      send >4 KB allocations to PSRAM.
- [x] 3b.5 **PSRAM measured on hardware**, one device per firmware (all PSRAM
      builds, pqc-a, Oct 30):

      | Firmware | Free heap | Min heap (low-water) | Max block | PSRAM free / total |
      |---|---|---|---|---|
      | 1.0.0-classical (C69E18) | 170.0 KB | 116.6 KB | 120.8 KB | 8,362,752 / 8,388,608 |
      | 2.0.0-pqc (FC22E0) | 169.9 KB | 116.4 KB | 118.8 KB | 8,362,752 / 8,388,608 |
      | 3.0.0-mldsa (C69E44) | 170.3 KB | 136.9 KB | 118.8 KB | 8,354,296 / 8,388,608 |
      | 4.0.0-signer (C69DE0) | 170.2 KB | 136.9 KB | 120.8 KB | 8,354,296 / 8,388,608 |

      Before PSRAM, FC22E0 on 4.0.0-signer: free 140 KB, **min 63 KB**. Internal
      heap is now flat across all variants and the heaviest path's low-water
      more than doubled. ML-DSA variants hold ~8 KB more in PSRAM (cert/root
      buffers). The higher internal low-water on 3.0.0/4.0.0 vs classical is
      observed, not traced in the allocator. Classical handshake ~2.05 s with
      and without PSRAM; PQC/ML-DSA handshake times on the PSRAM build were not
      captured. Dashboard card now shows **PSRAM free / used / total**.
- [x] 3b.6 **`deploy-workshop.sh --stage-only`** added for fast in-place updates
      (re-stage sources + rebuild dashboard; no stack/image/cert/SSM changes).
      Verified on pqc-a: 139 s vs ~20 min for a full redeploy. Documented in
      DEPLOYMENT.md and the steering.
- [ ] 3b.7a **T024: dedicated firmware-signing roots (Nov 4, in progress).**
      Two new ACM PCA roots (FirmwareSignerCA ECDSA P-256, MlDsaFirmwareSignerCA
      ML-DSA-44) issue ONLY the two OTA signers, with CodeSigningCertificate/V1
      (EKU codeSigning, verified on pqc-a for both algorithms). Identity format 2
      adds sig_cls / sig_mldsa (identity job reads them with
      GetCertificateAuthorityCertificate only, refuses a signing root equal to a
      device root). Firmware trusts only the root matching sigAlg and requires
      codeSigning; device CA roots are TLS-only; format-1 identities stay online
      but refuse signed OTA (no fallback). Cloud side verified on pqc-a: CAs
      ACTIVE, signers re-issued, 5 shared images rebuilt and signed, identity
      job output checked (format 2, all 8 blobs). STILL TO DO on hardware:
      reflash a device (fmt 2), OTA 1->2->3->4->5, and the T024 attack test
      (/tmp/t024_attack.sh device-ca and no-eku must both be refused); then
      update the threat model (T024 -> resolved, M029 resolved) and the
      workshop pages (user approval). Cost: 4 general-purpose CAs instead of 2.
- [x] 3b.7 Backlog (superseded by 3b.7a): signer certs share a CA with device certs
      and carry the same EKU (no `codeSigning`), and `otaVerifyChainToRoot` checks
      only the chain, so any device's key could sign an OTA every device accepts.
      Fix: issue signer certs with the `codeSigning` EKU and require it on the
      device (or separate signer CAs / pinned signer keys). Also: the Builder-Pi
      GUI has no "rebuild firmware" for a registered device (Reflash reuses the
      S3 image), so a firmware-setting change needs Deregister + Register; and
      optionally report `ESP.getMinFreePsram()` as a PSRAM low-water metric.

- [x] 3b.8 **Memory deep dive (Oct 31, PSRAM build, FC22E0).** Tools added:
      `cmd/<thing>/diag` (JSON on `dt/<thing>/diag` + console table; "full" adds
      `heap_caps_print_heap_info`, "dump" adds `heap_caps_dump`), an OTA-peak log
      line before the post-OTA reboot, and a static breakdown with `esp-idf-size`
      on the build `.map`. Findings:
      - **Static RAM is not the lever:** 55 KB total (.bss 32 KB + .data 23 KB),
        almost all ESP-IDF/WiFi/LWIP. Ours: sketch 7.5 KB, of which the three 2 KB
        OTA URL buffers are 6 KB; wolfSSL `SessionCache` 1.1 KB. Big crypto
        tables (AES Te/Td, ML-KEM zetas) and `S3_ROOT_CA` are in flash (0x3c…).
      - **"Max contiguous block" ≈ main-region free space, not fragmentation.**
        Internal RAM is five regions; the main one (284 KB) had 15 free blocks
        and a 120.8 KB largest block; a separate 32 KB region at 0x3fcf0000 is
        almost entirely free but cannot join it.
      - **Task stacks are the lever.** Peak use: MQTTTask 3.8 KB of 32 KB on both
        classical and ML-DSA-cert handshakes (wolfSSL buffers live on the heap);
        OTATask 11.5 KB of 32 KB on the heaviest path (ML-DSA-signed OTA verify);
        loopTask ~2 KB of 8 KB; WiFiTask 2.7 KB of 4 KB; BlinkTask 0.7 KB of 2 KB.
      - Heaviest-path low-water marks: internal heap 108.8 KB during the
        ML-DSA OTA; PSRAM ~78 KB in use during OTA, ~104 KB during the ML-DSA
        handshake (of 8 MB).
- [x] 3b.9 **Reclaim internal RAM (Nov 1).** All three sketches: MQTTTask stack
      32→8 KB, OTATask 32→24 KB (2x the 11.5 KB peak; 16 KB would only be 1.4x),
      loopTask 8→4 KB (`SET_LOOP_TASK_STACK_SIZE`), the three 2 KB OTA URL
      buffers moved to PSRAM (allocated once in `setup()`, internal fallback),
      and wolfSSL `NO_SESSION_CACHE` (the sketches never resume sessions).
      Static RAM 55,184 → 47,968 B. Creating the OTA task only during an OTA was
      NOT done: it frees RAM only while idle, and the low-water mark that matters
      is during the OTA, when the task exists anyway.
      **Hardware-verified on pqc-a (FC22E0):** USB flash of the new classical
      image, then OTA 1→2→3→4→5 (every verify path: unsigned→ECDSA, ECDSA,
      ML-DSA-signed), plus an old-image device (C69DE0, pre-change v4) OTA-ing
      onto the new v5. Each OTA ~99 s, no failures. Measured: MQTTTask 3.8 of
      8 KB, loopTask ~2 of 4 KB, OTATask 11.7 of 24 KB on the ML-DSA-signed OTA.
      **Heaviest-path internal heap low-water 108.8 KB → 155.7 KB (+47 KB)**;
      idle internal free ~215 KB.

## Workstream 4 — Shared images and migration

- [x] 4.1 Build each variant once per deployment with no per-device inputs, sign
      it, and upload to `shared/<version>/`. _Requirements: R6.1_
      **Done:** `raw_device_builder/codebuild/build-shared-firmware.sh` starts one
      firmware build per variant from `variants.json` (the same file prepare-build
      reads), each writing `shared/<version>/`. The buildspec writes
      `S3_PREFIX=shared/$FIRMWARE_VERSION` and drops all per-device/serial inputs.
      **Chained off the builder-image build** (option 1, per the user): buildspec-image.yml
      post_build runs the script with `--wait` when the image push succeeds, so
      the shared images build in parallel with Builder-Pi provisioning (99.4/99.6).
      The ImageBuilder role gained codebuild start/get on the firmware project and
      SSM read of the broker endpoint; timeout 30→45 min. Script is bash-3.2 safe
      (runs on macOS) and retries the CodeBuild concurrency limit.
- [x] 4.2 `prepare-build` / state machine: no per-device build for migrations;
      `publish-ota` presigns the shared image and signature. Update the ASL test.
      _Requirements: R6.2_
      **Done:** prepare-build resolves the variant to `shared/<version>/firmware.bin`,
      requires it to exist (raises `SharedFirmwareMissing` otherwise — no fallback
      build, which could pair a firmware.bin with another concurrent build's
      signature), and no longer returns buildNeeded/buildEnv. The state machine
      dropped `BuildNeeded?` and `WaitForBuild` (both ASL copies); the migration
      role lost CodeBuild + EventBridge. publish-ota already derives the signature
      and signer-cert keys from `s3Key`, so it needed no change. ASL + prepare-build
      tests rewritten.
- [x] 4.3 Registrar first flash uses the shared classical image (pairs with 2.1).
      **Done:** the registrar starts only the identity job and returns
      `firmwarePrefix: shared/1.0.0-classical` (buildId null); it 503s if that
      shared image is not built yet (both register and reflash), so the Pi never
      fails mid-download. Its role lost the firmware-build and SSM grants, and its
      env dropped BUILD_PROJECT/broker/WiFi. The Pi's `download_firmware` takes the
      prefix (default `shared/<version>`). Also fixed WS5.2's gap early: the
      deregistrar now deletes `identity/<serial>/` (device keys) as well as any
      legacy `<serial>/` firmware, and leaves `shared/` alone. `check_setup_status.sh`
      gained section 10c (builder image + each shared image, warn-if-missing).
      All 124 tests pass. Not yet deployed to pqc-a.

## Workstream 5 — Validation and docs

- [ ] 5.1 pqc-a end to end: plug in and register two devices; both online on the
      dashboard; each migrates classical → PQC → ML-DSA; identity intact after
      every OTA; `identity/<serial>/certs.bin` removed after flashing. _Requirements: R7.1_
- [ ] 5.2 Teardown: deregister removes the Thing and registry entry; lifecycle
      clears any stale identity objects; update `teardown-workshop.sh` if needed.
- [x] 5.3 Docs (Nov 3). The workshop content (now on its `mainline`) was
      updated by the user and is the golden source; README.md (code walkthrough,
      OTA delivery, registration, memory, troubleshooting, security) and
      raw_device_builder/README.md were brought into line with it. Workshop page
      3.7.3's min-heap table predates the 3b.9 memory reclaim (reported to the
      user, not edited).
- [x] 5.4 Threat model updated (Nov 3) in all three files, then
      `add_tc_metadata.py` re-run (24 threats). T003 and T013 resolved (identity-
      free shared images; PSK only in SSM via PARAMETER_STORE), T011 reduced to
      Medium (in-flight identities only, still bucket-wide GetObject, M011),
      T005 note updated (identity in the unencrypted certs partition), T022/T023
      "key-bearing image" wording removed, M012/M013 resolved. NEW T024 (signer
      and device certs share CA + EKU; High/Possible) with M029 (codeSigning EKU,
      enforced on the device; = 3b.7). Stale component / data-asset text
      (C001, C002, C003, C012, C014, C015, C017, C022, CN001, CN034, CN040,
      CN051, D011, DP001, DP004, DP005, A001, A006) rewritten. The MCP server's
      persisted state was NOT updated: re-exporting would overwrite these edits.
- [ ] 5.5 Definition of done: destroy and rebuild pqc-b, then pqc-a, from
      scratch, both fully working. _Requirements: R7.2_
- [ ] 5.6 (after 5.5) **Re-assess the high-risk findings against the new
      design.** Go through the threat model's High/Critical threats
      (`.threatmodel/`, Threat Composer export) and the latest security scans,
      and record for each whether the NVS identity + shared images close,
      reduce, or leave it unchanged. Expected to move: T003/T011 (per-device keys
      and WiFi PSK in every firmware image in S3 — images are now identity-free
      and shared), T013 (plaintext WiFi in the registry/build env — now SSM-only),
      T005/T006 notes (OTA trust now from NVS CA roots). New to add: signer certs
      share a CA and EKU with device certs (3b.7). Not expected to move: the
      Holmes HIGHs in `holmes-report.json` (Oct 29) are all suppressed Checkov
      IAM-wildcard rules (CKV_AWS_109/111, plus 193/366), unrelated to identity
      storage; confirm with a fresh Holmes + Slingshot scan of the merged branch
      and update SECURITY_FINDINGS.md.
