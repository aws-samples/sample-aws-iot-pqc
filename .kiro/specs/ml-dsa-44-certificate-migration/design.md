# Design — ML-DSA-44 Certificate Migration (Step 2)

> Status: **design decisions locked** for the OTA/partition strategy, firmware
> layout, and dashboard flow (see "Key design decisions" below). Per-workstream
> detail still to be fleshed out as each is implemented. Grounded in the existing
> (Step 1) components.

## Key design decisions (locked)

1. **OTA partition strategy: Option A — reuse the two existing OTA slots.** The
   partition table is unchanged (`ota_0` + `ota_1`, 3.5 MB each, `otadata`, no
   factory). A new firmware image is downloaded into whichever slot is currently
   inactive (`esp_ota_get_next_update_partition`), boot is pointed there via
   `otadata` (`esp_ota_set_boot_partition`), and the device reboots. This
   inherently keeps the **previous firmware in the other slot → a one-version
   rollback path** for free. No partition redesign (which would shrink slots
   below the ~2.5 MB firmware + fail the 3.5 MB / 0x380000 size guard).
   - Consequence: the fixed "ota_0 = classical, ota_1 = PQC" assumption baked
     into the `migrate-pqc` / `migrate-classical` swap handlers must become
     **slot-content-aware** (inspect which app is in each slot via
     `esp_ota_get_partition_description` and target by *what's there*, not by a
     hardcoded subtype↔variant convention), OR the swap shortcuts are dropped in
     favour of always doing a download-OTA. Decision: keep a generalized
     "boot the other slot" revert, but do not assume which variant lives where.

2. **All firmware variants carry the full download `otaTask`.** Today only the
   classical sketch has the download-and-write OTA path; the PQC sketch can only
   swap to an already-resident slot. We will give **all three `.ino` files**
   (classical, pqc, pqc-mldsa) the complete `otaTask` (download over TLS →
   `esp_ota_begin/write/end` → `set_boot_partition` → reboot), so **any running
   variant can receive an OTA command and flash the next slot**. This makes OTA
   uniform and variant-agnostic across the fleet.

3. **Dashboard: firmware picker + Flash button.** Replace the single hardcoded
   "Migrate to PQC" action with (a) a **picker field** to select the target
   firmware variant (classical / pqc / pqc-mldsa), and (b) a **Flash button**
   that: if that variant is not already built for the device, triggers the build
   first, then issues the OTA job. This generalizes the migration flow to
   "select firmware → flash OTA" for any variant, in either direction.

4. **ML-DSA-44 CA is a managed ACM Private CA** (ACM PCA supports ML-DSA-44 for
   the root), not a self-managed OpenSSL/oqs CA. Supersedes the earlier
   feasibility caveat.

5. **Ports:** classical device listener stays **8883**; ML-DSA-44 device listener
   is **8884**; bridge to IoT Core stays classical. NLB gains a second listener +
   target group for 8884.

## Workstream 1 spike findings (task 1.1 — pinned)

Verified against authoritative sources (boto3 `create_certificate_authority`
reference, AWS Private CA userguide, the AWS Security blog "Post-quantum (ML-DSA)
code signing with AWS Private CA and AWS KMS", OpenSSL 3.5 `EVP_PKEY-ML-DSA`
docs, and wolfSSL PQC posts). Content rephrased for compliance with licensing
restrictions.

- **ACM PCA enums (exact, do not guess):**
  - `KeyAlgorithm: ML_DSA_44` (full enum set now:
    `RSA_2048|RSA_3072|RSA_4096|EC_prime256v1|EC_secp384r1|EC_secp521r1|ML_DSA_44|ML_DSA_65|ML_DSA_87|SM2`).
  - `SigningAlgorithm: ML_DSA_44` — used both on the CA config and on
    `issue_certificate`. (ML-DSA has no separate hash-suffix like
    `SHA256WITHECDSA`; the algorithm name is the signing algorithm.)
  - **ROOT CA with ML-DSA is supported** (the blog builds a full ROOT→SUBORDINATE
    ML-DSA chain; we only need a ROOT, matching the classical `PrivateCA`). The
    `arn:aws:acm-pca:::template/RootCACertificate/V1` template + the
    Certificate + Activation flow are unchanged — only KeyAlgorithm /
    SigningAlgorithm differ from the classical CA.
  - Launched Nov 2025; use **us-east-1** (our workshop region) — confirm the CA
    reaches `ACTIVE` there on first deploy.

- **Feasibility gate 1 — CA-side keygen + CSR (RESOLVED, positive).** Device and
  broker keypairs are generated **server-side in Lambda via the `openssl` CLI**
  (see `raw_device_builder/lambda/device-registrar/index.py`,
  `.../prepare-build/index.py`, `.../pqc-upgrader/index.py`, and the broker
  `CertProvisionerFunction`) — the ESP32 never generates its own keypair; the
  private key is baked into the firmware at build time. OpenSSL **3.5+** can
  generate an ML-DSA-44 key and CSR (`openssl req -newkey mldsa44 ...` /
  `EVP_PKEY-ML-DSA`). **Constraint:** the Lambda's bundled OpenSSL is older than
  3.5, so ML-DSA keygen needs an **OpenSSL 3.5+ Lambda layer or a container-image
  Lambda**. Build-side detail, not a design blocker.

