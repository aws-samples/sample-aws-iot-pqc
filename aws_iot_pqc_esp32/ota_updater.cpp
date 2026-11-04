// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

#include "app_config.h"
#include "ota_updater.h"
#include "device_identity.h"
#include "pqc_tls_client.h"
#include "json_util.h"

// wolfcrypt primitives for OTA firmware signature verification (Workstream 3).
#include <wolfssl/wolfcrypt/sha256.h>
#include <wolfssl/wolfcrypt/ecc.h>
#include <wolfssl/wolfcrypt/dilithium.h>
#include <wolfssl/wolfcrypt/asn_public.h>
// asn.h (not just asn_public.h) declares DecodedCert, wc_InitDecodedCert,
// wc_ParseCert, and the NO_VERIFY parse flag used to pull the signer leaf's
// public key out of its cert for the OTA signature verify.
#include <wolfssl/wolfcrypt/asn.h>

TaskHandle_t OtaUpdater::s_task = NULL;

// Amazon Root CA 1 — used to verify the S3 TLS connection during OTA download.
// The device's own CA roots (NVS ca_cls / ca_mldsa) are the workshop Private CAs (for the Mosquitto
// broker); S3 presents a public Amazon-signed cert, so a different root is
// required to verify it. This is Amazon's public root and is safe to embed.
static const char S3_ROOT_CA[] PROGMEM = R"EOF(
-----BEGIN CERTIFICATE-----
MIIDQTCCAimgAwIBAgITBmyfz5m/jAo54vB4ikPmljZbyjANBgkqhkiG9w0BAQsF
ADA5MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6
b24gUm9vdCBDQSAxMB4XDTE1MDUyNjAwMDAwMFoXDTM4MDExNzAwMDAwMFowOTEL
MAkGA1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJv
b3QgQ0EgMTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBALJ4gHHKeNXj
ca9HgFB0fW7Y14h29Jlo91ghYPl0hAEvrAIthtOgQ3pOsqTQNroBvo3bSMgHFzZM
9O6II8c+6zf1tRn4SWiw3te5djgdYZ6k/oI2peVKVuRF4fn9tBb6dNqcmzU5L/qw
IFAGbHrQgLKm+a/sRxmPUDgH3KKHOVj4utWp+UhnMJbulHheb4mjUcAwhmahRWa6
VOujw5H5SNz/0egwLX0tdHA114gk957EWW67c4cX8jJGKLhD+rcdqsq08p8kDi1L
93FcXmn/6pUCyziKrlA4b9v7LWIbxcceVOF34GfID5yHI9Y/QCB/IIDEgEw+OyQm
jgSubJrIqg0CAwEAAaNCMEAwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMC
AYYwHQYDVR0OBBYEFIQYzIU07LwMlJQuCFmcx7IQTgoIMA0GCSqGSIb3DQEBCwUA
A4IBAQCY8jdaQZChGsV2USggNiMOruYou6r4lK5IpDB/G/wkjUu0yKGX9rbxenDI
U5PMCCjjmCXPI6T53iHTfIUJrU6adTrCC2qJeHZERxhlbI1Bjjt/msv0tadQ1wU
sN+gDS63pYaACbvXy8MWy7Vu33PqUXHeeE6V/Uq2V8viTO96LXFvKWlJbYK8U90
vvo/ufQJVtMVT8QtPHRh8jrdkPSHCa2XV4cdFyQzR1bldZwgJcJmApzyMZFo6IQ
6XU5MsI+yMRQ+hDKXJioaldXgjUkK642M4UwtBV8ob2xJNDd2ZhwLnoQdeXeGAD
Xn8YS87CVUvpuvL/f5D+CSt+q2FT4Fj4CzHwmTgn
-----END CERTIFICATE-----
)EOF";


// OTA state (shared between the MQTT handler and the OTA task). Set by
// handleCommand() when an "ota-update" command arrives, then consumed by the
// task, which downloads the firmware from the presigned S3 URL.
static volatile bool g_otaPending = false;
static char* g_otaUrl = NULL;            // OTA_URL_MAX bytes, allocated in begin() (PSRAM)
static char g_otaVersion[32] = {0};
static int  g_otaSize = 0;
static SemaphoreHandle_t g_otaSemaphore = NULL;

// OTA signature material (Workstream 3). Populated from the cmd/<thing>/ota
// payload's sigUrl/sigAlg/signerCertUrl when the push is SIGNED; left empty for
// an unsigned push (classical 1.0.0 / legacy command). The task fetches the
// detached signature + signer leaf from these presigned URLs and verifies the
// freshly-written image before switching the boot partition.
static char* g_otaSigUrl = NULL;         // presigned firmware.bin.sig (OTA_URL_MAX, PSRAM)
static char* g_otaSignerCertUrl = NULL;  // presigned signer.crt leaf (OTA_URL_MAX, PSRAM)
static char g_otaSigAlg[16] = {0};       // "ecdsa-p256" | "mldsa44" | ""
static char g_otaSha256[65] = {0};       // expected image sha256 (hex), enforced before boot

// Allocate one zeroed OTA_URL_MAX buffer, preferring PSRAM.
static char* allocOtaBuffer() {
    char* p = (char*)heap_caps_calloc(1, OTA_URL_MAX, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!p) p = (char*)calloc(1, OTA_URL_MAX);
    return p;
}

