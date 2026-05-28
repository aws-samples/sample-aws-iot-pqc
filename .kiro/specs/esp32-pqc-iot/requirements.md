# Requirements Document

## Introduction

This project is an Arduino sketch sample that demonstrates connecting an ESP32 microcontroller to AWS IoT Core using post-quantum cryptography (PQC) via wolfSSL. WiFi connectivity is managed as a FreeRTOS task with automatic recovery, and MQTT communication uses mutual TLS authentication with hybrid post-quantum key exchange algorithms.

## Glossary

| Term | Definition |
|------|-----------|
| PQC | Post-Quantum Cryptography — cryptographic algorithms designed to be secure against quantum computer attacks |
| ML-KEM/Kyber | Module-Lattice-based Key Encapsulation Mechanism, a NIST-standardized post-quantum key exchange algorithm |
| TLS 1.3 | Transport Layer Security version 1.3, the latest TLS protocol version |
| mTLS | Mutual TLS — both client and server authenticate each other using certificates |
| ECDHE | Elliptic Curve Diffie-Hellman Ephemeral — a classical key exchange algorithm |
| FreeRTOS | Free Real-Time Operating System, built into the ESP32 Arduino core |
| MQTT | Message Queuing Telemetry Transport — a lightweight messaging protocol for IoT |
| SNI | Server Name Indication — a TLS extension for routing to the correct server |
| PROGMEM | Program Memory — flash storage on microcontrollers used to avoid heap consumption |
| AWS IoT Core | Amazon Web Services managed cloud service for IoT device connectivity |

## Requirements

### Requirement 1: Post-Quantum TLS Connection
**User Story:** As an IoT developer, I want to establish a TLS 1.3 connection using hybrid post-quantum key exchange, so that my device communication is protected against both classical and quantum attacks.

#### Acceptance Criteria
1. The sketch establishes a TLS 1.3 connection to AWS IoT Core.
2. The connection uses a hybrid post-quantum key exchange algorithm combining classical ECDHE with ML-KEM/Kyber.

### Requirement 2: Mutual TLS Authentication
**User Story:** As an IoT developer, I want to authenticate my device using mutual TLS with X.509 certificates, so that only authorized devices can connect to my AWS IoT Core endpoint.

#### Acceptance Criteria
1. The sketch authenticates to AWS IoT Core using mutual TLS (mTLS).
2. Authentication uses X.509 device certificates including device certificate, private key, and Amazon Root CA.

### Requirement 3: MQTT Publish
**User Story:** As an IoT developer, I want my device to publish JSON telemetry at regular intervals, so that I can monitor device health and status remotely.

#### Acceptance Criteria
1. The sketch publishes JSON telemetry messages to a configurable MQTT topic.
2. Messages are published at a regular interval (default 30 seconds).
3. Telemetry includes device name, uptime, WiFi RSSI, free heap, PQC algorithm name, and TLS version.

### Requirement 4: MQTT Subscribe
**User Story:** As an IoT developer, I want my device to subscribe to an MQTT topic and display received messages, so that I can send commands or configuration updates to the device.

#### Acceptance Criteria
1. The sketch subscribes to a configurable MQTT topic.
2. Received messages are printed to the serial console.

### Requirement 5: WiFi as FreeRTOS Task
**User Story:** As an embedded developer, I want WiFi managed as a dedicated FreeRTOS task, so that WiFi connectivity operates independently of MQTT logic and improves system reliability.

#### Acceptance Criteria
1. WiFi connectivity is managed as a dedicated FreeRTOS task.
2. The WiFi task runs independently of the MQTT logic.

### Requirement 6: Automatic WiFi Recovery
**User Story:** As an IoT developer, I want the device to automatically reconnect WiFi when the connection is lost, so that the device recovers from network interruptions without manual intervention.

#### Acceptance Criteria
1. If the WiFi connection is lost, the WiFi task automatically attempts to reconnect.
2. Reconnection uses a configurable timeout and retry delay.
3. Recovery does not require a device reboot.