- **Feasibility gate 2 — device-side TLS auth (RESOLVED, positive).** wolfSSL
  supports **ML-DSA/Dilithium for TLS 1.3 authentication** (signing the
  CertificateVerify and verifying an ML-DSA peer cert). The ESP32 only needs to
  *use* a pre-issued ML-DSA private key, not generate one. Enable
  `HAVE_DILITHIUM` / `WOLFSSL_DILITHIUM` (ML-DSA) in
  `wolfssl_user_settings_arduino.h`. Task 3.2 still must verify the ESP32-S3
  flash/RAM budget with ML-KEM **and** ML-DSA enabled together (stay under the
  0x380000 per-slot ceiling).

- **Interop note (call out in attendee docs):** ML-DSA in TLS 1.3 is still an
  IETF draft (`draft-ietf-tls-mldsa`); certificate/CertificateVerify codepoints
  are not yet in a final RFC. This is fine here because **both ends use wolfSSL**
  (self-built ESP32 ↔ self-managed Mosquitto+wolfSSL broker), exactly like the
  Step 1 ML-KEM situation — no cross-implementation interop is required.

## Guiding principles

- **Parallel, not flag-day.** Classical (ECDSA) and ML-DSA-44 worlds coexist:
  two CAs, two device certs each, two broker device-facing listeners, and a new
  firmware variant. Migration is per-device over the existing OTA path and is
  reversible.
- **Bridge stays classical.** Only the device-facing side gains PQC certificates.
  The broker→IoT Core bridge keeps its classical IoT-issued cert because IoT Core
  is classical-only.
- **Reuse the Step 1 machinery.** The migration Step Function, publish-ota
  Lambda, per-device CodeBuild, and OTA command channel already exist — extend
  them rather than build parallel infrastructure.

## Current-state anchors (what exists today)

- **Classical CA:** a ROOT ACM Private CA (`PQC Workshop Private CA`) issuing
  ECDSA P-256 certs, created in `broker-container/cloudformation/broker-container-stack.yaml`.
  Broker + device certs are issued from it; a Lambda custom resource issues the
  broker server cert and stores it in Secrets Manager.
- **Broker:** Mosquitto (OpenSSL 3.5 + liboqs + oqs-provider) on ECS Fargate,
  one device-facing listener on **8883** (`mosquitto.conf`), bridge config
  written at runtime by `entrypoint.sh` into `conf.d/bridge.conf`. Fronted by an
  internet-facing **NLB** (`pqc-broker-nlb`), TCP passthrough, one listener +
  target group on 8883, task in private subnets.
- **Device registration:** the registrar Lambda (device-builder stack) creates
  the device identity + classical cert and starts a per-device CodeBuild.
- **Firmware build:** CodeBuild `pqc-workshop-firmware-build` runs `buildspec.yml`,
  selecting the sketch by `FIRMWARE_VARIANT` (`classical` → `aws_iot_pqc_esp32_v1_classical.ino`,
  `pqc` → `aws_iot_pqc_esp32.ino`), injects per-device `secrets.h` (certs/keys),
  compiles for the XIAO ESP32-S3 FQBN, uploads to `s3://<fw-bucket>/<serial>/<version>/`.
- **Migration:** Step Function `pqc-workshop-pqc-migration`
  (`cloudformation/statemachine/pqc-migration.asl.json`): SetStatusBuilding →
  PrepareBuild → WaitForBuild (CodeBuild `.sync`, `FIRMWARE_VARIANT=pqc`) →
  SetStatusPushing → PublishOta (`cmd/<thing>/ota`) → SetStatusRebooting.
- **wolfSSL:** `raw_device_builder/codebuild/wolfssl_user_settings_arduino.h`
  has ML-KEM enabled; ML-DSA sign/verify is the new addition.

## Workstream 1 — Second CA (ML-DSA-44)

- Add a second ACM Private CA resource with signing algorithm **ML-DSA-44**,
  created as a ROOT CA. Likely lives alongside the classical CA in the broker
  stack (or a small dedicated CA stack if cleaner for lifecycle).
- Confirm the ACM PCA API surface for ML-DSA-44 (signing-algorithm enum,
  certificate template, us-east-1 availability) and IAM for issuing from it.
- Both CAs export their ARNs so the registrar/broker-cert issuers can target the
  right one. Teardown must delete both (order/retention considerations).

## Workstream 2 — Dual-cert generation & registration

- **Device registration:** extend the registrar so each device gets TWO certs —
  classical (existing path) + ML-DSA-44 (issue from the new CA). Registry schema
  gains fields for the second cert (and its key material handling).
- **Broker certs:** the broker-cert Lambda custom resource issues TWO server
  certs (one per CA) into Secrets Manager; `entrypoint.sh` fetches both.
- Keep the classical path untouched for un-migrated devices.

