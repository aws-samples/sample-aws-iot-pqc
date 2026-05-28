# Technical Design

## Overview

The system is an Arduino sketch that connects an ESP32 microcontroller to AWS IoT Core using post-quantum cryptography (PQC) via wolfSSL. It consists of three concurrent execution contexts running on the ESP32's FreeRTOS kernel: a WiFi management task with automatic recovery, an MQTT task handling TLS 1.3 connections with hybrid post-quantum key exchange, and the Arduino main loop for status reporting.

```
+------------------------------------------------------------------+
|                        ESP32 FreeRTOS                             |
|                                                                  |
|  Core 0                          Core 1 (or Core 0 if unicore)   |
|  +----------------------------+  +-----------------------------+ |
|  |     WiFi Task (4 KB)       |  |     MQTT Task (16 KB)       | |
|  |                            |  |                             | |
|  |  - WiFi.begin/disconnect   |  |  - wolfSSL context init     | |
|  |  - Connection monitoring   |  |  - WolfSSLClient (TLS 1.3)  | |
|  |  - Auto reconnect loop     |  |  - PubSubClient (MQTT)      | |
|  |  - Status flag update       |  |  - Publish/Subscribe loop   | |
|  +----------------------------+  +-----------------------------+ |
|                                                                  |
|  +----------------------------+                                  |
|  |     Main Loop              |  Shared State (mutex-protected): |
|  |  - Status reporting (60s)  |  - g_wifiConnected (volatile)   |
|  |  - Watchdog monitoring     |  - g_mqttConnected (volatile)   |
|  +----------------------------+  - g_wifiMutex (semaphore)       |
+------------------------------------------------------------------+
```

## Architecture

The system uses a multi-task architecture on the ESP32's FreeRTOS kernel. The WiFi task (Core 0) manages connectivity independently from the MQTT task (Core 1), which handles TLS 1.3 PQC connections and MQTT pub/sub. Shared state is synchronized via a FreeRTOS mutex. The Arduino main loop provides periodic status reporting. This separation ensures WiFi recovery does not block MQTT logic and vice versa.

## Components and Interfaces

### 1. WolfSSLClient Class

A custom Arduino `Client` implementation that layers wolfSSL TLS 1.3 on top of a plain `WiFiClient`.

**Responsibilities:**
- Manage TCP connection via `WiFiClient`
- Create and configure `WOLFSSL` session objects
- Bridge wolfSSL I/O to Arduino's `WiFiClient` via custom callbacks
- Request hybrid PQC key exchange during handshake
- Expose standard Arduino `Client` interface for PubSubClient compatibility

**Design decisions:**
- Uses wolfSSL's custom I/O callback mechanism (`wolfSSL_SSLSetIORecv`/`Send`) rather than file descriptors, because Arduino `WiFiClient` does not expose raw sockets.
- Non-blocking handshake loop with `vTaskDelay` yields to FreeRTOS during `WANT_READ`/`WANT_WRITE`.
- PQC key share is set per-session (not per-context) to allow fallback logic.

### 2. wolfSSL Context (WOLFSSL_CTX)

A single shared TLS context created once during initialization.

**Configuration:**
- TLS 1.3 client method only (`wolfTLSv1_3_client_method`)
- Amazon Root CA 1 loaded for server verification
- Device certificate and private key loaded for client authentication
- Peer verification enabled (`WOLFSSL_VERIFY_PEER`)
- Custom I/O callbacks registered at context level

**Thread safety:** The context is created once before any connections and is read-only thereafter, making it safe to share across tasks.

### 3. WiFi FreeRTOS Task

**State machine:**
```
    +--------+     WiFi.begin()     +-----------+
    | DISCON |--------------------->| CONNECTING|
    +--------+                      +-----------+
        ^                                |
        |  timeout / failure             | WL_CONNECTED
        +--------------------------------+
                                         v
                                    +-----------+
                                    | CONNECTED |
                                    +-----------+
                                         |
                                    WiFi.status() != WL_CONNECTED
                                         |
                                         v
                                    +--------+
                                    | DISCON  |
                                    +--------+
```

**Recovery strategy:**
1. Disconnect cleanly (`WiFi.disconnect(true)`)
2. Wait 500 ms for radio to settle
3. Call `WiFi.begin()` with credentials
4. Poll `WiFi.status()` with 500 ms intervals up to 15 s timeout
5. On failure, wait 5 s before retrying

### 4. MQTT FreeRTOS Task

**Lifecycle:**
1. Block until `g_wifiConnected == true`
2. Initialize wolfSSL context (once)
3. Create `WolfSSLClient` and `PubSubClient` instances
4. Enter main loop:
   - If WiFi lost: disconnect MQTT, block until WiFi returns
   - If MQTT disconnected: attempt reconnect with 5 s retry delay
   - If connected: call `mqttClient.loop()`, publish telemetry every 30 s

### 5. PQC Key Exchange Strategy

