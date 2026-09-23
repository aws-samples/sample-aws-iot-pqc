# Design — ML-DSA-44 Certificate Migration (Step 2)

> Status: **high-level draft**, seeded from the four workstreams. To be detailed
> further before implementation. Grounded in the existing (Step 1) components.

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

- **New sketch variant:** a third `.ino` (e.g. `aws_iot_pqc_esp32_v2_mldsa`) that
  does hybrid ML-KEM key exchange AND presents the ML-DSA-44 device cert +
  trusts the ML-DSA-44 CA, and targets the broker's **8884** endpoint.
- **buildspec / FIRMWARE_VARIANT:** extend selection beyond `classical`/`pqc` to
  a third value (e.g. `pqc-mldsa`), mapping to the new sketch and injecting the
  device's ML-DSA-44 cert/key into `secrets.h`.
- **wolfSSL:** enable ML-DSA sign/verify in `wolfssl_user_settings_arduino.h`;
  validate the ESP32-S3 memory budget with ML-KEM + ML-DSA together.
- **OTA path:** reuse the migration Step Function / publish-ota. Verify the S3
  presigned-URL GET works for a SECOND/THIRD OTA to the same device (presign
  expiry, per-version keys `<serial>/<version>/`, bucket versioning) so Step 1 →
  Step 2 → future updates all work.
- Likely a new migration entry point / state-machine parameter to select the
  ML-DSA variant (or a variant field on the migration input).

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

- **Dashboard:** represent a device's certificate state (Step 1 classical cert vs
  Step 2 ML-DSA-44 cert), likely from telemetry/registry — mirror how Step 1's
  `pqc_algorithm` telemetry drives "migrated".
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
