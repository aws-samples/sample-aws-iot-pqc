# Tasks — ML-DSA-44 Certificate Migration (Step 2)

> Status: **key decisions locked** (Option A OTA slot reuse; all variants carry
> the download `otaTask`; dashboard picker + Flash button; managed ACM PCA
> ML-DSA-44 CA; ports 8883/8884). Epics below still get detailed into
> implementation-ready sub-tasks (acceptance criteria, file edits, tests) as each
> workstream is picked up. Sequencing follows the design's proposed order;
> dependencies noted per group. Validation account: **pqc-a** (rebuild/redeploy).

## 1. Second CA (ML-DSA-44) — foundational

- [x] 1.1 Confirm ACM PCA ML-DSA-44 specifics: signing-algorithm enum,
      certificate template, IAM actions, us-east-1 availability. (spike)
      → DONE. `KeyAlgorithm: ML_DSA_44`, `SigningAlgorithm: ML_DSA_44` (no hash
      suffix), ROOT + `RootCACertificate/V1` template unchanged. Both feasibility
      gates resolved positive: Lambda-side ML-DSA keygen/CSR needs OpenSSL 3.5+
      (layer/container image); wolfSSL supports ML-DSA TLS 1.3 auth on device.
      Full findings in design.md "Workstream 1 spike findings".
- [x] 1.2 Add a second ACM Private CA (ROOT, ML-DSA-44) to CloudFormation
      alongside the classical CA; export its ARN. Keep the classical CA
      unchanged.
      → DONE. Added `MlDsaPrivateCA` + `MlDsaPrivateCACertificate` +
      `MlDsaPrivateCAActivation` to `broker-container-stack.yaml` (mirrors the
      classical `PrivateCA` block, KeyAlgorithm/SigningAlgorithm = `ML_DSA_44`).
      New output `MlDsaPrivateCAArn` on the broker stack, surfaced as a
      `MlDsaPrivateCAArn` output on `pqc-workshop-main.yaml`. Classical CA
      untouched. (Wiring the ARN into DeviceBuilderStack is deferred to WS2, when
      the matching parameter is added there.)
- [x] 1.3 Ensure from-scratch deploy creates both CAs with no manual step, and
      teardown deletes both cleanly (order/retention).
      → DONE (by construction). Both CAs are pure CFN resources with no
      `DeletionPolicy: Retain`, so a from-scratch deploy creates both (no manual
      step) and stack delete removes both. The ML-DSA CA follows the identical
      lifecycle CFN already manages for the classical CA (create → issue root cert
      → activate; on delete CFN disables + deletes). No separate teardown handling
      needed beyond what the classical CA already has.

- [x] 1.4 **VALIDATED on pqc-a (087195661091, us-east-1).** Ran the exact ACM PCA
      flow the 3 CFN resources perform, via CLI: `create-certificate-authority`
      (KeyAlgorithm/SigningAlgorithm `ML_DSA_44`, ROOT) → `PENDING_CERTIFICATE`
      → `get-certificate-authority-csr` → `issue-certificate` (SigningAlgorithm
      `ML_DSA_44`, template `RootCACertificate/V1`, 10 YEARS) →
      `get-certificate` → `import-certificate-authority-certificate` → **ACTIVE**.
      Root cert OID `2.16.840.1.101.3.4.3.17` = ML-DSA-44 confirmed. Probe CA then
      disabled + deleted (no orphan). This proves the CFN block will create +
      activate on a from-scratch deploy. (A full stack from-scratch rebuild will
      still exercise it in place during a later cross-workstream validation.)
- _Depends on: nothing. Blocks: workstreams 2, 3, 4._

## 2. Dual-certificate generation & registration

- [ ] 2.1 Extend device registration to issue TWO device certs (classical +
      ML-DSA-44) for the same identity; update the registry schema + key
      handling.
- [ ] 2.2 Issue TWO broker server certs (one per CA) via the broker-cert Lambda
      custom resource; store both in Secrets Manager.
