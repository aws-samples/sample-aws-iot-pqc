# Tasks — ML-DSA-44 Certificate Migration (Step 2)

> Status: **high-level breakdown**, seeded from the four workstreams. These are
> epics/task groups, NOT yet implementation-ready sub-tasks — to be detailed
> (acceptance criteria, file-level edits, tests) before coding. Sequencing
> follows the design's proposed order; dependencies noted per group.

## 1. Second CA (ML-DSA-44) — foundational

- [ ] 1.1 Confirm ACM PCA ML-DSA-44 specifics: signing-algorithm enum,
      certificate template, IAM actions, us-east-1 availability. (spike)
- [ ] 1.2 Add a second ACM Private CA (ROOT, ML-DSA-44) to CloudFormation
      alongside the classical CA; export its ARN. Keep the classical CA
      unchanged.
- [ ] 1.3 Ensure from-scratch deploy creates both CAs with no manual step, and
      teardown deletes both cleanly (order/retention).
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

- [ ] 3.1 Create the third firmware sketch (ML-KEM key exchange + ML-DSA-44 cert,
      targets broker 8884). New `.ino`.
- [ ] 3.2 Enable ML-DSA sign/verify in `wolfssl_user_settings_arduino.h`; verify
      ESP32-S3 memory budget (ML-KEM + ML-DSA together). (spike + config)
- [ ] 3.3 Extend `buildspec.yml` / `FIRMWARE_VARIANT` to select the third variant
      and inject the device's ML-DSA-44 cert into `secrets.h`.
- [ ] 3.4 Extend the migration Step Function / entry point to trigger the ML-DSA
      variant (variant field on the migration input).
- [ ] 3.5 Verify the S3 presigned-URL OTA works for a SECOND/THIRD OTA to the
      same device (Step 1 → Step 2 → later): presign expiry, per-version keys,
      bucket versioning.
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

- [ ] 5.1 Dashboard: represent certificate state (Step 1 classical vs Step 2
      ML-DSA-44), mirroring the `pqc_algorithm`-telemetry approach.
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
