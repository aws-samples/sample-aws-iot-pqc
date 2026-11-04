// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// status_led.h — heartbeat LED (own low-priority task). The blink period is
// per variant (VARIANT_BLINK_MS): slow on classical, fast on PQC. The
// dashboard's led-off / led-on commands pause and resume it.

#ifndef STATUS_LED_H
#define STATUS_LED_H

#include "app_config.h"

class StatusLed {
public:
    static void begin();   // LED on, start blinking
    static void off();     // pause the blink, LED off
    static void on();      // resume the blink
    static TaskHandle_t taskHandle()    { return s_task; }

private:
    static void task(void* pvParameters);
    static TaskHandle_t s_task;
};

#endif  // STATUS_LED_H
