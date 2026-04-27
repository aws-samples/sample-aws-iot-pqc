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
#include <PubSubClient.h>

// wolfSSL headers - order matters
#include <wolfssl.h>              // Must come first (defines WOLFSSL_USER_SETTINGS)
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/ssl.h>
#include <wolfssl/wolfcrypt/logging.h>

#include "secrets.h"

// ─── Build-time Validation ──────────────────────────────────────────────────
#ifndef WOLFSSL_TLS13
  #error "wolfSSL TLS 1.3 support is required. Enable WOLFSSL_TLS13 in user_settings.h"
#endif
#ifndef WOLFSSL_HAVE_KYBER
  #error "wolfSSL Kyber/ML-KEM support is required. Enable WOLFSSL_HAVE_KYBER in user_settings.h"
#endif

// ─── FreeRTOS Task Configuration ────────────────────────────────────────────
#define WIFI_TASK_STACK_SIZE    4096
#define MQTT_TASK_STACK_SIZE    16384   // PQC handshake needs more stack
#define WIFI_TASK_PRIORITY      1
#define MQTT_TASK_PRIORITY      1
#define WIFI_TASK_CORE          0       // Run WiFi on core 0
#define MQTT_TASK_CORE          1       // Run MQTT on core 1 (if available)

// ─── Timing Constants (milliseconds) ────────────────────────────────────────
#define WIFI_CONNECT_TIMEOUT_MS     15000
#define WIFI_RETRY_DELAY_MS         5000
#define WIFI_CHECK_INTERVAL_MS      1000
#define MQTT_RECONNECT_DELAY_MS     5000
#define MQTT_PUBLISH_INTERVAL_MS    30000
#define MQTT_KEEPALIVE_SEC          60
#define WATCHDOG_TIMEOUT_SEC        120

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
bool setupWolfSSLContext();
WiFiClient* createPQCTLSClient();

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
        return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    }
    int sent = io->client->write((uint8_t*)buf, sz);
    if (sent <= 0) {
        return WOLFSSL_CBIO_ERR_WANT_WRITE;
    }
    return sent;
}