bool OtaUpdater::begin(int core) {
    // URL buffers live in PSRAM (internal SRAM is the scarce pool; only the
    // MQTT handler and the OTA task touch them). Falls back to internal RAM on
    // a board without PSRAM. Allocated once, before the task starts.
    g_otaUrl           = allocOtaBuffer();
    g_otaSigUrl        = allocOtaBuffer();
    g_otaSignerCertUrl = allocOtaBuffer();
    if (!g_otaUrl || !g_otaSigUrl || !g_otaSignerCertUrl) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to allocate OTA URL buffers");
        return false;
    }
    // The task waits on this semaphore for ota-update commands.
    g_otaSemaphore = xSemaphoreCreateBinary();
    if (g_otaSemaphore == NULL) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create OTA semaphore");
        return false;
    }
    BaseType_t rc = xTaskCreatePinnedToCore(task, "OTATask", OTA_TASK_STACK_SIZE,
                                            NULL, OTA_TASK_PRIORITY, &s_task, core);
    if (rc != pdPASS) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create OTA task (rc=%d)", rc);
        return false;
    }
    ESP_LOGD(TAG_MAIN, "OTA task created (stack=%d, priority=%d, core=%d)",
             OTA_TASK_STACK_SIZE, OTA_TASK_PRIORITY, core);
    return true;
}

// OTA download: {"action":"ota-update","url":"...","version":"x.y.z","size":N,
// "sha256":"...", "sigAlg":"...", "sigUrl":"...", "signerCertUrl":"..."}.
// Downloads firmware from a presigned S3 URL and writes it to the next OTA
// partition (see task()).
void OtaUpdater::handleCommand(const String& payload) {
    ESP_LOGI(TAG_MQTT, "OTA topic detected, checking payload...");

    if (payload.indexOf("ota-update") >= 0) {
        String url = jsonGetString(payload, "url");
        if (url.length() > 0) {
            url.toCharArray(g_otaUrl, OTA_URL_MAX);

            String ver = jsonGetString(payload, "version");
            if (ver.length() > 0) {
                ver.toCharArray(g_otaVersion, sizeof(g_otaVersion));
            } else {
                strcpy(g_otaVersion, "unknown");
            }

            g_otaSize = (int)jsonGetNumber(payload, "size");

            // Expected image hash (Workstream 3). The cloud always sends
            // sha256; the task enforces it before switching the boot slot.
            String sha = jsonGetString(payload, "sha256");
            sha.toCharArray(g_otaSha256, sizeof(g_otaSha256));

            // Signature material (Workstream 3). Present only for SIGNED
            // variants; absent for an unsigned push (classical 1.0.0 /
            // legacy command). Parsed tolerantly — empty strings mean
            // "unsigned" and the task handles that per its verify policy.
            String sigAlg = jsonGetString(payload, "sigAlg");
            sigAlg.toCharArray(g_otaSigAlg, sizeof(g_otaSigAlg));
            String sigUrl = jsonGetString(payload, "sigUrl");
            sigUrl.toCharArray(g_otaSigUrl, OTA_URL_MAX);
            String signerCertUrl = jsonGetString(payload, "signerCertUrl");
            signerCertUrl.toCharArray(g_otaSignerCertUrl, OTA_URL_MAX);

            g_otaPending = true;
            if (g_otaSemaphore) xSemaphoreGive(g_otaSemaphore);
            ESP_LOGI(TAG_MQTT, "OTA download queued: v%s, %d bytes, sigAlg=%s",
                     g_otaVersion, g_otaSize,
                     g_otaSigAlg[0] ? g_otaSigAlg : "<unsigned>");
        } else {
            ESP_LOGE(TAG_MQTT, "ota-update command missing/invalid url");
        }
        return;
    }

    // The old partition-swap commands (migrate-classical / migrate-pqc /
    // migrate-mldsa) picked a slot by the image's project name, which was the
    // sketch name. One sketch now builds every variant, so every image has the
    // same project name and a swap could boot the wrong variant. Migrations
    // (and rollbacks) are OTA downloads of the shared image for the target
    // variant, started from the dashboard.
    if (payload.indexOf("migrate-") >= 0) {
        ESP_LOGW(TAG_MQTT, "Partition-swap command not supported; migrate from the dashboard (OTA): '%s'",
                 payload.c_str());
        return;
    }
    ESP_LOGW(TAG_MQTT, "OTA topic but unrecognized payload: '%s'", payload.c_str());
}

// ═══════════════════════════════════════════════════════════════════════════
//  OTA Firmware Signature Verification (Workstream 3)
// ═══════════════════════════════════════════════════════════════════════════
//
// The security spine of the OTA path: every field-delivered image is signed in
// the cloud build by a STABLE per-era signer and verified HERE before the device
// switches its boot partition. USB flash on the builder-Pi is trusted/unsigned;
// OTA is not. Trust model:
//   - The device holds NO signing key — it only VERIFIES, so wolfSSL crypto
//     primitives (not a TPM) are sufficient.
//   - The signer LEAF cert travels WITH each OTA (signerCertUrl). The device
//     requires it to chain to the firmware-signing root for the image's
//     algorithm (sig_cls for ecdsa-p256, sig_mldsa for mldsa44, from the
//     "certs" NVS partition) and to carry the codeSigning EKU, then verifies
//     the image signature with the leaf's public key. The device CA roots
//     (ca_cls / ca_mldsa) are never trusted here: they issue every device's
//     certificate, so trusting them would let any device key sign firmware.
//   - classical era (ecdsa-p256): ECDSA-P256 over the image's SHA-256 digest,
//     DER-encoded signature (~71 B). ML-DSA era (mldsa44): ML-DSA-44 over the
//     RAW image (pure scheme, hashes internally), fixed 2420 B signature.
//
// The firmware image is never fully in RAM (streamed to flash). For ECDSA we
// hash incrementally during the download; for ML-DSA we memory-map the written
// partition read-only to get a contiguous pointer for the one-shot verify.

// Max sizes for the small artifacts fetched into RAM. ML-DSA-44 signature is a
// fixed 2420 B; a signer leaf PEM is ~5.7 KB. Generous caps with headroom.
#define OTA_SIG_BUF_MAX        2600
#define OTA_SIGNER_CERT_MAX    8192

