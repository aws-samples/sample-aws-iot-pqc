// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// variant_config.h — everything that differs between the firmware variants.
//
// One sketch builds all three steps of the migration. The build picks the
// variant with FIRMWARE_VARIANT in secrets.h (CodeBuild generates it from the
// variant in variants.json; for a local build set it in your secrets.h):
//
//   FW_VARIANT_CLASSICAL  1.0.0  classical key exchange, ECDSA P-256 cert, port 8883
//   FW_VARIANT_PQC        2.0.0  hybrid ML-KEM key exchange, ECDSA P-256 cert, port 8883
//   FW_VARIANT_PQC_MLDSA  3.0.0+ hybrid ML-KEM key exchange, ML-DSA-44 cert, port 8884
//
// The 4.0.0-signer and 5.0.0-verified versions build FW_VARIANT_PQC_MLDSA;
// they differ only in how the cloud signs the image (FIRMWARE_OTA_SIG_ALG).
//
// Workshop tip: this file is the whole classical-vs-PQC difference. Compare the
// FW_VARIANT_CLASSICAL and FW_VARIANT_PQC blocks below, then see how
// VARIANT_PQC_KEX switches the key share in pqc_tls_client.cpp.

#ifndef VARIANT_CONFIG_H
#define VARIANT_CONFIG_H

#define FW_VARIANT_CLASSICAL  1
#define FW_VARIANT_PQC        2
#define FW_VARIANT_PQC_MLDSA  3

// Fail the build rather than silently compiling a default variant (an unset or
// misspelled value evaluates to 0 in #if, which matches none of the variants).
#if !defined(FIRMWARE_VARIANT)
  #error "FIRMWARE_VARIANT is not set. Define it in secrets.h, e.g. #define FIRMWARE_VARIANT FW_VARIANT_PQC"
#endif
#if FIRMWARE_VARIANT != FW_VARIANT_CLASSICAL && FIRMWARE_VARIANT != FW_VARIANT_PQC && FIRMWARE_VARIANT != FW_VARIANT_PQC_MLDSA
  #error "FIRMWARE_VARIANT must be FW_VARIANT_CLASSICAL, FW_VARIANT_PQC or FW_VARIANT_PQC_MLDSA"
#endif

#if FIRMWARE_VARIANT == FW_VARIANT_CLASSICAL
  // ─── Step 0: classical baseline ──────────────────────────────────────────
  #define VARIANT_NAME            "classical"
  #define VARIANT_PQC_KEX         0          // classical ECDHE P-256 key share
  #define VARIANT_DEFAULT_VERSION "1.0.0-classical"
  // Certificate presented in the TLS handshake (reported in telemetry):
  #define CERT_SIGNATURE_ALG      "ECDSA-P256"
  // NVS identity entries for the broker connection (classical CA, listener 8883):
  #define ID_TLS_CA               "ca_cls"
  #define ID_TLS_CERT             "crt_cls"
  #define ID_TLS_KEY              "key_cls"
  #define VARIANT_BANNER_1        "  ESP32 AWS IoT Core — CLASSICAL TLS (Pre-PQC)"
  #define VARIANT_BANNER_2        "  Firmware v1.0.0 — Awaiting PQC Migration"
  // Slow blink on the classical firmware, fast on PQC: a visible migration cue.
  #define VARIANT_BLINK_MS        500

#elif FIRMWARE_VARIANT == FW_VARIANT_PQC
  // ─── Step 1: post-quantum key exchange ───────────────────────────────────
  // Only the key exchange changes; the device still presents its classical
  // ECDSA P-256 certificate on the classical listener.
  #define VARIANT_NAME            "pqc"
  #define VARIANT_PQC_KEX         1          // hybrid SecP256r1MLKEM768 key share
  #define VARIANT_DEFAULT_VERSION "2.0.0-pqc"
  #define CERT_SIGNATURE_ALG      "ECDSA-P256"
  #define ID_TLS_CA               "ca_cls"
  #define ID_TLS_CERT             "crt_cls"
  #define ID_TLS_KEY              "key_cls"
  #define VARIANT_BANNER_1        "  ESP32 AWS IoT Core - Post-Quantum Cryptography"
  #define VARIANT_BANNER_2        "  TLS 1.3 + Hybrid ML-KEM Key Exchange"
  #define VARIANT_BLINK_MS        200

#else
  // ─── Step 2: post-quantum certificate ────────────────────────────────────
  // Hybrid ML-KEM key exchange plus the ML-DSA-44 (FIPS 204) device
  // certificate, verified by the ML-DSA-44 CA on broker listener 8884.
  // key_mldsa is a seed-only PKCS#8 that wolfSSL loads directly.
  #define VARIANT_NAME            "pqc-mldsa"
  #define VARIANT_PQC_KEX         1
  #define VARIANT_DEFAULT_VERSION "3.0.0-mldsa"
  #define CERT_SIGNATURE_ALG      "ML-DSA-44"
  #define ID_TLS_CA               "ca_mldsa"
  #define ID_TLS_CERT             "crt_mldsa"
  #define ID_TLS_KEY              "key_mldsa"
  #define VARIANT_BANNER_1        "  ESP32 AWS IoT Core - Post-Quantum Cryptography"
  #define VARIANT_BANNER_2        "  TLS 1.3 + ML-KEM Key Exchange + ML-DSA-44 Cert"
  #define VARIANT_BLINK_MS        200
#endif

#endif  // VARIANT_CONFIG_H
