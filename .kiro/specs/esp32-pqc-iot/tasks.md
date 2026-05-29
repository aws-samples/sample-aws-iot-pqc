# Implementation Plan: ESP32 AWS IoT Core with Post-Quantum Cryptography

## Overview

This plan covers the full implementation of an Arduino sketch connecting an ESP32 to AWS IoT Core using post-quantum cryptography via wolfSSL. Tasks span project scaffolding, wolfSSL PQC configuration, TLS/MQTT connectivity with FreeRTOS task management, build validation, and documentation.

## Tasks

- [x] 1. Project scaffolding and secrets template
  - [x] 1.1 Create Arduino sketch folder structure (`aws_iot_pqc_esp32/`)
  - [x] 1.2 Create `secrets.h` with placeholder WiFi credentials, AWS IoT endpoint, Thing name, MQTT topics, and PEM certificate placeholders
  - [x] 1.3 Add security comment warning against committing real credentials
  **Requirements:** 10, 15

- [x] 2. wolfSSL PQC configuration reference
  - [x] 2.1 Create `wolfssl_user_settings.h` with all required `#define` values for ESP32 + TLS 1.3 + Kyber/ML-KEM
  - [x] 2.2 Include ESP32 hardware acceleration defines
  - [x] 2.3 Include memory tuning defines for constrained environments
  - [x] 2.4 Include comments explaining each section and linking to wolfSSL documentation
  - [x] 2.5 Disable unused legacy features (DES3, RC4, MD4, MD5, TLS 1.2)
  **Requirements:** 1, 15

- [x] 3. wolfSSL I/O callback layer
  - [x] 3.1 Implement `wolfssl_send_cb` bridging wolfSSL writes to `WiFiClient::write()`
  - [x] 3.2 Implement `wolfssl_recv_cb` bridging wolfSSL reads from `WiFiClient::read()` with non-blocking `WANT_READ` support
  - [x] 3.3 Define `wolfssl_io_ctx_t` struct to pass WiFiClient pointer through the callback context
  **Requirements:** 1, 2

- [x] 4. WolfSSLClient class (Arduino Client interface)
  - [x] 4.1 Implement `connect(host, port)` with TCP connection, wolfSSL session creation, PQC key share request, SNI, and handshake with timeout
  - [x] 4.2 Implement PQC fallback from P-521 + Kyber Level 5 to P-256 + Kyber Level 1
  - [x] 4.3 Implement `write()`, `read()`, `available()`, `peek()`, `flush()`, `stop()`, `connected()`
  - [x] 4.4 Handle non-blocking I/O in handshake loop with `vTaskDelay` yield
  - [x] 4.5 Print diagnostic information (protocol version, cipher suite, handshake time)
  **Requirements:** 1, 2, 8, 12

- [x] 5. wolfSSL context initialization
  - [x] 5.1 Implement `setupWolfSSLContext()` function
  - [x] 5.2 Initialize wolfSSL library
  - [x] 5.3 Create TLS 1.3 client method context
  - [x] 5.4 Load Amazon Root CA 1 from PROGMEM buffer
  - [x] 5.5 Load device certificate from PROGMEM buffer
  - [x] 5.6 Load device private key from PROGMEM buffer
  - [x] 5.7 Enable peer verification
  - [x] 5.8 Register I/O callbacks at context level
  **Requirements:** 1, 2, 15

- [x] 6. WiFi FreeRTOS task with auto-recovery
  - [x] 6.1 Implement `wifiTask()` as an infinite loop on core 0
  - [x] 6.2 Configure WiFi STA mode with auto-reconnect
  - [x] 6.3 Implement connection attempt with configurable timeout (15 s)
  - [x] 6.4 Implement disconnect-and-retry on failure with configurable delay (5 s)
  - [x] 6.5 Update `g_wifiConnected` flag protected by mutex
  - [x] 6.6 Print connection status, IP address, and RSSI
  **Requirements:** 5, 6, 9, 13

