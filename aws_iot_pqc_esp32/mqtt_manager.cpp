// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

#include "app_config.h"
#include "mqtt_manager.h"
#include "device_identity.h"
#include "wifi_manager.h"
#include "pqc_tls_client.h"
#include "ota_updater.h"
#include "diagnostics.h"
#include "status_led.h"
#include <MQTT.h>

volatile bool MqttManager::s_connected = false;
TaskHandle_t  MqttManager::s_task      = NULL;

// Built at runtime from the MAC-derived device identity
static char g_topicTelemetry[128];   // dt/<thing>/telemetry
static char g_topicLifecycle[128];   // dt/<thing>/lifecycle
static char g_topicCmdAll[128];      // cmd/<thing>/+  (subscribe wildcard)
static char g_topicCmdReq[128];      // cmd/<thing>/request

bool MqttManager::begin(int core) {
    BaseType_t rc = xTaskCreatePinnedToCore(task, "MQTTTask", MQTT_TASK_STACK_SIZE,
                                            NULL, MQTT_TASK_PRIORITY, &s_task, core);
    if (rc != pdPASS) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create MQTT task (rc=%d)", rc);
        return false;
    }
    ESP_LOGD(TAG_MAIN, "MQTT task created (stack=%d, priority=%d, core=%d)",
             MQTT_TASK_STACK_SIZE, MQTT_TASK_PRIORITY, core);
    return true;
}

// ═══════════════════════════════════════════════════════════════════════════
//  Exponential Backoff with Jitter
// ═══════════════════════════════════════════════════════════════════════════