// Fetch a small object from a presigned S3 URL into a caller buffer over TLS.
// Mirrors otaTask's S3 download (fresh context, verify disabled — integrity for
// these artifacts comes from the signature chain they feed, not the transport),
// but reads the whole body into `out` instead of streaming to flash. Returns
// true and sets *outLen on success. Used for the detached signature and the
// signer leaf cert; NOT for the firmware image (that is streamed).
static bool otaHttpsGetToBuffer(const char* urlCstr, uint8_t* out, size_t cap, size_t* outLen) {
    if (!urlCstr || !urlCstr[0]) return false;
    String url(urlCstr), host, path;
    uint16_t port = 443;
    if (url.startsWith("https://")) url = url.substring(8);
    int pathStart = url.indexOf('/');
    if (pathStart <= 0) { ESP_LOGE(TAG_OTA, "sig GET: bad URL"); return false; }
    host = url.substring(0, pathStart);
    path = url.substring(pathStart);
    int colonIdx = host.indexOf(':');
    if (colonIdx > 0) { port = host.substring(colonIdx + 1).toInt(); host = host.substring(0, colonIdx); }

    WiFiClient tcp;
    if (!tcp.connect(host.c_str(), port)) { ESP_LOGE(TAG_OTA, "sig GET: TCP connect failed"); return false; }

    WOLFSSL_CTX* ctx = wolfSSL_CTX_new(wolfTLSv1_3_client_method());
    if (!ctx) { tcp.stop(); return false; }
    wolfSSL_CTX_load_verify_buffer(ctx, (const unsigned char*)S3_ROOT_CA, strlen(S3_ROOT_CA), WOLFSSL_FILETYPE_PEM);
    wolfSSL_CTX_set_verify(ctx, WOLFSSL_VERIFY_NONE, NULL);
    wolfSSL_CTX_SetIORecv(ctx, wolfssl_recv_cb);
    wolfSSL_CTX_SetIOSend(ctx, wolfssl_send_cb);

    wolfssl_io_ctx_t io = { .client = &tcp };
    WOLFSSL* ssl = wolfSSL_new(ctx);
    if (!ssl) { wolfSSL_CTX_free(ctx); tcp.stop(); return false; }
    wolfSSL_SetIOReadCtx(ssl, &io);
    wolfSSL_SetIOWriteCtx(ssl, &io);
    #ifdef HAVE_SNI
    wolfSSL_UseSNI(ssl, WOLFSSL_SNI_HOST_NAME, host.c_str(), host.length());
    #endif

    unsigned long hsStart = millis();
    int ret;
    do {
        ret = wolfSSL_connect(ssl);
        if (ret == WOLFSSL_SUCCESS) break;
        int e = wolfSSL_get_error(ssl, ret);
        if (e == WOLFSSL_ERROR_WANT_READ || e == WOLFSSL_ERROR_WANT_WRITE) { vTaskDelay(pdMS_TO_TICKS(10)); continue; }
        ESP_LOGE(TAG_OTA, "sig GET: TLS handshake failed (err=%d)", e);
        wolfSSL_free(ssl); wolfSSL_CTX_free(ctx); tcp.stop(); return false;
    } while ((millis() - hsStart) < 15000);
    if (ret != WOLFSSL_SUCCESS) { wolfSSL_free(ssl); wolfSSL_CTX_free(ctx); tcp.stop(); return false; }

    String req = "GET " + path + " HTTP/1.1\r\nHost: " + host + "\r\nConnection: close\r\n\r\n";
    wolfSSL_write(ssl, req.c_str(), req.length());

    // Parse headers for Content-Length (1 byte at a time, WANT_READ tolerant).
    String line, status;
    int contentLength = -1;
    bool headersDone = false;
    unsigned long hdrStart = millis();
    while (!headersDone && (millis() - hdrStart) < 15000) {
        char c; int r = wolfSSL_read(ssl, &c, 1);
        if (r <= 0) {
            int e = wolfSSL_get_error(ssl, r);
            if (e == WOLFSSL_ERROR_WANT_READ || e == WOLFSSL_ERROR_WANT_WRITE) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }
            break;
        }
        line += c;
        if (line.endsWith("\r\n")) {
            String t = line; t.trim();
            if (t.length() == 0) { headersDone = true; break; }
            if (status.length() == 0) status = t;
            String lower = line; lower.toLowerCase();
            if (lower.startsWith("content-length:")) contentLength = line.substring(line.indexOf(':') + 1).toInt();
            line = "";
        }
    }
    if (!headersDone || contentLength <= 0 || (size_t)contentLength > cap) {
        ESP_LOGE(TAG_OTA, "sig GET: bad length %d (cap %u, status '%s')", contentLength, (unsigned)cap, status.c_str());
        wolfSSL_free(ssl); wolfSSL_CTX_free(ctx); tcp.stop(); return false;
    }

    // Read exactly contentLength body bytes into out.
    size_t got = 0;
    unsigned long bodyStart = millis();
    while (got < (size_t)contentLength && (millis() - bodyStart) < 20000) {
        int r = wolfSSL_read(ssl, out + got, (int)((size_t)contentLength - got));
        if (r > 0) { got += r; continue; }
        int e = wolfSSL_get_error(ssl, r);
        if (e == WOLFSSL_ERROR_WANT_READ || e == WOLFSSL_ERROR_WANT_WRITE) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }
        break;
    }
    wolfSSL_shutdown(ssl);
    wolfSSL_free(ssl);
    wolfSSL_CTX_free(ctx);
    tcp.stop();

    if (got != (size_t)contentLength) { ESP_LOGE(TAG_OTA, "sig GET: short read %u/%d", (unsigned)got, contentLength); return false; }
    *outLen = got;
    return true;
}

