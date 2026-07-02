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

// Increase max handshake message size for PQC (Kyber keys are larger)
#define MAX_CERTIFICATE_SZ   4096
#define LARGE_STATIC_BUFFERS

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