// Calculates the next retry delay using exponential backoff with full jitter.
// This prevents "thundering herd" when many devices reconnect simultaneously.
// See: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
unsigned long MqttManager::computeBackoff(int attempt) {
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

void MqttManager::initTopics() {
    snprintf(g_topicTelemetry, sizeof(g_topicTelemetry),
             "%s%s/telemetry", TOPIC_PREFIX_DT, DeviceIdentity::thingName());
    snprintf(g_topicLifecycle, sizeof(g_topicLifecycle),
             "%s%s/lifecycle", TOPIC_PREFIX_DT, DeviceIdentity::thingName());
    snprintf(g_topicCmdAll, sizeof(g_topicCmdAll),
             "%s%s/+", TOPIC_PREFIX_CMD, DeviceIdentity::thingName());
    snprintf(g_topicCmdReq, sizeof(g_topicCmdReq),
             "%s%s/request", TOPIC_PREFIX_CMD, DeviceIdentity::thingName());

    ESP_LOGD(TAG_MQTT, "Topics initialized:");
    ESP_LOGD(TAG_MQTT, "  Telemetry: %s", g_topicTelemetry);
    ESP_LOGD(TAG_MQTT, "  Lifecycle: %s", g_topicLifecycle);
    ESP_LOGD(TAG_MQTT, "  Commands:  %s", g_topicCmdAll);
}

// ═══════════════════════════════════════════════════════════════════════════
//  Command dispatch (cmd/<thing>/+)
// ═══════════════════════════════════════════════════════════════════════════

void MqttManager::onMessage(String& topic, String& payload) {
    ESP_LOGI(TAG_MQTT, "Message received on topic '%s' (%d bytes)", topic.c_str(), payload.length());
    ESP_LOGI(TAG_MQTT, "Payload: %s", payload.c_str());

    // OTA commands on cmd/<thing>/ota. Check OTA commands FIRST before LED
    // controls (an OTA payload could contain any text).
    if (topic.indexOf("/ota") >= 0) {
        OtaUpdater::handleCommand(payload);
        return;
    }

    // Memory diagnostics: cmd/<thing>/diag — "dump" > "full" > summary.
    if (topic.endsWith("/diag")) {
        Diagnostics::request(payload);
        return;
    }

    // LED control: {"action":"led-off"} or {"action":"led-on"}
    if (payload.indexOf("led-off") >= 0) {
        ESP_LOGI(TAG_MQTT, "Blink OFF command received");
        StatusLed::off();
        return;
    }
    if (payload.indexOf("led-on") >= 0) {
        ESP_LOGI(TAG_MQTT, "Blink ON command received");
        StatusLed::on();
        return;
    }

    ESP_LOGW(TAG_MQTT, "Unhandled message on topic '%s'", topic.c_str());
}

// ═══════════════════════════════════════════════════════════════════════════
//  MQTT FreeRTOS Task
// ═══════════════════════════════════════════════════════════════════════════

void MqttManager::task(void* pvParameters) {
    ESP_LOGI(TAG_MQTT, "Task started on core %d, waiting for WiFi...", xPortGetCoreID());

    // Wait until WiFi is connected before proceeding
    while (!WifiManager::isConnected()) {
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
    if (!WolfSSLClient::setupContext()) {
        ESP_LOGE(TAG_MQTT, "FATAL: wolfSSL context setup failed. Task halted.");
        vTaskDelete(NULL);
        return;
    }

    // Initialize topic strings
    initTopics();

    // Create the wolfSSL-backed TLS client and MQTT client
    static WolfSSLClient tlsClient;
    // 8 KB buffer: the SIGNED OTA command (Workstream 2+) carries url + sigUrl +
    // signerCertUrl (three ~1.5 KB presigned S3 URLs) + sha256, ~4.9 KB total,
    // which overflowed the old 4 KB buffer — the MQTT library silently dropped
    // the message and the OTA never started. 8 KB gives comfortable headroom.
    static MQTTClient mqttClient(8192);

    mqttClient.begin(MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT, tlsClient);
    mqttClient.onMessage(onMessage);
    mqttClient.setKeepAlive(MQTT_KEEPALIVE_SEC);
    mqttClient.setTimeout(10000); // 10 second timeout for read/write operations
    ESP_LOGD(TAG_MQTT, "MQTTClient configured: endpoint=%s, port=%d, keepalive=%ds, timeout=10s",
             MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT, MQTT_KEEPALIVE_SEC);

    // ─── Last Will and Testament (LWT) ──────────────────────────────────────
    static char lwtPayload[128];
    snprintf(lwtPayload, sizeof(lwtPayload),
             "{\"status\":\"offline\",\"reason\":\"unexpected\",\"device\":\"%s\"}",
             DeviceIdentity::thingName());
    mqttClient.setWill(g_topicLifecycle, lwtPayload, true, 1);

    unsigned long lastPublishTime = 0;
    unsigned long messageCount = 0;
    int connectAttempts = 0;

    for (;;) {
        // Check WiFi is still up
        if (!WifiManager::isConnected()) {
            ESP_LOGW(TAG_MQTT, "WiFi lost, disconnecting MQTT and waiting...");
            s_connected = false;
            mqttClient.disconnect();
            connectAttempts = 0; // Reset backoff on WiFi loss
            while (!WifiManager::isConnected()) {
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            ESP_LOGI(TAG_MQTT, "WiFi restored, reconnecting MQTT...");
        }

        // Connect or reconnect to MQTT
        if (!mqttClient.connected()) {
            s_connected = false;
            ESP_LOGI(TAG_MQTT, "Connecting to PQC Mosquitto broker (thing='%s', attempt=%d)...",
                     DeviceIdentity::thingName(), connectAttempts + 1);
            ESP_LOGD(TAG_MQTT, "Free heap before connect: %u bytes", ESP.getFreeHeap());

            // Connect with client ID (LWT already set via setWill above)
            bool connected = mqttClient.connect(DeviceIdentity::thingName());

            if (connected) {
                s_connected = true;
                connectAttempts = 0; // Reset backoff on success
                ESP_LOGI(TAG_MQTT, "Connected to PQC Mosquitto broker!");
                ESP_LOGI(TAG_MQTT, "  Client ID: %s", DeviceIdentity::thingName());
                ESP_LOGI(TAG_MQTT, "  Broker:    %s:%d", MQTT_BROKER_ENDPOINT, MQTT_BROKER_PORT);
                ESP_LOGI(TAG_MQTT, "  Local IP:  %s", WiFi.localIP().toString().c_str());

                // ─── Publish Online Lifecycle Event ──────────────────────────
                // Announce that the device is online. This is the counterpart
                // to the LWT offline message. Retained so new subscribers
                // immediately know the device state.
                char onlinePayload[448];  // headroom for ota_sig_alg + firmware_sha256
                snprintf(onlinePayload, sizeof(onlinePayload),
                         "{"
                         "\"status\":\"online\","
                         "\"device\":\"%s\","
                         "\"firmware_version\":\"%s\","
                         "\"pqc_algorithm\":\"%s\","
                         "\"tls_version\":\"%s\","
                         "\"cert_sig_alg\":\"%s\","
                         "\"ota_sig_alg\":\"%s\","
                         "\"firmware_sha256\":\"%s\","
                         "\"ip\":\"%s\","
                         "\"uptime_sec\":%lu"
                         "}",
                         DeviceIdentity::thingName(),
                         FIRMWARE_VERSION,
                         WolfSSLClient::keyExchange(),
                         WolfSSLClient::tlsVersion(),
                         CERT_SIGNATURE_ALG,
                         FIRMWARE_OTA_SIG_ALG,
                         DeviceIdentity::firmwareSha256(),
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
                unsigned long backoffDelay = computeBackoff(connectAttempts);
                ESP_LOGW(TAG_MQTT, "Connection failed (rc=%d). Backoff: %lu ms (attempt %d)",
                         mqttClient.lastError(), backoffDelay, connectAttempts);
                vTaskDelay(pdMS_TO_TICKS(backoffDelay));
                continue;
            }
        }

        // Process incoming messages
        mqttClient.loop();

        Diagnostics::runIfPending(mqttClient);

        // Publish telemetry at regular intervals
        unsigned long now = millis();
        if (now - lastPublishTime >= MQTT_PUBLISH_INTERVAL_MS) {
            lastPublishTime = now;
            messageCount++;

            // Build a JSON telemetry message
            float chipTempC = temperatureRead(); // Internal die temperature (°C)
            char payload[768];  // headroom for ota_sig_alg + firmware_sha256 (64 hex)
            snprintf(payload, sizeof(payload),
                     "{"
                     "\"device\":\"%s\","
                     "\"firmware_version\":\"%s\","
                     "\"message_id\":%lu,"
                     "\"timestamp\":%lu,"
                     "\"uptime_sec\":%lu,"
                     "\"wifi_rssi\":%d,"
                     "\"free_heap\":%u,"
                     "\"heap_total\":%u,"
                     "\"min_heap\":%u,"
                     "\"max_alloc_heap\":%u,"
                     "\"sketch_size\":%u,"
                     "\"free_sketch_space\":%u,"
                     "\"psram_size\":%u,"
                     "\"psram_free\":%u,"
                     "\"chip_temp_c\":%.1f,"
                     "\"pqc_algorithm\":\"%s\","
                     "\"tls_version\":\"%s\","
                     "\"cert_sig_alg\":\"%s\","
                     "\"ota_sig_alg\":\"%s\","
                     "\"firmware_sha256\":\"%s\""
                     "}",
                     DeviceIdentity::thingName(),
                     FIRMWARE_VERSION,
                     messageCount,
                     (unsigned long)(millis() / 1000), // Approximate epoch placeholder
                     millis() / 1000,
                     WiFi.RSSI(),
                     ESP.getFreeHeap(),
                     ESP.getHeapSize(),
                     ESP.getMinFreeHeap(),
                     ESP.getMaxAllocHeap(),
                     ESP.getSketchSize(),
                     ESP.getFreeSketchSpace(),
                     ESP.getPsramSize(),
                     ESP.getFreePsram(),
                     chipTempC,
                     WolfSSLClient::keyExchange(),
                     WolfSSLClient::tlsVersion(),
                     CERT_SIGNATURE_ALG,
                     FIRMWARE_OTA_SIG_ALG,
                     DeviceIdentity::firmwareSha256());

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