// True if the leaf certificate (PEM) carries the codeSigning extended key usage.
// The OTA signers are issued with ACM PCA's CodeSigningCertificate template;
// device and broker certificates are not, so this rejects a TLS certificate
// presented as a signer even if it were to chain to a signing root.
static bool otaLeafHasCodeSigningEku(const uint8_t* leafPem, size_t leafLen) {
    uint8_t* der = (uint8_t*)malloc(leafLen);  // DER is always smaller than its PEM
    if (!der) return false;
    int derLen = wc_CertPemToDer(leafPem, (int)leafLen, der, (int)leafLen, CERT_TYPE);
    bool ok = false;
    if (derLen > 0) {
        DecodedCert cert;
        wc_InitDecodedCert(&cert, der, (word32)derLen, NULL);
        if (wc_ParseCert(&cert, CERT_TYPE, NO_VERIFY, NULL) == 0) {
            ok = cert.extExtKeyUsageSet && (cert.extExtKeyUsage & EXTKEYUSE_CODESIGN);
        }
        wc_FreeDecodedCert(&cert);
    }
    free(der);
    return ok;
}

// Validate the signer leaf PEM against the ONE firmware-signing root that
// matches the image's signature algorithm (sig_cls for ecdsa-p256, sig_mldsa
// for mldsa44), and require the codeSigning EKU. Uses a throwaway wolfSSL cert
// manager so the trust decision is independent of the TLS session. Fails closed
// if the root is missing (a legacy format-1 identity has none).
static bool otaVerifyChainToRoot(const char* sigAlg, const uint8_t* leafPem, size_t leafLen) {
    const IdBlob* root = nullptr;
    const char* rootName = "";
    if (strcmp(sigAlg, "ecdsa-p256") == 0) {
        root = &DeviceIdentity::signerRootClassical();
        rootName = "sig_cls";
    } else if (strcmp(sigAlg, "mldsa44") == 0) {
        root = &DeviceIdentity::signerRootMlDsa();
        rootName = "sig_mldsa";
    } else {
        ESP_LOGE(TAG_OTA, "Unknown sigAlg '%s'; refusing", sigAlg);
        return false;
    }
    if (!root->len) {
        ESP_LOGE(TAG_OTA, "No firmware-signing root %s in this identity; refusing "
                 "(fail-closed). Reflash at the Builder-Pi.", rootName);
        return false;
    }

    WOLFSSL_CERT_MANAGER* cm = wolfSSL_CertManagerNew();
    if (!cm) { ESP_LOGE(TAG_OTA, "CertManagerNew failed"); return false; }
    int rc = wolfSSL_CertManagerLoadCABuffer(cm, root->data, (long)root->len,
                                             WOLFSSL_FILETYPE_ASN1);
    if (rc != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_OTA, "Load firmware-signing root %s failed (rc=%d); refusing", rootName, rc);
        wolfSSL_CertManagerFree(cm);
        return false;
    }
    int ret = wolfSSL_CertManagerVerifyBuffer(cm, leafPem, (long)leafLen, WOLFSSL_FILETYPE_PEM);
    wolfSSL_CertManagerFree(cm);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_OTA, "Signer leaf does NOT chain to firmware-signing root %s (rc=%d)",
                 rootName, ret);
        return false;
    }
    if (!otaLeafHasCodeSigningEku(leafPem, leafLen)) {
        ESP_LOGE(TAG_OTA, "Signer leaf has no codeSigning extended key usage; refusing");
        return false;
    }
    ESP_LOGI(TAG_OTA, "Signer leaf verified against firmware-signing root %s (codeSigning)",
             rootName);
    return true;
}

// Parse the signer leaf PEM and copy out its DER-encoded SubjectPublicKeyInfo
// into `pubDer` (caller buffer, cap pubCap). Returns the DER length, or <0 on
// error. The resulting SPKI DER feeds wc_EccPublicKeyDecode /
// wc_Dilithium_PublicKeyDecode for the signature verify. Uses the documented
// public path: PEM->DER (wc_CertPemToDer) -> DecodedCert (wc_InitDecodedCert /
// wc_ParseCert) -> wc_GetPubKeyDerFromCert.
static int otaSignerPublicKeyDer(const uint8_t* leafPem, size_t leafLen, uint8_t* pubDer, size_t pubCap) {
    // 1. Convert the leaf PEM to DER into a scratch buffer. A signer leaf DER is
    //    well under MAX_CERTIFICATE_SZ (16 KB); ML-DSA leaf DER is ~5 KB.
    uint8_t* derBuf = (uint8_t*)malloc(leafLen);  // DER is always smaller than its PEM
    if (!derBuf) { ESP_LOGE(TAG_OTA, "signer der alloc failed"); return -1; }
    int derLen = wc_CertPemToDer(leafPem, (int)leafLen, derBuf, (int)leafLen, CERT_TYPE);
    if (derLen <= 0) {
        ESP_LOGE(TAG_OTA, "wc_CertPemToDer failed (%d)", derLen);
        free(derBuf);
        return -1;
    }

    // 2. Parse the DER cert and extract its public key in DER (SPKI).
    DecodedCert cert;
    wc_InitDecodedCert(&cert, derBuf, (word32)derLen, NULL);
    int ret = wc_ParseCert(&cert, CERT_TYPE, NO_VERIFY, NULL);
    if (ret != 0) {
        ESP_LOGE(TAG_OTA, "wc_ParseCert failed (%d)", ret);
        wc_FreeDecodedCert(&cert);
        free(derBuf);
        return -1;
    }
    word32 pubLen = (word32)pubCap;
    ret = wc_GetPubKeyDerFromCert(&cert, pubDer, &pubLen);
    wc_FreeDecodedCert(&cert);
    free(derBuf);
    if (ret != 0) {
        ESP_LOGE(TAG_OTA, "wc_GetPubKeyDerFromCert failed (%d)", ret);
        return -1;
    }
    return (int)pubLen;
}