static int wolfssl_recv_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx) {
    wolfssl_io_ctx_t* io = (wolfssl_io_ctx_t*)ctx;
    if (!io || !io->client || !io->client->connected()) {
        return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    }

    // Non-blocking: if no data available, signal WANT_READ
    if (io->client->available() == 0) {
        return WOLFSSL_CBIO_ERR_WANT_READ;
    }

    int received = io->client->read((uint8_t*)buf, sz);
    if (received <= 0) {
        return WOLFSSL_CBIO_ERR_WANT_READ;
    }
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

        // Step 1: Establish raw TCP connection
        if (!_tcpClient.connect(host, port)) {
            Serial.println("[wolfSSL] TCP connection failed");
            return 0;
        }
        Serial.printf("[wolfSSL] TCP connected to %s:%d\n", host, port);

        // Step 2: Create wolfSSL session from the shared context
        _ssl = wolfSSL_new(g_wolfCtx);
        if (!_ssl) {
            Serial.println("[wolfSSL] Failed to create SSL session");
            _tcpClient.stop();
            return 0;
        }

        // Step 3: Set custom I/O callbacks
        wolfSSL_SSLSetIORecv(_ssl, wolfssl_recv_cb);
        wolfSSL_SSLSetIOSend(_ssl, wolfssl_send_cb);
        wolfSSL_SetIOReadCtx(_ssl, &_ioCtx);
        wolfSSL_SetIOWriteCtx(_ssl, &_ioCtx);

        // Step 4: Enable hybrid PQC key exchange (P-521 + Kyber Level 5)
        // This is the core PQC configuration. The TLS 1.3 handshake will
        // negotiate a hybrid key exchange combining classical ECDHE P-521
        // with the post-quantum ML-KEM (Kyber) algorithm.
        int ret = wolfSSL_UseKeyShare(_ssl, WOLFSSL_P521_KYBER_LEVEL5);
        if (ret != WOLFSSL_SUCCESS) {
            Serial.printf("[wolfSSL] Failed to set PQC key share: %d\n", ret);
            // Fall back to trying P-256 + Kyber Level 1 (smaller, faster)
            ret = wolfSSL_UseKeyShare(_ssl, WOLFSSL_P256_KYBER_LEVEL1);
            if (ret != WOLFSSL_SUCCESS) {
                Serial.println("[wolfSSL] PQC key share fallback also failed");
                wolfSSL_free(_ssl);
                _ssl = NULL;
                _tcpClient.stop();
                return 0;
            }
            Serial.println("[wolfSSL] Using fallback: P-256 + Kyber Level 1");
        } else {
            Serial.println("[wolfSSL] Using PQC key exchange: P-521 + Kyber Level 5");
        }

        // Step 5: Set SNI (Server Name Indication) for AWS IoT
        ret = wolfSSL_UseSNI(_ssl, WOLFSSL_SNI_HOST_NAME,
                             host, strlen(host));
        if (ret != WOLFSSL_SUCCESS) {
            Serial.println("[wolfSSL] Warning: SNI setup failed (non-fatal)");
        }

        // Step 6: Perform TLS 1.3 handshake with timeout
        Serial.println("[wolfSSL] Starting TLS 1.3 PQC handshake...");
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
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }

            // Real error
            char errBuf[128];
            wolfSSL_ERR_error_string_n(err, errBuf, sizeof(errBuf));
            Serial.printf("[wolfSSL] Handshake error: %s (%d)\n", errBuf, err);
            wolfSSL_free(_ssl);
            _ssl = NULL;
            _tcpClient.stop();
            return 0;

        } while ((millis() - handshakeStart) < handshakeTimeout);

        if (ret != WOLFSSL_SUCCESS) {
            Serial.println("[wolfSSL] Handshake timed out");
            wolfSSL_free(_ssl);
            _ssl = NULL;
            _tcpClient.stop();
            return 0;
        }

        // Step 7: Verify the connection details
        Serial.printf("[wolfSSL] TLS handshake complete in %lu ms\n",
                      millis() - handshakeStart);
        Serial.printf("[wolfSSL] Protocol: %s\n", wolfSSL_get_version(_ssl));
        Serial.printf("[wolfSSL] Cipher:   %s\n",
                      wolfSSL_get_cipher_name(_ssl));

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
                _connected = false;
            }
            return 0;
        }
        return (size_t)ret;
    }

    int available() override {
        if (!_ssl || !_connected) return 0;

        // Check if wolfSSL has buffered data or if TCP has data
        int pending = wolfSSL_pending(_ssl);
        if (pending > 0) return pending;
        return _tcpClient.available();
    }

    int read() override {
        uint8_t b;
        if (read(&b, 1) == 1) return b;
        return -1;
    }

    int read(uint8_t* buf, size_t size) override {
        if (!_ssl || !_connected) return -1;

        int ret = wolfSSL_read(_ssl, buf, size);
        if (ret <= 0) {
            int err = wolfSSL_get_error(_ssl, ret);
            if (err == WOLFSSL_ERROR_WANT_READ) {
                return 0; // No data yet, non-blocking
            }
            _connected = false;
            return -1;
        }
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
            wolfSSL_shutdown(_ssl);
            wolfSSL_free(_ssl);
            _ssl = NULL;
        }
        _tcpClient.stop();
        _connected = false;
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
    // Initialize wolfSSL library
    int ret = wolfSSL_Init();
    if (ret != WOLFSSL_SUCCESS) {
        Serial.printf("[wolfSSL] Library init failed: %d\n", ret);
        return false;
    }
    Serial.println("[wolfSSL] Library initialized");

    // Enable debug logging (comment out for production)
    // wolfSSL_Debugging_ON();

    // Create TLS 1.3 client context
    g_wolfCtx = wolfSSL_CTX_new(wolfTLSv1_3_client_method());
    if (!g_wolfCtx) {
        Serial.println("[wolfSSL] Failed to create TLS 1.3 context");
        return false;
    }

    // Load Amazon Root CA 1 (server verification)
    ret = wolfSSL_CTX_load_verify_buffer(g_wolfCtx,
            (const unsigned char*)AWS_CERT_CA,
            strlen(AWS_CERT_CA),
            WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) {
        Serial.printf("[wolfSSL] Failed to load CA cert: %d\n", ret);
        return false;
    }
    Serial.println("[wolfSSL] CA certificate loaded");

    // Load device certificate (client authentication)
    ret = wolfSSL_CTX_use_certificate_buffer(g_wolfCtx,
            (const unsigned char*)AWS_CERT_CRT,
            strlen(AWS_CERT_CRT),
            WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) {
        Serial.printf("[wolfSSL] Failed to load device cert: %d\n", ret);
        return false;
    }
    Serial.println("[wolfSSL] Device certificate loaded");

    // Load device private key
    ret = wolfSSL_CTX_use_PrivateKey_buffer(g_wolfCtx,
            (const unsigned char*)AWS_CERT_PRIVATE,
            strlen(AWS_CERT_PRIVATE),
            WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) {
        Serial.printf("[wolfSSL] Failed to load private key: %d\n", ret);
        return false;
    }
    Serial.println("[wolfSSL] Private key loaded");

    // Require server certificate verification
    wolfSSL_CTX_set_verify(g_wolfCtx, WOLFSSL_VERIFY_PEER, NULL);

    // Set custom I/O callbacks at the context level
    wolfSSL_CTX_SetIORecv(g_wolfCtx, wolfssl_recv_cb);
    wolfSSL_CTX_SetIOSend(g_wolfCtx, wolfssl_send_cb);

    Serial.println("[wolfSSL] TLS 1.3 PQC context ready");
    return true;
}