```
wolfSSL_UseKeyShare(ssl, WOLFSSL_P521_KYBER_LEVEL5)
    |
    +-- SUCCESS --> Use P-521 + Kyber Level 5
    |
    +-- FAILURE --> wolfSSL_UseKeyShare(ssl, WOLFSSL_P256_KYBER_LEVEL1)
                       |
                       +-- SUCCESS --> Use P-256 + Kyber Level 1
                       |
                       +-- FAILURE --> Abort connection
```

If neither hybrid option is accepted by the server during the TLS handshake, wolfSSL will negotiate the best available classical key exchange (server-driven selection).

### 6. Data Flow

#### Outbound (Publish)
```
Telemetry JSON --> PubSubClient::publish()
    --> WolfSSLClient::write()
        --> wolfSSL_write() [encrypts with TLS 1.3]
            --> wolfssl_send_cb()
                --> WiFiClient::write() [TCP]
                    --> WiFi radio --> AWS IoT Core
```

#### Inbound (Subscribe)
```
AWS IoT Core --> WiFi radio --> TCP buffer
    --> WiFiClient::read()
        --> wolfssl_recv_cb()
            --> wolfSSL_read() [decrypts]
                --> WolfSSLClient::read()
                    --> PubSubClient::loop() [parses MQTT]
                        --> mqttCallback()
```

### File Organization

| File | Responsibility |
|------|---------------|
| `aws_iot_pqc_esp32.ino` | All runtime code: classes, tasks, setup/loop |
| `secrets.h` | User-editable configuration and credentials |
| `wolfssl_user_settings.h` | Reference wolfSSL compile-time configuration (not included directly) |

Single-file sketch design was chosen for Arduino Cloud IDE compatibility, where multi-file `.cpp`/`.h` projects require more complex import workflows.

### Dependencies

| Library | Version | Purpose |
|---------|---------|---------|
| wolfSSL (Arduino) | 5.7.0+ | TLS 1.3, Kyber/ML-KEM, X.509, mTLS |
| PubSubClient | 2.8+ | MQTT 3.1.1 client |
| WiFi (ESP32 core) | Built-in | WiFi STA mode |
| FreeRTOS (ESP32 core) | Built-in | Task management, mutexes |

## Data Models

### Telemetry Message Format (JSON)

Published every 30 seconds to the configured MQTT topic:

```json
{
  "device": "<THING_NAME>",
  "uptime_ms": 123456,
  "wifi_rssi": -45,
  "free_heap": 180000,
  "pqc_algorithm": "P-521_KYBER_LEVEL5",
  "tls_version": "TLSv1.3"
}
```

### Shared State

| Variable | Type | Protection | Purpose |
|----------|------|-----------|---------|
| `g_wifiConnected` | `volatile bool` | Mutex (`g_wifiMutex`) | WiFi connection status flag |
| `g_mqttConnected` | `volatile bool` | Mutex (`g_wifiMutex`) | MQTT connection status flag |
| `g_wifiMutex` | `SemaphoreHandle_t` | N/A | FreeRTOS binary semaphore for shared state |

### wolfSSL I/O Context

```c
typedef struct {
    WiFiClient* client;  // Pointer to underlying TCP client
} wolfssl_io_ctx_t;
```

### Configuration Constants (from secrets.h)

| Constant | Type | Description |
|----------|------|-------------|
| `WIFI_SSID` | `const char*` | WiFi network name |
| `WIFI_PASSWORD` | `const char*` | WiFi password |
| `AWS_IOT_ENDPOINT` | `const char*` | AWS IoT Core endpoint hostname |
| `THING_NAME` | `const char*` | AWS IoT Thing name (MQTT client ID) |
| `MQTT_PUB_TOPIC` | `const char*` | MQTT publish topic |
| `MQTT_SUB_TOPIC` | `const char*` | MQTT subscribe topic |
| `aws_root_ca_pem` | `const char[] PROGMEM` | Amazon Root CA 1 certificate |
| `device_cert_pem` | `const char[] PROGMEM` | Device X.509 certificate |
| `device_key_pem` | `const char[] PROGMEM` | Device private key |

### Timing Constants

| Constant | Default | Description |
|----------|---------|-------------|
| `WIFI_TIMEOUT_MS` | 15000 | WiFi connection attempt timeout |
| `WIFI_RETRY_DELAY_MS` | 5000 | Delay between WiFi reconnection attempts |
| `MQTT_RETRY_DELAY_MS` | 5000 | Delay between MQTT reconnection attempts |
| `PUBLISH_INTERVAL_MS` | 30000 | Telemetry publish interval |
| `STATUS_INTERVAL_MS` | 60000 | Main loop status report interval |
| `HANDSHAKE_TIMEOUT_MS` | 30000 | TLS handshake timeout |

## Correctness Properties

### Property 1: PQC Key Exchange
**Validates: Requirements 1.2**
The TLS connection MUST use a hybrid post-quantum key exchange (P-521 + Kyber Level 5 or P-256 + Kyber Level 1) or abort if neither is supported.

### Property 2: Mutual Authentication
**Validates: Requirements 2.1**
Both client and server MUST be authenticated via X.509 certificates; connections with unverified peers MUST be rejected.