- [ ] 2.3 Keep the classical issuance path fully working for un-migrated devices.
- _Depends on: 1 (CA ARNs). Blocks: 3 (device ML-DSA cert), 4 (broker ML-DSA cert)._

## 3. Firmware pipeline & third variant

- [ ] 3.1 Create the NET-NEW third firmware sketch
      (`aws_iot_pqc_esp32_v3_mldsa/…`): ML-KEM key exchange + ML-DSA-44 device
      cert + trust the ML-DSA-44 CA, `MQTT_BROKER_PORT` = 8884.
- [ ] 3.2 Enable ML-DSA sign/verify in `wolfssl_user_settings_arduino.h`; verify
      ESP32-S3 memory budget (ML-KEM + ML-DSA together, under 0x380000). (spike + config)
- [ ] 3.3 **Port the full download `otaTask` into ALL THREE sketches** (classical
      already has it; add to pqc and pqc-mldsa) so any variant can receive an
      `ota-update` and write the inactive slot.
- [ ] 3.4 **Generalize the `migrate-*` swap handlers to be slot-content-aware**
      (inspect each slot via `esp_ota_get_partition_description`, target by what's
      resident) instead of assuming ota_0=classical / ota_1=PQC — preserves the
      one-version rollback under Option A.
- [ ] 3.5 Extend `buildspec.yml` / `FIRMWARE_VARIANT` to select the third variant
      and inject the device's ML-DSA-44 cert into `secrets.h` (prefer a
      variant→sketch→cert-source mapping so future variants are data, not code).
- [ ] 3.6 Extend the migration Step Function / entry point to take a **variant**
      selector (from the dashboard picker) and build+push any chosen variant.
- [ ] 3.7 Verify the S3 presigned-URL OTA works for a SECOND/THIRD OTA to the
      same device (classical → pqc → pqc-mldsa, + one-step rollback): presign
      expiry, per-version keys, bucket versioning.
- _Depends on: 1, 2 (device ML-DSA cert), 4 (8884 endpoint to connect to)._

## 4. Broker: second listener + NLB + bridge

- [ ] 4.1 Add `listener 8884` (ML-DSA-44 server cert) to `mosquitto.conf`,
      keeping 8883; configure per-listener CA trust for client certs.
- [ ] 4.2 Update `entrypoint.sh` to fetch BOTH broker certs and wire the second
      listener.
- [ ] 4.3 Add an NLB listener + target group on 8884 (TCP passthrough); open the
      broker security group to inbound 8884.
- [ ] 4.4 Confirm the bridge to IoT Core is UNCHANGED (classical cert).
- _Depends on: 1, 2 (broker ML-DSA cert). Blocks: 3.5 end-to-end._

## 5. Cross-cutting (last)

- [ ] 5.1 Dashboard: replace the single migrate action with a **firmware picker
      field** (classical / pqc / pqc-mldsa) + a **Flash button** that builds the
      variant if not already built, then issues the OTA job; wire the AppSync
      mutation / migration entry point to take a `variant` argument. Show the
      device's current cert/algorithm state (from `pqc_algorithm` telemetry) so
      the operator sees running-vs-target.
- [ ] 5.2 Extend `check_setup_status.sh` to validate the second CA + 8884
      listener/target group.
- [ ] 5.3 From-scratch deploy + teardown validation on both accounts (merge
      gate) with both CAs + both listeners present.
- [ ] 5.4 Docs: update DEPLOYMENT.md and write attendee Section 6
      (`6-migrate-certificates`) click-by-click steps (currently a placeholder).
- _Depends on: 1–4._

## Detailing checklist (before implementation)

- [ ] Turn each epic above into concrete sub-tasks with acceptance criteria.
- [ ] Resolve the design's open questions (ACM PCA params, memory budget,
      per-listener CA trust, cert storage schema).
- [ ] Decide test approach per workstream (unit for Lambdas/state machine,
      integration for the end-to-end migration).