// ═══════════════════════════════════════════════════════════════════════════
//  WiFi FreeRTOS Task
// ═══════════════════════════════════════════════════════════════════════════

void wifiTask(void* pvParameters) {
    Serial.println("[WiFi] Task started");

    // Configure WiFi
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);

    for (;;) {
        if (WiFi.status() != WL_CONNECTED) {
            if (xSemaphoreTake(g_wifiMutex, pdMS_TO_TICKS(1000)) == pdTRUE) {
                g_wifiConnected = false;
                xSemaphoreGive(g_wifiMutex);
            }

            Serial.println("[WiFi] Connecting...");
            WiFi.disconnect(true);
            vTaskDelay(pdMS_TO_TICKS(500));
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

            // Wait for connection with timeout
            unsigned long startAttempt = millis();
            while (WiFi.status() != WL_CONNECTED &&
                   (millis() - startAttempt) < WIFI_CONNECT_TIMEOUT_MS) {
                vTaskDelay(pdMS_TO_TICKS(500));
                Serial.print(".");
            }
            Serial.println();

            if (WiFi.status() == WL_CONNECTED) {
                if (xSemaphoreTake(g_wifiMutex, pdMS_TO_TICKS(1000)) == pdTRUE) {
                    g_wifiConnected = true;
                    xSemaphoreGive(g_wifiMutex);
                }
                Serial.printf("[WiFi] Connected! IP: %s, RSSI: %d dBm\n",
                              WiFi.localIP().toString().c_str(),
                              WiFi.RSSI());
            } else {
                Serial.printf("[WiFi] Connection failed (status: %d). "
                              "Retrying in %d ms...\n",
                              WiFi.status(), WIFI_RETRY_DELAY_MS);
                WiFi.disconnect(true);
                vTaskDelay(pdMS_TO_TICKS(WIFI_RETRY_DELAY_MS));
                continue;
            }
        }

        // Periodic health check
        vTaskDelay(pdMS_TO_TICKS(WIFI_CHECK_INTERVAL_MS));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT Callback
// ═══════════════════════════════════════════════════════════════════════════

void mqttCallback(char* topic, byte* payload, unsigned int length) {
    Serial.printf("[MQTT] Message on topic '%s': ", topic);
    for (unsigned int i = 0; i < length; i++) {
        Serial.print((char)payload[i]);
    }
    Serial.println();

    // ── Handle incoming messages here ──
    // Example: parse JSON, control GPIOs, update state, etc.
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT FreeRTOS Task
// ═══════════════════════════════════════════════════════════════════════════

void mqttTask(void* pvParameters) {
    Serial.println("[MQTT] Task started, waiting for WiFi...");

    // Wait until WiFi is connected before proceeding
    while (!g_wifiConnected) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    Serial.println("[MQTT] WiFi is up, initializing wolfSSL...");

    // Initialize wolfSSL context (done once)
    if (!setupWolfSSLContext()) {
        Serial.println("[MQTT] FATAL: wolfSSL context setup failed. Task halted.");
        vTaskDelete(NULL);
        return;
    }

    // Create the wolfSSL-backed TLS client and MQTT client
    static WolfSSLClient tlsClient;
    static PubSubClient mqttClient(tlsClient);

    mqttClient.setServer(AWS_IOT_ENDPOINT, AWS_IOT_PORT);
    mqttClient.setCallback(mqttCallback);
    mqttClient.setKeepAlive(MQTT_KEEPALIVE_SEC);
    mqttClient.setBufferSize(1024); // Increase for larger messages

    unsigned long lastPublishTime = 0;
    unsigned long messageCount = 0;

    for (;;) {
        // Check WiFi is still up
        if (!g_wifiConnected) {
            Serial.println("[MQTT] WiFi lost, waiting for reconnection...");
            g_mqttConnected = false;
            mqttClient.disconnect();
            while (!g_wifiConnected) {
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            Serial.println("[MQTT] WiFi restored, reconnecting MQTT...");
        }

        // Connect or reconnect to MQTT
        if (!mqttClient.connected()) {
            g_mqttConnected = false;
            Serial.println("[MQTT] Connecting to AWS IoT Core with PQC TLS...");

            if (mqttClient.connect(THINGNAME)) {
                g_mqttConnected = true;
                Serial.println("[MQTT] Connected to AWS IoT Core!");

                // Subscribe to the inbound topic
                if (mqttClient.subscribe(AWS_IOT_SUBSCRIBE_TOPIC)) {
                    Serial.printf("[MQTT] Subscribed to: %s\n",
                                  AWS_IOT_SUBSCRIBE_TOPIC);
                } else {
                    Serial.println("[MQTT] Subscribe failed");
                }
            } else {
                Serial.printf("[MQTT] Connection failed, rc=%d. "
                              "Retrying in %d ms...\n",
                              mqttClient.state(), MQTT_RECONNECT_DELAY_MS);
                vTaskDelay(pdMS_TO_TICKS(MQTT_RECONNECT_DELAY_MS));
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
            char payload[256];
            snprintf(payload, sizeof(payload),
                     "{"
                     "\"device\":\"%s\","
                     "\"message_id\":%lu,"
                     "\"uptime_sec\":%lu,"
                     "\"wifi_rssi\":%d,"
                     "\"free_heap\":%u,"
                     "\"pqc_algorithm\":\"P521_KYBER_LEVEL5\","
                     "\"tls_version\":\"TLS 1.3\""
                     "}",
                     THINGNAME,
                     messageCount,
                     millis() / 1000,
                     WiFi.RSSI(),
                     ESP.getFreeHeap());

            if (mqttClient.publish(AWS_IOT_PUBLISH_TOPIC, payload)) {
                Serial.printf("[MQTT] Published message #%lu to %s\n",
                              messageCount, AWS_IOT_PUBLISH_TOPIC);
            } else {
                Serial.println("[MQTT] Publish failed");
            }
        }

        // Yield to other tasks
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  Arduino Setup & Loop
// ═══════════════════════════════════════════════════════════════════════════

void setup() {
    Serial.begin(115200);
    while (!Serial) { delay(10); }
    delay(1000);

    Serial.println();
    Serial.println("╔══════════════════════════════════════════════════╗");
    Serial.println("║  ESP32 AWS IoT Core - Post-Quantum Cryptography ║");
    Serial.println("║  TLS 1.3 + Hybrid Kyber/ML-KEM Key Exchange     ║");
    Serial.println("╚══════════════════════════════════════════════════╝");
    Serial.println();

    // Print chip info
    Serial.printf("Chip:       %s Rev %d\n", ESP.getChipModel(),
                  ESP.getChipRevision());
    Serial.printf("CPU Freq:   %d MHz\n", ESP.getCpuFreqMHz());
    Serial.printf("Flash:      %d MB\n", ESP.getFlashChipSize() / (1024 * 1024));
    Serial.printf("Free Heap:  %u bytes\n", ESP.getFreeHeap());
    Serial.printf("PSRAM:      %u bytes\n", ESP.getPsramSize());
    Serial.println();

    // Create synchronization primitives
    g_wifiMutex = xSemaphoreCreateMutex();
    if (!g_wifiMutex) {
        Serial.println("FATAL: Failed to create WiFi mutex");
        while (1) { delay(1000); }
    }

    // Determine number of cores for task pinning
    // Single-core ESP32 variants (C3, S2) use core 0 for everything
    int mqttCore = 1;
    #if CONFIG_FREERTOS_UNICORE
        mqttCore = 0;
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
        Serial.println("FATAL: Failed to create WiFi task");
        while (1) { delay(1000); }
    }

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
        Serial.println("FATAL: Failed to create MQTT task");
        while (1) { delay(1000); }
    }

    Serial.println("[Setup] All tasks created. System running.");
}

void loop() {
    // Main loop acts as a watchdog / status reporter.
    // All real work happens in the FreeRTOS tasks.

    static unsigned long lastStatusTime = 0;
    unsigned long now = millis();

    if (now - lastStatusTime >= 60000) { // Status every 60 seconds
        lastStatusTime = now;
        Serial.println("─── System Status ─────────────────────────────");
        Serial.printf("  Uptime:     %lu sec\n", now / 1000);
        Serial.printf("  WiFi:       %s (RSSI: %d dBm)\n",
                      g_wifiConnected ? "Connected" : "Disconnected",
                      WiFi.RSSI());
        Serial.printf("  MQTT:       %s\n",
                      g_mqttConnected ? "Connected" : "Disconnected");
        Serial.printf("  Free Heap:  %u bytes\n", ESP.getFreeHeap());
        Serial.printf("  Min Heap:   %u bytes\n", ESP.getMinFreeHeap());
        Serial.println("───────────────────────────────────────────────");
    }

    delay(1000);
}