### Requirement 7: Automatic MQTT Recovery
**User Story:** As an IoT developer, I want the MQTT connection to automatically re-establish after a disconnection, so that telemetry publishing resumes without manual intervention once WiFi is restored.

#### Acceptance Criteria
1. If the MQTT connection is lost (due to WiFi loss or server disconnect), the MQTT task detects the failure.
2. The TLS and MQTT connection is automatically re-established once WiFi is restored.

### Requirement 8: PQC Fallback
**User Story:** As an IoT developer, I want the device to fall back to a lower-level PQC algorithm if the primary one is unsupported, so that the connection succeeds even with varying server capabilities.

#### Acceptance Criteria
1. If the primary PQC key exchange (P-521 + Kyber Level 5) is not supported by the server, the sketch falls back to P-256 + Kyber Level 1.
2. The sketch only fails the connection after the fallback option is also rejected.

### Requirement 9: Multi-Chip Compatibility
**User Story:** As an embedded developer, I want the sketch to work on any ESP32 WiFi variant, so that I can use it across different hardware without modification.

#### Acceptance Criteria
1. The sketch works on ESP32, ESP32-S2, ESP32-S3, ESP32-C3, and ESP32-C6.
2. Task pinning automatically adapts for single-core vs dual-core chips.

### Requirement 10: Arduino Cloud IDE Compatibility
**User Story:** As an IoT developer, I want the sketch structured for the Arduino Cloud IDE, so that I can easily import and use it without restructuring files.

#### Acceptance Criteria
1. The sketch is structured to work with the Arduino Cloud IDE (app.arduino.cc).
2. File organization follows Arduino Cloud IDE conventions.
3. Documentation covers importing custom libraries.

### Requirement 11: Memory Efficiency
**User Story:** As an embedded developer, I want the sketch to operate within memory constraints and report usage, so that I can ensure stable long-term operation on resource-constrained hardware.

#### Acceptance Criteria
1. The sketch operates with a minimum of 80 KB free heap available before initiating the TLS handshake.
2. Heap usage is reported via serial output.

### Requirement 12: Handshake Performance
**User Story:** As an IoT developer, I want the TLS handshake to complete within a reasonable time with a timeout mechanism, so that the device does not hang indefinitely during connection attempts.

#### Acceptance Criteria
1. The TLS 1.3 PQC handshake completes within 30 seconds.
2. A timeout mechanism prevents indefinite blocking.

### Requirement 13: Thread Safety
**User Story:** As an embedded developer, I want shared state between tasks to be properly synchronized, so that concurrent access does not cause data corruption or race conditions.

#### Acceptance Criteria
1. Shared state between the WiFi and MQTT tasks (e.g., connection status) is protected by a mutex or equivalent synchronization primitive.

### Requirement 14: Serial Diagnostics
**User Story:** As an IoT developer, I want structured diagnostic output on the serial console, so that I can monitor and debug device behavior during development and deployment.

#### Acceptance Criteria
1. The sketch outputs structured diagnostic messages to the serial console at 115200 baud.
2. Diagnostics cover WiFi status, TLS handshake progress, MQTT connection state, and periodic system health.
3. Messages use structured prefixes for easy filtering.

### Requirement 15: Security
**User Story:** As an IoT developer, I want the sketch to follow security best practices, so that credentials and communications are protected in both the repository and at runtime.

#### Acceptance Criteria
1. Certificates are stored in PROGMEM (flash) to avoid heap consumption.
2. Server certificate verification is enabled (no insecure skip-verify).
3. SNI (Server Name Indication) is set for AWS IoT endpoint routing.
4. The `secrets.h` file contains placeholder values only in the repository (no real credentials committed).

### Requirement 16: Documentation
**User Story:** As an IoT developer, I want comprehensive documentation, so that I can set up the project end-to-end without prior knowledge of the specific tools and services involved.

