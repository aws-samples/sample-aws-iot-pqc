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
 *   2. PubSubClient     (by Nick O'Leary) - lightweight MQTT client
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

// ─── Log Tags ───────────────────────────────────────────────────────────────
// Each component gets its own tag for per-module log level control.
// Use esp_log_level_set(TAG, level) to adjust at runtime.
static const char* TAG_MAIN    = "main";
static const char* TAG_WIFI    = "wifi";
static const char* TAG_MQTT    = "mqtt";
static const char* TAG_TLS     = "tls";
static const char* TAG_WOLFSSL = "wolfssl";

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
#define WIFI_TASK_PRIORITY      1
#define MQTT_TASK_PRIORITY      1
#define BLINK_TASK_PRIORITY     0       // Lowest priority — just visual indicator
#define WIFI_TASK_CORE          0       // Run WiFi on core 0
#define MQTT_TASK_CORE          1       // Run MQTT on core 1 (if available)

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

// wolfSSL context (shared, thread-safe after init)
static WOLFSSL_CTX* g_wolfCtx = NULL;

// Task handles for watchdog monitoring
static TaskHandle_t g_wifiTaskHandle = NULL;
static TaskHandle_t g_mqttTaskHandle = NULL;

// ─── Forward Declarations ───────────────────────────────────────────────────
void wifiTask(void* pvParameters);
void mqttTask(void* pvParameters);
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
    if (!io || !io->client || !io->client->connected()) {
        ESP_LOGD(TAG_TLS, "recv_cb: connection closed");
        return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    }

    // Non-blocking: if no data available, signal WANT_READ
    if (io->client->available() == 0) {
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
// and exposes the Arduino Client interface so PubSubClient can use it.

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

        // Step 4: Enable hybrid PQC key exchange (P-256 + ML-KEM-768)
        // This is the core PQC configuration. The TLS 1.3 handshake will
        // negotiate a hybrid key exchange combining classical ECDHE P-256
        // with the post-quantum ML-KEM-768 algorithm.
        // Uses IANA code point 4587 (SecP256r1MLKEM768) for interoperability
        // with OpenSSL 3.5+ based servers (e.g., Mosquitto PQC broker).
        int ret = wolfSSL_UseKeyShare(_ssl, WOLFSSL_SECP256R1MLKEM768);
        if (ret != WOLFSSL_SUCCESS) {
            ESP_LOGW(TAG_TLS, "P-256 + ML-KEM-768 key share failed (rc=%d), trying fallback", ret);
            // Fall back to X25519 + ML-KEM-768 (also IANA-standard, code point 4588)
            ret = wolfSSL_UseKeyShare(_ssl, WOLFSSL_X25519MLKEM768);
            if (ret != WOLFSSL_SUCCESS) {
                ESP_LOGE(TAG_TLS, "PQC key share fallback (X25519 + ML-KEM-768) also failed (rc=%d)", ret);
                wolfSSL_free(_ssl);
                _ssl = NULL;
                _tcpClient.stop();
                return 0;
            }
            ESP_LOGI(TAG_TLS, "Using fallback PQC: X25519 + ML-KEM-768");
        } else {
            ESP_LOGI(TAG_TLS, "Using PQC key exchange: P-256 + ML-KEM-768 (SecP256r1MLKEM768)");
        }

        // Step 5: Set SNI (Server Name Indication) for AWS IoT
        ret = wolfSSL_UseSNI(_ssl, WOLFSSL_SNI_HOST_NAME,
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

        // Step 7: Verify the connection details
        unsigned long handshakeDuration = millis() - handshakeStart;
        ESP_LOGI(TAG_TLS, "TLS handshake complete in %lu ms", handshakeDuration);
        ESP_LOGI(TAG_TLS, "Protocol: %s, Cipher: %s",
                 wolfSSL_get_version(_ssl), wolfSSL_get_cipher_name(_ssl));
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
        // If so, we report 1 byte available to trigger PubSubClient
        // to call read(), which will invoke wolfSSL_read() to decrypt.
        if (_tcpClient.available() > 0) return 1;

        return 0;
    }

    int read() override {
        uint8_t b;
        // PubSubClient reads one byte at a time during readPacket().
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

// MQTTClient message handler (wraps mqttCallback)
void mqttMessageHandler(String &topic, String &payload) {
    ESP_LOGI(TAG_MQTT, "Message received on topic '%s' (%d bytes)", topic.c_str(), payload.length());
    ESP_LOGD(TAG_MQTT, "Payload: %s", payload.c_str());
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

    // Create the wolfSSL-backed TLS client and MQTT client
    static WolfSSLClient tlsClient;
    static MQTTClient mqttClient(1024); // 1024 byte buffer

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
                char onlinePayload[256];
                snprintf(onlinePayload, sizeof(onlinePayload),
                         "{"
                         "\"status\":\"online\","
                         "\"device\":\"%s\","
                         "\"firmware_version\":\"2.0.0\","
                         "\"pqc_algorithm\":\"SECP256R1_MLKEM768\","
                         "\"ip\":\"%s\","
                         "\"uptime_sec\":%lu"
                         "}",
                         g_thingName,
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
            char payload[384];
            snprintf(payload, sizeof(payload),
                     "{"
                     "\"device\":\"%s\","
                     "\"message_id\":%lu,"
                     "\"timestamp\":%lu,"
                     "\"uptime_sec\":%lu,"
                     "\"wifi_rssi\":%d,"
                     "\"free_heap\":%u,"
                     "\"min_heap\":%u,"
                     "\"chip_temp_c\":%.1f,"
                     "\"pqc_algorithm\":\"SECP256R1_MLKEM768\","
                     "\"tls_version\":\"TLS 1.3\""
                     "}",
                     g_thingName,
                     messageCount,
                     (unsigned long)(millis() / 1000), // Approximate epoch placeholder
                     millis() / 1000,
                     WiFi.RSSI(),
                     ESP.getFreeHeap(),
                     ESP.getMinFreeHeap(),
                     chipTempC);

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
        vTaskDelay(pdMS_TO_TICKS(200));
        digitalWrite(LED_PIN, LOW);
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

void setup() {
    // Start blink task immediately — visual indicator the device is alive
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);  // LED on right away
    xTaskCreatePinnedToCore(blinkTask, "BlinkTask", BLINK_TASK_STACK_SIZE,
                            NULL, BLINK_TASK_PRIORITY, NULL, 0);

    Serial.begin(115200);

    // Give USB CDC time to enumerate — don't block, just wait
    // The ESP32-S3 HWCDC needs time for the host to recognize the port.
    // We proceed regardless after 10 seconds so WiFi/MQTT tasks can start.
    unsigned long serialStart = millis();
    while (!Serial && (millis() - serialStart < 10000)) {
        delay(100);
    }

    // Print banner using Serial directly (works once CDC is ready)
    Serial.println();
    Serial.println("══════════════════════════════════════════════════");
    Serial.println("  ESP32 AWS IoT Core - Post-Quantum Cryptography");
    Serial.println("  TLS 1.3 + Hybrid ML-KEM Key Exchange");
    Serial.println("══════════════════════════════════════════════════");
    Serial.println();
    Serial.printf("  Serial ready after %lu ms\n", millis() - serialStart);
    Serial.flush();

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
    ESP_LOGI(TAG_MAIN, "  ESP32 AWS IoT Core - Post-Quantum Cryptography");
    ESP_LOGI(TAG_MAIN, "  TLS 1.3 + Hybrid ML-KEM Key Exchange");
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

    ESP_LOGI(TAG_MAIN, "All tasks created. System running.");
}

void loop() {
    // Main loop acts as a watchdog / status reporter.
    // All real work happens in the FreeRTOS tasks.

    static unsigned long lastStatusTime = 0;
    static unsigned long lastHeartbeat = 0;
    unsigned long now = millis();

    // Serial heartbeat every 10s with timestamp
    if (now - lastHeartbeat >= 10000) {
        lastHeartbeat = now;
        if (Serial) {
            struct tm timeinfo;
            if (getLocalTime(&timeinfo)) {
                Serial.printf("[%04d-%02d-%02d %02d:%02d:%02d] WiFi:%s MQTT:%s Heap:%u RSSI:%d\n",
                    timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
                    timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec,
                    g_wifiConnected ? "OK" : "NO",
                    g_mqttConnected ? "OK" : "NO",
                    ESP.getFreeHeap(),
                    WiFi.RSSI());
            } else {
                Serial.printf("[%lus] WiFi:%s MQTT:%s Heap:%u RSSI:%d\n",
                    now / 1000,
                    g_wifiConnected ? "OK" : "NO",
                    g_mqttConnected ? "OK" : "NO",
                    ESP.getFreeHeap(),
                    WiFi.RSSI());
            }
            Serial.flush();
        }
    }

    if (now - lastStatusTime >= 60000) { // Status every 60 seconds
        lastStatusTime = now;
        ESP_LOGI(TAG_MAIN, "─── System Status ─────────────────────────────");
        ESP_LOGI(TAG_MAIN, "  Uptime:     %lu sec", now / 1000);
        ESP_LOGI(TAG_MAIN, "  WiFi:       %s (RSSI: %d dBm)",
                 g_wifiConnected ? "Connected" : "Disconnected", WiFi.RSSI());
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
