// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// wifi_manager.h — keeps the station connected (own FreeRTOS task, core 0),
// with exponential backoff and jitter between attempts.

#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include "app_config.h"

class WifiManager {
public:
    // Start the WiFi task. Credentials come from DeviceIdentity. Returns false
    // if the task or its mutex could not be created.
    static bool begin();
    static bool isConnected()           { return s_connected; }
    static TaskHandle_t taskHandle()    { return s_task; }

private:
    static void task(void* pvParameters);
    static void setConnected(bool connected);

    static volatile bool     s_connected;
    static SemaphoreHandle_t s_mutex;
    static TaskHandle_t      s_task;
};

#endif  // WIFI_MANAGER_H
