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

## 2. Dual-certificate generation & registration  ✅ IMPLEMENTED

> **Approach: Option B (issue-on-demand per variant) + a SHARED cert-issuer
> container.** Device certs are minted fresh at build time, not stored at
> registration; registrar stays classical-only. All TLS issuance — device certs
> (firmware CodeBuild) AND both broker server certs (a CodeBuild job) — goes
> through ONE shared script (`shared/cert-issuer/issue_cert.py`) in ONE image
> (OpenSSL 3.5 + ACM PCA). Runs as CodeBuild jobs today (fits the deploy model:
> stack first, images/values post-stack); drops into the future registration
> Step Function via `codebuild:startBuild.sync` unchanged. See design.md WS2.
> STATUS: implemented + unit-tested (committed `122e0c0`); pending on-account
> deploy validation.

- [x] 2.1 OpenSSL 3.5+ for ML-DSA keygen/CSR is provided via the SHARED
      cert-issuer image (OpenSSL 3.5.8, `shared/cert-issuer/Dockerfile`), NOT a
      Lambda layer — issuance moved OUT of the Lambdas entirely (Scope A). The
      image self-tests an ML-DSA-44 key + CSR at build time.
- [x] 2.2 `prepare-build` is variant-aware via `VARIANT_CONFIG`
      (classical/pqc → classical CA, 8883; pqc-mldsa → ML-DSA CA, 8884; default
      pqc). The actual cert issuance is performed IN the firmware CodeBuild (via
      the shared `issue_cert.py`, selecting CA + key mode + port from
      `FIRMWARE_VARIANT`), so the private key never leaves the build container.
- [x] 2.3 `MlDsaPrivateCAArn` is surfaced from the broker stack and consumed by
      the issuance path; the firmware CodeBuild role carries the `acm-pca` issue
      permissions for both CAs. (Issuance is in CodeBuild, so no `MLDSA_CA_ARN`
      env on `prepare-build` — the Lambda is pure orchestration.)
- [x] 2.4 Broker: a SECOND (ML-DSA) server cert is minted by
      `issue_broker_certs.py` via the `pqc-workshop-broker-cert-provisioner`
      CodeBuild job (from `MlDsaPrivateCA`) and stored in three new Secrets
      Manager secrets. Chosen over a container-image Lambda (Option A / CodeBuild)
      to fit the stack-first-then-values deploy model. Classical broker cert path
      unchanged. (`entrypoint.sh` fetch + 8884 listener wiring is WS4 — done.)
- [x] 2.5 Classical issuance path fully preserved for un-migrated devices;
      `device-registrar` reduced to pure orchestration (no openssl / no CA ARN /
      no cert material), still classical-only.
- _Depends on: 1 (CA ARNs). Blocks: 3 (device ML-DSA cert), 4 (broker ML-DSA cert)._
- NOTE: sub-task wording above was the pre-implementation plan (Lambda-layer
  issuance); the delivered design converged all issuance into the shared
  cert-issuer container run as CodeBuild jobs (Scope A). Checkmarks reflect the
  delivered design, not the original per-Lambda wording.

## 3. Firmware pipeline & third variant  ✅ IMPLEMENTED

- [x] 3.1 Created the NET-NEW third firmware sketch
      (`aws_iot_pqc_esp32_v3_mldsa/aws_iot_pqc_esp32_v3_mldsa.ino` +
      `secrets.h.template`) as a copy of the pqc sketch. Diff vs the pqc sketch is
      exactly 2 lines: `FIRMWARE_VERSION "3.0.0"` and the banner line
      `TLS 1.3 + ML-KEM Key Exchange + ML-DSA-44 Cert`. ML-KEM key exchange, the
      ML-DSA-44 device cert, ML-DSA CA trust, and port 8884 all flow in at build
      time via `secrets.h` (baked by the buildspec/cert-issuer), so the sketch
      body is identical to pqc by design.
