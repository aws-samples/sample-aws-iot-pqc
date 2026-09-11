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
