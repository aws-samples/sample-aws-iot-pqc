// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// mqtt_manager.h — the broker connection (own FreeRTOS task): NTP sync, TLS
// context, connect with backoff, lifecycle + telemetry publishing, and
// dispatch of cmd/<thing>/+ commands (OTA, diagnostics, LED).

#ifndef MQTT_MANAGER_H
#define MQTT_MANAGER_H

#include "app_config.h"

class MqttManager {
public:
    // Start the MQTT task on the given core. Returns false on failure.
    static bool begin(int core);
    static bool isConnected()           { return s_connected; }
    static TaskHandle_t taskHandle()    { return s_task; }

private:
    static void task(void* pvParameters);
    static void initTopics();
    static void onMessage(String& topic, String& payload);
    static unsigned long computeBackoff(int attempt);

    static volatile bool s_connected;
    static TaskHandle_t  s_task;
};

#endif  // MQTT_MANAGER_H
