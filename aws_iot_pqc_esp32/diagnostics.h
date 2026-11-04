// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// diagnostics.h — on-demand memory report (cmd/<thing>/diag).
//
// Payload: anything (summary), "full" (+ per-region heap info on the console),
// or "dump" (+ every heap block on the console). Publishes a JSON summary to
// dt/<thing>/diag and logs the same as a table:
//   internal / psram : total, free, min_free (low-water since boot), largest
//                      free block, free / allocated block counts — a large free
//                      total with a small largest block means fragmentation;
//   tasks            : per task, stack high-water mark (bytes NEVER used, so
//                      "used" = size - hwm), configured size for our tasks,
//                      core, priority and share of CPU time since boot.

#ifndef DIAGNOSTICS_H
#define DIAGNOSTICS_H

#include "app_config.h"
#include <MQTT.h>

class Diagnostics {
public:
    // Flag a report (called from the MQTT message handler).
    static void request(const String& payload);
    // Run a flagged report. Called from the MQTT task, which owns the client.
    static void runIfPending(MQTTClient& client);

private:
    static void run(MQTTClient& client, uint8_t level);
    static volatile bool    s_pending;
    static volatile uint8_t s_level;   // 0 summary, 1 + region info, 2 + block dump
};

#endif  // DIAGNOSTICS_H
