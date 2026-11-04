// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// ota_updater.h — over-the-air firmware updates (own FreeRTOS task).
//
// An "ota-update" command on cmd/<thing>/ota carries presigned S3 URLs for the
// image, its detached signature and the signer certificate. The task streams
// the image into the inactive OTA slot, checks its SHA-256, verifies the
// signature (ECDSA P-256 or ML-DSA-44), that the signer chains to the matching
// firmware-signing root in the device's NVS identity and has the codeSigning
// EKU, and only then switches the boot slot.

#ifndef OTA_UPDATER_H
#define OTA_UPDATER_H

#include "app_config.h"

class OtaUpdater {
public:
    // Allocate the URL buffers (PSRAM), create the semaphore and start the OTA
    // task on the given core. Returns false on failure.
    static bool begin(int core);

    // Handle a cmd/<thing>/ota payload (called from the MQTT task). Queues an
    // "ota-update" for the OTA task; other payloads are logged and ignored.
    static void handleCommand(const String& payload);

    static TaskHandle_t taskHandle()    { return s_task; }

private:
    static void task(void* pvParameters);
    static TaskHandle_t s_task;
};

#endif  // OTA_UPDATER_H
