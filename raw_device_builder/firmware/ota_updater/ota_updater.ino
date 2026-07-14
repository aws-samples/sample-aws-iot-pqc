/*
 * ota_updater.ino — Production Device Firmware (Firmware 0)
 *
 * Full-featured ESP32 firmware for production-registered devices.
 * Connects to the Mosquitto PQC broker via wolfSSL TLS 1.3 + ML-KEM,
 * publishes telemetry, and supports OTA firmware updates downloaded
 * from presigned S3 URLs using the wolfSSL HTTPS stack.
 *
 * OTA Flow:
 *   1. Device receives cmd/<thing>/ota: {"action":"ota-update","url":"...","version":"x.y.z","size":N}
 *   2. Downloads firmware from presigned S3 URL via HTTPS (wolfSSL)
 *   3. Writes to OTA partition using esp_ota API
 *   4. Validates and sets boot partition
 *   5. Reboots into new firmware
 *
 * Required Libraries:
 *   1. wolfSSL (by wolfSSL Inc.) - TLS 1.3 + PQC
 *   2. MQTT (by Joel Gaehwiler) - Arduino MQTT client
 *
 * Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

#include <WiFi.h>
#include <MQTT.h>
#include <esp_log.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>

// wolfSSL headers
#include <wolfssl.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/ssl.h>
#include <wolfssl/wolfcrypt/logging.h>

#include "secrets.h"

// ─── Log Tags ───────────────────────────────────────────────────────────────
static const char* TAG_MAIN    = "main";
static const char* TAG_WIFI    = "wifi";
static const char* TAG_MQTT    = "mqtt";
static const char* TAG_TLS     = "tls";
static const char* TAG_WOLFSSL = "wolfssl";
static const char* TAG_OTA     = "ota";

// ─── Build-time Validation ──────────────────────────────────────────────────
#ifndef WOLFSSL_TLS13
  #error "wolfSSL TLS 1.3 required"
#endif

// ─── FreeRTOS Task Configuration ────────────────────────────────────────────
#define WIFI_TASK_STACK_SIZE    4096
#define MQTT_TASK_STACK_SIZE    32768
#define BLINK_TASK_STACK_SIZE   2048
#define OTA_TASK_STACK_SIZE     32768
#define WIFI_TASK_PRIORITY      1
#define MQTT_TASK_PRIORITY      1
#define BLINK_TASK_PRIORITY     0
#define OTA_TASK_PRIORITY       2
#define WIFI_TASK_CORE          0
#define MQTT_TASK_CORE          1

// ─── LED Configuration ──────────────────────────────────────────────────────
#define LED_PIN                 21

// ─── Timing Constants ───────────────────────────────────────────────────────
#define WIFI_CONNECT_TIMEOUT_MS     15000
#define WIFI_RETRY_DELAY_MS         5000
#define WIFI_CHECK_INTERVAL_MS      1000
#define MQTT_PUBLISH_INTERVAL_MS    30000
#define MQTT_KEEPALIVE_SEC          60

// ─── Backoff Configuration ──────────────────────────────────────────────────
#define BACKOFF_BASE_MS             1000
#define BACKOFF_MAX_MS              120000
#define BACKOFF_MULTIPLIER          2

// ─── MQTT Topics ────────────────────────────────────────────────────────────
#define TOPIC_PREFIX_DT    "dt/"
#define TOPIC_PREFIX_CMD   "cmd/"

static char g_topicTelemetry[128];
static char g_topicLifecycle[128];
static char g_topicCmdAll[128];

// ─── Device Identity ────────────────────────────────────────────────────────
static char g_thingName[32];

// ─── Global State ───────────────────────────────────────────────────────────
static volatile bool g_wifiConnected = false;
static volatile bool g_mqttConnected = false;
static SemaphoreHandle_t g_wifiMutex = NULL;

// TLS negotiation info
static char g_tlsVersion[32] = "unknown";
static char g_tlsCipher[64] = "unknown";
static char g_tlsKeyExchange[64] = "none";

// wolfSSL context
static WOLFSSL_CTX* g_wolfCtx = NULL;

// Task handles
static TaskHandle_t g_wifiTaskHandle = NULL;
static TaskHandle_t g_mqttTaskHandle = NULL;
static TaskHandle_t g_blinkTaskHandle = NULL;

// OTA state (shared between MQTT handler and OTA task)
static volatile bool g_otaPending = false;
static char g_otaUrl[1024] = {0};
static char g_otaVersion[32] = {0};
static int g_otaSize = 0;
static SemaphoreHandle_t g_otaSemaphore = NULL;

// ─── Forward Declarations ───────────────────────────────────────────────────
void wifiTask(void* pvParameters);
void mqttTask(void* pvParameters);
void otaTask(void* pvParameters);
void mqttMessageHandler(String &topic, String &payload);
bool setupWolfSSLContext();
static void initDeviceIdentity();
static void initTopics();
static unsigned long calculateBackoff(int attempt);

// ═══════════════════════════════════════════════════════════════════════════
//  Device Identity
// ═══════════════════════════════════════════════════════════════════════════

static void initDeviceIdentity() {
    uint64_t mac = ESP.getEfuseMac();
    uint8_t* macBytes = (uint8_t*)&mac;
    snprintf(g_thingName, sizeof(g_thingName), "esp32-%02X%02X%02X",
             macBytes[3], macBytes[4], macBytes[5]);
    ESP_LOGI(TAG_MAIN, "Device identity: %s", g_thingName);
}

static void initTopics() {
    snprintf(g_topicTelemetry, sizeof(g_topicTelemetry), "%s%s/telemetry", TOPIC_PREFIX_DT, g_thingName);
    snprintf(g_topicLifecycle, sizeof(g_topicLifecycle), "%s%s/lifecycle", TOPIC_PREFIX_DT, g_thingName);
    snprintf(g_topicCmdAll, sizeof(g_topicCmdAll), "%s%s/+", TOPIC_PREFIX_CMD, g_thingName);
}

static unsigned long calculateBackoff(int attempt) {
    unsigned long expDelay = BACKOFF_BASE_MS;
    for (int i = 0; i < attempt && expDelay < BACKOFF_MAX_MS; i++) expDelay *= BACKOFF_MULTIPLIER;
    if (expDelay > BACKOFF_MAX_MS) expDelay = BACKOFF_MAX_MS;
    return random(0, expDelay);
}

// ═══════════════════════════════════════════════════════════════════════════
//  wolfSSL I/O Callbacks
// ═══════════════════════════════════════════════════════════════════════════

typedef struct { WiFiClient* client; } wolfssl_io_ctx_t;

static int wolfssl_send_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx) {
    wolfssl_io_ctx_t* io = (wolfssl_io_ctx_t*)ctx;
    if (!io || !io->client || !io->client->connected()) return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    int sent = io->client->write((uint8_t*)buf, sz);
    if (sent <= 0) return WOLFSSL_CBIO_ERR_WANT_WRITE;
    return sent;
}

static int wolfssl_recv_cb(WOLFSSL* ssl, char* buf, int sz, void* ctx) {
    wolfssl_io_ctx_t* io = (wolfssl_io_ctx_t*)ctx;
    if (!io || !io->client || !io->client->connected()) return WOLFSSL_CBIO_ERR_CONN_CLOSE;
    if (io->client->available() == 0) return WOLFSSL_CBIO_ERR_WANT_READ;
    int received = io->client->read((uint8_t*)buf, sz);
    if (received <= 0) return WOLFSSL_CBIO_ERR_WANT_READ;
    return received;
}

// ═══════════════════════════════════════════════════════════════════════════
//  WolfSSL TLS Client (PQC-capable)
// ═══════════════════════════════════════════════════════════════════════════

class WolfSSLClient : public Client {
public:
    WolfSSLClient() : _ssl(NULL), _connected(false), _pqcRequested(false) {
        _ioCtx.client = &_tcpClient;
    }
    ~WolfSSLClient() { stop(); }

    int connect(IPAddress ip, uint16_t port) override {
        return connect(ip.toString().c_str(), port);
    }

    int connect(const char* host, uint16_t port) override {
        stop();
        _pqcRequested = false;
        if (!_tcpClient.connect(host, port)) return 0;

        _ssl = wolfSSL_new(g_wolfCtx);
        if (!_ssl) { _tcpClient.stop(); return 0; }

        wolfSSL_SSLSetIORecv(_ssl, wolfssl_recv_cb);
        wolfSSL_SSLSetIOSend(_ssl, wolfssl_send_cb);
        wolfSSL_SetIOReadCtx(_ssl, &_ioCtx);
        wolfSSL_SetIOWriteCtx(_ssl, &_ioCtx);

        // Enable PQC key exchange if available
        #ifdef WOLFSSL_HAVE_MLKEM
        int ret = wolfSSL_UseKeyShare(_ssl, WOLFSSL_SECP256R1MLKEM768);
        if (ret == WOLFSSL_SUCCESS) {
            _pqcRequested = true;
            ESP_LOGI(TAG_TLS, "PQC key exchange: P-256 + ML-KEM-768");
        }
        #endif

        // SNI
        #ifdef HAVE_SNI
        wolfSSL_UseSNI(_ssl, WOLFSSL_SNI_HOST_NAME, host, strlen(host));
        #endif

        // TLS handshake with timeout
        ESP_LOGI(TAG_TLS, "Starting TLS handshake to %s:%d...", host, port);
        unsigned long handshakeStart = millis();
        int ret2;
        do {
            ret2 = wolfSSL_connect(_ssl);
            if (ret2 == WOLFSSL_SUCCESS) break;
            int err = wolfSSL_get_error(_ssl, ret2);
            if (err == WOLFSSL_ERROR_WANT_READ || err == WOLFSSL_ERROR_WANT_WRITE) {
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }
            char errBuf[128];
            wolfSSL_ERR_error_string_n(err, errBuf, sizeof(errBuf));
            ESP_LOGE(TAG_TLS, "Handshake error: %s", errBuf);
            wolfSSL_free(_ssl); _ssl = NULL; _tcpClient.stop();
            return 0;
        } while ((millis() - handshakeStart) < 30000);

        if (ret2 != WOLFSSL_SUCCESS) {
            wolfSSL_free(_ssl); _ssl = NULL; _tcpClient.stop();
            return 0;
        }

        // Store negotiated params
        const char* ver = wolfSSL_get_version(_ssl);
        const char* cipher = wolfSSL_get_cipher_name(_ssl);
        strncpy(g_tlsVersion, ver ? ver : "unknown", sizeof(g_tlsVersion) - 1);
        strncpy(g_tlsCipher, cipher ? cipher : "unknown", sizeof(g_tlsCipher) - 1);
        if (_pqcRequested) {
            strncpy(g_tlsKeyExchange, "SECP256R1_MLKEM768", sizeof(g_tlsKeyExchange) - 1);
        } else {
            strncpy(g_tlsKeyExchange, "ECDHE-P256", sizeof(g_tlsKeyExchange) - 1);
        }
        ESP_LOGI(TAG_TLS, "TLS connected: %s, %s, KeyEx: %s", g_tlsVersion, g_tlsCipher, g_tlsKeyExchange);
        _connected = true;
        return 1;
    }

    size_t write(uint8_t b) override { return write(&b, 1); }
    size_t write(const uint8_t* buf, size_t size) override {
        if (!_ssl || !_connected) return 0;
        int ret = wolfSSL_write(_ssl, buf, size);
        if (ret <= 0) { _connected = false; return 0; }
        return (size_t)ret;
    }
    int available() override {
        if (!_ssl || !_connected) return 0;
        int pending = wolfSSL_pending(_ssl);
        if (pending > 0) return pending;
        if (_tcpClient.available() > 0) return 1;
        return 0;
    }

    int read() override {
        uint8_t b;
        if (!_ssl || !_connected) return -1;
        unsigned long start = millis();
        while (available() == 0) {
            if (millis() - start > 5000 || !_connected) return -1;
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
                for (int i = 0; i < 10 && _tcpClient.available() > 0; i++) {
                    delay(10);
                    ret = wolfSSL_read(_ssl, buf, size);
                    if (ret > 0) return ret;
                }
                return 0;
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
        return (ret <= 0) ? -1 : b;
    }
    void flush() override { _tcpClient.flush(); }
    void stop() override {
        if (_ssl) { wolfSSL_shutdown(_ssl); wolfSSL_free(_ssl); _ssl = NULL; }
        _tcpClient.stop();
        _connected = false;
    }
    uint8_t connected() override { return _connected && _tcpClient.connected(); }
    operator bool() override { return connected(); }

private:
    WiFiClient _tcpClient;
    WOLFSSL* _ssl;
    wolfssl_io_ctx_t _ioCtx;
    bool _connected;
    bool _pqcRequested;
};

// ═══════════════════════════════════════════════════════════════════════════
//  wolfSSL Context Setup
// ═══════════════════════════════════════════════════════════════════════════

bool setupWolfSSLContext() {
    if (wolfSSL_Init() != WOLFSSL_SUCCESS) return false;
    g_wolfCtx = wolfSSL_CTX_new(wolfTLSv1_3_client_method());
    if (!g_wolfCtx) return false;

    int ret = wolfSSL_CTX_load_verify_buffer(g_wolfCtx,
        (const unsigned char*)AWS_CERT_CA, strlen(AWS_CERT_CA), WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) { ESP_LOGE(TAG_WOLFSSL, "CA cert load failed"); return false; }

    ret = wolfSSL_CTX_use_certificate_buffer(g_wolfCtx,
        (const unsigned char*)AWS_CERT_CRT, strlen(AWS_CERT_CRT), WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) { ESP_LOGE(TAG_WOLFSSL, "Device cert load failed"); return false; }

    ret = wolfSSL_CTX_use_PrivateKey_buffer(g_wolfCtx,
        (const unsigned char*)AWS_CERT_PRIVATE, strlen(AWS_CERT_PRIVATE), WOLFSSL_FILETYPE_PEM);
    if (ret != WOLFSSL_SUCCESS) { ESP_LOGE(TAG_WOLFSSL, "Private key load failed"); return false; }

    wolfSSL_CTX_set_verify(g_wolfCtx, WOLFSSL_VERIFY_PEER, NULL);
    wolfSSL_CTX_SetIORecv(g_wolfCtx, wolfssl_recv_cb);
    wolfSSL_CTX_SetIOSend(g_wolfCtx, wolfssl_send_cb);

    ESP_LOGI(TAG_WOLFSSL, "TLS 1.3 context ready");
    return true;
}

// ═══════════════════════════════════════════════════════════════════════════
//  WiFi Task
// ═══════════════════════════════════════════════════════════════════════════

void wifiTask(void* pvParameters) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    int attempts = 0;

    for (;;) {
        if (WiFi.status() != WL_CONNECTED) {
            xSemaphoreTake(g_wifiMutex, portMAX_DELAY);
            g_wifiConnected = false;
            xSemaphoreGive(g_wifiMutex);

            ESP_LOGI(TAG_WIFI, "Connecting to '%s'...", WIFI_SSID);
            WiFi.disconnect(true);
            vTaskDelay(pdMS_TO_TICKS(500));
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

            unsigned long start = millis();
            while (WiFi.status() != WL_CONNECTED && (millis() - start) < WIFI_CONNECT_TIMEOUT_MS)
                vTaskDelay(pdMS_TO_TICKS(500));

            if (WiFi.status() == WL_CONNECTED) {
                xSemaphoreTake(g_wifiMutex, portMAX_DELAY);
                g_wifiConnected = true;
                xSemaphoreGive(g_wifiMutex);
                attempts = 0;
                ESP_LOGI(TAG_WIFI, "Connected! IP: %s", WiFi.localIP().toString().c_str());
            } else {
                attempts++;
                unsigned long backoff = calculateBackoff(attempts);
                ESP_LOGW(TAG_WIFI, "Failed (attempt %d), retry in %lu ms", attempts, backoff);
                vTaskDelay(pdMS_TO_TICKS(backoff));
            }
        }
        vTaskDelay(pdMS_TO_TICKS(WIFI_CHECK_INTERVAL_MS));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  OTA Download Task (wolfSSL HTTPS client)
// ═══════════════════════════════════════════════════════════════════════════

void otaTask(void* pvParameters) {
    ESP_LOGI(TAG_OTA, "OTA task started, waiting for update command...");

    for (;;) {
        // Wait for OTA semaphore signal from MQTT handler
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

        // Check for port in host
        int colonIdx = host.indexOf(':');
        if (colonIdx > 0) {
            port = host.substring(colonIdx + 1).toInt();
            host = host.substring(0, colonIdx);
        }

        ESP_LOGI(TAG_OTA, "Connecting to %s:%d", host.c_str(), port);

        // Create a separate wolfSSL connection for S3 download
        // Note: This does NOT use client certs (S3 presigned URL doesn't need mTLS)
        WiFiClient tcpClient;
        if (!tcpClient.connect(host.c_str(), port)) {
            ESP_LOGE(TAG_OTA, "TCP connection to S3 failed");
            g_otaPending = false;
            continue;
        }

        // For S3 download, create a fresh TLS context without client certs
        // (presigned URLs use query-string auth, not mTLS)
        WOLFSSL_CTX* otaCtx = wolfSSL_CTX_new(wolfTLSv1_3_client_method());
        if (!otaCtx) {
            ESP_LOGE(TAG_OTA, "Failed to create OTA TLS context");
            tcpClient.stop();
            g_otaPending = false;
            continue;
        }

        // Load Amazon Root CA for S3 verification
        wolfSSL_CTX_load_verify_buffer(otaCtx,
            (const unsigned char*)AWS_CERT_CA, strlen(AWS_CERT_CA), WOLFSSL_FILETYPE_PEM);
        wolfSSL_CTX_set_verify(otaCtx, WOLFSSL_VERIFY_PEER, NULL);

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

        // PQC for S3 when available
        #ifdef WOLFSSL_HAVE_MLKEM
        wolfSSL_UseKeyShare(otaSsl, WOLFSSL_SECP256R1MLKEM768);
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

        // Send HTTP GET request
        String httpReq = "GET " + path + " HTTP/1.1\r\nHost: " + host + "\r\nConnection: close\r\n\r\n";
        wolfSSL_write(otaSsl, httpReq.c_str(), httpReq.length());

        // Read HTTP response headers to get content length
        String headerLine;
        int contentLength = 0;
        bool headersComplete = false;

        while (!headersComplete) {
            char c;
            int r = wolfSSL_read(otaSsl, &c, 1);
            if (r <= 0) {
                if (wolfSSL_get_error(otaSsl, r) == WOLFSSL_ERROR_WANT_READ) {
                    vTaskDelay(pdMS_TO_TICKS(10));
                    continue;
                }
                break;
            }
            headerLine += c;
            if (headerLine.endsWith("\r\n")) {
                if (headerLine == "\r\n") { headersComplete = true; break; }
                if (headerLine.startsWith("Content-Length:")) {
                    contentLength = headerLine.substring(16).toInt();
                }
                headerLine = "";
            }
        }

        if (contentLength <= 0) {
            ESP_LOGE(TAG_OTA, "Invalid content length: %d", contentLength);
            wolfSSL_free(otaSsl); wolfSSL_CTX_free(otaCtx); tcpClient.stop();
            g_otaPending = false;
            continue;
        }

        ESP_LOGI(TAG_OTA, "Firmware size: %d bytes, starting OTA write...", contentLength);

        // Begin OTA write
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

        while (totalRead < contentLength) {
            int toRead = min((int)sizeof(buf), contentLength - totalRead);
            int bytesRead = wolfSSL_read(otaSsl, buf, toRead);

            if (bytesRead <= 0) {
                int sslErr = wolfSSL_get_error(otaSsl, bytesRead);
                if (sslErr == WOLFSSL_ERROR_WANT_READ) {
                    vTaskDelay(pdMS_TO_TICKS(10));
                    continue;
                }
                ESP_LOGE(TAG_OTA, "Download error at %d/%d bytes", totalRead, contentLength);
                downloadOk = false;
                break;
            }

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

        // Cleanup TLS connection
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

        // Finalize OTA
        err = esp_ota_end(otaHandle);
        if (err != ESP_OK) {
            ESP_LOGE(TAG_OTA, "esp_ota_end failed: %s", esp_err_to_name(err));
            g_otaPending = false;
            continue;
        }

        // Set boot partition and restart
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
//  MQTT Message Handler
// ═══════════════════════════════════════════════════════════════════════════

void mqttMessageHandler(String &topic, String &payload) {
    ESP_LOGI(TAG_MQTT, "Message on '%s' (%d bytes)", topic.c_str(), payload.length());
    ESP_LOGI(TAG_MQTT, "Payload: %s", payload.c_str());

    // OTA update command
    if (topic.indexOf("/ota") >= 0) {
        if (payload.indexOf("ota-update") >= 0) {
            // Extract URL
            int urlStart = payload.indexOf("\"url\":\"") + 7;
            int urlEnd = payload.indexOf("\"", urlStart);
            if (urlStart >= 7 && urlEnd > urlStart) {
                String url = payload.substring(urlStart, urlEnd);
                url.toCharArray(g_otaUrl, sizeof(g_otaUrl));

                // Extract version
                int verStart = payload.indexOf("\"version\":\"") + 11;
                int verEnd = payload.indexOf("\"", verStart);
                if (verStart >= 11 && verEnd > verStart) {
                    payload.substring(verStart, verEnd).toCharArray(g_otaVersion, sizeof(g_otaVersion));
                } else {
                    strcpy(g_otaVersion, "unknown");
                }

                // Extract size
                int sizeStart = payload.indexOf("\"size\":") + 7;
                if (sizeStart >= 7) g_otaSize = payload.substring(sizeStart).toInt();

                g_otaPending = true;
                xSemaphoreGive(g_otaSemaphore);
                ESP_LOGI(TAG_OTA, "OTA queued: v%s, %d bytes", g_otaVersion, g_otaSize);
            }
        }
        return;
    }

    // LED control
    if (payload.indexOf("led-off") >= 0) {
        if (g_blinkTaskHandle) vTaskSuspend(g_blinkTaskHandle);
        digitalWrite(LED_PIN, LOW);
        return;
    }
    if (payload.indexOf("led-on") >= 0) {
        if (g_blinkTaskHandle) vTaskResume(g_blinkTaskHandle);
        return;
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT Task
// ═══════════════════════════════════════════════════════════════════════════

void mqttTask(void* pvParameters) {
    while (!g_wifiConnected) vTaskDelay(pdMS_TO_TICKS(1000));

    // NTP sync
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    struct tm timeinfo;
    int ntpRetries = 0;
    while (!getLocalTime(&timeinfo) && ntpRetries < 15) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        ntpRetries++;
    }

    if (!setupWolfSSLContext()) {
        ESP_LOGE(TAG_MQTT, "wolfSSL setup failed!");
        vTaskDelete(NULL);
        return;
    }

    initTopics();
    static WolfSSLClient tlsClient;
    static MQTTClient mqttClient(2048);

    mqttClient.begin(MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT, tlsClient);
    mqttClient.onMessage(mqttMessageHandler);
    mqttClient.setKeepAlive(MQTT_KEEPALIVE_SEC);
    mqttClient.setTimeout(10000);

    // LWT
    char lwtPayload[128];
    snprintf(lwtPayload, sizeof(lwtPayload),
        "{\"status\":\"offline\",\"device\":\"%s\"}", g_thingName);
    mqttClient.setWill(g_topicLifecycle, lwtPayload, true, 1);

    unsigned long lastPublish = 0;
    unsigned long msgCount = 0;
    int connectAttempts = 0;

    for (;;) {
        if (!g_wifiConnected) {
            g_mqttConnected = false;
            mqttClient.disconnect();
            while (!g_wifiConnected) vTaskDelay(pdMS_TO_TICKS(1000));
        }

        if (!mqttClient.connected()) {
            g_mqttConnected = false;
            bool connected = mqttClient.connect(g_thingName);
            if (connected) {
                g_mqttConnected = true;
                connectAttempts = 0;
                ESP_LOGI(TAG_MQTT, "Connected to broker as '%s'", g_thingName);

                // Publish online lifecycle
                char onlinePayload[256];
                snprintf(onlinePayload, sizeof(onlinePayload),
                    "{\"status\":\"online\",\"device\":\"%s\",\"firmware_version\":\"%s\","
                    "\"pqc_algorithm\":\"%s\",\"tls_version\":\"%s\",\"ip\":\"%s\"}",
                    g_thingName, FIRMWARE_VERSION, g_tlsKeyExchange,
                    g_tlsVersion, WiFi.localIP().toString().c_str());
                mqttClient.publish(g_topicLifecycle, onlinePayload, true, 1);

                // Subscribe to commands
                mqttClient.subscribe(g_topicCmdAll, 1);
                ESP_LOGI(TAG_MQTT, "Subscribed: %s", g_topicCmdAll);
            } else {
                connectAttempts++;
                vTaskDelay(pdMS_TO_TICKS(calculateBackoff(connectAttempts)));
                continue;
            }
        }

        mqttClient.loop();

        // Telemetry
        unsigned long now = millis();
        if (now - lastPublish >= MQTT_PUBLISH_INTERVAL_MS) {
            lastPublish = now;
            msgCount++;
            char payload[384];
            snprintf(payload, sizeof(payload),
                "{\"device\":\"%s\",\"message_id\":%lu,\"uptime_sec\":%lu,"
                "\"wifi_rssi\":%d,\"free_heap\":%u,\"min_heap\":%u,"
                "\"chip_temp_c\":%.1f,\"pqc_algorithm\":\"%s\","
                "\"tls_version\":\"%s\",\"firmware_version\":\"%s\"}",
                g_thingName, msgCount, now / 1000,
                WiFi.RSSI(), ESP.getFreeHeap(), ESP.getMinFreeHeap(),
                temperatureRead(), g_tlsKeyExchange, g_tlsVersion, FIRMWARE_VERSION);
            mqttClient.publish(g_topicTelemetry, payload);
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  Blink Task
// ═══════════════════════════════════════════════════════════════════════════

void blinkTask(void* pvParameters) {
    pinMode(LED_PIN, OUTPUT);
    for (;;) {
        digitalWrite(LED_PIN, HIGH);
        vTaskDelay(pdMS_TO_TICKS(300));
        digitalWrite(LED_PIN, LOW);
        vTaskDelay(pdMS_TO_TICKS(300));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  Arduino Setup & Loop
// ═══════════════════════════════════════════════════════════════════════════

void setup() {
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);

    // Start blink immediately
    xTaskCreatePinnedToCore(blinkTask, "Blink", BLINK_TASK_STACK_SIZE,
        NULL, BLINK_TASK_PRIORITY, &g_blinkTaskHandle, 0);

    Serial.begin(115200);
    unsigned long start = millis();
    while (!Serial && (millis() - start < 10000)) delay(100);

    esp_log_level_set("*", (esp_log_level_t)LOG_LEVEL);

    g_wifiMutex = xSemaphoreCreateMutex();
    g_otaSemaphore = xSemaphoreCreateBinary();
    randomSeed(analogRead(0) ^ (uint32_t)(ESP.getEfuseMac() >> 16));

    initDeviceIdentity();

    ESP_LOGI(TAG_MAIN, "");
    ESP_LOGI(TAG_MAIN, "══════════════════════════════════════════════════");
    ESP_LOGI(TAG_MAIN, "  PQC Production Firmware (OTA-Ready)");
    ESP_LOGI(TAG_MAIN, "  Firmware: %s | Device: %s", FIRMWARE_VERSION, g_thingName);
    ESP_LOGI(TAG_MAIN, "══════════════════════════════════════════════════");
    ESP_LOGI(TAG_MAIN, "");

    int mqttCore = 1;
    #if CONFIG_FREERTOS_UNICORE
    mqttCore = 0;
    #endif

    xTaskCreatePinnedToCore(wifiTask, "WiFi", WIFI_TASK_STACK_SIZE,
        NULL, WIFI_TASK_PRIORITY, &g_wifiTaskHandle, WIFI_TASK_CORE);
    xTaskCreatePinnedToCore(mqttTask, "MQTT", MQTT_TASK_STACK_SIZE,
        NULL, MQTT_TASK_PRIORITY, &g_mqttTaskHandle, mqttCore);
    xTaskCreatePinnedToCore(otaTask, "OTA", OTA_TASK_STACK_SIZE,
        NULL, OTA_TASK_PRIORITY, NULL, mqttCore);

    ESP_LOGI(TAG_MAIN, "All tasks started. Ready for OTA commands on cmd/%s/ota", g_thingName);
}

void loop() {
    static unsigned long lastStatus = 0;
    unsigned long now = millis();
    if (now - lastStatus >= 60000) {
        lastStatus = now;
        ESP_LOGI(TAG_MAIN, "Status: WiFi=%s MQTT=%s Heap=%u OTA=%s",
            g_wifiConnected ? "OK" : "DOWN",
            g_mqttConnected ? "OK" : "DOWN",
            ESP.getFreeHeap(),
            g_otaPending ? "ACTIVE" : "idle");
    }
    delay(1000);
}