## Workstream 3 — Firmware pipeline & third variant

- **New sketch variant:** a NET-NEW third `.ino` (e.g.
  `aws_iot_pqc_esp32_v3_mldsa/aws_iot_pqc_esp32_v3_mldsa.ino`) that does hybrid
  ML-KEM key exchange AND presents the ML-DSA-44 device cert + trusts the
  ML-DSA-44 CA, and targets the broker's **8884** endpoint (`MQTT_BROKER_PORT`
  injected as 8884 for this variant).
- **Uniform OTA (`otaTask`) in ALL three sketches (decision 2):** port the full
  download-and-write `otaTask` from the classical sketch into the PQC sketch and
  the new ML-DSA sketch, so every variant can receive an `ota-update` command and
  write the inactive slot. The download path is variant-agnostic
  (`esp_ota_get_next_update_partition`). Generalize the `migrate-*` swap handlers
  to be slot-content-aware (decision 1) rather than assuming ota_0=classical /
  ota_1=PQC.
- **buildspec / FIRMWARE_VARIANT:** extend selection beyond `classical`/`pqc` to
  a third value (e.g. `pqc-mldsa`), mapping to the new sketch and injecting the
  device's ML-DSA-44 cert/key into `secrets.h`. (Consider a small mapping table
  variant→sketch→cert-source so adding future variants is data, not code.)
- **wolfSSL:** enable ML-DSA sign/verify in `wolfssl_user_settings_arduino.h`;
  validate the ESP32-S3 memory budget with ML-KEM + ML-DSA together (must stay
  under the 3.5 MB / 0x380000 per-slot ceiling; wolfSSL+ML-KEM already ~2.5 MB).
- **OTA path:** reuse the migration Step Function / publish-ota. Verify the S3
  presigned-URL GET works for a SECOND/THIRD OTA to the same device (presign
  expiry, per-version keys `<serial>/<version>/`, bucket versioning) so a device
  can go classical → pqc → pqc-mldsa (and roll back one step) over the air.
- The migration input carries a **variant/version selector** (from the dashboard
  picker) so the same Step Function builds+pushes any chosen variant.

## Workstream 4 — Broker second listener + NLB + bridge

- **mosquitto.conf:** add a second `listener 8884` presenting the ML-DSA-44
  server cert (`certfile`/`keyfile` for the ML-DSA cert), keep 8883 as-is. Both
  require client cert (mutual TLS) against the appropriate CA.
- **entrypoint.sh:** fetch both broker certs from Secrets Manager and wire the
  second listener; the CA trust for device client certs must cover both CAs.
- **NLB:** add a second listener + target group on **8884** (TCP passthrough) to
  the same broker task; extend the broker security group to allow inbound 8884.
- **Bridge:** unchanged — still presents the classical IoT-issued cert to IoT
  Core on the outbound bridge.

## Cross-cutting

- **Dashboard — firmware picker + Flash button (decision 3):** replace the single
  "Migrate to PQC" action on the device card with:
  - a **picker field** listing the selectable firmware variants (classical / pqc
    / pqc-mldsa), and
  - a **Flash button** that triggers the flow for the selected variant: if that
    variant is not already built for the device (no artifacts at
    `s3://<fw-bucket>/<serial>/<version-for-variant>/`), start the build first,
    then issue the OTA job (publish-ota → `cmd/<thing>/ota`).
  This means the AppSync mutation / migration entry point takes a **variant**
  argument (not a hardcoded target), and the Step Function's build step passes
  the corresponding `FIRMWARE_VARIANT`. The card also shows the device's current
  certificate/algorithm state (from telemetry, mirroring how Step 1's
  `pqc_algorithm` field drives "migrated") so the operator sees what's running vs
  what they're flashing.
- **Deploy/teardown/validation:** both CAs + both listeners must come up on a
  from-scratch deploy with no manual step; `check_setup_status.sh` extended to
  check the second CA / 8884 listener; teardown cleans up the new CA, NLB
  listener/target group, and any ML-DSA secrets.
- **DEPLOYMENT.md / attendee Section 6:** update once implemented (Section 6 is
  currently a challenge placeholder).

## Sequencing (proposed)

1. Workstream 1 (second CA) — foundational; everything else needs the CA ARN.
2. Workstream 2 (dual-cert issuance) — needs the CA.
3. Workstream 4 (broker listener + NLB) — needs the broker's ML-DSA cert.
4. Workstream 3 (firmware variant + OTA) — needs the 8884 endpoint + device
   ML-DSA cert to connect to.
5. Cross-cutting (dashboard, validation, docs) — last.

## Risks / to confirm in detailing

- ACM PCA ML-DSA-44 exact parameters + region availability.
- ESP32-S3 memory headroom for ML-KEM + ML-DSA in one image.
- Mutual-TLS trust config when the broker must accept client certs from EITHER
  CA depending on the listener (per-listener CA trust vs shared).
- Whether a device presents its ML-DSA cert only on 8884 (and how it chooses the
  port post-migration — baked into the v2 firmware).
