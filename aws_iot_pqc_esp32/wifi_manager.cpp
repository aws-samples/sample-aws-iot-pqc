// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

#include "app_config.h"
#include "wifi_manager.h"
#include "device_identity.h"

volatile bool     WifiManager::s_connected = false;
SemaphoreHandle_t WifiManager::s_mutex     = NULL;
TaskHandle_t      WifiManager::s_task      = NULL;

bool WifiManager::begin() {
    s_mutex = xSemaphoreCreateMutex();
    if (!s_mutex) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create WiFi mutex");
        return false;
    }
    BaseType_t rc = xTaskCreatePinnedToCore(task, "WiFiTask", WIFI_TASK_STACK_SIZE,
                                            NULL, WIFI_TASK_PRIORITY, &s_task, WIFI_TASK_CORE);
    if (rc != pdPASS) {
        ESP_LOGE(TAG_MAIN, "FATAL: Failed to create WiFi task (rc=%d)", rc);
        return false;
    }
    ESP_LOGD(TAG_MAIN, "WiFi task created (stack=%d, priority=%d, core=%d)",
             WIFI_TASK_STACK_SIZE, WIFI_TASK_PRIORITY, WIFI_TASK_CORE);
    return true;
}

void WifiManager::setConnected(bool connected) {
    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(1000)) == pdTRUE) {
        s_connected = connected;
        xSemaphoreGive(s_mutex);
    }
}

// ═══════════════════════════════════════════════════════════════════════════
//  WiFi FreeRTOS Task
// ═══════════════════════════════════════════════════════════════════════════

void WifiManager::task(void* pvParameters) {
    ESP_LOGI(TAG_WIFI, "Task started on core %d", xPortGetCoreID());

    // Configure WiFi
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    ESP_LOGD(TAG_WIFI, "WiFi mode set to STA, auto-reconnect enabled");

    int wifiAttempts = 0;

    for (;;) {
        if (WiFi.status() != WL_CONNECTED) {
            setConnected(false);

            ESP_LOGI(TAG_WIFI, "Connecting to SSID '%s' (attempt %d)...", DeviceIdentity::wifiSsid(), wifiAttempts + 1);
            WiFi.disconnect(true);
            ESP_LOGD(TAG_WIFI, "Previous connection disconnected, waiting 500ms for radio reset");
            vTaskDelay(pdMS_TO_TICKS(500));
            WiFi.begin(DeviceIdentity::wifiSsid(), DeviceIdentity::wifiPsk());

            // Wait for connection with timeout
            unsigned long startAttempt = millis();
            while (WiFi.status() != WL_CONNECTED &&
                   (millis() - startAttempt) < WIFI_CONNECT_TIMEOUT_MS) {
                vTaskDelay(pdMS_TO_TICKS(500));
                ESP_LOGV(TAG_WIFI, "Waiting... status=%d, elapsed=%lu ms",
                         WiFi.status(), millis() - startAttempt);
            }

            if (WiFi.status() == WL_CONNECTED) {
                setConnected(true);
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