// Verify an ECDSA-P256 signature (DER SEQUENCE{r,s}) over `digest` (32-byte
// SHA-256) using the signer leaf's public key. Returns true iff valid.
static bool otaVerifyEcdsaP256(const uint8_t* sig, size_t sigLen,
                               const uint8_t* digest,
                               const uint8_t* leafPem, size_t leafLen) {
    uint8_t pubDer[128];
    int pubLen = otaSignerPublicKeyDer(leafPem, leafLen, pubDer, sizeof(pubDer));
    if (pubLen < 0) return false;

    ecc_key key;
    if (wc_ecc_init(&key) != 0) return false;
    word32 idx = 0;
    bool ok = false;
    if (wc_EccPublicKeyDecode(pubDer, &idx, &key, (word32)pubLen) == 0) {
        int verifyStatus = 0;
        int ret = wc_ecc_verify_hash(sig, (word32)sigLen, digest, 32, &verifyStatus, &key);
        ok = (ret == 0 && verifyStatus == 1);
        if (!ok) ESP_LOGE(TAG_OTA, "ECDSA verify failed (ret=%d, status=%d)", ret, verifyStatus);
    } else {
        ESP_LOGE(TAG_OTA, "ECDSA pubkey decode failed");
    }
    wc_ecc_free(&key);
    return ok;
}

// Verify an ML-DSA-44 signature over the FULL image using the signer leaf's
// public key. ML-DSA hashes internally, so it needs the whole message in one
// call — we memory-map the written OTA partition read-only (zero-copy) to get a
// contiguous pointer rather than buffering 3.5 MB in RAM. The dilithium_key is
// multi-KB, so it is PSRAM-allocated. Returns true iff valid.
static bool otaVerifyMlDsa44(const uint8_t* sig, size_t sigLen,
                             const esp_partition_t* part, size_t imageLen,
                             const uint8_t* leafPem, size_t leafLen) {
    uint8_t pubDer[2048];  // ML-DSA-44 SubjectPublicKeyInfo DER ~1334 B
    int pubLen = otaSignerPublicKeyDer(leafPem, leafLen, pubDer, sizeof(pubDer));
    if (pubLen < 0) return false;

    // Map the whole image contiguously (read-only) from flash.
    const void* mapPtr = NULL;
    esp_partition_mmap_handle_t mapHandle;
    esp_err_t merr = esp_partition_mmap(part, 0, imageLen, ESP_PARTITION_MMAP_DATA, &mapPtr, &mapHandle);
    if (merr != ESP_OK || !mapPtr) {
        ESP_LOGE(TAG_OTA, "esp_partition_mmap failed: %s", esp_err_to_name(merr));
        return false;
    }

    // dilithium_key is large; keep it off the task stack (PSRAM preferred).
    dilithium_key* key = (dilithium_key*)heap_caps_malloc(sizeof(dilithium_key), MALLOC_CAP_SPIRAM);
    if (!key) key = (dilithium_key*)malloc(sizeof(dilithium_key));
    if (!key) { ESP_LOGE(TAG_OTA, "dilithium_key alloc failed"); esp_partition_munmap(mapHandle); return false; }

    bool ok = false;
    if (wc_dilithium_init(key) == 0) {
        word32 idx = 0;
        // wc_GetPubKeyDerFromCert returns the RAW ML-DSA-44 public key (1312 B,
        // leading bytes are key material, NOT a 0x30 SEQUENCE), not a full
        // SubjectPublicKeyInfo — so there is no algorithm OID for the decoder to
        // read the parameter set from. We MUST set the level explicitly before
        // decode or wc_Dilithium_PublicKeyDecode fails. Proven on hardware
        // (pqc-a, C69E18, 4.0.0-signer): with the level set, an OpenSSL-3.5
        // ML-DSA-44 signature verifies on the wolfSSL device. WC_ML_DSA_44 is the
        // FIPS 204 Level-2 parameter set this workshop uses. The pubDer len/head
        // log stays as a cheap, useful breadcrumb for any future key-format drift.
        ESP_LOGI(TAG_OTA, "ML-DSA pubDer len=%d head=%02x %02x %02x %02x",
                 pubLen, pubDer[0], pubDer[1], pubDer[2], pubDer[3]);
        int lvlRet = wc_dilithium_set_level(key, WC_ML_DSA_44);
        if (lvlRet != 0) ESP_LOGW(TAG_OTA, "wc_dilithium_set_level(44) rc=%d (continuing)", lvlRet);
        int decRet = wc_Dilithium_PublicKeyDecode(pubDer, &idx, key, (word32)pubLen);
        if (decRet != 0) {
            ESP_LOGE(TAG_OTA, "ML-DSA pubkey decode failed (ret=%d, idx=%u)", decRet, (unsigned)idx);
        }
        if (decRet == 0) {
            // FIPS 204 pure ML-DSA with an EMPTY context (what OpenSSL 3.5
            // `pkeyutl -sign -rawin` produces). Use the explicit-context verify
            // with ctx=NULL/ctxLen=0 so this is correct regardless of whether
            // the build defines WOLFSSL_MLDSA_NO_CTX (the plain _verify_msg
            // macro only exists in the NO_CTX build).
            int verifyStatus = 0;
            int ret = wc_dilithium_verify_ctx_msg(sig, (word32)sigLen,
                                                  NULL, 0,
                                                  (const byte*)mapPtr, (word32)imageLen,
                                                  &verifyStatus, key);
            ok = (ret == 0 && verifyStatus == 1);
            if (!ok) ESP_LOGE(TAG_OTA, "ML-DSA verify failed (ret=%d, status=%d)", ret, verifyStatus);
        }
        wc_dilithium_free(key);
    }
    free(key);
    esp_partition_munmap(mapHandle);
    return ok;
}

