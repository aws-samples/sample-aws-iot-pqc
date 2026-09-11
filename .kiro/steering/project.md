# Project Steering

## OVERRIDING DESIGN PARADIGM (highest priority — governs every decision)

The entire stack, plus a small number of clearly-documented manual steps, MUST be
deployable in ONE attempt, with ALL features working: ESP32 device registration,
firmware builds, and the Mosquitto broker, along with every supporting AWS
resource. This project is intended for **AWS Workshop Studio**, so excessive or
fragile manual steps are unacceptable — minimise them and document the few that
remain.

The ONLY test that truly matters is the **end-to-end test**: a from-scratch
deployment followed by setting up the first ESP32 device successfully. A command
exiting cleanly is NOT success — success is the full path working end to end.

There MUST be no need to modify workshop code or the environment the day before
(or during) the re:Invent workshop. Any fix that only works via in-place patching
of a running environment is NOT done; it must survive a clean deploy.

**Definition of done (final acceptance):** a destroy + from-scratch rebuild in
`pqc-b`, then a destroy + from-scratch rebuild in `pqc-a`, with BOTH environments
fully working after their clean deployment. Until both pass a from-scratch
deploy, the work is not complete. Note `pqc-b` has VPC Block Public Access
(block-ingress) enabled and `pqc-a` does not — the deployment must work in both,
so it cannot rely on public-IP + IGW egress for the broker task.

Practical implications for any change:
- Prefer fixes in the deployable source (CloudFormation, Dockerfile, deploy
  scripts) over manual console/CLI actions on a live environment.
- If a manual step is unavoidable, it must be minimal, documented, and part of
  the deploy runbook.
- Validate changes by reasoning about (and ultimately performing) a clean
  destroy + redeploy, not just an in-place update.

## Clean teardown / rebuild notes (avoid re-discovering these)

Deleting the master stack `pqc-workshop` cascades to all nested stacks, but a
few resources are NOT cleaned up by CloudFormation and must be handled around
the teardown/rebuild. Handle these to keep a from-scratch deploy clean:

- **Versioned S3 buckets block stack deletion.** Empty ALL object versions +
  delete-markers first for: `pqc-workshop-broker-source-<acct>`,
  `pqc-workshop-firmware-<acct>`, `pqc-workshop-firmware-dashboard-<acct>`.
- **ECR images block repo deletion.** Batch-delete images in
  `pqc-workshop-mosquitto-broker` (and `pqc-workshop-firmware-builder`) before
  deleting the stack.
- **IoT Thing Type `esp32-pqc-device` deletion** needs no associated Things and
  a ~5-min deprecation wait. Detach principals + delete any `esp32-*` device
  Things first. (Leave `pqc-workshop-broker-bridge` to the broker stack's
  BridgeProvisioner custom resource.)
- **Stale SSM managed instances (builder-Pi).** Each `bundle-for-builder-pi.sh`
  run mints a NEW SSM managed-instance id; the physical Pi re-registers under a
  new `mi-...` id and the old ones linger as `ConnectionLost`. After registering
  the Pi, deregister stale entries:
  `aws ssm describe-instance-information` -> for each `ConnectionLost`
  `pqc-workshop-builder-pi`, `aws ssm deregister-managed-instance --instance-id mi-...`.
  Keep only the `Online` one. (Backlog: have the bundler/setup reuse a stable
  registration or auto-prune stale ones.)
- **IAM roles created by the bundler survive teardown.** `bundle-for-builder-pi.sh`
  creates `pqc-workshop-builder-pi-ssm` outside CloudFormation; it persists and
  is reused across rebuilds (fine, just don't expect the stack to own it).

Backlog candidates surfaced during rebuilds (not yet fixed):
- **Firmware builder image is a manual post-deploy build (should be automatic).**
  The broker image auto-builds via a BuildTrigger Lambda + `Custom::TriggerBuild`
  custom resource in broker-container-stack.yaml; the device-builder stack defines
  `ImageBuilderProject` (`pqc-workshop-build-image`) + its ECR repo but has NO
  trigger, so nothing builds it — a fresh deploy leaves `pqc-workshop-firmware-builder`
  ECR empty and the first device flash fails at PROVISIONING
  (`CannotPullImageManifestError`). NOT a dependency issue — just missing wiring.
  Fix: fold the firmware image build into `deploy-workshop.sh` step 3 (trigger +
  wait, exactly like the broker image build), removing the manual step entirely.
  Interim: DEPLOYMENT.md step 4 documents it loudly as mandatory-before-flash.
- Bundler does not bake `broker_endpoint` into the Pi `config.json` (only api +
  firmware bucket), so it must be entered in the GUI. Consider resolving it from
  the `/pqc-workshop/*` SSM params / broker NLB output.
- Broker image build depends on `apt-get` from the Ubuntu mirror in the builder
  stage; hardened with retries/timeouts + 60-min CodeBuild timeout, but a fully
  offline/pinned base or pre-baked deps would be more robust for Workshop Studio.

## Overview

This is an Arduino sample project demonstrating how to connect an ESP32 microcontroller to AWS IoT Core using post-quantum cryptography (PQC). It uses wolfSSL for TLS 1.3 with hybrid Kyber/ML-KEM key exchange, and manages WiFi and MQTT as independent FreeRTOS tasks with automatic failure recovery.

## Tech Stack

- **Platform:** ESP32 (all WiFi-capable variants) via Arduino framework
- **IDE:** Arduino Cloud IDE (app.arduino.cc) and Arduino Desktop IDE
- **TLS Library:** wolfSSL 5.7.0+ with Kyber/ML-KEM PQC support
- **MQTT Library:** PubSubClient 2.8+
- **RTOS:** FreeRTOS (built into ESP32 Arduino core)
- **Cloud Service:** AWS IoT Core (MQTT over TLS 1.3, port 8883)
- **Authentication:** Mutual TLS with X.509 certificates

## Conventions

- Single `.ino` sketch file for Arduino Cloud IDE compatibility
- Configuration and credentials isolated in `secrets.h` (never commit real values)
- wolfSSL compile-time configuration documented in `wolfssl_user_settings.h` (reference only, not included directly)
- FreeRTOS tasks use `xTaskCreatePinnedToCore` with explicit stack sizes
- Serial output uses structured prefixes: `[WiFi]`, `[MQTT]`, `[wolfSSL]`, `[Setup]`
- All timing constants defined as `#define` at the top of the sketch for easy tuning
- Use inclusive language in all code, comments, and documentation (see inclusivity rules)
- C-style code following Arduino conventions (camelCase for functions, g_ prefix for globals)
