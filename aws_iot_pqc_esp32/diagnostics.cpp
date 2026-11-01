// Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

#include "app_config.h"
#include "diagnostics.h"
#include "device_identity.h"

volatile bool    Diagnostics::s_pending = false;
volatile uint8_t Diagnostics::s_level   = 0;

// "dump" > "full" > summary. Only flagged here; the MQTT task runs the report.
void Diagnostics::request(const String& payload) {
    s_level = payload.indexOf("dump") >= 0 ? 2 : (payload.indexOf("full") >= 0 ? 1 : 0);
    s_pending = true;
    ESP_LOGI(TAG_MQTT, "Diagnostics requested (level %u)", s_level);
}

void Diagnostics::runIfPending(MQTTClient& client) {
    if (!s_pending) return;
    s_pending = false;
    run(client, s_level);
}

static size_t diagHeapJson(char* out, size_t cap, const char* name, uint32_t caps) {
    multi_heap_info_t i;
    heap_caps_get_info(&i, caps);
    return snprintf(out, cap,
        "\"%s\":{\"total\":%u,\"free\":%u,\"min_free\":%u,\"largest\":%u,"
        "\"free_blocks\":%u,\"alloc_blocks\":%u}",
        name, (unsigned)heap_caps_get_total_size(caps), (unsigned)i.total_free_bytes,
        (unsigned)i.minimum_free_bytes, (unsigned)i.largest_free_block,
        (unsigned)i.free_blocks, (unsigned)i.allocated_blocks);
}

// Configured stack size (bytes) for the tasks this sketch creates; 0 = not ours.
static uint32_t diagKnownStackSize(const char* name) {
    if (!strcmp(name, "WiFiTask"))  return WIFI_TASK_STACK_SIZE;
    if (!strcmp(name, "MQTTTask"))  return MQTT_TASK_STACK_SIZE;
    if (!strcmp(name, "OTATask"))   return OTA_TASK_STACK_SIZE;
    if (!strcmp(name, "BlinkTask")) return BLINK_TASK_STACK_SIZE;
    if (!strcmp(name, "loopTask"))  return getArduinoLoopTaskStackSize();
    return 0;
}

void Diagnostics::run(MQTTClient& client, uint8_t level) {
    const size_t cap = 6144;
    // The report buffer itself goes to PSRAM so it does not perturb what it measures.
    char* json = (char*)heap_caps_malloc(cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!json) json = (char*)malloc(cap);
    UBaseType_t n = uxTaskGetNumberOfTasks() + 4;
    TaskStatus_t* ts = (TaskStatus_t*)heap_caps_malloc(n * sizeof(TaskStatus_t),
                                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!ts) ts = (TaskStatus_t*)malloc(n * sizeof(TaskStatus_t));
    if (!json || !ts) {
        ESP_LOGE(TAG_MAIN, "[diag] out of memory for the report");
        free(json); free(ts);
        return;
    }
    uint32_t totalRun = 0;
    n = uxTaskGetSystemState(ts, n, &totalRun);

    size_t len = snprintf(json, cap, "{\"device\":\"%s\",\"firmware_version\":\"%s\",\"uptime_sec\":%lu,",
                          DeviceIdentity::thingName(), FIRMWARE_VERSION, millis() / 1000);
    len += diagHeapJson(json + len, cap - len, "internal", MALLOC_CAP_INTERNAL);
    len += snprintf(json + len, cap - len, ",");
    len += diagHeapJson(json + len, cap - len, "psram", MALLOC_CAP_SPIRAM);
    len += snprintf(json + len, cap - len, ",\"tasks\":[");

    ESP_LOGI(TAG_MAIN, "[diag] ── memory report (level %u) ──", level);
    ESP_LOGI(TAG_MAIN, "[diag] internal: free %u B, min %u B, largest %u B, %u free blocks",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             (unsigned)[]{ multi_heap_info_t i; heap_caps_get_info(&i, MALLOC_CAP_INTERNAL); return i.free_blocks; }());
    ESP_LOGI(TAG_MAIN, "[diag] psram:    free %u B, min %u B, largest %u B",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_SPIRAM),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
    ESP_LOGI(TAG_MAIN, "[diag] %-16s %6s %6s %6s %4s %4s %5s", "task", "size", "hwm", "used", "core", "prio", "cpu%");
    for (UBaseType_t k = 0; k < n && len < cap - 200; k++) {
        const TaskStatus_t& t = ts[k];
        uint32_t size = diagKnownStackSize(t.pcTaskName);
        uint32_t hwm = (uint32_t)t.usStackHighWaterMark * sizeof(StackType_t);
        int core = (t.xCoreID == tskNO_AFFINITY) ? -1 : (int)t.xCoreID;
        float cpu = totalRun ? (100.0f * t.ulRunTimeCounter / totalRun) : 0.0f;
        len += snprintf(json + len, cap - len,
                        "%s{\"name\":\"%s\",\"hwm\":%u,\"size\":%u,\"core\":%d,\"prio\":%u,\"cpu\":%.1f}",
                        k ? "," : "", t.pcTaskName, (unsigned)hwm, (unsigned)size, core,
                        (unsigned)t.uxCurrentPriority, cpu);
        if (size) {
            ESP_LOGI(TAG_MAIN, "[diag] %-16s %6u %6u %6u %4d %4u %5.1f", t.pcTaskName,
                     (unsigned)size, (unsigned)hwm, (unsigned)(size - hwm), core,
                     (unsigned)t.uxCurrentPriority, cpu);
        } else {
            ESP_LOGI(TAG_MAIN, "[diag] %-16s %6s %6u %6s %4d %4u %5.1f", t.pcTaskName, "-",
                     (unsigned)hwm, "-", core, (unsigned)t.uxCurrentPriority, cpu);
        }
    }
    len += snprintf(json + len, cap - len, "]}");

    char topic[96];
    snprintf(topic, sizeof(topic), "dt/%s/diag", DeviceIdentity::thingName());
    if (client.publish(topic, json)) {
        ESP_LOGI(TAG_MAIN, "[diag] published %u B → %s", (unsigned)len, topic);
    } else {
        ESP_LOGW(TAG_MAIN, "[diag] publish to %s failed", topic);
    }
    ESP_LOGI(TAG_MAIN, "[diag] json %s", json);

    if (level >= 1) {
        heap_caps_print_heap_info(MALLOC_CAP_INTERNAL);
        heap_caps_print_heap_info(MALLOC_CAP_SPIRAM);
    }
    if (level >= 2) {
        heap_caps_dump(MALLOC_CAP_INTERNAL);
    }
    free(ts);
    free(json);
}