- [x] 3.2 Enabled ML-DSA sign/verify in `wolfssl_user_settings_arduino.h`
      (`HAVE_DILITHIUM`, `WOLFSSL_WC_DILITHIUM`, `WOLFSSL_DILITHIUM_VERIFY`,
      `WOLFSSL_DILITHIUM_SIGN`) for ALL variants (one uniform config; classical/pqc
      simply don't present an ML-DSA cert). On-device memory budget is validated by
      the CodeBuild arduino-cli compile size guard (`upload.maximum_size=3670016`),
      not offline.
- [x] 3.3 Ported the full download `otaTask` into ALL THREE sketches (classical,
      pqc, pqc-mldsa): TAG_OTA, S3_ROOT_CA, OTA globals, jsonGetString/jsonGetNumber,
      OTA_TASK_STACK_SIZE/PRIORITY, the `ota-update` handler branch, the `otaTask`
      fn (uses `esp_ota_get_next_update_partition(NULL)`, slot-agnostic), and
      semaphore+task creation in setup(). Brace balance verified
      (classical 170/170, pqc 173/173, mldsa 173/173).
- [x] 3.4 Made the `migrate-*` swap handlers slot-content-aware via two helpers
      `bootSlotMatchingProject(marker)` (strstr) + `bootSlotExactProject(name)`
      (strcmp) in all 3 sketches: migrate-classical→strstr("classical"),
      migrate-mldsa→strstr("v3_mldsa"), migrate-pqc→strcmp("aws_iot_pqc_esp32")
      (exact, since it is a substring of the other two project names). Targets by
      resident slot content, preserving one-version rollback under Option A.
- [x] 3.5 Extended `buildspec.yml` / `FIRMWARE_VARIANT` to select the third
      variant and inject the ML-DSA-44 device cert into `secrets.h` (WS2's shared
      cert-issuer). `prepare-build` VARIANT_CONFIG maps
      classical→0.1.0, pqc→1.0.0-pqc, pqc-mldsa→2.0.0-mldsa; `deploy-workshop.sh`
      stages the v3 sketch into firmware `source/` (added to REQUIRED_FW_SOURCE).
- [x] 3.6 Extended the migration entry point + Step Function to take a **variant**
      selector: `start-migration` validates `variant`
      (VALID_VARIANTS={classical,pqc,pqc-mldsa}, default pqc) and injects it into
      the SFN input; the ASL threads `variant`/`version`/`buildEnv` through
      PrepareBuild → WaitForBuild → PublishOta; `publish-ota` derives `version`
      from the event. ALSO fixed a latent double-build: `prepare-build` no longer
      starts a CodeBuild (it now resolves per-device build env — WiFi/broker/IoT
      via SSM→registry→env — and returns `buildEnv`); WaitForBuild is the single
      authoritative build, which also fixes a WiFi-correctness bug.
- [x] 3.7 S3 presigned-URL OTA for a SECOND/THIRD OTA to the same device
      (classical → pqc → pqc-mldsa + one-step rollback) is sound by construction:
      publish-ota / firmware-pusher presign fresh per invocation (5-min expiry),
      keys are per-version (`<serial>/<version>/`), bucket versioning is on. (Full
      on-device confirmation is part of the WS5 cross-workstream validation.)
- _Depends on: 1, 2 (device ML-DSA cert), 4 (8884 endpoint to connect to)._
- Validation criterion (on-account): CodeBuild arduino-cli compile of the v3
  sketch succeeds under the size guard; a pqc-mldsa device flashes, connects to
  8884, and negotiates ML-KEM + ML-DSA-44. STATUS: offline validation complete
  (50 unit tests green, ASL JSON valid, all Lambdas py_compile OK, sketch braces
  balanced, deploy-workshop.sh `bash -n` OK); pending on-account deploy in WS5.

## 4. Broker: second listener + NLB + bridge  ✅ IMPLEMENTED

- [x] 4.1 Added `listener 8884` (ML-DSA-44 server cert) to `mosquitto.conf`,
      keeping 8883. `per_listener_settings true` (already set) makes 8884's
      cafile/certfile/keyfile + `require_certificate` mTLS apply to that listener
      only; 8884 trusts the ML-DSA-44 CA. Cert files at /mosquitto/certs/mldsa/.
- [x] 4.2 `entrypoint.sh` fetches the 3 ML-DSA secrets
      (MLDSA_CA_CERT_SECRET / MLDSA_BROKER_CERT_SECRET / MLDSA_BROKER_KEY_SECRET)
      into /mosquitto/certs/mldsa/{ca.crt,broker.crt,broker.key}. 8883 fetch
      unchanged.
- [x] 4.3 Added `MlDsaBrokerTargetGroup` + `MlDsaBrokerListener` on 8884 (TCP
      passthrough, same NLB); container PortMapping 8884; ECS service
      LoadBalancers entry 8884; security group ingress 8884 (0.0.0.0/0). Task
      role reads the 3 ML-DSA secrets; task env passes their ARNs. BrokerService
      DependsOn both listeners. New output BrokerMlDsaPort=8884.
- [x] 4.4 Bridge to IoT Core UNCHANGED (still classical IoT-issued cert; only the
      device-facing side gained the 8884 ML-DSA listener).
- Note: BPA — 8884 inbound (IGW ingress) is covered by the same VPC-wide
  VpcBpaExclusion as 8883; no extra BPA work.
- Validation criterion (on-account): `openssl s_client -connect <nlb>:8884`
  negotiates the ML-DSA-44 broker cert; a pqc-mldsa device (WS3) connects + does
  ML-KEM + ML-DSA. STATUS: CFN/entrypoint/mosquitto validated offline (parse,
  bash -n, listener present); pending on-account deploy.
- _Depends on: 1, 2 (broker ML-DSA cert). Blocks: 3 end-to-end._

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