- [x] 7. MQTT FreeRTOS task with auto-recovery
  - [x] 7.1 Implement `mqttTask()` as an infinite loop on core 1 (or core 0 for unicore)
  - [x] 7.2 Wait for WiFi before initializing wolfSSL
  - [x] 7.3 Create WolfSSLClient and PubSubClient instances
  - [x] 7.4 Implement MQTT connect with Thing name as client ID
  - [x] 7.5 Subscribe to configurable inbound topic
  - [x] 7.6 Publish JSON telemetry at configurable interval (30 s)
  - [x] 7.7 Detect WiFi loss, disconnect MQTT, wait for WiFi, then reconnect
  - [x] 7.8 Implement reconnect delay (5 s) on MQTT connection failure
  **Requirements:** 3, 4, 7, 9, 11

- [x] 8. Arduino setup and main loop
  - [x] 8.1 Initialize serial at 115200 baud
  - [x] 8.2 Print banner with project name and PQC algorithm
  - [x] 8.3 Print chip info (model, revision, CPU freq, flash size, heap, PSRAM)
  - [x] 8.4 Create WiFi mutex
  - [x] 8.5 Detect single-core vs dual-core for task pinning (`CONFIG_FREERTOS_UNICORE`)
  - [x] 8.6 Create WiFi task pinned to core 0
  - [x] 8.7 Create MQTT task pinned to core 1 (or 0)
  - [x] 8.8 Implement periodic status report in `loop()` (every 60 s)
  **Requirements:** 9, 14

- [x] 9. Build-time validation
  - [x] 9.1 Add `#ifndef WOLFSSL_TLS13` compile error
  - [x] 9.2 Add `#ifndef WOLFSSL_HAVE_KYBER` compile error
  **Requirements:** 1

- [x] 10. Documentation
  - [x] 10.1 Write comprehensive README.md with table of contents
  - [x] 10.2 Document Arduino Cloud IDE installation (6 steps)
  - [x] 10.3 Document Arduino Desktop IDE installation (5 steps)
  - [x] 10.4 Document AWS IoT Core setup (Thing, certificates, policy, endpoint)
  - [x] 10.5 Document sketch configuration (WiFi, endpoint, certificates)
  - [x] 10.6 Document expected serial output with sample
  - [x] 10.7 Write code walkthrough explaining each component
  - [x] 10.8 Document PQC key exchange details (algorithms, fallback, constants)
  - [x] 10.9 Document memory and performance characteristics
  - [x] 10.10 Write troubleshooting guide (build errors and runtime errors)
  - [x] 10.11 Document security considerations
  **Requirements:** 16

- [x] 11. Kiro project configuration
  - [x] 11.1 Create spec files (requirements.md, design.md, tasks.md)
  - [x] 11.2 Update steering file with project-specific context
  **Requirements:** 16

- [x] 12. Implement Espressif logger with configurable log levels
  - [x] 12.1 Add `#include <esp_log.h>` and define log tags for each component (main, wifi, mqtt, tls, wolfssl)
  - [x] 12.2 Add `LOG_LEVEL` define to `secrets.h` with documentation of all levels (0-5)
  - [x] 12.3 Call `esp_log_level_set("*", LOG_LEVEL)` in `setup()` with per-module override examples
  - [x] 12.4 Replace all `Serial.println`/`Serial.printf` calls with appropriate `ESP_LOGx` macros
  - [x] 12.5 Add ESP_LOGE for all error conditions (connection failures, cert load failures, handshake errors)
  - [x] 12.6 Add ESP_LOGW for warnings (PQC fallback, SNI failure, WiFi retry, MQTT retry)
  - [x] 12.7 Add ESP_LOGI for normal operations (connected, published, subscribed, handshake complete)
  - [x] 12.8 Add ESP_LOGD for diagnostics (heap usage, cert sizes, task creation params, stack high water marks)
  - [x] 12.9 Add ESP_LOGV for I/O-level detail (send/recv byte counts, handshake polling, WiFi status polling)
  **Requirements:** 17