#### Acceptance Criteria
1. Documentation includes installation steps for both Arduino Cloud IDE and Desktop IDE.
2. Documentation covers AWS IoT Core setup (Thing creation, certificates, policies).
3. Documentation includes wolfSSL PQC configuration instructions.
4. Documentation provides a code walkthrough explaining each component.
5. Documentation includes a troubleshooting guide for common errors.

### Requirement 17: Configurable Logging with Espressif Logger
**User Story:** As an IoT developer, I want to control the verbosity of serial output using the Espressif logging framework with per-module log levels, so that I can efficiently debug issues in development while keeping production output minimal.

#### Acceptance Criteria
1. The sketch uses the Espressif `esp_log` framework (`ESP_LOGE`, `ESP_LOGW`, `ESP_LOGI`, `ESP_LOGD`, `ESP_LOGV`) instead of raw `Serial.print` calls.
2. A configurable `LOG_LEVEL` define in `secrets.h` sets the global log verbosity (0=NONE through 5=VERBOSE).
3. Each logical component (WiFi, MQTT, TLS, wolfSSL, main) has its own log tag for per-module filtering via `esp_log_level_set()`.
4. All functions include logging at appropriate levels: errors for failures, warnings for retries/fallbacks, info for normal operations, debug for diagnostics, verbose for I/O-level detail.
5. The log level can be changed without modifying the main sketch file.

### Requirement 18: MQTT Last Will and Testament (LWT)
**User Story:** As an IoT developer, I want the MQTT broker to automatically publish an offline status message when my device disconnects unexpectedly, so that other services and dashboards know the device is unreachable without waiting for a polling timeout.

#### Acceptance Criteria
1. The MQTT connect call includes a Last Will and Testament (LWT) configuration.
2. The LWT message is published to the device's lifecycle topic with a JSON payload indicating offline status and reason "unexpected".
3. The LWT uses QoS 1 (at least once delivery) to ensure the message is not lost.
4. The LWT uses the retained flag so new subscribers immediately see the last known device state.

### Requirement 19: Device Lifecycle Events
**User Story:** As an IoT developer, I want my device to publish online/offline lifecycle events, so that cloud services can track device presence and trigger automations based on connectivity changes.

#### Acceptance Criteria
1. On successful MQTT connection, the device publishes a retained "online" lifecycle event with device metadata (thing name, IP address, firmware version, PQC algorithm, uptime).
2. On unexpected disconnection, the broker publishes the LWT "offline" event automatically.
3. Lifecycle events are published to a dedicated lifecycle topic separate from telemetry.
4. The online message is retained so new subscribers immediately know the device state.

### Requirement 20: Exponential Backoff with Jitter
**User Story:** As an IoT developer, I want reconnection attempts to use exponential backoff with jitter, so that many devices recovering from a network outage do not overwhelm the broker simultaneously (thundering herd prevention).

#### Acceptance Criteria
1. Both WiFi and MQTT reconnection use exponential backoff (configurable base delay, multiplier, and maximum delay).
2. Full jitter is applied to the backoff delay (random value between 0 and the calculated exponential delay).
3. The backoff counter resets to zero on successful connection.
4. The random number generator is seeded with hardware entropy (analog noise + chip ID) to ensure different devices produce different jitter values.
5. Backoff parameters are configurable via `#define` constants.

### Requirement 21: Structured MQTT Topic Hierarchy
**User Story:** As an IoT developer, I want MQTT topics to follow a structured hierarchy with clear prefixes and thing-name scoping, so that I can write fine-grained IoT policies and route messages efficiently with the IoT Rules Engine.

#### Acceptance Criteria
1. Telemetry is published to `dt/<thingName>/telemetry` (device-to-cloud data).
2. Lifecycle events are published to `dt/<thingName>/lifecycle` (online/offline status).
3. Commands are received on `cmd/<thingName>/+` (cloud-to-device, wildcard subscribe).
4. Topics are constructed dynamically from the configured Thing name at runtime.
5. The topic structure enables per-thing, per-action IoT policy rules.
