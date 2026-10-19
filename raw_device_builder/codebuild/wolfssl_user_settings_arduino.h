// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/*
 * wolfssl_user_settings.h
 *
 * Reference wolfSSL configuration for ESP32 + AWS IoT Core + PQC.
 *
 * HOW TO USE:
 *   Copy the #define lines below into the wolfSSL library's user_settings.h
 *   file. The file location depends on your OS:
 *
 *   Arduino IDE (local):
 *     Windows: C:\Users\<you>\Documents\Arduino\libraries\wolfssl\src\user_settings.h
 *     macOS:   ~/Documents/Arduino/libraries/wolfssl/src/user_settings.h
 *     Linux:   ~/Arduino/libraries/wolfssl/src/user_settings.h
 *
 *   Arduino Cloud IDE:
 *     Upload this as a custom library or use the Secret tab for certs.
 *     You may need to fork the wolfSSL Arduino library and modify
 *     user_settings.h in your fork, then import it.
 *
 * IMPORTANT:
 *   - Do NOT #include this file directly in your sketch.
 *   - These defines go into the wolfSSL library's own user_settings.h.
 *   - After changing user_settings.h, do a clean rebuild (Sketch > Clean).
 */

#ifndef WOLFSSL_USER_SETTINGS_PQC_H
#define WOLFSSL_USER_SETTINGS_PQC_H

// ─── Platform: Espressif ESP32 ──────────────────────────────────────────────
// Note: Do NOT define WOLFSSL_ESPIDF when using Arduino ESP32 core v3.x
// (which is built on ESP-IDF internally). Only define WOLFSSL_ESP32.
#ifdef ARDUINO
  // Arduino ESP32 core — do not set ESPIDF flag
  #define WOLFSSL_ESP32
#else
  // Native ESP-IDF build
  #define WOLFSSL_ESPIDF
  #define WOLFSSL_ESPWROOM32
  #define WOLFSSL_ESP32
#endif

// Use ESP32 hardware acceleration where available
// Note: HW crypto (WOLFSSL_ESP32_CRYPT) requires native ESP-IDF build.
// Arduino ESP32 core v3.x does not expose the esp32 HW crypto API directly.
#ifndef ARDUINO
  #define WOLFSSL_ESP32_CRYPT       // HW AES, SHA, RSA acceleration
#endif
#define USE_FAST_MATH
#define TFM_TIMING_RESISTANT

// ─── TLS 1.3 ────────────────────────────────────────────────────────────────
#define WOLFSSL_TLS13
#define HAVE_TLS_EXTENSIONS
#define HAVE_SUPPORTED_CURVES
#define HAVE_FFDHE_2048             // Finite-field DH for TLS 1.3
#define HAVE_HKDF                   // Required for TLS 1.3 key derivation

// ─── Post-Quantum Cryptography (ML-KEM / FIPS 203) ─────────────────────────
// Uses the IANA-standardized ML-KEM hybrid key exchange groups for
// interoperability with OpenSSL 3.5+ (e.g., Mosquitto PQC broker).
//
// Available hybrids (code points):
//   SecP256r1MLKEM768  (4587) — P-256 + ML-KEM-768 [recommended]
//   X25519MLKEM768     (4588) — X25519 + ML-KEM-768
//   SecP384r1MLKEM1024 (4589) — P-384 + ML-KEM-1024
//
// Reference: wolfSSL appendix G (Key Exchange Groups, section B-8)
#define WOLFSSL_HAVE_MLKEM          // Enable ML-KEM (FIPS 203) support
#define HAVE_PQC                    // General PQC flag
#define WOLFSSL_PQC_HYBRIDS         // Enable hybrid key exchange groups
#define WOLFSSL_WC_MLKEM            // Use wolfSSL's native ML-KEM implementation (5.8.x)
#define WOLFSSL_SHA3                // Required by ML-KEM

// ─── Post-Quantum Authentication (ML-DSA / FIPS 204) — Step 2 ──────────────
// ML-DSA (Dilithium) is the post-quantum SIGNATURE scheme. It is used for TLS
// 1.3 AUTHENTICATION: the ML-DSA firmware variant presents an ML-DSA-44 client
// certificate and verifies the broker's ML-DSA-44 server certificate (both ends
// are wolfSSL, so interop is guaranteed). This is separate from ML-KEM (key
// exchange) above — Step 2 migrates the CERTIFICATE signature to post-quantum.
// Enabled for ALL firmware variants (the classical/pqc sketches simply do not
// present an ML-DSA cert); keeping one wolfSSL config keeps the build uniform.
#define HAVE_DILITHIUM              // Enable ML-DSA (FIPS 204) signatures
#define WOLFSSL_WC_DILITHIUM        // wolfSSL native ML-DSA implementation
#define WOLFSSL_DILITHIUM_VERIFY    // Verify an ML-DSA peer certificate (broker cert)
#define WOLFSSL_DILITHIUM_SIGN      // Sign the CertificateVerify with the ML-DSA key
// Explicitly enable the ML-DSA public-key path the OTA verifier uses
// (Workstream 3). WOLFSSL_MLDSA_PUBLIC_KEY compiles wc_MlDsaKey_PublicKeyDecode
// (aliased wc_Dilithium_PublicKeyDecode) so we can parse the signer leaf's
// public key from its SubjectPublicKeyInfo and verify the firmware signature
// over the mmap'd image. TLS already parses ML-DSA certs, but we state the
// dependency rather than rely on it being pulled in transitively.
#define WOLFSSL_MLDSA_PUBLIC_KEY
// ML-DSA uses SHAKE128/256 (already enabled below for ML-KEM) and SHA3.

