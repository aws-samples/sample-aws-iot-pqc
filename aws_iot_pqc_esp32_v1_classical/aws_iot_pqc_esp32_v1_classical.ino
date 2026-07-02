/*
 * aws_iot_pqc_esp32_v1_classical.ino
 *
 * FIRMWARE v1.0 — Pre-PQC Baseline (Classical TLS)
 *
 * This sketch connects to AWS IoT Core using standard WiFiClientSecure
 * (mbedTLS, classical TLS 1.2) WITHOUT post-quantum cryptography.
 * It serves as the "before" state for the PQC migration demo.
 *
 * When the device receives a migrate-pqc command on its MQTT command
 * topic, it switches the boot partition to OTA_1 (which should be
 * pre-flashed with the v2.0 PQC firmware) and reboots.
 *
 * Key differences from v2.0 (PQC):
 *   - Uses WiFiClientSecure (mbedTLS) instead of wolfSSL
 *   - TLS 1.2 with classical ECDHE key exchange
 *   - No Kyber/ML-KEM post-quantum algorithms
 *   - Includes OTA partition-switch handler for migration demo
 *   - Reports pqc_algorithm as "none" in telemetry
 *
 * Required Arduino Libraries:
 *   1. PubSubClient (by Nick O'Leary) - lightweight MQTT client
 *
 * Required Board Package:
 *   - esp32 by Espressif Systems (Board Manager)
 *
 * Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <MQTT.h>
#include <esp_log.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>

#include "secrets.h"

// ─── Log Tags ───────────────────────────────────────────────────────────────
static const char* TAG_MAIN = "main";
static const char* TAG_WIFI = "wifi";
static const char* TAG_MQTT = "mqtt";
static const char* TAG_OTA  = "ota";

// ─── Firmware Version ───────────────────────────────────────────────────────
#define FIRMWARE_VERSION "1.0.0"

// ─── FreeRTOS Task Configuration ────────────────────────────────────────────
#define WIFI_TASK_STACK_SIZE    4096
#define MQTT_TASK_STACK_SIZE    8192    // Classical TLS needs less stack
#define BLINK_TASK_STACK_SIZE   2048
#define WIFI_TASK_PRIORITY      1
#define MQTT_TASK_PRIORITY      1
#define BLINK_TASK_PRIORITY     0
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

// ─── Exponential Backoff ────────────────────────────────────────────────────
#define BACKOFF_BASE_MS             1000
#define BACKOFF_MAX_MS              120000
#define BACKOFF_MULTIPLIER          2

// ─── MQTT Topics ────────────────────────────────────────────────────────────
static char g_topicTelemetry[128];
static char g_topicLifecycle[128];
static char g_topicCmdAll[128];

// ─── Device Identity (derived from MAC address at runtime) ──────────────────
static char g_thingName[32];  // "esp32-XXYYZZ" format

// ─── Global State ───────────────────────────────────────────────────────────
static volatile bool g_wifiConnected = false;
static volatile bool g_mqttConnected = false;
static SemaphoreHandle_t g_wifiMutex = NULL;
static TaskHandle_t g_wifiTaskHandle = NULL;
static TaskHandle_t g_mqttTaskHandle = NULL;

// ─── Forward Declarations ───────────────────────────────────────────────────
void wifiTask(void* pvParameters);
void mqttTask(void* pvParameters);
void mqttMessageHandler(String &topic, String &payload);
void handleOtaMigration();
static void initDeviceIdentity();
void blinkTask(void* pvParameters);

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

// ─── Backoff Calculation ────────────────────────────────────────────────────
static unsigned long calculateBackoff(int attempt) {
    unsigned long expDelay = BACKOFF_BASE_MS;
    for (int i = 0; i < attempt && expDelay < BACKOFF_MAX_MS; i++) {
        expDelay *= BACKOFF_MULTIPLIER;
    }
    if (expDelay > BACKOFF_MAX_MS) expDelay = BACKOFF_MAX_MS;
    return random(0, expDelay);
}

// ─── Topic Initialization ───────────────────────────────────────────────────
static void initTopics() {
    snprintf(g_topicTelemetry, sizeof(g_topicTelemetry), "dt/%s/telemetry", g_thingName);
    snprintf(g_topicLifecycle, sizeof(g_topicLifecycle), "dt/%s/lifecycle", g_thingName);
    snprintf(g_topicCmdAll, sizeof(g_topicCmdAll), "cmd/%s/+", g_thingName);
    ESP_LOGD(TAG_MQTT, "Topics: telemetry=%s, lifecycle=%s, cmd=%s",
             g_topicTelemetry, g_topicLifecycle, g_topicCmdAll);
}

// ═══════════════════════════════════════════════════════════════════════════
//  OTA Partition Switch (for PQC Migration Demo)
// ═══════════════════════════════════════════════════════════════════════════

void handleOtaMigration() {
    ESP_LOGI(TAG_OTA, "PQC migration command received!");
    ESP_LOGI(TAG_OTA, "Switching boot partition to OTA_1 (PQC firmware v2.0)...");

    // Find the next OTA partition (OTA_1 if currently on OTA_0)
    const esp_partition_t* nextPartition = esp_ota_get_next_update_partition(NULL);
    if (!nextPartition) {
        ESP_LOGE(TAG_OTA, "Failed to find next OTA partition. Is the partition table configured for OTA?");
        return;
    }

    ESP_LOGI(TAG_OTA, "Next partition: label='%s', offset=0x%x, size=%u",
             nextPartition->label, nextPartition->address, nextPartition->size);

    // Verify the next partition has valid firmware
    esp_app_desc_t appDesc;
    esp_err_t err = esp_ota_get_partition_description(nextPartition, &appDesc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG_OTA, "No valid firmware found in partition '%s' (err=0x%x)",
                 nextPartition->label, err);
        ESP_LOGE(TAG_OTA, "Ensure v2.0 PQC firmware is pre-flashed to this partition");
        return;
    }

    ESP_LOGI(TAG_OTA, "Found firmware in next partition: %s v%s",
             appDesc.project_name, appDesc.version);

    // Set the next boot partition
    err = esp_ota_set_boot_partition(nextPartition);
    if (err != ESP_OK) {
        ESP_LOGE(TAG_OTA, "Failed to set boot partition (err=0x%x)", err);
        return;
    }

    ESP_LOGI(TAG_OTA, "Boot partition set to '%s'. Rebooting in 2 seconds...",
             nextPartition->label);

    // Brief delay so the log message and any final MQTT publish can complete
    vTaskDelay(pdMS_TO_TICKS(2000));

    // Reboot into the PQC firmware
    esp_restart();
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT Callback
// ═══════════════════════════════════════════════════════════════════════════

void mqttMessageHandler(String &topic, String &payload) {
    ESP_LOGI(TAG_MQTT, "Message on '%s' (%d bytes)", topic.c_str(), payload.length());
    ESP_LOGD(TAG_MQTT, "Payload: %s", payload.c_str());

    // Check for OTA migration command
    if (topic.indexOf("/ota") >= 0 && payload.indexOf("migrate-pqc") >= 0) {
        handleOtaMigration();
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  WiFi FreeRTOS Task
// ═══════════════════════════════════════════════════════════════════════════

void wifiTask(void* pvParameters) {
    ESP_LOGI(TAG_WIFI, "Task started on core %d", xPortGetCoreID());
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);

    int wifiAttempts = 0;

    for (;;) {
        if (WiFi.status() != WL_CONNECTED) {
            if (xSemaphoreTake(g_wifiMutex, pdMS_TO_TICKS(1000)) == pdTRUE) {
                g_wifiConnected = false;
                xSemaphoreGive(g_wifiMutex);
            }

            ESP_LOGI(TAG_WIFI, "Connecting to '%s' (attempt %d)...", WIFI_SSID, wifiAttempts + 1);
            WiFi.disconnect(true);
            vTaskDelay(pdMS_TO_TICKS(500));
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

            unsigned long startAttempt = millis();
            while (WiFi.status() != WL_CONNECTED &&
                   (millis() - startAttempt) < WIFI_CONNECT_TIMEOUT_MS) {
                vTaskDelay(pdMS_TO_TICKS(500));
            }

            if (WiFi.status() == WL_CONNECTED) {
                if (xSemaphoreTake(g_wifiMutex, pdMS_TO_TICKS(1000)) == pdTRUE) {
                    g_wifiConnected = true;
                    xSemaphoreGive(g_wifiMutex);
                }
                wifiAttempts = 0;
                ESP_LOGI(TAG_WIFI, "Connected! IP: %s, RSSI: %d dBm",
                         WiFi.localIP().toString().c_str(), WiFi.RSSI());
                ESP_LOGI(TAG_WIFI, "  Gateway: %s, Subnet: %s",
                         WiFi.gatewayIP().toString().c_str(),
                         WiFi.subnetMask().toString().c_str());
            } else {
                wifiAttempts++;
                unsigned long backoff = calculateBackoff(wifiAttempts);
                ESP_LOGW(TAG_WIFI, "Failed (status=%d). Backoff: %lu ms", WiFi.status(), backoff);
                WiFi.disconnect(true);
                vTaskDelay(pdMS_TO_TICKS(backoff));
                continue;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(WIFI_CHECK_INTERVAL_MS));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT FreeRTOS Task (Classical TLS via WiFiClientSecure)
// ═══════════════════════════════════════════════════════════════════════════

void mqttTask(void* pvParameters) {
    ESP_LOGI(TAG_MQTT, "Task started on core %d, waiting for WiFi...", xPortGetCoreID());

    while (!g_wifiConnected) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    ESP_LOGI(TAG_MQTT, "WiFi is up, configuring classical TLS...");

    // Sync time via NTP (required for TLS certificate date validation)
    ESP_LOGI(TAG_MQTT, "Syncing time via NTP...");
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    struct tm timeinfo;
    int ntpRetries = 0;
    while (!getLocalTime(&timeinfo) && ntpRetries < 15) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        ntpRetries++;
    }
    if (ntpRetries < 15) {
        ESP_LOGI(TAG_MQTT, "Time synced: %04d-%02d-%02d %02d:%02d:%02d UTC",
                 timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
                 timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
    } else {
        ESP_LOGW(TAG_MQTT, "NTP sync failed — cert date check may fail");
    }

    // Initialize topics
    initTopics();

    // Configure WiFiClientSecure with certificates (classical mbedTLS)
    static WiFiClientSecure tlsClient;
    tlsClient.setCACert(AWS_CERT_CA);
    tlsClient.setCertificate(AWS_CERT_CRT);
    tlsClient.setPrivateKey(AWS_CERT_PRIVATE);
    ESP_LOGI(TAG_MQTT, "Classical TLS configured (WiFiClientSecure / mbedTLS)");

    // Create MQTT client (arduino-mqtt)
    static MQTTClient mqttClient(1024);
    mqttClient.begin(MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT, tlsClient);
    mqttClient.onMessage(mqttMessageHandler);
    mqttClient.setKeepAlive(MQTT_KEEPALIVE_SEC);
    mqttClient.setTimeout(10000);

    // LWT for offline detection (includes device name)
    static char lwtPayload[128];
    snprintf(lwtPayload, sizeof(lwtPayload),
             "{\"status\":\"offline\",\"reason\":\"unexpected\",\"device\":\"%s\"}",
             g_thingName);
    mqttClient.setWill(g_topicLifecycle, lwtPayload, true, 1);

    unsigned long lastPublishTime = 0;
    unsigned long messageCount = 0;
    int connectAttempts = 0;

    for (;;) {
        // Check WiFi
        if (!g_wifiConnected) {
            ESP_LOGW(TAG_MQTT, "WiFi lost, waiting...");
            g_mqttConnected = false;
            mqttClient.disconnect();
            connectAttempts = 0;
            while (!g_wifiConnected) { vTaskDelay(pdMS_TO_TICKS(1000)); }
            ESP_LOGI(TAG_MQTT, "WiFi restored, reconnecting...");
        }

        // Connect or reconnect
        if (!mqttClient.connected()) {
            g_mqttConnected = false;
            ESP_LOGI(TAG_MQTT, "Connecting to broker (classical TLS, attempt %d)...",
                     connectAttempts + 1);

            bool connected = mqttClient.connect(g_thingName);

            if (connected) {
                g_mqttConnected = true;
                connectAttempts = 0;
                ESP_LOGI(TAG_MQTT, "Connected to Mosquitto broker (classical TLS 1.2)");
                ESP_LOGI(TAG_MQTT, "  Client ID: %s", g_thingName);
                ESP_LOGI(TAG_MQTT, "  Broker:    %s:%d", MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT);

                // Publish online lifecycle event
                char onlinePayload[256];
                snprintf(onlinePayload, sizeof(onlinePayload),
                         "{\"status\":\"online\",\"device\":\"%s\","
                         "\"firmware_version\":\"%s\","
                         "\"pqc_algorithm\":\"none\","
                         "\"tls_version\":\"TLS 1.2\","
                         "\"ip\":\"%s\",\"uptime_sec\":%lu}",
                         g_thingName, FIRMWARE_VERSION,
                         WiFi.localIP().toString().c_str(), millis() / 1000);
                mqttClient.publish(g_topicLifecycle, onlinePayload, true, 1);
                ESP_LOGI(TAG_MQTT, "Published lifecycle: online");

                // Subscribe to commands (including OTA trigger)
                mqttClient.subscribe(g_topicCmdAll, 1);
                ESP_LOGI(TAG_MQTT, "Subscribed to: %s", g_topicCmdAll);
            } else {
                connectAttempts++;
                unsigned long backoff = calculateBackoff(connectAttempts);
                ESP_LOGW(TAG_MQTT, "Failed (rc=%d). Backoff: %lu ms",
                         mqttClient.lastError(), backoff);
                vTaskDelay(pdMS_TO_TICKS(backoff));
                continue;
            }
        }

        mqttClient.loop();

        // Publish telemetry
        unsigned long now = millis();
        if (now - lastPublishTime >= MQTT_PUBLISH_INTERVAL_MS) {
            lastPublishTime = now;
            messageCount++;

            float chipTempC = temperatureRead();
            char payload[384];
            snprintf(payload, sizeof(payload),
                     "{\"device\":\"%s\","
                     "\"firmware_version\":\"%s\","
                     "\"message_id\":%lu,"
                     "\"timestamp\":%lu,"
                     "\"uptime_sec\":%lu,"
                     "\"wifi_rssi\":%d,"
                     "\"free_heap\":%u,"
                     "\"min_heap\":%u,"
                     "\"chip_temp_c\":%.1f,"
                     "\"pqc_algorithm\":\"none\","
                     "\"tls_version\":\"TLS 1.2\"}",
                     g_thingName, FIRMWARE_VERSION,
                     messageCount, millis() / 1000, millis() / 1000,
                     WiFi.RSSI(), ESP.getFreeHeap(), ESP.getMinFreeHeap(),
                     chipTempC);

            if (mqttClient.publish(g_topicTelemetry, payload)) {
                ESP_LOGI(TAG_MQTT, "Telemetry #%lu published", messageCount);
            } else {
                ESP_LOGE(TAG_MQTT, "Publish failed for #%lu", messageCount);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  Arduino Setup & Loop
// ═══════════════════════════════════════════════════════════════════════════

// ─── Blink Task ─────────────────────────────────────────────────────────────
void blinkTask(void* pvParameters) {
    pinMode(LED_PIN, OUTPUT);
    for (;;) {
        digitalWrite(LED_PIN, HIGH);
        vTaskDelay(pdMS_TO_TICKS(500)); // Slower blink for classical (distinguishes from PQC)
        digitalWrite(LED_PIN, LOW);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void setup() {
    // Start blink task immediately
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);
    xTaskCreatePinnedToCore(blinkTask, "BlinkTask", BLINK_TASK_STACK_SIZE,
                            NULL, BLINK_TASK_PRIORITY, NULL, 0);

    Serial.begin(115200);
    unsigned long serialStart = millis();
    while (!Serial && (millis() - serialStart < 10000)) { delay(100); }

    esp_log_level_set("*", (esp_log_level_t)LOG_LEVEL);

    g_wifiMutex = xSemaphoreCreateMutex();
    randomSeed(analogRead(0) ^ (uint32_t)(ESP.getEfuseMac() >> 16));
    initDeviceIdentity();

    ESP_LOGI(TAG_MAIN, "");
    ESP_LOGI(TAG_MAIN, "══════════════════════════════════════════════════");
    ESP_LOGI(TAG_MAIN, "  ESP32 AWS IoT Core — CLASSICAL TLS (Pre-PQC)");
    ESP_LOGI(TAG_MAIN, "  Firmware v%s — Awaiting PQC Migration", FIRMWARE_VERSION);
    ESP_LOGI(TAG_MAIN, "══════════════════════════════════════════════════");
    ESP_LOGI(TAG_MAIN, "");
    ESP_LOGI(TAG_MAIN, "┌──────────────────────────────────────────────┐");
    ESP_LOGI(TAG_MAIN, "│  YOUR DEVICE NAME:  %-24s │", g_thingName);
    ESP_LOGI(TAG_MAIN, "└──────────────────────────────────────────────┘");
    ESP_LOGI(TAG_MAIN, "");
    ESP_LOGI(TAG_MAIN, "Chip: %s Rev %d, %d MHz", ESP.getChipModel(),
             ESP.getChipRevision(), ESP.getCpuFreqMHz());
    ESP_LOGI(TAG_MAIN, "Flash: %d MB, Heap: %u bytes, PSRAM: %u bytes",
             ESP.getFlashChipSize() / (1024 * 1024), ESP.getFreeHeap(), ESP.getPsramSize());
    ESP_LOGI(TAG_MAIN, "TLS: Classical (WiFiClientSecure / mbedTLS)");
    ESP_LOGI(TAG_MAIN, "PQC: NONE — send 'migrate-pqc' to cmd/%s/ota to upgrade", g_thingName);

    const esp_partition_t* running = esp_ota_get_running_partition();
    ESP_LOGI(TAG_MAIN, "Running from partition: '%s' (offset 0x%x)",
             running->label, running->address);

    int mqttCore = 1;
    #if CONFIG_FREERTOS_UNICORE
        mqttCore = 0;
    #endif

    xTaskCreatePinnedToCore(wifiTask, "WiFiTask", WIFI_TASK_STACK_SIZE,
                            NULL, WIFI_TASK_PRIORITY, &g_wifiTaskHandle, WIFI_TASK_CORE);
    xTaskCreatePinnedToCore(mqttTask, "MQTTTask", MQTT_TASK_STACK_SIZE,
                            NULL, MQTT_TASK_PRIORITY, &g_mqttTaskHandle, mqttCore);

    ESP_LOGI(TAG_MAIN, "All tasks created. Waiting for migration command...");
}

void loop() {
    static unsigned long lastStatusTime = 0;
    unsigned long now = millis();

    if (now - lastStatusTime >= 60000) {
        lastStatusTime = now;

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
        ESP_LOGI(TAG_MAIN, "  TLS:        Classical (mbedTLS / TLS 1.2)");
        ESP_LOGI(TAG_MAIN, "  PQC:        NONE — awaiting migration");
        ESP_LOGI(TAG_MAIN, "  Free Heap:  %u bytes", ESP.getFreeHeap());
        ESP_LOGI(TAG_MAIN, "  Min Heap:   %u bytes", ESP.getMinFreeHeap());
        ESP_LOGI(TAG_MAIN, "───────────────────────────────────────────────");
    }
    delay(1000);
}
