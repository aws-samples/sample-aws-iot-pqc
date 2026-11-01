/*
 * aws_iot_pqc_esp32.ino
 *
 * ESP32-S3 firmware for the AWS IoT post-quantum migration workshop. One
 * sketch builds every step of the migration; FIRMWARE_VARIANT in secrets.h
 * picks the variant (see variant_config.h):
 *
 *   classical  classical TLS 1.3 key exchange, ECDSA P-256 device certificate
 *   pqc        hybrid ML-KEM key exchange (SecP256r1MLKEM768), ECDSA P-256 cert
 *   pqc-mldsa  hybrid ML-KEM key exchange, ML-DSA-44 (FIPS 204) device cert
 *
 * The device connects over mutual TLS 1.3 (wolfSSL) to the workshop's
 * Mosquitto broker, which bridges to AWS IoT Core. WiFi, MQTT, OTA and the
 * status LED each run as their own FreeRTOS task with automatic recovery.
 *
 * Modules (Arduino IDE tabs):
 *   app_config.h         includes, build settings, tuning constants
 *   variant_config.h     everything that differs between the variants
 *   device_identity.*    Thing name + keys/certs/WiFi from the "certs" NVS partition
 *   wifi_manager.*       WiFi station task
 *   pqc_tls_client.*     wolfSSL TLS 1.3 (+ ML-KEM) as an Arduino Client
 *   mqtt_manager.*       broker connection, telemetry, command dispatch
 *   ota_updater.*        signed OTA download + verification
 *   diagnostics.*        on-demand memory report (cmd/<thing>/diag)
 *   status_led.*         heartbeat LED
 *   json_util.*          tiny JSON field extractors
 *
 * Required Arduino Libraries (install via Library Manager):
 *   1. wolfSSL  (by wolfSSL Inc.)      - TLS 1.3 + PQC support (5.8.4)
 *   2. MQTT     (by Joel Gaehwiler)    - Arduino MQTT client (2.5.3)
 *
 * Required Board Package:
 *   - esp32 by Espressif Systems (Board Manager), 3.3.10
 *
 * wolfSSL Configuration:
 *   wolfSSL is configured at compile time by user_settings.h in the wolfSSL
 *   library's src/ directory. The workshop build copies
 *   raw_device_builder/codebuild/wolfssl_user_settings_arduino.h there before
 *   every compile; for a local build copy it there yourself.
 *
 * Device identity:
 *   Keys, certificates, CA roots and WiFi are NOT in this image. They are read
 *   at boot from the "certs" NVS partition, which the Builder-Pi writes at
 *   registration (see device_identity.h).
 *
 * Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

#include "app_config.h"
#include "device_identity.h"
#include "wifi_manager.h"
#include "mqtt_manager.h"
#include "ota_updater.h"
#include "status_led.h"

SET_LOOP_TASK_STACK_SIZE(LOOP_TASK_STACK_SIZE);  // Arduino-ESP32 loopTask stack

// Never returns: logs the reason and stops this boot.
static void haltForever(const char* what) {
    for (;;) {
        ESP_LOGE(TAG_MAIN, "FATAL: %s", what);
        delay(10000);
    }
}

void setup() {
    // Start the heartbeat immediately — visual indicator the device is alive
    StatusLed::begin();

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

    // Seed random number generator for backoff jitter
    randomSeed(analogRead(0) ^ (uint32_t)(ESP.getEfuseMac() >> 16));

    // Derive device identity from MAC address FIRST (needed for banner)
    DeviceIdentity::initFromMac();

    // Load keys, certs and WiFi from the "certs" NVS partition. Fail closed:
    // without a valid identity for THIS chip, never start WiFi/MQTT/OTA.
    if (!DeviceIdentity::loadFromNvs()) {
        for (;;) {
            ESP_LOGE(TAG_MAIN, "[Setup] FATAL: no valid device identity for %s. "
                     "Register and flash this device at the Builder-Pi.",
                     DeviceIdentity::thingName());
            delay(10000);
        }
    }

    // Hash the running image once at boot for the dashboard integrity badge.
    DeviceIdentity::computeRunningFirmwareSha256();

    ESP_LOGI(TAG_MAIN, "");
    ESP_LOGI(TAG_MAIN, "══════════════════════════════════════════════════");
    ESP_LOGI(TAG_MAIN, VARIANT_BANNER_1);
    ESP_LOGI(TAG_MAIN, VARIANT_BANNER_2);
    ESP_LOGI(TAG_MAIN, "══════════════════════════════════════════════════");
    ESP_LOGI(TAG_MAIN, "");
    ESP_LOGI(TAG_MAIN, "┌──────────────────────────────────────────────┐");
    ESP_LOGI(TAG_MAIN, "│  YOUR DEVICE NAME:  %-24s │", DeviceIdentity::thingName());
    ESP_LOGI(TAG_MAIN, "└──────────────────────────────────────────────┘");
    ESP_LOGI(TAG_MAIN, "");

    // Print chip info
    ESP_LOGI(TAG_MAIN, "Chip:       %s Rev %d", ESP.getChipModel(), ESP.getChipRevision());
    ESP_LOGI(TAG_MAIN, "CPU Freq:   %d MHz", ESP.getCpuFreqMHz());
    ESP_LOGI(TAG_MAIN, "Flash:      %d MB", ESP.getFlashChipSize() / (1024 * 1024));
    ESP_LOGI(TAG_MAIN, "Free Heap:  %u bytes", ESP.getFreeHeap());
    ESP_LOGI(TAG_MAIN, "PSRAM:      %u bytes", ESP.getPsramSize());
    ESP_LOGI(TAG_MAIN, "Log Level:  %d", LOG_LEVEL);
    ESP_LOGI(TAG_MAIN, "Firmware:   %s (variant %s)", FIRMWARE_VERSION, VARIANT_NAME);

    // Determine number of cores for task pinning
    // Single-core ESP32 variants (C3, S2) use core 0 for everything
    int mqttCore = MQTT_TASK_CORE;
    #if CONFIG_FREERTOS_UNICORE
        mqttCore = 0;
        ESP_LOGD(TAG_MAIN, "Unicore detected, all tasks on core 0");
    #else
        ESP_LOGD(TAG_MAIN, "Dual-core detected, WiFi=core0, MQTT=core1");
    #endif

    if (!WifiManager::begin())         haltForever("WiFi task start failed");
    if (!MqttManager::begin(mqttCore)) haltForever("MQTT task start failed");
    if (!OtaUpdater::begin(mqttCore))  haltForever("OTA task start failed");

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
        ESP_LOGI(TAG_MAIN, "  Device:     %s", DeviceIdentity::thingName());
        ESP_LOGI(TAG_MAIN, "  Uptime:     %lu sec", now / 1000);
        ESP_LOGI(TAG_MAIN, "  WiFi:       %s (RSSI: %d dBm)",
                 WifiManager::isConnected() ? "Connected" : "Disconnected", WiFi.RSSI());
        ESP_LOGI(TAG_MAIN, "  IP:         %s", WiFi.localIP().toString().c_str());
        ESP_LOGI(TAG_MAIN, "  MQTT:       %s",
                 MqttManager::isConnected() ? "Connected" : "Disconnected");
        ESP_LOGI(TAG_MAIN, "  Free Heap:  %u bytes", ESP.getFreeHeap());
        ESP_LOGI(TAG_MAIN, "  Min Heap:   %u bytes", ESP.getMinFreeHeap());
        ESP_LOGI(TAG_MAIN, "───────────────────────────────────────────────");

        ESP_LOGD(TAG_MAIN, "  WiFi task stack HWM: %u bytes",
                 uxTaskGetStackHighWaterMark(WifiManager::taskHandle()) * sizeof(StackType_t));
        ESP_LOGD(TAG_MAIN, "  MQTT task stack HWM: %u bytes",
                 uxTaskGetStackHighWaterMark(MqttManager::taskHandle()) * sizeof(StackType_t));
    }

    delay(1000);
}
