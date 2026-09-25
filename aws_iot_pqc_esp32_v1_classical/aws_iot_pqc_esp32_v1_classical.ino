/*
 * aws_iot_pqc_esp32.ino
 *
 * ESP32 Arduino sketch demonstrating a connection to AWS IoT Core using
 * post-quantum cryptography (PQC) via wolfSSL with TLS 1.3.
 *
 * Key features:
 *   - Hybrid PQC key exchange: P-521 + Kyber (ML-KEM) Level 5
 *   - TLS 1.3 mutual authentication (mTLS) with X.509 certificates
 *   - WiFi managed as a FreeRTOS task with automatic recovery
 *   - MQTT publish/subscribe managed as a separate FreeRTOS task
 *   - Watchdog supervision of both tasks
 *   - Works with any ESP32 variant (ESP32, ESP32-S2, ESP32-S3, ESP32-C3, etc.)
 *
 * Required Arduino Libraries (install via Library Manager):
 *   1. wolfSSL          (by wolfSSL Inc.) - TLS 1.3 + PQC support
 *   2. MQTT            (by Joel Gaehwiler) - Arduino MQTT client
 *
 * Required Board Package:
 *   - esp32 by Espressif Systems (Board Manager)
 *
 * wolfSSL Configuration:
 *   After installing the wolfSSL library, you MUST edit the user_settings.h
 *   file in the wolfSSL library's src/ directory to enable PQC. See the
 *   companion file wolfssl_user_settings.h in this project for the required
 *   defines, or copy its contents into the library's user_settings.h.
 *
 * AWS IoT Core Setup:
 *   1. Create a Thing in the AWS IoT Console
 *   2. Create and download certificates (device cert, private key, root CA)
 *   3. Attach an IoT policy allowing Connect, Publish, Subscribe, Receive
 *   4. Copy your endpoint, certs, and WiFi credentials into secrets.h
 *
 * Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

#include <WiFi.h>
#include <MQTT.h>

// Espressif logging framework
#include <esp_log.h>

// wolfSSL headers - order matters
#include <wolfssl.h>              // Must come first (defines WOLFSSL_USER_SETTINGS)
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/ssl.h>
#include <wolfssl/wolfcrypt/logging.h>

#include "secrets.h"

#include <esp_ota_ops.h>
#include <esp_partition.h>

// ─── Log Tags ───────────────────────────────────────────────────────────────
// Each component gets its own tag for per-module log level control.
// Use esp_log_level_set(TAG, level) to adjust at runtime.
static const char* TAG_MAIN    = "main";
static const char* TAG_WIFI    = "wifi";
static const char* TAG_MQTT    = "mqtt";
static const char* TAG_TLS     = "tls";
static const char* TAG_WOLFSSL = "wolfssl";
static const char* TAG_OTA     = "ota";

// ─── Build-time Validation ──────────────────────────────────────────────────
#ifndef WOLFSSL_TLS13
  #error "wolfSSL TLS 1.3 support is required. Enable WOLFSSL_TLS13 in user_settings.h"
#endif
#ifndef WOLFSSL_HAVE_MLKEM
  #error "wolfSSL ML-KEM support is required. Enable WOLFSSL_HAVE_MLKEM in user_settings.h"
#endif

// ─── FreeRTOS Task Configuration ────────────────────────────────────────────
#define WIFI_TASK_STACK_SIZE    4096
#define MQTT_TASK_STACK_SIZE    32768   // PQC handshake needs significant stack
#define BLINK_TASK_STACK_SIZE   2048
#define OTA_TASK_STACK_SIZE     32768   // OTA download over TLS needs large stack
#define WIFI_TASK_PRIORITY      1
#define MQTT_TASK_PRIORITY      1
#define BLINK_TASK_PRIORITY     0       // Lowest priority — just visual indicator
#define OTA_TASK_PRIORITY       2       // Above MQTT so download runs promptly
#define WIFI_TASK_CORE          0       // Run WiFi on core 0
#define MQTT_TASK_CORE          1       // Run MQTT on core 1 (if available)

// ─── Firmware Version ───────────────────────────────────────────────────────
// Single canonical version per variant. CodeBuild injects FIRMWARE_VERSION into
// secrets.h (from prepare-build's VARIANT_CONFIG), so the value the device
// reports in telemetry is identical to the S3 artifact dir and the OTA job
// version — the dashboard, firmware bucket, and device all agree. This #define
// is only a fallback for a local/desktop build where secrets.h did not set it.
#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "1.0.0-classical"
#endif

// ─── Certificate Signature Algorithm ────────────────────────────────────────
// The signature algorithm of the X.509 client certificate this firmware
// presents during the TLS handshake. Fixed per firmware variant: the classical
// firmware presents an ECDSA P-256 signed certificate. Reported in live
// telemetry so the dashboard can show which certificate type is in use.
#define CERT_SIGNATURE_ALG "ECDSA-P256"

// ─── LED Configuration ──────────────────────────────────────────────────────
// XIAO ESP32-S3 built-in LED is on GPIO 21
#define LED_PIN                 21

// ─── Timing Constants (milliseconds) ────────────────────────────────────────
#define WIFI_CONNECT_TIMEOUT_MS     15000
#define WIFI_RETRY_DELAY_MS         5000
#define WIFI_CHECK_INTERVAL_MS      1000
#define MQTT_PUBLISH_INTERVAL_MS    30000
#define MQTT_KEEPALIVE_SEC          60
#define WATCHDOG_TIMEOUT_SEC        120

// ─── Exponential Backoff Configuration ──────────────────────────────────────
#define BACKOFF_BASE_MS             1000    // Initial retry delay: 1 second
#define BACKOFF_MAX_MS              120000  // Maximum retry delay: 2 minutes
#define BACKOFF_MULTIPLIER          2       // Double each attempt
#define BACKOFF_MAX_ATTEMPTS        0       // 0 = unlimited retries

// ─── MQTT Topic Structure (AWS IoT Best Practices) ──────────────────────────
// Topics follow the pattern: <prefix>/<thingName>/<purpose>
// This enables fine-grained IoT policy rules and IoT Rules Engine routing.
//
// Telemetry (device → cloud):  dt/<thingName>/telemetry
// Commands (cloud → device):   cmd/<thingName>/+
// Lifecycle (device status):   dt/<thingName>/lifecycle
// LWT (broker-published):      dt/<thingName>/lifecycle  (offline payload)

// Helper macro to build topic strings at compile time
#define TOPIC_PREFIX_DT    "dt/"
#define TOPIC_PREFIX_CMD   "cmd/"

// These are built at runtime using the MAC-derived device identity
static char g_topicTelemetry[128];   // dt/<thing>/telemetry
static char g_topicLifecycle[128];   // dt/<thing>/lifecycle
static char g_topicCmdAll[128];      // cmd/<thing>/+  (subscribe wildcard)
static char g_topicCmdReq[128];      // cmd/<thing>/request

// ─── Device Identity (derived from MAC address at runtime) ──────────────────
static char g_thingName[32];  // "esp32-XXYYZZ" format

// ─── Global State ───────────────────────────────────────────────────────────
static volatile bool g_wifiConnected = false;
static volatile bool g_mqttConnected = false;
static SemaphoreHandle_t g_wifiMutex  = NULL;

// Negotiated TLS info (populated after successful handshake)
static char g_tlsVersion[32] = "unknown";
static char g_tlsCipher[64] = "unknown";
static char g_tlsKeyExchange[64] = "none";

// wolfSSL context (shared, thread-safe after init)
static WOLFSSL_CTX* g_wolfCtx = NULL;

// Task handles for watchdog monitoring
static TaskHandle_t g_wifiTaskHandle = NULL;
static TaskHandle_t g_mqttTaskHandle = NULL;
static TaskHandle_t g_blinkTaskHandle = NULL;
static TaskHandle_t g_otaTaskHandle = NULL;

// Amazon Root CA 1 — used to verify the S3 TLS connection during OTA download.
// The device's own AWS_CERT_CA is the workshop Private CA (for the Mosquitto
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

// OTA state (shared between MQTT handler and OTA task)
// Set by the mqttMessageHandler when an "ota-update" command arrives, then
// consumed by otaTask which downloads the firmware from the presigned S3 URL.
static volatile bool g_otaPending = false;
static char g_otaUrl[2048] = {0};   // presigned S3 URL is ~1500 bytes; 1024 truncated it
static char g_otaVersion[32] = {0};
static int  g_otaSize = 0;
static SemaphoreHandle_t g_otaSemaphore = NULL;

// ─── Forward Declarations ───────────────────────────────────────────────────
void wifiTask(void* pvParameters);
void mqttTask(void* pvParameters);
void otaTask(void* pvParameters);
void mqttCallback(char* topic, byte* payload, unsigned int length);
void mqttMessageHandler(String &topic, String &payload);
bool setupWolfSSLContext();
WiFiClient* createPQCTLSClient();
static void initDeviceIdentity();

// ─── Device Identity from MAC Address ───────────────────────────────────────
// Derives a unique Thing name from the ESP32's factory-burned MAC address.
// Format: "esp32-XXYYZZ" where XXYYZZ are the last 3 bytes of the MAC.
// This allows a single firmware binary to run on any device.
static void initDeviceIdentity() {
    uint64_t mac = ESP.getEfuseMac();
    uint8_t* macBytes = (uint8_t*)&mac;
    snprintf(g_thingName, sizeof(g_thingName), "esp32-%02X%02X%02X",
             macBytes[3], macBytes[4], macBytes[5]);
    ESP_LOGI(TAG_MAIN, "Device identity: %s (MAC-derived)", g_thingName);
}

// ═══════════════════════════════════════════════════════════════════════════
//  Exponential Backoff with Jitter
// ═══════════════════════════════════════════════════════════════════════════

// Calculates the next retry delay using exponential backoff with full jitter.
// This prevents "thundering herd" when many devices reconnect simultaneously.
// See: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
static unsigned long calculateBackoff(int attempt) {
    // Calculate exponential delay: base * 2^attempt
    unsigned long expDelay = BACKOFF_BASE_MS;
    for (int i = 0; i < attempt && expDelay < BACKOFF_MAX_MS; i++) {
        expDelay *= BACKOFF_MULTIPLIER;
    }
    if (expDelay > BACKOFF_MAX_MS) {
        expDelay = BACKOFF_MAX_MS;
    }

    // Apply full jitter: random value between 0 and expDelay
    // This decorrelates retry attempts across multiple devices
    unsigned long jitteredDelay = random(0, expDelay);

    ESP_LOGD(TAG_MQTT, "Backoff: attempt=%d, exp=%lu ms, jittered=%lu ms",
             attempt, expDelay, jitteredDelay);
    return jitteredDelay;
}

// ═══════════════════════════════════════════════════════════════════════════
//  Topic Initialization
// ═══════════════════════════════════════════════════════════════════════════

static void initTopics() {
    snprintf(g_topicTelemetry, sizeof(g_topicTelemetry),
             "%s%s/telemetry", TOPIC_PREFIX_DT, g_thingName);
    snprintf(g_topicLifecycle, sizeof(g_topicLifecycle),
             "%s%s/lifecycle", TOPIC_PREFIX_DT, g_thingName);
    snprintf(g_topicCmdAll, sizeof(g_topicCmdAll),
             "%s%s/+", TOPIC_PREFIX_CMD, g_thingName);
    snprintf(g_topicCmdReq, sizeof(g_topicCmdReq),
             "%s%s/request", TOPIC_PREFIX_CMD, g_thingName);

    ESP_LOGD(TAG_MQTT, "Topics initialized:");
    ESP_LOGD(TAG_MQTT, "  Telemetry: %s", g_topicTelemetry);
    ESP_LOGD(TAG_MQTT, "  Lifecycle: %s", g_topicLifecycle);
    ESP_LOGD(TAG_MQTT, "  Commands:  %s", g_topicCmdAll);
}

// ═══════════════════════════════════════════════════════════════════════════
//  wolfSSL I/O Callbacks for Arduino WiFiClient
// ═══════════════════════════════════════════════════════════════════════════

// We wrap a raw WiFiClient in wolfSSL's custom I/O callbacks so that
// wolfSSL handles the TLS layer while Arduino WiFiClient handles TCP.

typedef struct {
    WiFiClient* client;
} wolfssl_io_ctx_t;

static int wolfssl_send_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx) {
    wolfssl_io_ctx_t* io = (wolfssl_io_ctx_t*)ctx;
    if (!io || !io->client || !io->client->connected()) {
        ESP_LOGD(TAG_TLS, "send_cb: connection closed");
        return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    }
    int sent = io->client->write((uint8_t*)buf, sz);
    if (sent <= 0) {
        ESP_LOGV(TAG_TLS, "send_cb: WANT_WRITE (requested %d bytes)", sz);
        return WOLFSSL_CBIO_ERR_WANT_WRITE;
    }
    ESP_LOGV(TAG_TLS, "send_cb: sent %d/%d bytes", sent, sz);
    return sent;
}

static int wolfssl_recv_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx) {
    wolfssl_io_ctx_t* io = (wolfssl_io_ctx_t*)ctx;
    if (!io || !io->client) {
        ESP_LOGD(TAG_TLS, "recv_cb: no client");
        return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    }

    // IMPORTANT: drain buffered bytes BEFORE honouring connected()==false.
    // When the peer (e.g. S3 serving the OTA firmware) half-closes the TCP
    // connection right after sending the final TLS record, WiFiClient::
    // connected() can report false while available() still has the last bytes
    // buffered. If we returned CONN_CLOSE here we would drop those bytes and the
    // OTA download would fail a few hundred bytes short of EOF (observed:
    // "Download error at 1199644/1200416"). So: if there is buffered data, read
    // it regardless of connected(); only report CONN_CLOSE once the socket is
    // BOTH closed AND fully drained.
    int avail = io->client->available();
    if (avail == 0) {
        if (!io->client->connected()) {
            ESP_LOGD(TAG_TLS, "recv_cb: connection closed and drained");
            return WOLFSSL_CBIO_ERR_CONN_CLOSE;
        }
        ESP_LOGV(TAG_TLS, "recv_cb: no data available, WANT_READ");
        return WOLFSSL_CBIO_ERR_WANT_READ;
    }

    int received = io->client->read((uint8_t*)buf, sz);
    if (received <= 0) {
        ESP_LOGV(TAG_TLS, "recv_cb: read returned %d, WANT_READ", received);
        return WOLFSSL_CBIO_ERR_WANT_READ;
    }
    ESP_LOGV(TAG_TLS, "recv_cb: received %d/%d bytes", received, sz);
    return received;
}

// ═══════════════════════════════════════════════════════════════════════════
//  wolfSSL PQC TLS Client Wrapper
// ═══════════════════════════════════════════════════════════════════════════

// This class wraps a plain WiFiClient with wolfSSL TLS 1.3 + PQC,
// and exposes the Arduino Client interface so MQTTClient can use it.

class WolfSSLClient : public Client {
public:
    WolfSSLClient() : _ssl(NULL), _connected(false) {
        _ioCtx.client = &_tcpClient;
    }

    ~WolfSSLClient() {
        stop();
    }

    int connect(IPAddress ip, uint16_t port) override {
        return connect(ip.toString().c_str(), port);
    }

    int connect(const char* host, uint16_t port) override {
        stop(); // Clean up any previous connection
        _pqcRequested = false;

        ESP_LOGD(TAG_TLS, "Connecting to %s:%d", host, port);

        // Step 1: Establish raw TCP connection
        if (!_tcpClient.connect(host, port)) {
            ESP_LOGE(TAG_TLS, "TCP connection failed to %s:%d", host, port);
            return 0;
        }
        ESP_LOGI(TAG_TLS, "TCP connected to %s:%d", host, port);

        // Step 2: Create wolfSSL session from the shared context
        _ssl = wolfSSL_new(g_wolfCtx);
        if (!_ssl) {
            ESP_LOGE(TAG_TLS, "Failed to create SSL session (out of memory?)");
            _tcpClient.stop();
            return 0;
        }
        ESP_LOGD(TAG_TLS, "SSL session object created");

        // Step 3: Set custom I/O callbacks
        wolfSSL_SSLSetIORecv(_ssl, wolfssl_recv_cb);
        wolfSSL_SSLSetIOSend(_ssl, wolfssl_send_cb);
        wolfSSL_SetIOReadCtx(_ssl, &_ioCtx);
        wolfSSL_SetIOWriteCtx(_ssl, &_ioCtx);
        ESP_LOGD(TAG_TLS, "I/O callbacks registered");

        // Step 4: Classical TLS — no PQC key exchange requested
        ESP_LOGI(TAG_TLS, "Using classical key exchange (ECDHE P-256, no PQC)");

        // Step 5: Set SNI (Server Name Indication)
        int ret = wolfSSL_UseSNI(_ssl, WOLFSSL_SNI_HOST_NAME,
                             host, strlen(host));
        if (ret != WOLFSSL_SUCCESS) {
            ESP_LOGW(TAG_TLS, "SNI setup failed (rc=%d) - non-fatal", ret);
        } else {
            ESP_LOGD(TAG_TLS, "SNI set to '%s'", host);
        }

        // Step 6: Perform TLS 1.3 handshake with timeout
        ESP_LOGI(TAG_TLS, "Starting TLS 1.3 PQC handshake...");
        unsigned long handshakeStart = millis();
        const unsigned long handshakeTimeout = 30000; // 30s for PQC handshake

        do {
            ret = wolfSSL_connect(_ssl);
            if (ret == WOLFSSL_SUCCESS) {
                break;
            }

            int err = wolfSSL_get_error(_ssl, ret);
            if (err == WOLFSSL_ERROR_WANT_READ || err == WOLFSSL_ERROR_WANT_WRITE) {
                // Non-blocking: yield and retry
                ESP_LOGV(TAG_TLS, "Handshake in progress (WANT_%s), elapsed %lu ms",
                         err == WOLFSSL_ERROR_WANT_READ ? "READ" : "WRITE",
                         millis() - handshakeStart);
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }

            // Real error
            char errBuf[128];
            wolfSSL_ERR_error_string_n(err, errBuf, sizeof(errBuf));
            ESP_LOGE(TAG_TLS, "Handshake error: %s (code=%d)", errBuf, err);
            wolfSSL_free(_ssl);
            _ssl = NULL;
            _tcpClient.stop();
            return 0;

        } while ((millis() - handshakeStart) < handshakeTimeout);

        if (ret != WOLFSSL_SUCCESS) {
            ESP_LOGE(TAG_TLS, "Handshake timed out after %lu ms", millis() - handshakeStart);
            wolfSSL_free(_ssl);
            _ssl = NULL;
            _tcpClient.stop();
            return 0;
        }

        // Step 7: Verify the connection details and store negotiated params
        unsigned long handshakeDuration = millis() - handshakeStart;
        const char* version = wolfSSL_get_version(_ssl);
        const char* cipher = wolfSSL_get_cipher_name(_ssl);

        // Store for telemetry reporting
        strncpy(g_tlsVersion, version ? version : "unknown", sizeof(g_tlsVersion) - 1);
        strncpy(g_tlsCipher, cipher ? cipher : "unknown", sizeof(g_tlsCipher) - 1);

        // Detect PQC from cipher/handshake — if we requested PQC key share
        // and the handshake succeeded, the server accepted it
        if (strstr(cipher ? cipher : "", "MLKEM") ||
            strstr(cipher ? cipher : "", "mlkem") ||
            strstr(cipher ? cipher : "", "KYBER")) {
            strncpy(g_tlsKeyExchange, "SECP256R1_MLKEM768", sizeof(g_tlsKeyExchange) - 1);
        } else if (_pqcRequested) {
            // We requested PQC and handshake succeeded — PQC was negotiated
            strncpy(g_tlsKeyExchange, "SECP256R1_MLKEM768", sizeof(g_tlsKeyExchange) - 1);
        } else {
            strncpy(g_tlsKeyExchange, "ECDHE-P256", sizeof(g_tlsKeyExchange) - 1);
        }

        ESP_LOGI(TAG_TLS, "TLS handshake complete in %lu ms", handshakeDuration);
        ESP_LOGI(TAG_TLS, "Protocol: %s, Cipher: %s", g_tlsVersion, g_tlsCipher);
        ESP_LOGI(TAG_TLS, "Key Exchange: %s", g_tlsKeyExchange);
        ESP_LOGD(TAG_TLS, "Free heap after handshake: %u bytes", ESP.getFreeHeap());

        _connected = true;
        return 1;
    }

    size_t write(uint8_t b) override {
        return write(&b, 1);
    }

    size_t write(const uint8_t* buf, size_t size) override {
        if (!_ssl || !_connected) return 0;

        int ret = wolfSSL_write(_ssl, buf, size);
        if (ret <= 0) {
            int err = wolfSSL_get_error(_ssl, ret);
            if (err != WOLFSSL_ERROR_WANT_WRITE) {
                ESP_LOGE(TAG_TLS, "write failed (err=%d), marking disconnected", err);
                _connected = false;
            }
            return 0;
        }
        ESP_LOGV(TAG_TLS, "write: %d/%zu bytes sent", ret, size);
        return (size_t)ret;
    }

    int available() override {
        if (!_ssl || !_connected) return 0;

        // Check if wolfSSL has already-decrypted data buffered
        int pending = wolfSSL_pending(_ssl);
        if (pending > 0) return pending;

        // Check if the underlying TCP has encrypted data waiting.
        // If so, we report 1 byte available to trigger MQTTClient
        // to call read(), which will invoke wolfSSL_read() to decrypt.
        if (_tcpClient.available() > 0) return 1;

        return 0;
    }

    int read() override {
        uint8_t b;
        // MQTTClient reads one byte at a time during packet parsing.
        // We must block briefly to allow wolfSSL to decrypt incoming records.
        if (!_ssl || !_connected) return -1;

        unsigned long startWait = millis();
        while (available() == 0) {
            if (millis() - startWait > 5000) return -1; // 5s timeout
            if (!_connected) return -1;
            delay(1);
        }
        if (read(&b, 1) == 1) return b;
        return -1;
    }

    int read(uint8_t* buf, size_t size) override {
        if (!_ssl || !_connected) return -1;

        int ret = wolfSSL_read(_ssl, buf, size);
        if (ret <= 0) {
            int err = wolfSSL_get_error(_ssl, ret);
            if (err == WOLFSSL_ERROR_WANT_READ) {
                // If TCP has data, wolfSSL needs another call to finish
                // decrypting a record. Retry a few times with small delays.
                for (int i = 0; i < 10 && _tcpClient.available() > 0; i++) {
                    delay(10);
                    ret = wolfSSL_read(_ssl, buf, size);
                    if (ret > 0) {
                        ESP_LOGV(TAG_TLS, "read: %d/%zu bytes (after %d retries)", ret, size, i + 1);
                        return ret;
                    }
                }
                return 0; // No data yet, non-blocking
            }
            ESP_LOGE(TAG_TLS, "read failed (err=%d), marking disconnected", err);
            _connected = false;
            return -1;
        }
        ESP_LOGV(TAG_TLS, "read: %d/%zu bytes received", ret, size);
        return ret;
    }

    int peek() override {
        if (!_ssl || !_connected) return -1;

        uint8_t b;
        int ret = wolfSSL_peek(_ssl, &b, 1);
        if (ret <= 0) return -1;
        return b;
    }

    void flush() override {
        _tcpClient.flush();
    }

    void stop() override {
        if (_ssl) {
            ESP_LOGD(TAG_TLS, "Shutting down TLS session");
            wolfSSL_shutdown(_ssl);
            wolfSSL_free(_ssl);
            _ssl = NULL;
        }
        _tcpClient.stop();
        _connected = false;
        ESP_LOGD(TAG_TLS, "Connection stopped");
    }

    uint8_t connected() override {
        return _connected && _tcpClient.connected();
    }

    operator bool() override {
        return connected();
    }

private:
    WiFiClient       _tcpClient;
    WOLFSSL*         _ssl;
    wolfssl_io_ctx_t _ioCtx;
    bool             _connected;
    bool             _pqcRequested;
};

// ═══════════════════════════════════════════════════════════════════════════
//  wolfSSL Context Initialization
// ═══════════════════════════════════════════════════════════════════════════

bool setupWolfSSLContext() {
    ESP_LOGI(TAG_WOLFSSL, "Initializing wolfSSL library...");

    // Initialize wolfSSL library
    int ret = wolfSSL_Init();
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Library init failed (rc=%d)", ret);
        return false;
    }
    ESP_LOGD(TAG_WOLFSSL, "wolfSSL_Init() succeeded");

    // Enable debug logging (comment out for production)
    // wolfSSL_Debugging_ON();

    // Create TLS 1.3 client context
    g_wolfCtx = wolfSSL_CTX_new(wolfTLSv1_3_client_method());
    if (!g_wolfCtx) {
        ESP_LOGE(TAG_WOLFSSL, "Failed to create TLS 1.3 context");
        return false;
    }
    ESP_LOGD(TAG_WOLFSSL, "TLS 1.3 client context created");

    // Load Amazon Root CA 1 (server verification)
    ESP_LOGD(TAG_WOLFSSL, "Loading CA certificate (%d bytes)", strlen(AWS_CERT_CA));
    ret = wolfSSL_CTX_load_verify_buffer(g_wolfCtx,
            (const unsigned char*)AWS_CERT_CA,
            strlen(AWS_CERT_CA),
            WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Failed to load CA cert (rc=%d)", ret);
        return false;
    }
    ESP_LOGI(TAG_WOLFSSL, "CA certificate loaded");

    // Load device certificate (client authentication)
    ESP_LOGD(TAG_WOLFSSL, "Loading device certificate (%d bytes)", strlen(AWS_CERT_CRT));
    ret = wolfSSL_CTX_use_certificate_buffer(g_wolfCtx,
            (const unsigned char*)AWS_CERT_CRT,
            strlen(AWS_CERT_CRT),
            WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Failed to load device cert (rc=%d)", ret);
        return false;
    }
    ESP_LOGI(TAG_WOLFSSL, "Device certificate loaded");

    // Load device private key
    ESP_LOGD(TAG_WOLFSSL, "Loading device private key");
    ret = wolfSSL_CTX_use_PrivateKey_buffer(g_wolfCtx,
            (const unsigned char*)AWS_CERT_PRIVATE,
            strlen(AWS_CERT_PRIVATE),
            WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) {
        ESP_LOGE(TAG_WOLFSSL, "Failed to load private key (rc=%d)", ret);
        return false;
    }
    ESP_LOGI(TAG_WOLFSSL, "Private key loaded");

    // Require server certificate verification
    wolfSSL_CTX_set_verify(g_wolfCtx, WOLFSSL_VERIFY_PEER, NULL);
    ESP_LOGD(TAG_WOLFSSL, "Peer verification enabled");

    // Set custom I/O callbacks at the context level
    wolfSSL_CTX_SetIORecv(g_wolfCtx, wolfssl_recv_cb);
    wolfSSL_CTX_SetIOSend(g_wolfCtx, wolfssl_send_cb);
    ESP_LOGD(TAG_WOLFSSL, "I/O callbacks registered at context level");

    ESP_LOGI(TAG_WOLFSSL, "TLS 1.3 PQC context ready (heap free: %u bytes)", ESP.getFreeHeap());
    return true;
}

// ═══════════════════════════════════════════════════════════════════════════
//  WiFi FreeRTOS Task
// ═══════════════════════════════════════════════════════════════════════════

void wifiTask(void* pvParameters) {
    ESP_LOGI(TAG_WIFI, "Task started on core %d", xPortGetCoreID());

    // Configure WiFi
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    ESP_LOGD(TAG_WIFI, "WiFi mode set to STA, auto-reconnect enabled");

    int wifiAttempts = 0;

    for (;;) {
        if (WiFi.status() != WL_CONNECTED) {
            if (xSemaphoreTake(g_wifiMutex, pdMS_TO_TICKS(1000)) == pdTRUE) {
                g_wifiConnected = false;
                xSemaphoreGive(g_wifiMutex);
            }

            ESP_LOGI(TAG_WIFI, "Connecting to SSID '%s' (attempt %d)...", WIFI_SSID, wifiAttempts + 1);
            WiFi.disconnect(true);
            ESP_LOGD(TAG_WIFI, "Previous connection disconnected, waiting 500ms for radio reset");
            vTaskDelay(pdMS_TO_TICKS(500));
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

            // Wait for connection with timeout
            unsigned long startAttempt = millis();
            while (WiFi.status() != WL_CONNECTED &&
                   (millis() - startAttempt) < WIFI_CONNECT_TIMEOUT_MS) {
                vTaskDelay(pdMS_TO_TICKS(500));
                ESP_LOGV(TAG_WIFI, "Waiting... status=%d, elapsed=%lu ms",
                         WiFi.status(), millis() - startAttempt);
            }

            if (WiFi.status() == WL_CONNECTED) {
                if (xSemaphoreTake(g_wifiMutex, pdMS_TO_TICKS(1000)) == pdTRUE) {
                    g_wifiConnected = true;
                    xSemaphoreGive(g_wifiMutex);
                }
                wifiAttempts = 0; // Reset backoff on success
                ESP_LOGI(TAG_WIFI, "Connected! IP: %s, RSSI: %d dBm, Channel: %d",
                         WiFi.localIP().toString().c_str(),
                         WiFi.RSSI(), WiFi.channel());
                ESP_LOGI(TAG_WIFI, "  Gateway: %s, Subnet: %s, DNS: %s",
                         WiFi.gatewayIP().toString().c_str(),
                         WiFi.subnetMask().toString().c_str(),
                         WiFi.dnsIP().toString().c_str());
                ESP_LOGD(TAG_WIFI, "BSSID: %s, DNS: %s",
                         WiFi.BSSIDstr().c_str(),
                         WiFi.dnsIP().toString().c_str());
            } else {
                wifiAttempts++;
                // Exponential backoff with jitter for WiFi retries
                unsigned long backoff = WIFI_RETRY_DELAY_MS;
                for (int i = 1; i < wifiAttempts && backoff < BACKOFF_MAX_MS; i++) {
                    backoff *= BACKOFF_MULTIPLIER;
                }
                if (backoff > BACKOFF_MAX_MS) backoff = BACKOFF_MAX_MS;
                unsigned long jittered = random(backoff / 2, backoff);

                ESP_LOGW(TAG_WIFI, "Connection failed (status=%d, attempt=%d). Backoff: %lu ms",
                         WiFi.status(), wifiAttempts, jittered);
                WiFi.disconnect(true);
                vTaskDelay(pdMS_TO_TICKS(jittered));
                continue;
            }
        }

        // Periodic health check
        ESP_LOGV(TAG_WIFI, "Health check: status=%d, RSSI=%d dBm", WiFi.status(), WiFi.RSSI());
        vTaskDelay(pdMS_TO_TICKS(WIFI_CHECK_INTERVAL_MS));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT Callback
// ═══════════════════════════════════════════════════════════════════════════

void mqttCallback(char* topic, byte* payload, unsigned int length) {
    ESP_LOGI(TAG_MQTT, "Message received on topic '%s' (%u bytes)", topic, length);

    // Log the payload at debug level (may contain sensitive data)
    if (length < 512) {
        char msgBuf[513];
        memcpy(msgBuf, payload, length);
        msgBuf[length] = '\0';
        ESP_LOGD(TAG_MQTT, "Payload: %s", msgBuf);
    } else {
        ESP_LOGD(TAG_MQTT, "Payload too large to log (%u bytes)", length);
    }

    // ── Handle incoming messages here ──
    // Example: parse JSON, control GPIOs, update state, etc.
}

// ─── Minimal whitespace-tolerant JSON value extractors ─────────────────────
// These handle both compact ({"k":"v"}) and spaced ({"k": "v"}) JSON, since
// Python's json.dumps() (used by the OTA Lambda) inserts a space after the colon.

// Returns the string value for key, or "" if not found.
static String jsonGetString(const String &json, const char *key) {
    String needle = String("\"") + key + "\"";
    int k = json.indexOf(needle);
    if (k < 0) return String("");
    int colon = json.indexOf(':', k + needle.length());
    if (colon < 0) return String("");
    int i = colon + 1;
    while (i < (int)json.length() && (json[i] == ' ' || json[i] == '\t')) i++;
    if (i >= (int)json.length() || json[i] != '"') return String("");
    int start = i + 1;
    int end = json.indexOf('"', start);
    if (end < 0) return String("");
    return json.substring(start, end);
}

// Returns the numeric value for key, or 0 if not found.
static long jsonGetNumber(const String &json, const char *key) {
    String needle = String("\"") + key + "\"";
    int k = json.indexOf(needle);
    if (k < 0) return 0;
    int colon = json.indexOf(':', k + needle.length());
    if (colon < 0) return 0;
    int i = colon + 1;
    while (i < (int)json.length() && (json[i] == ' ' || json[i] == '\t')) i++;
    return json.substring(i).toInt();
}

// ─── Slot-content-aware partition swap ──────────────────────────────────────
// The two OTA slots (ota_0/ota_1) can hold ANY two of the firmware variants
// (classical / pqc / pqc-mldsa) depending on migration history, so we must NOT
// assume "classical=ota_0, pqc=ota_1". Instead, inspect what firmware actually
// lives in each OTA slot (esp_ota_get_partition_description -> project_name) and
// boot the slot whose project_name contains the requested marker. This makes a
// revert/swap work for any 2-of-3 combination resident on the device.
//   marker examples: "classical" (v1 sketch), "v3_mldsa" (ML-DSA sketch),
//   or the plain pqc sketch project name "aws_iot_pqc_esp32".
// Returns true if a matching slot was found and set as the boot partition.
static bool bootSlotMatchingProject(const char* marker) {
    const esp_partition_subtype_t subtypes[2] = {
        ESP_PARTITION_SUBTYPE_APP_OTA_0, ESP_PARTITION_SUBTYPE_APP_OTA_1
    };
    for (int i = 0; i < 2; i++) {
        const esp_partition_t* part = esp_partition_find_first(
            ESP_PARTITION_TYPE_APP, subtypes[i], NULL);
        if (!part) continue;
        esp_app_desc_t appDesc;
        if (esp_ota_get_partition_description(part, &appDesc) != ESP_OK) continue;
        ESP_LOGI(TAG_MQTT, "Slot '%s' holds project '%s' v%s",
                 part->label, appDesc.project_name, appDesc.version);
        if (strstr(appDesc.project_name, marker) != NULL) {
            ESP_LOGI(TAG_MQTT, "Match for marker '%s' in slot '%s' — setting boot partition",
                     marker, part->label);
            esp_err_t err = esp_ota_set_boot_partition(part);
            if (err != ESP_OK) {
                ESP_LOGE(TAG_MQTT, "Failed to set boot partition: %s", esp_err_to_name(err));
                return false;
            }
            return true;
        }
    }
    ESP_LOGE(TAG_MQTT, "No OTA slot holds a firmware matching marker '%s'", marker);
    return false;
}

// Exact-match variant of bootSlotMatchingProject. Needed for the plain pqc
// sketch, whose project_name "aws_iot_pqc_esp32" is a SUBSTRING of both
// "aws_iot_pqc_esp32_v1_classical" and "aws_iot_pqc_esp32_v3_mldsa", so a naive
// strstr would mis-match. Boots the slot whose project_name is EXACTLY name.
static bool bootSlotExactProject(const char* name) {
    const esp_partition_subtype_t subtypes[2] = {
        ESP_PARTITION_SUBTYPE_APP_OTA_0, ESP_PARTITION_SUBTYPE_APP_OTA_1
    };
    for (int i = 0; i < 2; i++) {
        const esp_partition_t* part = esp_partition_find_first(
            ESP_PARTITION_TYPE_APP, subtypes[i], NULL);
        if (!part) continue;
        esp_app_desc_t appDesc;
        if (esp_ota_get_partition_description(part, &appDesc) != ESP_OK) continue;
        ESP_LOGI(TAG_MQTT, "Slot '%s' holds project '%s' v%s",
                 part->label, appDesc.project_name, appDesc.version);
        if (strcmp(appDesc.project_name, name) == 0) {
            ESP_LOGI(TAG_MQTT, "Exact match for '%s' in slot '%s' — setting boot partition",
                     name, part->label);
            esp_err_t err = esp_ota_set_boot_partition(part);
            if (err != ESP_OK) {
                ESP_LOGE(TAG_MQTT, "Failed to set boot partition: %s", esp_err_to_name(err));
                return false;
            }
            return true;
        }
    }
    ESP_LOGE(TAG_MQTT, "No OTA slot holds a firmware exactly named '%s'", name);
    return false;
}

// MQTTClient message handler (wraps mqttCallback)
void mqttMessageHandler(String &topic, String &payload) {
    ESP_LOGI(TAG_MQTT, "Message received on topic '%s' (%d bytes)", topic.c_str(), payload.length());
    ESP_LOGD(TAG_MQTT, "Payload: %s", payload.c_str());

    // LED control: {"action":"led-off"} or {"action":"led-on"}
    if (payload.indexOf("led-off") >= 0) {
        ESP_LOGI(TAG_MQTT, "LED OFF command received");
        if (g_blinkTaskHandle) vTaskSuspend(g_blinkTaskHandle);
        digitalWrite(LED_PIN, LOW);
        return;
    }
    if (payload.indexOf("led-on") >= 0) {
        ESP_LOGI(TAG_MQTT, "LED ON command received");
        if (g_blinkTaskHandle) vTaskResume(g_blinkTaskHandle);
        return;
    }

    // OTA download: {"action":"ota-update","url":"...","version":"x.y.z","size":N}
    // on cmd/<thing>/ota. Downloads PQC firmware from a presigned S3 URL and
    // writes it to the next OTA partition (see otaTask).
    if (topic.indexOf("/ota") >= 0 && payload.indexOf("ota-update") >= 0) {
        // Extract a JSON string value tolerant of whitespace after the colon,
        // e.g. both {"url":"..."} and {"url": "..."}. Finds the key, skips the
        // colon and any spaces, then reads between the opening and closing quotes.
        String url = jsonGetString(payload, "url");
        if (url.length() > 0) {
            url.toCharArray(g_otaUrl, sizeof(g_otaUrl));

            String ver = jsonGetString(payload, "version");
            if (ver.length() > 0) {
                ver.toCharArray(g_otaVersion, sizeof(g_otaVersion));
            } else {
                strcpy(g_otaVersion, "unknown");
            }

            g_otaSize = (int)jsonGetNumber(payload, "size");

            g_otaPending = true;
            if (g_otaSemaphore) xSemaphoreGive(g_otaSemaphore);
            ESP_LOGI(TAG_MQTT, "OTA download queued: v%s, %d bytes", g_otaVersion, g_otaSize);
        } else {
            ESP_LOGE(TAG_MQTT, "ota-update command missing/invalid url");
        }
        return;
    }

    // OTA migration (partition-swap): boot whichever variant is already
    // resident in the other OTA slot. Slot-content-aware, so it works for any
    // 2-of-3 combination of {classical, pqc, mldsa} on the device.
    if (topic.indexOf("/ota") >= 0) {
        bool swapped = false;
        if (payload.indexOf("migrate-classical") >= 0) {
            ESP_LOGI(TAG_MQTT, "Classical migration command received!");
            swapped = bootSlotMatchingProject("classical");
        } else if (payload.indexOf("migrate-mldsa") >= 0) {
            ESP_LOGI(TAG_MQTT, "ML-DSA migration command received!");
            swapped = bootSlotMatchingProject("v3_mldsa");
        } else if (payload.indexOf("migrate-pqc") >= 0) {
            ESP_LOGI(TAG_MQTT, "PQC migration command received!");
            swapped = bootSlotExactProject("aws_iot_pqc_esp32");
        } else {
            return;
        }

        if (swapped) {
            ESP_LOGI(TAG_MQTT, "Boot partition updated, restarting...");
            vTaskDelay(pdMS_TO_TICKS(2000));
            esp_restart();
        }
        return;
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  OTA Download Task (wolfSSL HTTPS client)
// ═══════════════════════════════════════════════════════════════════════════

// Waits for an ota-update command, downloads the firmware from the presigned
// S3 URL over TLS, streams it into the next OTA partition, then reboots into
// the new firmware. The S3 connection uses a fresh wolfSSL context WITHOUT the
// device client cert (presigned URLs authenticate via query string, not mTLS).
void otaTask(void* pvParameters) {
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

        err = esp_ota_set_boot_partition(otaPartition);
        if (err != ESP_OK) {
            ESP_LOGE(TAG_OTA, "Failed to set boot partition: %s", esp_err_to_name(err));
            g_otaPending = false;
            continue;
        }

        ESP_LOGI(TAG_OTA, "OTA complete! v%s written to '%s'. Rebooting in 3s...",
                 g_otaVersion, otaPartition->label);
        vTaskDelay(pdMS_TO_TICKS(3000));
        esp_restart();
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT FreeRTOS Task
// ═══════════════════════════════════════════════════════════════════════════

void mqttTask(void* pvParameters) {
    ESP_LOGI(TAG_MQTT, "Task started on core %d, waiting for WiFi...", xPortGetCoreID());

    // Wait until WiFi is connected before proceeding
    while (!g_wifiConnected) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    ESP_LOGI(TAG_MQTT, "WiFi is up, initializing wolfSSL...");

    // Sync time via NTP (required for TLS certificate date validation)
    ESP_LOGI(TAG_MQTT, "Syncing time via NTP...");
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    struct tm timeinfo;
    int ntpRetries = 0;
    while (!getLocalTime(&timeinfo) && ntpRetries < 15) {
        ESP_LOGD(TAG_MQTT, "Waiting for NTP sync... (%d)", ntpRetries);
        vTaskDelay(pdMS_TO_TICKS(1000));
        ntpRetries++;
    }
    if (ntpRetries < 15) {
        ESP_LOGI(TAG_MQTT, "Time synced: %04d-%02d-%02d %02d:%02d:%02d UTC",
                 timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
                 timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
    } else {
        ESP_LOGW(TAG_MQTT, "NTP sync failed after %d attempts — cert date check may fail", ntpRetries);
    }

    // Initialize wolfSSL context (done once)
    if (!setupWolfSSLContext()) {
        ESP_LOGE(TAG_MQTT, "FATAL: wolfSSL context setup failed. Task halted.");
        vTaskDelete(NULL);
        return;
    }

    // Initialize topic strings
    initTopics();

    // Create the wolfSSL-backed TLS client and MQTT client.
    // Buffer must be large enough for the OTA command payload, which contains
    // a presigned S3 URL (~1500 bytes). 1024 was too small and silently dropped
    // the ota-update command. 4096 gives comfortable headroom.
    static WolfSSLClient tlsClient;
    static MQTTClient mqttClient(4096); // 4096 byte buffer (OTA cmd ~1.5KB)

    mqttClient.begin(MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT, tlsClient);
    mqttClient.onMessage(mqttMessageHandler);
    mqttClient.setKeepAlive(MQTT_KEEPALIVE_SEC);
    mqttClient.setTimeout(10000); // 10 second timeout for read/write operations
    ESP_LOGD(TAG_MQTT, "MQTTClient configured: endpoint=%s, port=%d, keepalive=%ds, timeout=10s",
             MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT, MQTT_KEEPALIVE_SEC);

    // ─── Last Will and Testament (LWT) ──────────────────────────────────────
    static char lwtPayload[128];
    snprintf(lwtPayload, sizeof(lwtPayload),
             "{\"status\":\"offline\",\"reason\":\"unexpected\",\"device\":\"%s\"}",
             g_thingName);
    mqttClient.setWill(g_topicLifecycle, lwtPayload, true, 1);

    unsigned long lastPublishTime = 0;
    unsigned long messageCount = 0;
    int connectAttempts = 0;

    for (;;) {
        // Check WiFi is still up
        if (!g_wifiConnected) {
            ESP_LOGW(TAG_MQTT, "WiFi lost, disconnecting MQTT and waiting...");
            g_mqttConnected = false;
            mqttClient.disconnect();
            connectAttempts = 0; // Reset backoff on WiFi loss
            while (!g_wifiConnected) {
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            ESP_LOGI(TAG_MQTT, "WiFi restored, reconnecting MQTT...");
        }

        // Connect or reconnect to MQTT
        if (!mqttClient.connected()) {
            g_mqttConnected = false;
            ESP_LOGI(TAG_MQTT, "Connecting to PQC Mosquitto broker (thing='%s', attempt=%d)...",
                     g_thingName, connectAttempts + 1);
            ESP_LOGD(TAG_MQTT, "Free heap before connect: %u bytes", ESP.getFreeHeap());

            // Connect with client ID (LWT already set via setWill above)
            bool connected = mqttClient.connect(g_thingName);

            if (connected) {
                g_mqttConnected = true;
                connectAttempts = 0; // Reset backoff on success
                ESP_LOGI(TAG_MQTT, "Connected to PQC Mosquitto broker!");
                ESP_LOGI(TAG_MQTT, "  Client ID: %s", g_thingName);
                ESP_LOGI(TAG_MQTT, "  Broker:    %s:%d", MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT);
                ESP_LOGI(TAG_MQTT, "  Local IP:  %s", WiFi.localIP().toString().c_str());

                // ─── Publish Online Lifecycle Event ──────────────────────────
                // Announce that the device is online. This is the counterpart
                // to the LWT offline message. Retained so new subscribers
                // immediately know the device state.
                char onlinePayload[320];
                snprintf(onlinePayload, sizeof(onlinePayload),
                         "{"
                         "\"status\":\"online\","
                         "\"device\":\"%s\","
                         "\"firmware_version\":\"%s\","
                         "\"pqc_algorithm\":\"%s\","
                         "\"tls_version\":\"%s\","
                         "\"cert_sig_alg\":\"%s\","
                         "\"ip\":\"%s\","
                         "\"uptime_sec\":%lu"
                         "}",
                         g_thingName,
                         FIRMWARE_VERSION,
                         g_tlsKeyExchange,
                         g_tlsVersion,
                         CERT_SIGNATURE_ALG,
                         WiFi.localIP().toString().c_str(),
                         millis() / 1000);

                if (mqttClient.publish(g_topicLifecycle, onlinePayload, true, 1)) {
                    ESP_LOGI(TAG_MQTT, "Published lifecycle: online → %s", g_topicLifecycle);
                } else {
                    ESP_LOGW(TAG_MQTT, "Failed to publish online lifecycle event");
                }

                // ─── Subscribe to Command Topics ────────────────────────────
                // Subscribe to cmd/<thingName>/+ to receive all commands
                if (mqttClient.subscribe(g_topicCmdAll, 1)) {
                    ESP_LOGI(TAG_MQTT, "Subscribed to commands: %s (QoS 1)", g_topicCmdAll);
                } else {
                    ESP_LOGE(TAG_MQTT, "Subscribe to '%s' failed", g_topicCmdAll);
                }
            } else {
                connectAttempts++;
                unsigned long backoffDelay = calculateBackoff(connectAttempts);
                ESP_LOGW(TAG_MQTT, "Connection failed (rc=%d). Backoff: %lu ms (attempt %d)",
                         mqttClient.lastError(), backoffDelay, connectAttempts);
                vTaskDelay(pdMS_TO_TICKS(backoffDelay));
                continue;
            }
        }

        // Process incoming messages
        mqttClient.loop();

        // Publish telemetry at regular intervals
        unsigned long now = millis();
        if (now - lastPublishTime >= MQTT_PUBLISH_INTERVAL_MS) {
            lastPublishTime = now;
            messageCount++;

            // Build a JSON telemetry message
            float chipTempC = temperatureRead(); // Internal die temperature (°C)
            char payload[576];
            snprintf(payload, sizeof(payload),
                     "{"
                     "\"device\":\"%s\","
                     "\"firmware_version\":\"%s\","
                     "\"message_id\":%lu,"
                     "\"timestamp\":%lu,"
                     "\"uptime_sec\":%lu,"
                     "\"wifi_rssi\":%d,"
                     "\"free_heap\":%u,"
                     "\"min_heap\":%u,"
                     "\"chip_temp_c\":%.1f,"
                     "\"pqc_algorithm\":\"%s\","
                     "\"tls_version\":\"%s\","
                     "\"cert_sig_alg\":\"%s\","
                     "\"broker_endpoint\":\"%s:%d\""
                     "}",
                     g_thingName,
                     FIRMWARE_VERSION,
                     messageCount,
                     (unsigned long)(millis() / 1000), // Approximate epoch placeholder
                     millis() / 1000,
                     WiFi.RSSI(),
                     ESP.getFreeHeap(),
                     ESP.getMinFreeHeap(),
                     chipTempC,
                     g_tlsKeyExchange,
                     g_tlsVersion,
                     CERT_SIGNATURE_ALG,
                     MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT);

            if (mqttClient.publish(g_topicTelemetry, payload)) {
                ESP_LOGI(TAG_MQTT, "Published telemetry #%lu → %s", messageCount, g_topicTelemetry);
                ESP_LOGD(TAG_MQTT, "Payload: %s", payload);
            } else {
                ESP_LOGE(TAG_MQTT, "Publish failed for telemetry #%lu", messageCount);
            }
        }

        // Yield to other tasks
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  Arduino Setup & Loop
// ═══════════════════════════════════════════════════════════════════════════

// ─── Blink Task ─────────────────────────────────────────────────────────────
// Visual heartbeat indicator. Blink rate indicates state:
//   Fast blink (200ms): booting / connecting
//   Slow blink (1000ms): connected and running
//   Solid ON: error state
void blinkTask(void* pvParameters) {
    pinMode(LED_PIN, OUTPUT);
    for (;;) {
        digitalWrite(LED_PIN, HIGH);
        vTaskDelay(pdMS_TO_TICKS(500));
        digitalWrite(LED_PIN, LOW);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void setup() {
    // Start blink task immediately — visual indicator the device is alive
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);  // LED on right away
    xTaskCreatePinnedToCore(blinkTask, "BlinkTask", BLINK_TASK_STACK_SIZE,
                            NULL, BLINK_TASK_PRIORITY, &g_blinkTaskHandle, 0);

    Serial.begin(115200);

    // Give USB CDC time to enumerate — don't block, just wait
    // The ESP32-S3 HWCDC needs time for the host to recognize the port.
    // We proceed regardless after 10 seconds so WiFi/MQTT tasks can start.
    unsigned long serialStart = millis();
    while (!Serial && (millis() - serialStart < 10000)) {
        delay(100);
    }

    // ─── Configure Espressif Log Levels ─────────────────────────────────────
    // Set the default log level for all tags. Change LOG_LEVEL in secrets.h
    // to control verbosity:
    //   ESP_LOG_NONE (0)    - No output
    //   ESP_LOG_ERROR (1)   - Errors only
    //   ESP_LOG_WARN (2)    - Errors + warnings
    //   ESP_LOG_INFO (3)    - Normal operational messages (default)
    //   ESP_LOG_DEBUG (4)   - Detailed diagnostic information
    //   ESP_LOG_VERBOSE (5) - Everything, including I/O byte counts
    esp_log_level_set("*", (esp_log_level_t)LOG_LEVEL);

    // Override per-module if needed (uncomment and adjust):
    // esp_log_level_set(TAG_TLS, ESP_LOG_DEBUG);
    // esp_log_level_set(TAG_WIFI, ESP_LOG_VERBOSE);
    // esp_log_level_set(TAG_MQTT, ESP_LOG_INFO);
    // esp_log_level_set(TAG_WOLFSSL, ESP_LOG_DEBUG);

    // Create synchronization primitives
    g_wifiMutex = xSemaphoreCreateMutex();
    if (!g_wifiMutex) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create WiFi mutex");
        while (1) { delay(1000); }
    }

    // Seed random number generator for backoff jitter
    randomSeed(analogRead(0) ^ (uint32_t)(ESP.getEfuseMac() >> 16));

    // Derive device identity from MAC address FIRST (needed for banner)
    initDeviceIdentity();

    ESP_LOGI(TAG_MAIN, "");
    ESP_LOGI(TAG_MAIN, "══════════════════════════════════════════════════");
    ESP_LOGI(TAG_MAIN, "  ESP32 AWS IoT Core — CLASSICAL TLS (Pre-PQC)");
    ESP_LOGI(TAG_MAIN, "  Firmware v1.0.0 — Awaiting PQC Migration");
    ESP_LOGI(TAG_MAIN, "══════════════════════════════════════════════════");
    ESP_LOGI(TAG_MAIN, "");
    ESP_LOGI(TAG_MAIN, "┌──────────────────────────────────────────────┐");
    ESP_LOGI(TAG_MAIN, "│  YOUR DEVICE NAME:  %-24s │", g_thingName);
    ESP_LOGI(TAG_MAIN, "└──────────────────────────────────────────────┘");
    ESP_LOGI(TAG_MAIN, "");

    // Print chip info
    ESP_LOGI(TAG_MAIN, "Chip:       %s Rev %d", ESP.getChipModel(), ESP.getChipRevision());
    ESP_LOGI(TAG_MAIN, "CPU Freq:   %d MHz", ESP.getCpuFreqMHz());
    ESP_LOGI(TAG_MAIN, "Flash:      %d MB", ESP.getFlashChipSize() / (1024 * 1024));
    ESP_LOGI(TAG_MAIN, "Free Heap:  %u bytes", ESP.getFreeHeap());
    ESP_LOGI(TAG_MAIN, "PSRAM:      %u bytes", ESP.getPsramSize());
    ESP_LOGI(TAG_MAIN, "Log Level:  %d", LOG_LEVEL);

    // Determine number of cores for task pinning
    // Single-core ESP32 variants (C3, S2) use core 0 for everything
    int mqttCore = 1;
    #if CONFIG_FREERTOS_UNICORE
        mqttCore = 0;
        ESP_LOGD(TAG_MAIN, "Unicore detected, all tasks on core 0");
    #else
        ESP_LOGD(TAG_MAIN, "Dual-core detected, WiFi=core0, MQTT=core1");
    #endif

    // Create WiFi management task
    BaseType_t rc = xTaskCreatePinnedToCore(
        wifiTask,
        "WiFiTask",
        WIFI_TASK_STACK_SIZE,
        NULL,
        WIFI_TASK_PRIORITY,
        &g_wifiTaskHandle,
        WIFI_TASK_CORE
    );
    if (rc != pdPASS) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create WiFi task (rc=%d)", rc);
        while (1) { delay(1000); }
    }
    ESP_LOGD(TAG_MAIN, "WiFi task created (stack=%d, priority=%d, core=%d)",
             WIFI_TASK_STACK_SIZE, WIFI_TASK_PRIORITY, WIFI_TASK_CORE);

    // Create MQTT + PQC TLS task
    rc = xTaskCreatePinnedToCore(
        mqttTask,
        "MQTTTask",
        MQTT_TASK_STACK_SIZE,
        NULL,
        MQTT_TASK_PRIORITY,
        &g_mqttTaskHandle,
        mqttCore
    );
    if (rc != pdPASS) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create MQTT task (rc=%d)", rc);
        while (1) { delay(1000); }
    }
    ESP_LOGD(TAG_MAIN, "MQTT task created (stack=%d, priority=%d, core=%d)",
             MQTT_TASK_STACK_SIZE, MQTT_TASK_PRIORITY, mqttCore);

    // Create OTA download task (waits on semaphore for ota-update commands)
    g_otaSemaphore = xSemaphoreCreateBinary();
    if (g_otaSemaphore == NULL) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create OTA semaphore");
        while (1) { delay(1000); }
    }
    rc = xTaskCreatePinnedToCore(
        otaTask,
        "OTATask",
        OTA_TASK_STACK_SIZE,
        NULL,
        OTA_TASK_PRIORITY,
        &g_otaTaskHandle,
        mqttCore
    );
    if (rc != pdPASS) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create OTA task (rc=%d)", rc);
        while (1) { delay(1000); }
    }
    ESP_LOGD(TAG_MAIN, "OTA task created (stack=%d, priority=%d, core=%d)",
             OTA_TASK_STACK_SIZE, OTA_TASK_PRIORITY, mqttCore);

    ESP_LOGI(TAG_MAIN, "All tasks created. System running.");
}

void loop() {
    // Main loop acts as a watchdog / status reporter.
    // All real work happens in the FreeRTOS tasks.

    static unsigned long lastStatusTime = 0;
    unsigned long now = millis();

    if (now - lastStatusTime >= 60000) { // Status every 60 seconds
        lastStatusTime = now;

        // Get current timestamp
        struct tm timeinfo;
        char timebuf[24] = "no-ntp";
        if (getLocalTime(&timeinfo)) {
            snprintf(timebuf, sizeof(timebuf), "%04d-%02d-%02d %02d:%02d:%02d",
                     timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
                     timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
        }

        ESP_LOGI(TAG_MAIN, "─── System Status ─────────────────────────────");
        ESP_LOGI(TAG_MAIN, "  Time:       %s", timebuf);
        ESP_LOGI(TAG_MAIN, "  Timezone:   %s", getenv("TZ") ? getenv("TZ") : "UTC (default)");
        ESP_LOGI(TAG_MAIN, "  Device:     %s", g_thingName);
        ESP_LOGI(TAG_MAIN, "  Uptime:     %lu sec", now / 1000);
        ESP_LOGI(TAG_MAIN, "  WiFi:       %s (RSSI: %d dBm)",
                 g_wifiConnected ? "Connected" : "Disconnected", WiFi.RSSI());
        ESP_LOGI(TAG_MAIN, "  IP:         %s", WiFi.localIP().toString().c_str());
        ESP_LOGI(TAG_MAIN, "  MQTT:       %s",
                 g_mqttConnected ? "Connected" : "Disconnected");
        ESP_LOGI(TAG_MAIN, "  Free Heap:  %u bytes", ESP.getFreeHeap());
        ESP_LOGI(TAG_MAIN, "  Min Heap:   %u bytes", ESP.getMinFreeHeap());
        ESP_LOGI(TAG_MAIN, "───────────────────────────────────────────────");

        ESP_LOGD(TAG_MAIN, "  WiFi task stack HWM: %u bytes",
                 uxTaskGetStackHighWaterMark(g_wifiTaskHandle) * sizeof(StackType_t));
        ESP_LOGD(TAG_MAIN, "  MQTT task stack HWM: %u bytes",
                 uxTaskGetStackHighWaterMark(g_mqttTaskHandle) * sizeof(StackType_t));
    }

    delay(1000);
}