// Top-level OTA verify gate. Given the signer material already fetched into RAM
// and the written image (partition + size + its SHA-256 digest), run the full
// policy: (1) chain the signer leaf to the firmware-signing root for sigAlg and
// require codeSigning, (2) verify the image signature with the correct
// algorithm. Returns true ONLY if BOTH pass.
static bool otaVerifyImage(const char* sigAlg,
                           const uint8_t* sig, size_t sigLen,
                           const uint8_t* leafPem, size_t leafLen,
                           const esp_partition_t* part, size_t imageLen,
                           const uint8_t* sha256Digest) {
    if (!otaVerifyChainToRoot(sigAlg, leafPem, leafLen)) return false;

    if (strcmp(sigAlg, "ecdsa-p256") == 0) {
        return otaVerifyEcdsaP256(sig, sigLen, sha256Digest, leafPem, leafLen);
    }
    if (strcmp(sigAlg, "mldsa44") == 0) {
        return otaVerifyMlDsa44(sig, sigLen, part, imageLen, leafPem, leafLen);
    }
    ESP_LOGE(TAG_OTA, "Unknown sigAlg '%s'; refusing", sigAlg);
    return false;
}

// ═══════════════════════════════════════════════════════════════════════════
//  OTA Download Task (wolfSSL HTTPS client)
// ═══════════════════════════════════════════════════════════════════════════