### Property 3: Thread Safety
**Validates: Requirements 13.1**
All reads and writes to `g_wifiConnected` and `g_mqttConnected` MUST be protected by the `g_wifiMutex` semaphore.

### Property 4: Memory Safety
**Validates: Requirements 11.1**
Free heap MUST remain above 80 KB before initiating a TLS handshake to prevent stack overflow or allocation failures.

### Property 5: Recovery Invariant
**Validates: Requirements 7.2**
After any WiFi disconnection, the MQTT task MUST detect the loss, tear down the TLS session, and re-establish both TLS and MQTT once WiFi is restored, without requiring a device reboot.

### Property 6: Handshake Timeout
**Validates: Requirements 12.2**
The TLS handshake MUST complete within 30 seconds or be aborted to prevent indefinite blocking.

### Property 7: Certificate Storage
**Validates: Requirements 15.1**
All certificates and keys MUST be stored in PROGMEM (flash) to avoid heap consumption.

### Property 8: Multi-Core Adaptation
**Validates: Requirements 9.2**
Task pinning MUST automatically adapt for single-core vs dual-core ESP32 variants using `CONFIG_FREERTOS_UNICORE`.

## Error Handling

### WiFi Errors
- **Connection timeout:** If `WiFi.status()` does not reach `WL_CONNECTED` within 15 seconds, the task disconnects cleanly and retries after a 5-second delay.
- **Unexpected disconnection:** Detected by polling `WiFi.status()` in the WiFi task loop. Triggers the reconnection state machine.
- **Radio failure:** `WiFi.disconnect(true)` is called before each retry to reset the radio hardware.

### TLS/wolfSSL Errors
- **Handshake timeout:** A 30-second timeout prevents indefinite blocking. On timeout, the session is freed and connection is retried.
- **PQC negotiation failure:** If the primary key share (P-521 + Kyber Level 5) fails, the system falls back to P-256 + Kyber Level 1. If both fail, the connection is aborted with a diagnostic message.
- **Certificate verification failure:** wolfSSL returns an error if the server certificate cannot be verified against the loaded Root CA. The connection is rejected and the error is logged.
- **I/O errors during handshake:** `WANT_READ`/`WANT_WRITE` are handled with `vTaskDelay` yields; other errors abort the handshake.

### MQTT Errors
- **Connection failure:** If `PubSubClient::connect()` fails, the task waits 5 seconds before retrying.
- **Publish failure:** Logged to serial; the task continues operating and retries on the next publish interval.
- **WiFi loss during MQTT operation:** The MQTT task detects WiFi loss, disconnects the MQTT client, and blocks until WiFi is restored before reconnecting.

### Memory Errors
- **Low heap detection:** Before initiating a TLS handshake, free heap is checked. If below 80 KB, the connection attempt is deferred with a warning message.
- **wolfSSL allocation failure:** wolfSSL returns error codes on memory allocation failures, which are logged and trigger a connection retry.

### Serial Diagnostics
All errors are reported to the serial console at 115200 baud with structured prefixes (`[WiFi]`, `[MQTT]`, `[wolfSSL]`, `[Setup]`) for easy filtering and debugging.

## Testing Strategy

### Build-Time Validation
- Compile-time `#error` directives verify that `WOLFSSL_TLS13` and `WOLFSSL_HAVE_KYBER` are defined, catching misconfigured wolfSSL builds before runtime.

### Hardware Integration Testing
Since this is an embedded Arduino sketch targeting physical ESP32 hardware with no simulator available, testing is performed through:

1. **Serial output verification:** Monitor serial console at 115200 baud to verify:
   - WiFi connection and IP address assignment
   - TLS handshake completion with PQC cipher suite reported
   - MQTT connection and subscription confirmation
   - Telemetry publish at 30-second intervals
   - Correct JSON payload format

2. **WiFi recovery testing:** Disable/enable the WiFi access point and verify:
   - WiFi task detects disconnection and enters retry loop
   - MQTT task detects WiFi loss and disconnects
   - Both tasks recover automatically when WiFi returns

3. **MQTT recovery testing:** Disconnect the MQTT broker (e.g., revoke IoT policy temporarily) and verify:
   - MQTT task detects disconnection
   - Automatic reconnection after policy is restored

4. **PQC fallback testing:** Configure a server that only supports Level 1 Kyber and verify:
   - Primary P-521 + Kyber Level 5 attempt fails gracefully
   - Fallback to P-256 + Kyber Level 1 succeeds

5. **Multi-chip testing:** Flash the sketch to ESP32, ESP32-S3, and ESP32-C3 variants and verify:
   - Correct core pinning behavior (dual-core vs single-core)
   - Successful TLS handshake and MQTT operation on each variant

6. **Memory monitoring:** Observe free heap reported in telemetry over extended operation (24+ hours) to verify no memory leaks.

### AWS IoT Core Verification
- Verify messages appear in the AWS IoT Core MQTT test client on the configured publish topic
- Send a message to the subscribe topic and verify it appears on the device serial console