- [x] 13. Implement MQTT best practices (LWT, lifecycle, backoff, topic structure)
  - [x] 13.1 Define structured topic hierarchy: `dt/<thing>/telemetry`, `dt/<thing>/lifecycle`, `cmd/<thing>/+`
  - [x] 13.2 Build topics dynamically from THINGNAME at runtime with `initTopics()` helper
  - [x] 13.3 Configure LWT in `mqttClient.connect()` with offline payload, QoS 1, and retained flag
  - [x] 13.4 Publish retained "online" lifecycle event on successful MQTT connection with device metadata
  - [x] 13.5 Implement `calculateBackoff()` with exponential delay and full jitter
  - [x] 13.6 Apply exponential backoff to MQTT reconnection (base 1s, max 2min, 2x multiplier)
  - [x] 13.7 Apply exponential backoff with jitter to WiFi reconnection
  - [x] 13.8 Seed RNG with analog noise + chip eFuse MAC for jitter entropy
  - [x] 13.9 Reset backoff counters on successful connection
  - [x] 13.10 Subscribe to command wildcard topic `cmd/<thing>/+` with QoS 1
  **Requirements:** 18, 19, 20, 21

- [x] 14. Create v1.0 classical TLS firmware for PQC migration demo
  - [x] 14.1 Create `aws_iot_pqc_esp32_v1_classical/` sketch folder with `secrets.h`
  - [x] 14.2 Implement WiFi task with exponential backoff (same pattern as v2.0)
  - [x] 14.3 Implement MQTT task using `WiFiClientSecure` (mbedTLS, classical TLS 1.2)
  - [x] 14.4 Report `pqc_algorithm: "none"` and `tls_version: "TLS 1.2"` in telemetry
  - [x] 14.5 Include `firmware_version: "1.0.0"` in telemetry and lifecycle events
  - [x] 14.6 Implement LWT and lifecycle events (same topic structure as v2.0)
  - [x] 14.7 Subscribe to `cmd/<thing>/+` for command handling
  - [x] 14.8 Implement `handleOtaMigration()` — validates next OTA partition has valid firmware
  - [x] 14.9 On `migrate-pqc` command, call `esp_ota_set_boot_partition()` and `esp_restart()`
  - [x] 14.10 Print current boot partition info at startup for debugging
  **Requirements:** 22

- [x] 15. Document PQC migration techniques
  - [x] 15.1 Add "PQC Migration Techniques" section to README with overview
  - [x] 15.2 Document Option A: HTTPS OTA pull (flow, pros, cons, best-fit)
  - [x] 15.3 Document Option B: AWS IoT Jobs (flow, pros, cons, best-fit)
  - [x] 15.4 Document Option C: Dual-partition switch (flow, pros, cons, best-fit)
  - [x] 15.5 Include pre-flashing instructions with `esptool.py` commands
  - [x] 15.6 Show before/after telemetry payload comparison
  - [x] 15.7 Add project structure showing both firmware folders
  **Requirements:** 23

## Notes

- All tasks are complete. The sketch is a single `.ino` file for Arduino Cloud IDE compatibility.
- wolfSSL configuration in `wolfssl_user_settings.h` is a reference file; users must configure their wolfSSL Arduino library installation to match these settings.
- Testing is hardware-based (no simulator); verification relies on serial output and AWS IoT Core MQTT test client.
- The project targets all ESP32 WiFi variants (ESP32, ESP32-S2, ESP32-S3, ESP32-C3, ESP32-C6) with automatic core-pinning adaptation.

## Task Dependency Graph

```json
{
  "waves": [
    [1],
    [2, 3, 6],
    [5],
    [4],
    [7, 9],
    [8],
    [12],
    [13],
    [14],
    [15],
    [10],
    [11]
  ]
}
```