// Waits for an ota-update command, downloads the firmware from the presigned
// S3 URL over TLS, streams it into the next OTA partition, then reboots into
// the new firmware. The S3 connection uses a fresh wolfSSL context WITHOUT the
// device client cert (presigned URLs authenticate via query string, not mTLS).
void OtaUpdater::task(void* pvParameters) {
    ESP_LOGI(TAG_OTA, "OTA task started, waiting for update command...");

    for (;;) {
        if (xSemaphoreTake(g_otaSemaphore, pdMS_TO_TICKS(5000)) != pdTRUE) continue;
        if (!g_otaPending) continue;

        ESP_LOGI(TAG_OTA, "OTA update initiated: v%s (%d bytes)", g_otaVersion, g_otaSize);
        ESP_LOGI(TAG_OTA, "URL: %.80s...", g_otaUrl);

        // Parse URL: extract host, port, path
        String url(g_otaUrl);
        String host, path;
        uint16_t port = 443;

        if (url.startsWith("https://")) url = url.substring(8);
        int pathStart = url.indexOf('/');
        if (pathStart > 0) {
            host = url.substring(0, pathStart);
            path = url.substring(pathStart);
        } else {
            ESP_LOGE(TAG_OTA, "Invalid URL format");
            g_otaPending = false;
            continue;
        }

        int colonIdx = host.indexOf(':');
        if (colonIdx > 0) {
            port = host.substring(colonIdx + 1).toInt();
            host = host.substring(0, colonIdx);
        }

        ESP_LOGI(TAG_OTA, "Connecting to %s:%d", host.c_str(), port);

        WiFiClient tcpClient;
        if (!tcpClient.connect(host.c_str(), port)) {
            ESP_LOGE(TAG_OTA, "TCP connection to S3 failed");
            g_otaPending = false;
            continue;
        }

        // Fresh TLS context for S3 (no client certs — presigned URL auth)
        WOLFSSL_CTX* otaCtx = wolfSSL_CTX_new(wolfTLSv1_3_client_method());
        if (!otaCtx) {
            ESP_LOGE(TAG_OTA, "Failed to create OTA TLS context");
            tcpClient.stop();
            g_otaPending = false;
            continue;
        }

        // S3 TLS verification. We embed Amazon Root CA 1, but this constrained
        // wolfSSL build does not fully validate S3's intermediate chain
        // (leaf -> Amazon RSA 2048 M04 -> Amazon Root CA 1), failing with
        // ASN_NO_SIGNER_E (-188). For the OTA download we disable peer
        // verification: download integrity is still guaranteed by the SHA256 in
        // the OTA command and the unguessable, short-lived presigned URL. The
        // MQTT broker connection keeps full mTLS verification.
        wolfSSL_CTX_load_verify_buffer(otaCtx,
            (const unsigned char*)S3_ROOT_CA, strlen(S3_ROOT_CA), WOLFSSL_FILETYPE_PEM);
        wolfSSL_CTX_set_verify(otaCtx, WOLFSSL_VERIFY_NONE, NULL);

        wolfssl_io_ctx_t otaIo = { .client = &tcpClient };
        wolfSSL_CTX_SetIORecv(otaCtx, wolfssl_recv_cb);
        wolfSSL_CTX_SetIOSend(otaCtx, wolfssl_send_cb);

        WOLFSSL* otaSsl = wolfSSL_new(otaCtx);
        if (!otaSsl) {
            ESP_LOGE(TAG_OTA, "Failed to create OTA SSL session");
            wolfSSL_CTX_free(otaCtx);
            tcpClient.stop();
            g_otaPending = false;
            continue;
        }

        wolfSSL_SSLSetIORecv(otaSsl, wolfssl_recv_cb);
        wolfSSL_SSLSetIOSend(otaSsl, wolfssl_send_cb);
        wolfSSL_SetIOReadCtx(otaSsl, &otaIo);
        wolfSSL_SetIOWriteCtx(otaSsl, &otaIo);
        #ifdef HAVE_SNI
        wolfSSL_UseSNI(otaSsl, WOLFSSL_SNI_HOST_NAME, host.c_str(), host.length());
        #endif

        // TLS handshake
        ESP_LOGI(TAG_OTA, "TLS handshake with S3...");
        unsigned long hsStart = millis();
        int ret;
        do {
            ret = wolfSSL_connect(otaSsl);
            if (ret == WOLFSSL_SUCCESS) break;
            int err = wolfSSL_get_error(otaSsl, ret);
            if (err == WOLFSSL_ERROR_WANT_READ || err == WOLFSSL_ERROR_WANT_WRITE) {
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }
            ESP_LOGE(TAG_OTA, "S3 TLS handshake failed (err=%d)", err);
            break;
        } while ((millis() - hsStart) < 15000);

        if (ret != WOLFSSL_SUCCESS) {
            wolfSSL_free(otaSsl);
            wolfSSL_CTX_free(otaCtx);
            tcpClient.stop();
            g_otaPending = false;
            continue;
        }
        ESP_LOGI(TAG_OTA, "S3 TLS connected, sending HTTP GET...");

        String httpReq = "GET " + path + " HTTP/1.1\r\nHost: " + host + "\r\nConnection: close\r\n\r\n";
        wolfSSL_write(otaSsl, httpReq.c_str(), httpReq.length());

        // Read HTTP response headers to get content length
        // Read HTTP response headers. wolfSSL_read frequently returns
        // WANT_READ between TLS records, so we retry with a bounded timeout
        // rather than breaking on the first non-data read. Header matching is
        // case-insensitive and tolerant of the space after the colon.
        String headerLine;
        String statusLine;
        int contentLength = 0;
        bool headersComplete = false;
        unsigned long hdrStart = millis();

        while (!headersComplete && (millis() - hdrStart) < 15000) {
            char c;
            int r = wolfSSL_read(otaSsl, &c, 1);
            if (r <= 0) {
                int e = wolfSSL_get_error(otaSsl, r);
                if (e == WOLFSSL_ERROR_WANT_READ || e == WOLFSSL_ERROR_WANT_WRITE) {
                    vTaskDelay(pdMS_TO_TICKS(5));
                    continue;
                }
                // Genuine read error/close before headers finished.
                ESP_LOGE(TAG_OTA, "Header read error (r=%d, err=%d)", r, e);
                break;
            }
            headerLine += c;
            if (headerLine.endsWith("\r\n")) {
                String trimmed = headerLine;
                trimmed.trim();
                if (trimmed.length() == 0) { headersComplete = true; break; }
                if (statusLine.length() == 0) {
                    statusLine = trimmed;  // first line is the HTTP status line
                    ESP_LOGI(TAG_OTA, "S3 response: %s", statusLine.c_str());
                }
                String lower = headerLine;
                lower.toLowerCase();
                if (lower.startsWith("content-length:")) {
                    int colon = headerLine.indexOf(':');
                    contentLength = headerLine.substring(colon + 1).toInt();
                }
                headerLine = "";
            }
        }

        if (contentLength <= 0) {
            ESP_LOGE(TAG_OTA, "Invalid content length: %d (status: %s)",
                     contentLength, statusLine.c_str());
            wolfSSL_free(otaSsl); wolfSSL_CTX_free(otaCtx); tcpClient.stop();
            g_otaPending = false;
            continue;
        }

        ESP_LOGI(TAG_OTA, "Firmware size: %d bytes, starting OTA write...", contentLength);

        const esp_partition_t* otaPartition = esp_ota_get_next_update_partition(NULL);
        if (!otaPartition) {
            ESP_LOGE(TAG_OTA, "No OTA partition available");
            wolfSSL_free(otaSsl); wolfSSL_CTX_free(otaCtx); tcpClient.stop();
            g_otaPending = false;
            continue;
        }

        esp_ota_handle_t otaHandle;
        esp_err_t err = esp_ota_begin(otaPartition, contentLength, &otaHandle);
        if (err != ESP_OK) {
            ESP_LOGE(TAG_OTA, "esp_ota_begin failed: %s", esp_err_to_name(err));
            wolfSSL_free(otaSsl); wolfSSL_CTX_free(otaCtx); tcpClient.stop();
            g_otaPending = false;
            continue;
        }

        // Download firmware in chunks and write to OTA partition
        uint8_t buf[4096];
        int totalRead = 0;
        int lastProgress = 0;
        bool downloadOk = true;

        // Incremental SHA-256 of the image AS IT STREAMS to flash (Workstream 3).
        // The image is never fully in RAM, so we hash each chunk here; the digest
        // is used both to enforce the transmitted sha256 (integrity) and as the
        // input to the ECDSA signature verify (authenticity). Cheap and runs in
        // lockstep with esp_ota_write.
        wc_Sha256 otaSha;
        bool shaOk = (wc_InitSha256(&otaSha) == 0);

        // Bounded WANT_READ retry: near EOF the last TLS record can be briefly
        // in flight (socket momentarily empty), so a WANT_READ is normal and we
        // wait for it rather than aborting. Cap the wait so a genuinely stalled
        // transfer still fails instead of hanging forever: 500 * 10ms = ~5s of
        // no progress. The counter resets on every successful read.
        const int OTA_MAX_WANT_READ_RETRIES = 500;
        int wantReadRetries = 0;

        while (totalRead < contentLength) {
            int toRead = min((int)sizeof(buf), contentLength - totalRead);
            int bytesRead = wolfSSL_read(otaSsl, buf, toRead);

            if (bytesRead <= 0) {
                int sslErr = wolfSSL_get_error(otaSsl, bytesRead);
                if (sslErr == WOLFSSL_ERROR_WANT_READ || sslErr == WOLFSSL_ERROR_WANT_WRITE) {
                    if (++wantReadRetries > OTA_MAX_WANT_READ_RETRIES) {
                        ESP_LOGE(TAG_OTA, "Download stalled at %d/%d bytes (WANT_READ timeout)", totalRead, contentLength);
                        downloadOk = false;
                        break;
                    }
                    vTaskDelay(pdMS_TO_TICKS(10));
                    continue;
                }
                ESP_LOGE(TAG_OTA, "Download error at %d/%d bytes (sslErr=%d)", totalRead, contentLength, sslErr);
                downloadOk = false;
                break;
            }
            wantReadRetries = 0;

            err = esp_ota_write(otaHandle, buf, bytesRead);
            if (err != ESP_OK) {
                ESP_LOGE(TAG_OTA, "OTA write failed: %s", esp_err_to_name(err));
                downloadOk = false;
                break;
            }
            // Hash the same bytes we just wrote to flash (Workstream 3).
            if (shaOk && wc_Sha256Update(&otaSha, buf, bytesRead) != 0) {
                ESP_LOGE(TAG_OTA, "SHA-256 update failed");
                shaOk = false;
            }

            totalRead += bytesRead;
            int progress = (totalRead * 100) / contentLength;
            if (progress - lastProgress >= 10) {
                ESP_LOGI(TAG_OTA, "Download progress: %d%% (%d/%d bytes)", progress, totalRead, contentLength);
                lastProgress = progress;
            }
        }

        wolfSSL_shutdown(otaSsl);
        wolfSSL_free(otaSsl);
        wolfSSL_CTX_free(otaCtx);
        tcpClient.stop();

        if (!downloadOk || totalRead != contentLength) {
            ESP_LOGE(TAG_OTA, "Download incomplete: %d/%d bytes", totalRead, contentLength);
            esp_ota_abort(otaHandle);
            g_otaPending = false;
            continue;
        }

        err = esp_ota_end(otaHandle);
        if (err != ESP_OK) {
            ESP_LOGE(TAG_OTA, "esp_ota_end failed: %s", esp_err_to_name(err));
            g_otaPending = false;
            continue;
        }

        // ─── Verify gate (Workstream 3) ─────────────────────────────────────
        // The image is written but NOT yet bootable. Before switching the boot
        // partition we (1) finalize the streamed SHA-256 and enforce the
        // transmitted sha256 (integrity), and (2) for a SIGNED push, verify the
        // signature + signer-leaf chain-to-device-CA-root (authenticity). On ANY
        // failure we DO NOT set the boot partition: the freshly written slot is
        // simply abandoned and the device keeps running its current firmware.
        uint8_t imgDigest[WC_SHA256_DIGEST_SIZE] = {0};
        if (!shaOk || wc_Sha256Final(&otaSha, imgDigest) != 0) {
            ESP_LOGE(TAG_OTA, "Image SHA-256 finalize failed; refusing to boot new image");
            g_otaPending = false;
            continue;
        }

        // Enforce the transmitted sha256 (hex) if present — closes the latent
        // gap where the device previously ignored it. A mismatch means the bytes
        // on flash are not what the cloud built; refuse.
        {
            char digestHex[2 * WC_SHA256_DIGEST_SIZE + 1];
            for (int i = 0; i < WC_SHA256_DIGEST_SIZE; i++)
                sprintf(digestHex + 2 * i, "%02x", imgDigest[i]);
            // g_otaSha256 was captured from the OTA command at parse time.
            if (g_otaSha256[0] && strcasecmp(g_otaSha256, digestHex) != 0) {
                ESP_LOGE(TAG_OTA, "sha256 mismatch!\n  expected %s\n  actual   %s",
                         g_otaSha256, digestHex);
                g_otaPending = false;
                continue;
            }
            if (g_otaSha256[0]) ESP_LOGI(TAG_OTA, "Image sha256 OK (%s)", digestHex);
        }

        // Signature verification. Policy: if the push declares a sigAlg, the
        // signature MUST verify. If no sigAlg (unsigned/legacy push), log loudly
        // and proceed (classical 1.0.0 USB-origin images and pre-signing
        // commands are unsigned). A signed-era device is only ever SENT signed
        // pushes by publish-ota, so "unsigned" here means the classical path.
        if (g_otaSigAlg[0]) {
            ESP_LOGI(TAG_OTA, "Verifying %s signature before boot...", g_otaSigAlg);

            uint8_t* sigBuf = (uint8_t*)malloc(OTA_SIG_BUF_MAX);
            uint8_t* leafBuf = (uint8_t*)malloc(OTA_SIGNER_CERT_MAX + 1);
            size_t sigLen = 0, leafLen = 0;
            bool fetched = sigBuf && leafBuf &&
                otaHttpsGetToBuffer(g_otaSigUrl, sigBuf, OTA_SIG_BUF_MAX, &sigLen) &&
                otaHttpsGetToBuffer(g_otaSignerCertUrl, leafBuf, OTA_SIGNER_CERT_MAX, &leafLen);

            bool verified = false;
            if (fetched) {
                leafBuf[leafLen] = '\0';  // PEM is text; NUL-terminate for safety
                verified = otaVerifyImage(g_otaSigAlg, sigBuf, sigLen,
                                          leafBuf, leafLen,
                                          otaPartition, (size_t)totalRead, imgDigest);
            } else {
                ESP_LOGE(TAG_OTA, "Failed to fetch signature/signer cert for verification");
            }
            if (sigBuf) free(sigBuf);
            if (leafBuf) free(leafBuf);

            if (!verified) {
                ESP_LOGE(TAG_OTA, "SIGNATURE VERIFICATION FAILED — refusing to boot new image. "
                                  "Running firmware is unchanged.");
                g_otaPending = false;
                continue;
            }
            ESP_LOGI(TAG_OTA, "Signature verified — image is authentic.");
        } else {
            ESP_LOGW(TAG_OTA, "OTA image is UNSIGNED (no sigAlg); proceeding (classical/legacy path).");
        }

        err = esp_ota_set_boot_partition(otaPartition);
        if (err != ESP_OK) {
            ESP_LOGE(TAG_OTA, "Failed to set boot partition: %s", esp_err_to_name(err));
            g_otaPending = false;
            continue;
        }

        // Peak memory of the OTA path (download + SHA-256 + chain + signature
        // verify), logged now because the reboot resets the high-water marks.
        ESP_LOGI(TAG_OTA, "[diag] OTA peak: OTATask stack used %u of %u B; internal heap "
                 "low-water %u B; PSRAM low-water %u B",
                 (unsigned)(OTA_TASK_STACK_SIZE - uxTaskGetStackHighWaterMark(NULL) * sizeof(StackType_t)),
                 (unsigned)OTA_TASK_STACK_SIZE,
                 (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_SPIRAM));
        ESP_LOGI(TAG_OTA, "OTA complete! v%s written to '%s'. Rebooting in 3s...",
                 g_otaVersion, otaPartition->label);
        vTaskDelay(pdMS_TO_TICKS(3000));
        esp_restart();
    }
}
