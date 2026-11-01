// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

#include "app_config.h"
#include "status_led.h"

TaskHandle_t StatusLed::s_task = NULL;

void StatusLed::begin() {
    // LED on right away: a visual indicator the device is alive
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);
    xTaskCreatePinnedToCore(task, "BlinkTask", BLINK_TASK_STACK_SIZE,
                            NULL, BLINK_TASK_PRIORITY, &s_task, 0);
}

void StatusLed::off() {
    if (s_task) vTaskSuspend(s_task);
    digitalWrite(LED_PIN, LOW);
    delay(1);  // Ensure GPIO state settles
}

void StatusLed::on() {
    if (s_task) vTaskResume(s_task);
}

// Visual heartbeat. VARIANT_BLINK_MS is the half-period: 500 ms on the
// classical firmware, 200 ms on the PQC variants.
void StatusLed::task(void* pvParameters) {
    pinMode(LED_PIN, OUTPUT);
    for (;;) {
        digitalWrite(LED_PIN, HIGH);
        vTaskDelay(pdMS_TO_TICKS(VARIANT_BLINK_MS));
        digitalWrite(LED_PIN, LOW);
        vTaskDelay(pdMS_TO_TICKS(VARIANT_BLINK_MS));
    }
}
