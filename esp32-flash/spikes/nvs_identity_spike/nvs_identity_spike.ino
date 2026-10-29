// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/*
 * nvs_identity_spike.ino — feasibility spike for the NVS device-identity design
 * (.kiro/specs/nvs-device-identity, tasks 0.1–0.3).
 *
 * Reads a test identity from the dedicated "certs" NVS partition and proves,
 * on the real device with the production wolfSSL build:
 *   0.1  the partition table has "certs" at 0x721000 (64 KB);
 *   0.3  every NVS entry reads back intact (lengths + SHA-256 printed so they
 *        can be compared with the host-side generator's output);
 *   0.2  the ML-DSA-44 key rebuilt from the 32-byte seed matches the device
 *        certificate's public key, signs, and loads into a TLS 1.3 context;
 *        plus the seed-only PKCS#8 fallback, and the classical P-256 identity.
 *
 * No WiFi, no broker: this only exercises identity storage and key material.
 * The identity it reads is a throwaway test identity from
 * esp32-flash/spikes/make_spike_identity.sh (test CAs, not the workshop CAs).
 *
 * Output: "[Spike] PASS/FAIL <check>" lines, then a summary, repeated every
 * 15 s so a late-attached serial monitor still sees the result.
 */

#include <Preferences.h>
#include <esp_partition.h>
#include <esp_ota_ops.h>
#include <esp_heap_caps.h>

#include "blob.h"

#include <wolfssl.h>              // Must come first (defines WOLFSSL_USER_SETTINGS)
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/ssl.h>
#include <wolfssl/wolfcrypt/sha256.h>
#include <wolfssl/wolfcrypt/random.h>
#include <wolfssl/wolfcrypt/ecc.h>
#include <wolfssl/wolfcrypt/dilithium.h>
#include <wolfssl/wolfcrypt/asn_public.h>
#include <wolfssl/wolfcrypt/asn.h>

#define ID_PARTITION   "certs"
#define ID_NAMESPACE   "devid"
#define ID_FORMAT      1
#define EXPECT_ADDR    0x721000
#define EXPECT_SIZE    0x10000
#define MLDSA_SEED_LEN 32

static int g_pass = 0, g_fail = 0;
static String g_report;

static void check(const char* name, bool ok, const String& detail = "") {
    String line = String("[Spike] ") + (ok ? "PASS " : "FAIL ") + name;
    if (detail.length()) line += "  (" + detail + ")";
    Serial.println(line);
    g_report += line + "\n";
    if (ok) g_pass++; else g_fail++;
}

static Preferences prefs;

static Blob readBlob(const char* key) {
    Blob b;
    size_t n = prefs.getBytesLength(key);
    if (n == 0) return b;
    b.data = (uint8_t*)malloc(n);
    if (!b.data) return b;
    b.len = prefs.getBytes(key, b.data, n);
    return b;
}

static void freeBlob(Blob& b) {
    free(b.data);
    b.data = nullptr;
    b.len = 0;
}

static String sha256Hex(const uint8_t* d, size_t n) {
    byte h[WC_SHA256_DIGEST_SIZE];
    if (wc_Sha256Hash(d, (word32)n, h) != 0) return "sha-error";
    char out[2 * WC_SHA256_DIGEST_SIZE + 1];
    for (int i = 0; i < WC_SHA256_DIGEST_SIZE; i++) sprintf(out + 2 * i, "%02x", h[i]);
    return String(out);
}

static void printEntry(const char* key, const Blob& b) {
    Serial.printf("[Spike] entry %-11s len=%5u sha256=%s\n", key, (unsigned)b.len,
                  b.len ? sha256Hex(b.data, b.len).c_str() : "-");
}

// Public key from a DER certificate, as wolfSSL hands it to the verifier.
// For ML-DSA this is the raw 1312-byte key; for ECC a SubjectPublicKeyInfo.
static int certPublicKey(const Blob& crt, uint8_t* out, word32 cap) {
    DecodedCert c;
    wc_InitDecodedCert(&c, crt.data, (word32)crt.len, NULL);
    int ret = wc_ParseCert(&c, CERT_TYPE, NO_VERIFY, NULL);
    word32 len = cap;
    if (ret == 0) ret = wc_GetPubKeyDerFromCert(&c, out, &len);
    wc_FreeDecodedCert(&c);
    return ret == 0 ? (int)len : ret;
}

static dilithium_key* newMlDsaKey() {
    // dilithium_key is multi-KB: keep it off the task stack (PSRAM if present).
    dilithium_key* k = (dilithium_key*)heap_caps_malloc(sizeof(dilithium_key), MALLOC_CAP_SPIRAM);
    if (!k) k = (dilithium_key*)malloc(sizeof(dilithium_key));
    if (!k) return nullptr;
    if (wc_dilithium_init(k) != 0 || wc_dilithium_set_level(k, WC_ML_DSA_44) != 0) {
        free(k);
        return nullptr;
    }
    return k;
}

static void freeMlDsaKey(dilithium_key*& k) {
    if (!k) return;
    wc_dilithium_free(k);
    free(k);
    k = nullptr;
}

// CA root verifies the device certificate (the same check TLS and the OTA
// signer verifier depend on).
static bool chainVerifies(const Blob& ca, const Blob& crt) {
    WOLFSSL_CERT_MANAGER* cm = wolfSSL_CertManagerNew();
    if (!cm) return false;
    bool ok = wolfSSL_CertManagerLoadCABuffer(cm, ca.data, (long)ca.len, WOLFSSL_FILETYPE_ASN1) == WOLFSSL_SUCCESS &&
              wolfSSL_CertManagerVerifyBuffer(cm, crt.data, (long)crt.len, WOLFSSL_FILETYPE_ASN1) == WOLFSSL_SUCCESS;
    wolfSSL_CertManagerFree(cm);
    return ok;
}

// Load a DER cert + DER private key into a TLS 1.3 client context, exactly as
// the firmware will, and ask wolfSSL whether they are a matching pair.
static bool tlsContextAccepts(const Blob& crt, const uint8_t* keyDer, word32 keyLen, String& why) {
    WOLFSSL_CTX* ctx = wolfSSL_CTX_new(wolfTLSv1_3_client_method());
    if (!ctx) { why = "CTX_new"; return false; }
    int r1 = wolfSSL_CTX_use_certificate_buffer(ctx, crt.data, (long)crt.len, WOLFSSL_FILETYPE_ASN1);
    int r2 = wolfSSL_CTX_use_PrivateKey_buffer(ctx, keyDer, (long)keyLen, WOLFSSL_FILETYPE_ASN1);
    int r3 = (r1 == WOLFSSL_SUCCESS && r2 == WOLFSSL_SUCCESS) ? wolfSSL_CTX_check_private_key(ctx) : 0;
    why = String("cert=") + r1 + " key=" + r2 + " pair=" + r3;
    wolfSSL_CTX_free(ctx);
    return r1 == WOLFSSL_SUCCESS && r2 == WOLFSSL_SUCCESS && r3 == WOLFSSL_SUCCESS;
}

// Sign with the private key, verify with the public key taken from the cert.
static bool mldsaSignVerify(dilithium_key* priv, const uint8_t* pubRaw, word32 pubLen, WC_RNG* rng) {
    static const byte msg[] = "nvs identity spike";
    word32 sigLen = (word32)wc_dilithium_sig_size(priv);
    byte* sig = (byte*)malloc(sigLen);
    dilithium_key* pub = newMlDsaKey();
    bool ok = false;
    if (sig && pub) {
        word32 idx = 0;
        int res = 0;
        int ret = wc_dilithium_sign_ctx_msg(NULL, 0, msg, sizeof(msg), sig, &sigLen, priv, rng);
        if (ret == 0) ret = wc_Dilithium_PublicKeyDecode(pubRaw, &idx, pub, pubLen);
        if (ret == 0) ret = wc_dilithium_verify_ctx_msg(sig, sigLen, NULL, 0, msg, sizeof(msg), &res, pub);
        ok = (ret == 0 && res == 1);
    }
    free(sig);
    freeMlDsaKey(pub);
    return ok;
}

static bool samePublicKey(dilithium_key* k, const uint8_t* certPub, int certPubLen) {
    uint8_t pub[ML_DSA_LEVEL2_PUB_KEY_SIZE];
    word32 len = sizeof(pub);
    return wc_dilithium_export_public(k, pub, &len) == 0 &&
           (int)len == certPubLen && memcmp(pub, certPub, len) == 0;
}

static void runSpike() {
    g_pass = g_fail = 0;
    g_report = "";
    Serial.println();
    Serial.println("[Spike] ===== NVS identity spike =====");

    // ── 0.1 partition table ──────────────────────────────────────────────
    const esp_partition_t* part = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_NVS, ID_PARTITION);
    check("0.1 certs partition present", part != nullptr);
    if (part) {
        check("0.1 certs at 0x721000, 64 KB",
              part->address == EXPECT_ADDR && part->size == EXPECT_SIZE,
              String("addr=0x") + String(part->address, HEX) + " size=0x" + String(part->size, HEX));
    }
    const esp_partition_t* run = esp_ota_get_running_partition();
    Serial.printf("[Spike] running from %s @0x%06x\n", run ? run->label : "?", run ? (unsigned)run->address : 0);

    // ── 0.3 NVS read-back ────────────────────────────────────────────────
    bool opened = prefs.begin(ID_NAMESPACE, true, ID_PARTITION);
    check("0.3 open certs/devid read-only", opened);
    if (!opened) return;

    uint8_t fmt = prefs.getUChar("fmt", 0);
    check("0.3 format version", fmt == ID_FORMAT, String("fmt=") + fmt);
    String serial = prefs.getString("serial", "");
    String ssid = prefs.getString("wifi_ssid", "");
    String psk = prefs.getString("wifi_psk", "");
    check("0.3 strings present", serial.length() && ssid.length() && psk.length(),
          "serial=" + serial + " ssid=" + ssid + " psk_len=" + String(psk.length()));

    Blob caCls = readBlob("ca_cls");
    Blob caMl = readBlob("ca_mldsa");
    Blob crtCls = readBlob("crt_cls");
    Blob keyCls = readBlob("key_cls");
    Blob crtMl = readBlob("crt_mldsa");
    Blob seedMl = readBlob("seed_mldsa");
    Blob p8Ml = readBlob("p8_mldsa");
    printEntry("ca_cls", caCls);
    printEntry("ca_mldsa", caMl);
    printEntry("crt_cls", crtCls);
    printEntry("key_cls", keyCls);
    printEntry("crt_mldsa", crtMl);
    printEntry("seed_mldsa", seedMl);
    printEntry("p8_mldsa", p8Ml);
    check("0.3 all blobs present",
          caCls.len && caMl.len && crtCls.len && keyCls.len && crtMl.len && seedMl.len && p8Ml.len);
    check("0.3 ML-DSA cert > 4000 B read back as blob", crtMl.len > 4000, String(crtMl.len) + " B");
    prefs.end();

    WC_RNG rng;
    bool rngOk = wc_InitRng(&rng) == 0;
    check("wolfCrypt RNG", rngOk);

    // ── chains ───────────────────────────────────────────────────────────
    if (caCls.len && crtCls.len) check("chain classical CA -> device cert", chainVerifies(caCls, crtCls));
    if (caMl.len && crtMl.len) check("chain ML-DSA-44 CA -> device cert", chainVerifies(caMl, crtMl));

    // ── classical identity ───────────────────────────────────────────────
    if (crtCls.len && keyCls.len) {
        String why;
        check("classical key+cert load into TLS 1.3 ctx (DER)",
              tlsContextAccepts(crtCls, keyCls.data, (word32)keyCls.len, why), why);
    }

    // ── 0.2 ML-DSA-44 from 32-byte seed (option A) ──────────────────────
    uint8_t certPub[ML_DSA_LEVEL2_PUB_KEY_SIZE + 64];
    int certPubLen = crtMl.len ? certPublicKey(crtMl, certPub, sizeof(certPub)) : -1;
    check("ML-DSA cert public key extracted", certPubLen == ML_DSA_LEVEL2_PUB_KEY_SIZE,
          String("len=") + certPubLen);

    if (seedMl.len == MLDSA_SEED_LEN && certPubLen > 0 && rngOk) {
        dilithium_key* k = newMlDsaKey();
        uint32_t heapBefore = ESP.getFreeHeap();
        uint32_t t0 = micros();
        int ret = k ? wc_dilithium_make_key_from_seed(k, seedMl.data) : -1;
        uint32_t dt = micros() - t0;
        uint32_t heapLow = ESP.getMinFreeHeap();
        check("0.2A make_key_from_seed", ret == 0,
              String("rc=") + ret + " " + String(dt / 1000.0, 1) + " ms, free heap " +
              heapBefore + " -> min " + heapLow);
        if (ret == 0) {
            check("0.2A seed-derived public key == cert public key", samePublicKey(k, certPub, certPubLen));
            check("0.2A sign with derived key, verify with cert key", mldsaSignVerify(k, certPub, certPubLen, &rng));

            word32 cap = ML_DSA_LEVEL2_BOTH_KEY_DER_SIZE + 64;
            uint8_t* der = (uint8_t*)malloc(cap);
            int derLen = der ? wc_Dilithium_KeyToDer(k, der, cap) : -1;
            check("0.2A export derived key to DER", derLen > 0, String("len=") + derLen);
            if (derLen > 0) {
                String why;
                check("0.2A derived key+cert load into TLS 1.3 ctx (DER)",
                      tlsContextAccepts(crtMl, der, (word32)derLen, why), why);
            }
            free(der);
        }
        freeMlDsaKey(k);
    } else {
        check("0.2A inputs", false, "seed or cert missing");
    }

    // ── 0.2 option B: seed-only PKCS#8 decode ───────────────────────────
    if (p8Ml.len && certPubLen > 0) {
        dilithium_key* k = newMlDsaKey();
        word32 idx = 0;
        int ret = k ? wc_Dilithium_PrivateKeyDecode(p8Ml.data, &idx, k, (word32)p8Ml.len) : -1;
        check("0.2B decode seed-only PKCS#8", ret == 0, String("rc=") + ret + " len=" + p8Ml.len);
        if (ret == 0) {
            check("0.2B decoded public key == cert public key", samePublicKey(k, certPub, certPubLen));
        }
        freeMlDsaKey(k);
        String why;
        check("0.2B seed-only PKCS#8 + cert load into TLS 1.3 ctx",
              tlsContextAccepts(crtMl, p8Ml.data, (word32)p8Ml.len, why), why);
    }

    if (rngOk) wc_FreeRng(&rng);
    freeBlob(caCls); freeBlob(caMl); freeBlob(crtCls); freeBlob(keyCls);
    freeBlob(crtMl); freeBlob(seedMl); freeBlob(p8Ml);

    Serial.printf("[Spike] ===== RESULT: %d passed, %d failed =====\n", g_pass, g_fail);
}

void setup() {
    Serial.begin(115200);
    delay(3000);  // give USB-CDC time to enumerate
    wolfSSL_Init();
    runSpike();
}

void loop() {
    delay(15000);
    Serial.println();
    Serial.print(g_report);
    Serial.printf("[Spike] ===== RESULT: %d passed, %d failed =====\n", g_pass, g_fail);
}