// ─── ECC (for hybrid PQC key exchange) ──────────────────────────────────────
#define HAVE_ECC
#define ECC_TIMING_RESISTANT
#define HAVE_ECC_SIGN
#define HAVE_ECC_VERIFY
#define HAVE_ECC_DHE
#define ECC_SHAMIR

// P-256 curve for hybrid key exchange (SecP256r1MLKEM768)
#define HAVE_ECC256                 // NIST P-256 (for SecP256r1MLKEM768)

// ─── RSA (for certificate verification) ─────────────────────────────────────
#define RSA_LOW_MEM
#define WC_RSA_BLINDING
#define WOLFSSL_KEY_GEN
#define WC_RSA_PSS                  // Required for TLS 1.3 with RSA certs

// ─── Cipher Suites ──────────────────────────────────────────────────────────
#define HAVE_AESGCM                 // AES-GCM (primary TLS 1.3 cipher)
#define HAVE_AESCCM
#define HAVE_CHACHA
#define HAVE_POLY1305
#define HAVE_ONE_TIME_AUTH

// ─── Hashing ────────────────────────────────────────────────────────────────
#define WOLFSSL_SHA384
#define WOLFSSL_SHA512
#define WOLFSSL_SHAKE128            // Required by ML-KEM (SHAKE for key gen)
#define WOLFSSL_SHAKE256            // Required by ML-KEM

// ─── X.509 Certificate Support ──────────────────────────────────────────────
#define HAVE_SNI                    // Server Name Indication (required for AWS)
#define WOLFSSL_ASN_TEMPLATE
#define WOLFSSL_BASE64_ENCODE

// ─── Memory / Performance Tuning for ESP32 ──────────────────────────────────
#define SMALL_SESSION_CACHE
#define NO_SESSION_CACHE_REF
#define WOLFSSL_SMALL_STACK
#define WOLFSSL_SMALL_STACK_CACHE

// Max handshake message size for PQC. ML-KEM keys are larger than classical,
// but ML-DSA-44 CERTIFICATES are much larger again: a single ML-DSA-44 cert is
// ~3.8 KB, and the broker sends a CHAIN (server cert + issuing CA cert), so the
// TLS Certificate message is ~8 KB+ — over the old 8192 cap. With the cap too
// small the device aborted the handshake right after "Starting TLS 1.3 PQC
// handshake" with wolfSSL BUFFER_E / "Handshake message too large" (code=-404)
// when receiving the broker's ML-DSA-44 cert on 8884 (observed on the pqc-a
// fw2->fw3 migration).
//
// 16 KB comfortably covers the ~8 KB inbound ML-DSA chain AND the device's own
// outbound ML-DSA cert + CertificateVerify, with headroom, while keeping RAM use
// modest — the ESP32-S3 min-heap during the handshake was ~44-90 KB, so we must
// not over-reserve. We deliberately do NOT define LARGE_STATIC_BUFFERS: that
// forces wolfSSL to reserve the record/cert buffers at their MAX size for the
// whole session, which would waste ~tens of KB of heap. Without it wolfSSL grows
// the input buffer dynamically only when the large cert actually arrives (peak
// memory ~ one message), which is how wolfSSL intends large PQC certs to be
// handled and is far safer on a constrained device.
#define MAX_CERTIFICATE_SZ   16384

// ─── Disable Unused Features (save flash/RAM) ──────────────────────────────
#define NO_DSA
#define NO_DES3
#define NO_RC4
#define NO_MD4
#define NO_MD5
#define NO_OLD_TLS
#define NO_PSK
#define WOLFSSL_NO_TLS12            // TLS 1.3 only (saves significant space)
#define NO_WOLFSSL_ESP32_CRYPT_HASH // Disable ESP32 HW hash (conflicts in Arduino)
#define NO_OLD_RNGNAME              // Avoid openssl compat RNG naming conflicts

// ─── Debugging (uncomment for development) ──────────────────────────────────
// #define DEBUG_WOLFSSL
// #define WOLFSSL_LOG_PRINTF

#endif // WOLFSSL_USER_SETTINGS_PQC_H
