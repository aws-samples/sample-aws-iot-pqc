# ESP32 AWS IoT Core with post-quantum cryptography (PQC)

The complete source for the **Post-Quantum Cryptography Migration for IoT**
workshop: ESP32 firmware (classical and PQC variants), a containerized
PQC-enabled Mosquitto broker, the cloud build/registration/OTA-migration
pipeline, and a live dashboard. A field device connects over TLS 1.3 with a
hybrid ML-KEM key exchange and is migrated to post-quantum cryptography over the
air — protecting data against both classical and future quantum attacks.

The device firmware (wolfSSL TLS 1.3 + ML-KEM) runs WiFi and MQTT as independent
FreeRTOS tasks with automatic failure recovery.

> **To deploy the workshop, see [`cloudformation/DEPLOYMENT.md`](cloudformation/DEPLOYMENT.md).**
> You do not build or flash anything by hand — firmware is compiled in the cloud
> and flashed from the Builder-Pi.

## Table of contents

- [Why post-quantum cryptography?](#why-post-quantum-cryptography)
- [How it works](#how-it-works)
- [Workshop architecture (containerized PQC broker)](#workshop-architecture-containerized-pqc-broker)
- [Project structure](#project-structure)
- [Supported hardware](#supported-hardware)
- [Deploying the workshop](#deploying-the-workshop)
- [Code walkthrough](#code-walkthrough)
- [MQTT best practices](#mqtt-best-practices)
- [PQC migration techniques](#pqc-migration-techniques)
- [Memory and performance](#memory-and-performance)
- [Troubleshooting](#troubleshooting)
- [Device registration](#device-registration)
- [Security considerations](#security-considerations)
- [Cleanup and teardown](#cleanup-and-teardown)
- [References](#references)
- [License](#license)

---

## Why post-quantum cryptography?

Current public-key algorithms (RSA, ECDSA, ECDHE) are vulnerable to future large-scale quantum computers. The "harvest now, decrypt later" (HNDL) threat means an adversary can capture encrypted traffic today and decrypt it later once a cryptographically relevant quantum computer (CRQC) exists.

Hybrid PQC key exchange addresses this by combining a classical algorithm (ECDHE) with a quantum-resistant one (ML-KEM / Kyber) in a single TLS handshake. If either algorithm is broken, the other still protects the session. AWS is actively migrating its services to PQC, and this sample shows how IoT devices can participate in that migration today.

For more detail, see the [AWS Post-Quantum Cryptography Migration Plan](https://aws.amazon.com/security/post-quantum-cryptography/migrating-to-post-quantum-cryptography/).

---

## How it works

An ESP32 device does not talk to AWS IoT Core directly. It connects over
post-quantum TLS to a Mosquitto broker we run (on ECS Fargate, behind a Network
Load Balancer), and the broker bridges the traffic up to AWS IoT Core over a
classical mTLS connection:

![How a field device reaches AWS IoT Core: an ESP32-S3 device connects over MQTT-over-PQC-TLS (hybrid ML-KEM) to the internet-facing pqc-broker-nlb Network Load Balancer (8883 classical / 8884 ML-DSA, TCP passthrough), which passes TCP 8883 through to the ECS Fargate Mosquitto PQC broker (OpenSSL 3.5 + ML-KEM, private subnet, no public IP), which bridges over classical mTLS to the AWS IoT Core MQTT data endpoint](docs/images/broker-path.png)

On the device side, the firmware loads its identity (certificates, keys, WiFi)
from the `certs` partition at boot, then runs independent FreeRTOS tasks:

1. The **WiFi task** manages the wireless connection on core 0 and reconnects on
   failure.
2. The **MQTT task** waits for WiFi, sets the clock over NTP, sets up wolfSSL
   with the device's certificate, then opens a TLS 1.3 connection to the
   Mosquitto broker's load balancer (8883, or 8884 for the ML-DSA variant). On
   the PQC variants it requests the hybrid P-256 + ML-KEM-768 key exchange. Once
   connected it publishes telemetry every 30 seconds and handles commands.
3. The **OTA task** waits for an update command, then downloads, verifies and
   installs the new firmware.
4. The **main loop** prints a status report every 60 seconds.

If WiFi drops, the WiFi task reconnects automatically. The MQTT task detects the loss, waits for WiFi to return, then re-establishes the TLS and MQTT connection from scratch.

---

## Workshop architecture (containerized PQC broker)

For the re:Invent builders session, the PQC-enabled Mosquitto broker runs as a **container on ECS Fargate** behind a Network Load Balancer — there is no Raspberry Pi gateway and no AWS IoT Greengrass in the data path. ESP32 devices connect straight to the load balancer over PQC TLS 1.3, and the broker bridges their traffic up to AWS IoT Core. A separate Raspberry Pi (the "builder-pi") is used only to flash devices, not to route MQTT.

![End-to-end architecture of the PQC IoT migration solution: the re:Invent physical room (Builder-Pi flashing/WiFi-AP station and ESP32-S3 devices), the VPC with the internet-facing Network Load Balancer in public subnets and the ECS Fargate Mosquitto PQC broker in private subnets (egress via NAT gateway), the two AWS Private CAs and Secrets Manager, the MQTT bridge to AWS IoT Core, IoT Rules feeding the telemetry-processor/message-logger Lambdas into DynamoDB and AppSync, Cognito, and the CloudFront+S3 v2 dashboard](docs/images/architecture.png)

### PQC key exchange: SecP256r1MLKEM768

Both the ESP32 and Mosquitto negotiate the **IANA-standardized** hybrid key exchange `SecP256r1MLKEM768` (code point 4587). This combines classical ECDHE P-256 with quantum-resistant ML-KEM-768 in a single TLS 1.3 handshake. The broker gets ML-KEM support from OpenSSL 3.5 with the oqs-provider.

| Component | TLS Library | Named Group | Code Point |
|-----------|-------------|-------------|-----------|
| ESP32 (PQC firmware) | wolfSSL 5.7+ | SecP256r1MLKEM768 | 4587 |
| Mosquitto broker (container) | OpenSSL 3.5+ | SecP256r1MLKEM768 | 4587 |
| Mosquitto → IoT Core bridge | OpenSSL 3.5 | secp256r1 (classical) | 23 |

### Certificate trust

The broker stack creates four **AWS Private CA** roots, in two pairs:

- **Device and broker roots** (ECDSA P-256 for the 8883 listener, ML-DSA-44 for
  8884) issue the broker's server certificates and every device certificate.
  Each broker listener accepts only client certificates from its own root, so
  every connection is mutually authenticated.
- **Firmware-signing roots** (ECDSA P-256 and ML-DSA-44) issue only the two OTA
  signer certificates, with the `codeSigning` EKU. A device trusts these for OTA
  signatures and nothing else, and the device roots for TLS and nothing else, so
  a key extracted from any board cannot sign firmware.

The broker's certificates and keys, the signers, and the IoT Core bridge
credentials are stored in Secrets Manager.

### Workshop flow

1. **Pre-session:** Facilitator deploys the single master CloudFormation stack, runs the one-time firmware image build, and registers the builder-pi. Devices are flashed with classical firmware (OTA_0).
2. **Attendee plugs in ESP32:** Device boots OTA_0 (classical TLS 1.3), connects to the container broker via the NLB, and appears on the dashboard.
3. **Code walkthrough:** Facilitator explains the PQC firmware differences.
4. **Firmware migration:** Attendee clicks **Migrate to PQC** on the dashboard → an asynchronous Step Functions workflow builds the PQC firmware, pushes it over the air, and the device reboots into OTA_1 (PQC). Live status (`building → pushing-ota → rebooting`) is shown on the dashboard.
5. **Verification:** Dashboard shows the device now negotiating `SecP256r1MLKEM768` over TLS 1.3.

See [`cloudformation/DEPLOYMENT.md`](cloudformation/DEPLOYMENT.md) for the full deployment guide, and the [PQC Migration Techniques](#pqc-migration-techniques) section for migration details.

---

## Project structure

```
Sample-aws-iot-pqc/
+-- README.md                              This file
+-- check_setup_status.sh                  Validates AWS environment readiness
+-- aws_iot_pqc_esp32/                     The device firmware: ONE sketch builds every variant
|   +-- aws_iot_pqc_esp32.ino              setup() / loop(); starts the module tasks
|   +-- variant_config.h                   Everything that differs between classical / pqc / pqc-mldsa
|   +-- app_config.h                       Includes, build settings, task + timing constants
|   +-- device_identity.h/.cpp             Thing name + keys/certs/WiFi from the "certs" NVS partition
|   +-- wifi_manager.h/.cpp                WiFi station task (backoff + jitter)
|   +-- pqc_tls_client.h/.cpp              wolfSSL TLS 1.3 (+ hybrid ML-KEM) as an Arduino Client
|   +-- mqtt_manager.h/.cpp                Broker connection, telemetry, command dispatch
|   +-- ota_updater.h/.cpp                 Signed OTA download + SHA-256 / signature / chain checks
|   +-- diagnostics.h/.cpp                 On-demand memory report (cmd/<thing>/diag)
|   +-- status_led.h/.cpp, json_util.h/.cpp
|   +-- secrets.h.template                 Local-build settings (variant, broker); generated by CodeBuild
|   +-- wolfssl_user_settings.h            Reference wolfSSL PQC configuration
+-- broker-container/                       Containerized PQC Mosquitto broker
|   +-- cloudformation/
|   |   +-- broker-container-stack.yaml    ECS Fargate + NLB + Private CA + ECR/CodeBuild
|   +-- docker/                            Dockerfile, mosquitto.conf, entrypoint (OpenSSL 3.5 + oqs)
+-- raw_device_builder/                     Device build + registration pipeline
|   +-- cloudformation/
|   |   +-- device-builder-stack.yaml      Builder API, firmware CodeBuild, PQC migration (Step Functions)
|   +-- lambda/                            Registrar, firmware-pusher, prepare-build, publish-ota, start-migration
|   +-- builder-pi/                        workshop-builder CLI + Flask GUI (shared builder_core)
|   +-- codebuild/                         Firmware buildspecs (generate secrets.h, arduino-cli compile)
|   +-- dashboard-v2/frontend/             v2 "PQC Firmware Manager" dashboard (Vite/React)
+-- dashboard/
|   +-- infrastructure/dashboard-stack.yaml AppSync, DynamoDB, Cognito, S3/CloudFront, IoT Rules
|   +-- lambda/                            telemetry-processor, message-logger, broker-log-reader, command-publisher
+-- cloudformation/
    +-- pqc-workshop-main.yaml             Master template (nests all 6 child stacks)
    +-- device-provisioning-stack.yaml     JIT device auto-registration
    +-- ssm-hybrid-activation-stack.yaml   Builder-Pi SSM hybrid activation
    +-- deploy-workshop.sh                 One-command deploy (self-hosted)
    +-- package.sh                         Stage assets for Workshop Studio
    +-- DEPLOYMENT.md                      Deployment guide + manual steps
+-- docs/
    +-- architecture.drawio                Architecture + deployment-pipeline diagram (editable source)
    +-- architecture.drawio.png            Rendered architecture diagram
    +-- images/                            Diagrams embedded in the README and DEPLOYMENT.md
```

| Path | Description |
|------|-------------|
| `aws_iot_pqc_esp32/` | The firmware: one Arduino sketch (a `.ino` plus `.h/.cpp` modules, shown as tabs in the Arduino IDE) that builds every variant. `FIRMWARE_VARIANT` in `secrets.h` selects classical, pqc or pqc-mldsa; `variant_config.h` holds every difference between them. |
| `secrets.h` | Build settings only (variant, version, broker endpoint and port, log level). CodeBuild generates it; copy `secrets.h.template` for a local build. Keys, certificates and WiFi are read from the device's "certs" NVS partition, never compiled in. |
| `wolfssl_user_settings.h` | Reference `#define` values for wolfSSL with ML-KEM PQC on ESP32. |
| `broker-container/` | The containerized PQC Mosquitto broker: CloudFormation (ECS Fargate, NLB, Private CA, ECR/CodeBuild image build) and the Docker image sources. |
| `raw_device_builder/` | Device build/registration pipeline: builder API + firmware CodeBuild, the PQC-migration Step Functions workflow, the builder-pi CLI/GUI, and the v2 dashboard frontend. |
| `dashboard/` | Dashboard backend infrastructure and Lambdas (telemetry, message log, broker log, command publisher). |
| `cloudformation/pqc-workshop-main.yaml` | Single master template that nests all six child stacks (network, broker, device-builder, device-provisioning, SSM activation, dashboard) for one-command / Workshop Studio deployment. |

---

## Supported hardware

This sketch works with any ESP32 variant that has WiFi:

| Chip | Cores | Notes |
|------|-------|-------|
| ESP32 (original) | Dual-core | Full support, tasks pinned to separate cores |
| ESP32-S2 | Single-core | Both tasks run on core 0 |
| ESP32-S3 | Dual-core | Full support, has PSRAM option (recommended) |
| ESP32-C3 | Single-core (RISC-V) | Both tasks run on core 0 |
| ESP32-C6 | Single-core (RISC-V) | Both tasks run on core 0 |
| ESP32-H2 | Single-core (RISC-V) | No WiFi (BLE/802.15.4 only) - not supported |

Boards with PSRAM (e.g., ESP32-S3 with 8MB PSRAM) are recommended because the PQC handshake uses more memory than classical TLS.

---

## Deploying the workshop

This project is deployed as an **AWS workshop**, not by hand in an IDE. You do
**not** install the Arduino IDE, edit `secrets.h`, or flash devices from your
laptop. The cloud builds each firmware variant once per deployment (CodeBuild),
every device runs the same image, and each device's identity is written to its
`certs` partition when it is registered and flashed at the Builder-Pi.

The full, authoritative deployment runbook — prerequisites, the one-command
stack deploy, the firmware builder image build, Builder-Pi provisioning, the
workshop WiFi access point, readiness checks, and registering/flashing the first
device — lives in:

**➡️ [`cloudformation/DEPLOYMENT.md`](cloudformation/DEPLOYMENT.md)**

It mirrors the Workshop Studio operator deployment (section 99). Start there to
stand up an environment. The sections below this one are **reference and
background**. The Workshop Studio content is the authoritative explanation of the
firmware, the build pipeline, flash and memory, and each migration step; these
sections summarise it for readers of the code.

---

## Code walkthrough

The firmware is **one Arduino sketch**, `aws_iot_pqc_esp32/`, that builds three
**variants**. It is C/C++ in the Arduino sketch style: a slim `.ino` with
`setup()` / `loop()`, plus one class per job in `.h/.cpp` modules (tabs in the
Arduino IDE). The build picks the variant with `FIRMWARE_VARIANT` in the
generated `secrets.h`; everything that differs between variants is in
`variant_config.h`.

| Variant (`FIRMWARE_VARIANT`) | Key exchange | TLS certificate | Broker port | Version |
|---|---|---|---|---|
| `classical` (`FW_VARIANT_CLASSICAL`) | Classical ECDHE P-256 (TLS 1.3) | ECDSA-P256 | 8883 | `1.0.0-classical` |
| `pqc` (`FW_VARIANT_PQC`) | Hybrid P-256 + ML-KEM-768 (SecP256r1MLKEM768) | ECDSA-P256 | 8883 | `2.0.0-pqc` |
| `pqc-mldsa` (`FW_VARIANT_PQC_MLDSA`) | Hybrid P-256 + ML-KEM-768 | ML-DSA-44 (FIPS 204) | 8884 | `3.0.0-mldsa` |

`4.0.0-signer` and `5.0.0-verified` build the `pqc-mldsa` variant again; they
differ only in how the cloud signs the image (ML-DSA-44 instead of ECDSA P-256).
The sketch refuses to compile if `FIRMWARE_VARIANT` is missing or unknown, or if
the wolfSSL configuration does not enable TLS 1.3 and ML-KEM.

### Modules

| File | What it does |
|---|---|
| `aws_iot_pqc_esp32.ino` | `setup()`: starts the LED, loads the identity (fail closed), then starts the WiFi, MQTT and OTA tasks. `loop()`: a status report every 60 s. |
| `variant_config.h` | Everything that differs between variants: `VARIANT_PQC_KEX` (key share), `ID_TLS_*` (which certificate and key to load), `CERT_SIGNATURE_ALG`, banner, blink rate. |
| `app_config.h` | Shared includes, task stack sizes and priorities, timeouts, topic prefixes, and the wolfSSL compile-time guards. |
| `device_identity.h/.cpp` | `DeviceIdentity`: the Thing name from the chip MAC (`esp32-XXYYZZ`), and keys, certificates, the device and firmware-signing CA roots and WiFi from the `certs` NVS partition. Refuses an identity issued for a different chip. |
| `wifi_manager.h/.cpp` | `WifiManager`: the WiFi station task (core 0), reconnect with exponential backoff and jitter. |
| `pqc_tls_client.h/.cpp` | `WolfSSLClient`: wolfSSL TLS 1.3 under the Arduino `Client` interface, so the MQTT client can use it like any other connection. |
| `mqtt_manager.h/.cpp` | `MqttManager`: NTP sync, the TLS context, broker connect with backoff, lifecycle and telemetry publishing, and dispatch of `cmd/<thing>/+`. |
| `ota_updater.h/.cpp` | `OtaUpdater`: the signed OTA download and verification (its own task). |
| `diagnostics.h/.cpp` | `Diagnostics`: the on-demand memory report on `cmd/<thing>/diag`. |
| `status_led.h/.cpp`, `json_util.h/.cpp` | The heartbeat LED, and tiny JSON field extractors. |

### TLS: `WolfSSLClient`

`WolfSSLClient::setupContext()` runs once, after NTP has set the clock. It
creates a TLS 1.3 client context, loads this variant's CA root, device
certificate and private key (DER, from `DeviceIdentity`), checks the key matches
the certificate, and turns on peer verification (`WOLFSSL_VERIFY_PEER`).

`WolfSSLClient::connect()`:

1. Opens a TCP connection with a plain `WiFiClient` to the broker's Network Load
   Balancer.
2. Creates a wolfSSL session and wires wolfSSL's I/O to the `WiFiClient` through
   `wolfssl_send_cb` / `wolfssl_recv_cb`. The receive callback drains buffered
   bytes before it reports a closed connection, so the last TLS record is not
   lost.
3. On the PQC variants (`#if VARIANT_PQC_KEX`), requests the hybrid key share:
   `wolfSSL_UseKeyShare(ssl, WOLFSSL_SECP256R1MLKEM768)`, falling back to
   `WOLFSSL_X25519MLKEM768`. The classical variant offers a P-256 key share.
4. Sets SNI and runs the handshake with a 30 s timeout, yielding to FreeRTOS on
   `WANT_READ` / `WANT_WRITE`.
5. Records the protocol, cipher and key exchange for telemetry.

### Tasks

| Task | Core | Stack | Job |
|---|---|---|---|
| `WiFiTask` | 0 | 4 KB | Keep the station connected. |
| `MQTTTask` | 1 | 8 KB | Broker connection, telemetry every 30 s, commands. |
| `OTATask` | 1 | 24 KB | Waits on a semaphore; runs a signed OTA when one is queued. |
| `BlinkTask` | 0 | 2 KB | Heartbeat LED (slow on classical, fast on PQC). |
| `loopTask` | 1 | 4 KB | Arduino `loop()`: status report. |

Stack sizes are about twice the peaks measured on the ESP32-S3; the `diag`
command reports the live high-water marks. wolfSSL's handshake buffers are on the
heap (in PSRAM), not on the task stacks.

### Commands (`cmd/<thing>/+`)

| Topic | Payload | Effect |
|---|---|---|
| `cmd/<thing>/ota` | `{"action":"ota-update", "url", "sha256", "sigAlg", "sigUrl", "signerCertUrl", ...}` | Queue a signed OTA (sent by the migration workflow). |
| `cmd/<thing>/control` | `{"action":"led-on"}` / `{"action":"led-off"}` | Resume / pause the heartbeat LED (the dashboard LED button). |
| `cmd/<thing>/diag` | anything, `"full"` or `"dump"` | Publish a memory report to `dt/<thing>/diag`. |

The old `migrate-classical` / `migrate-pqc` / `migrate-mldsa` partition-swap
commands are refused: they picked a slot by the image's project name, which is
now the same for every variant.

---

## MQTT best practices

This sketch implements several IoT MQTT best practices that are essential for production-quality device firmware.

### Topic structure

Topics follow a structured hierarchy for clean IoT policy rules and Rules Engine routing:

| Topic | Direction | Purpose | QoS |
|-------|-----------|---------|-----|
| `dt/<thingName>/telemetry` | Device → Cloud | Periodic sensor/status data | 0 |
| `dt/<thingName>/lifecycle` | Device → Cloud | Online/offline presence | 1 (retained) |
| `cmd/<thingName>/+` | Cloud → Device | Commands (wildcard subscribe) | 1 |

The `dt/` prefix means "device telemetry" (outbound data). The `cmd/` prefix means "command" (inbound instructions). This separation enables IoT policies like:

```json
{
  "Effect": "Allow",
  "Action": "iot:Publish",
  "Resource": "arn:aws:iot:*:*:topic/dt/esp32-XXYYZZ/*"
}
```

Topics are built at runtime from the MAC-derived Thing name (`esp32-XXYYZZ`), so the same image serves every device.

### Last will and testament (LWT)

The MQTT LWT is a message the broker holds and publishes automatically if the device disconnects unexpectedly (network loss, crash, keepalive timeout). This sketch configures:

- **LWT Topic:** `dt/<thingName>/lifecycle`
- **LWT Payload:** `{"status":"offline","reason":"unexpected"}`
- **LWT QoS:** 1 (at least once delivery)
- **LWT Retain:** true (persists so new subscribers see the last state)

This means cloud services always know when a device goes offline, even if the device cannot send a graceful disconnect message.

### Lifecycle events

On successful MQTT connection, the device publishes a retained "online" event:

```json
{
  "status": "online",
  "device": "esp32-XXYYZZ",
  "firmware_version": "2.0.0",
  "pqc_algorithm": "SECP256R1_MLKEM768",
  "ip": "192.168.1.42",
  "uptime_sec": 15
}
```

Combined with the LWT, this creates a complete presence system:
- **Device connects** → publishes `{"status":"online",...}` (retained)
- **Device disconnects gracefully** → could publish `{"status":"offline","reason":"shutdown"}` (not yet implemented)
- **Device disconnects unexpectedly** → broker publishes LWT `{"status":"offline","reason":"unexpected"}`

New subscribers to the lifecycle topic immediately receive the retained message showing the current device state.

### Exponential backoff with jitter

When a connection attempt fails, the sketch does not retry immediately or at a fixed interval. Instead, it uses exponential backoff with full jitter to prevent the "thundering herd" problem (many devices reconnecting simultaneously after an outage).

**Algorithm:**
```
expDelay = min(base * 2^attempt, maxDelay)
actualDelay = random(0, expDelay)    // Full jitter
```

**Configuration (in the sketch):**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `BACKOFF_BASE_MS` | 1000 | Initial retry delay (1 second) |
| `BACKOFF_MAX_MS` | 120000 | Maximum retry delay (2 minutes) |
| `BACKOFF_MULTIPLIER` | 2 | Doubles each attempt |

**Example progression:**
- Attempt 1: random(0, 1000) → e.g., 743 ms
- Attempt 2: random(0, 2000) → e.g., 1521 ms
- Attempt 3: random(0, 4000) → e.g., 2847 ms
- Attempt 4: random(0, 8000) → e.g., 5102 ms
- ...capped at random(0, 120000)

The backoff counter resets to zero on successful connection. Both WiFi and MQTT reconnection use this pattern.

The random number generator is seeded with hardware entropy (analog pin noise XOR'd with the chip's eFuse MAC address) to ensure different devices produce different jitter values even if they power on simultaneously.

For more detail on this pattern, see the [AWS Architecture Blog: Exponential Backoff and Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).

### IoT policy

The device registrar creates one policy per device, scoped to that device's
client ID and topics. For a device `esp32-XXYYZZ` it is equivalent to:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "iot:Connect",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:client/esp32-XXYYZZ"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Publish",
      "Resource": [
        "arn:aws:iot:REGION:ACCOUNT_ID:topic/dt/esp32-XXYYZZ/telemetry",
        "arn:aws:iot:REGION:ACCOUNT_ID:topic/dt/esp32-XXYYZZ/lifecycle"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "iot:Subscribe",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topicfilter/cmd/esp32-XXYYZZ/+"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Receive",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topic/cmd/esp32-XXYYZZ/*"
    }
  ]
}
```

---

## PQC migration techniques

### The full migration journey

The hands-on workshop walks a field device through a staged migration to
post-quantum cryptography, entirely over the air. Each stage is a firmware
variant delivered by the same OTA pipeline, so a fleet migrates gradually rather
than in a single flag-day cutover:

| Stage | What migrates | Key exchange | Device certificate | Firmware |
|---|---|---|---|---|
| **Classical** (factory) | — baseline | Classical ECDHE (TLS 1.3) | ECDSA P-256 (classical) | `1.0.0-classical` |
| **Key exchange → PQC** | the TLS key exchange | Hybrid **ML-KEM** (SecP256r1MLKEM768) | ECDSA P-256 (still classical) | `2.0.0-pqc` |
| **Certificate → PQC** | the certificate signature | Hybrid ML-KEM | **ML-DSA-44** (FIPS 204), broker port 8884 | `3.0.0-mldsa` |
| **Firmware signing → PQC** | the OTA trust anchor | Hybrid ML-KEM | ML-DSA-44 | `4.0.0-signer` / `5.0.0-verified` |

Migrating the **key exchange** first closes the "harvest now, decrypt later"
risk; migrating the **certificate** to ML-DSA-44 then removes the
classical-signature forgery risk; and the **firmware-signing** stage makes each
OTA update itself verifiable against a post-quantum trust anchor.

> The **Workshop Studio content is the authoritative, step-by-step walkthrough**
> of this journey (review risks → review architecture → migrate key exchange →
> migrate certificates → migrate firmware signing). This repository holds the
> code those stages deploy; see `cloudformation/DEPLOYMENT.md` for the deploy and
> the ML-DSA-44 reference section in `cloudformation/DEPLOYMENT.md` for the
> certificate step.

### How a migration is delivered

Every stage uses the same over-the-air path:

1. The dashboard's **Flash** button (the `migrateFirmware` mutation) starts the
   `pqc-workshop-pqc-migration` Step Functions workflow.
2. `prepare-build` resolves the target variant to its prebuilt shared image
   under `shared/<version>/` (built once per deployment from `variants.json`).
   No per-device build runs.
3. `publish-ota` creates presigned S3 URLs for the image, its signature and the
   signer certificate, and publishes an `ota-update` command to
   `cmd/<thing>/ota`.
4. The device streams the image into its **inactive** OTA slot, checks the
   SHA-256, verifies the signature, checks that the signer chains to the
   firmware-signing root for that algorithm in its `certs` partition and has the
   `codeSigning` EKU, and only then switches the boot slot and reboots.
5. The device reconnects on the new firmware; its telemetry (`firmware_version`,
   `pqc_algorithm`, `cert_sig_alg`, `ota_sig_alg`) shows the change on the
   dashboard. A rollback is the same path to an earlier variant.

On the workshop hardware a migration takes about 90-100 s from the click to the
first report from the new firmware.

**Alternative for production: AWS IoT Jobs.** IoT Jobs adds managed fleet
rollout (per-device status, staged percentages, timeouts and retries, an audit
trail) on top of the same download-verify-switch mechanism. The workshop uses a
direct MQTT command to keep the moving parts visible.

---

The sketch uses **hybrid key exchange**, combining a classical and a post-quantum algorithm in a single TLS 1.3 handshake:

| Component | Algorithm | NIST Security Level | Key Size | Purpose |
|-----------|-----------|-------------------|----------|---------|
| Classical | ECDHE P-256 | ~128-bit equivalent | 65 bytes | Protection against classical attacks |
| Post-Quantum | ML-KEM-768 (FIPS 203) | Level 3 | 1184 bytes public key | Protection against quantum attacks |

### Why hybrid?

The hybrid approach provides defense in depth:
- If ML-KEM is found to have a weakness, ECDHE P-256 still protects the session
- If a quantum computer breaks ECDHE, ML-KEM still protects the session
- Both algorithms must be broken simultaneously to compromise the key exchange

### Fallback behavior

The sketch tries two PQC key exchange options in order:

1. **P-256 + ML-KEM-768** (SecP256r1MLKEM768, IANA code point 4587, recommended)
2. **X25519 + ML-KEM-768** (X25519MLKEM768, IANA code point 4588, fallback)

If the server does not support either hybrid option, the TLS 1.3 handshake will still succeed using classical key exchange only (the server selects the best mutually-supported option). The device is "PQC-ready" regardless.

### wolfSSL key share constants

This project uses the **IANA-standardized ML-KEM hybrid groups** (section B-8 of the wolfSSL manual). These are interoperable with OpenSSL 3.5+.

| Constant | Classical Curve | ML-KEM Variant | IANA Code Point |
|----------|----------------|----------------|-----------------|
| `WOLFSSL_SECP256R1_MLKEM768` | NIST P-256 | ML-KEM-768 | 4587 |
| `WOLFSSL_X25519_MLKEM768` | X25519 | ML-KEM-768 | 4588 |
| `WOLFSSL_SECP384R1_MLKEM1024` | NIST P-384 | ML-KEM-1024 | 4589 |

The legacy Kyber Round 3 constants (`WOLFSSL_P521_KYBER_LEVEL5` etc.) use experimental code points (120xx) that are not interoperable with OpenSSL. They are deprecated in favor of the standard groups above.

---

## Memory and performance

The workshop's section 3.7 is the full explanation. In short:

- **PSRAM must be enabled in the build.** The XIAO ESP32-S3 has 8 MB of octal
  PSRAM, but the board option defaults to disabled. The build sets `PSRAM=opi`;
  the heap then sends allocations over 4 KB (wolfSSL's handshake buffers, the
  ML-DSA key and certificate) to PSRAM with no code changes.
- **Measured on the workshop devices** (current firmware):
  - static RAM: 47,968 B of 327,680 B;
  - internal heap free at idle: about 215 KB;
  - lowest internal heap on the heaviest path (ML-DSA certificate + ML-DSA-signed
    OTA): 155.7 KB;
  - PSRAM in use during the ML-DSA handshake: about 104 KB of 8 MB;
  - task stack peaks: MQTT 3.8 KB, OTA 11.7 KB.
- **Flash:** each variant is about 1.2 MB, a third of a 3.5 MB OTA slot. The
  partition table (`esp32-flash/partitions_pqc_workshop.csv`) has two 3.5 MB app
  slots and the 64 KB `certs` identity partition at `0x721000`, on 8 MB of flash.
- **Handshake time:** the classical handshake to the broker takes about 2 s on
  this board, with and without PSRAM.
- **Diagnostics:** publish to `cmd/<thing>/diag` (payload `full` or `dump` for
  more detail) to get heap, PSRAM and per-task stack figures on
  `dt/<thing>/diag` and the serial console.

---

## Troubleshooting

### Build errors

| Error | Cause | Solution |
|-------|-------|----------|
| `FIRMWARE_VARIANT is not set` / `must be FW_VARIANT_...` | `secrets.h` has no (or an unknown) variant | Set `#define FIRMWARE_VARIANT FW_VARIANT_CLASSICAL`, `FW_VARIANT_PQC` or `FW_VARIANT_PQC_MLDSA` (see `secrets.h.template`). CodeBuild generates this. |
| `wolfSSL TLS 1.3 support is required` / `ML-KEM support is required` | wolfSSL's `user_settings.h` is not the workshop config | Copy `raw_device_builder/codebuild/wolfssl_user_settings_arduino.h` to the wolfSSL library's `src/user_settings.h` (the CodeBuild build does this before every compile). |
| `wolfssl.h` / `MQTT.h: No such file or directory` | Library not installed | Install wolfSSL 5.8.4 and MQTT 2.5.3 (Joel Gaehwiler) with the Library Manager. |
| `WOLFSSL_SECP256R1MLKEM768 was not declared` / `wc_dilithium_*` errors | Wrong wolfSSL version | Use wolfSSL 5.8.4: 5.9.x renamed the ML-DSA API. |
| Build fails with the partition scheme | `pqc_workshop` partition table missing | Copy `esp32-flash/partitions_pqc_workshop.csv` into the ESP32 core's `tools/partitions/` as `pqc_workshop.csv`. |

### Runtime errors

| Symptom | Cause | Solution |
|---------|-------|----------|
| `[Setup] FATAL: no valid device identity` | The `certs` partition is missing, an older format, or was issued for a different board (the line above says which) | Register (or Reflash) the device at the Builder-Pi; it re-issues and flashes the identity. |
| `4WAY_HANDSHAKE_TIMEOUT` / `AUTH_FAIL` in the WiFi log | The WiFi password in the device's identity does not match the access point | WiFi comes from SSM `/pqc-workshop/wifi/*`. Make the AP and SSM agree (`setup-builder-pi-remote.sh --only configure-wifi-ap`), and Reflash devices if SSM changed. |
| `[tls] TCP connection failed` | Cannot reach the broker | Check `MQTT_BROKER_ENDPOINT` (the broker NLB). On an account with VPC Block Public Access, the network stack's BPA exclusion must exist. |
| `[wolfssl] Failed to load ...` / `Private key does not match the device certificate` | Corrupt or mixed identity entries | Reflash at the Builder-Pi. |
| `Handshake error ... -188` on the broker connection | The broker's certificate does not chain to the CA the variant trusts | The ML-DSA variant must use port 8884, the others 8883 (set by the build from the variant). |
| `Partition-swap command not supported` | A legacy `migrate-*` command | Migrate from the dashboard instead. |
| `SIGNATURE VERIFICATION FAILED` / `sha256 mismatch` | The OTA image or its signer does not verify | The device keeps running its current firmware. Rebuild the shared images (`build-shared-firmware.sh`) and retry. |

### Debugging tips

1. **Serial logs:** the Builder-Pi GUI's serial view, or
   `/var/log/workshop-builder/device-<serial>.log` on the Pi (also shipped to the
   `/pqc-workshop/builder-pi` CloudWatch log group).
2. **wolfSSL debug output:** uncomment `wolfSSL_Debugging_ON()` in
   `WolfSSLClient::setupContext()` and add `#define DEBUG_WOLFSSL` to the wolfSSL
   configuration.
3. **Memory:** use the `diag` command (above).
4. **MQTT traffic:** subscribe to `dt/esp32-<serial>/#` in the AWS IoT console's
   MQTT test client, or read the dashboard's message log.

---

## Device registration

Devices are registered at the Builder-Pi: plug in the board, click **Register &
Flash**. The registrar creates the IoT Thing and policy, and a CodeBuild job
issues **both** device certificates (ECDSA P-256 and ML-DSA-44) from the two
Private CAs and packs them, both device CA roots, both firmware-signing roots
and the WiFi credentials into a `certs` partition image. The Pi flashes that image with the shared classical firmware,
then deletes the identity image from S3 so the private keys exist only on the
device. Issuing the post-quantum identity alongside the classical one means the
later certificate migration is a firmware change, not a re-provisioning.

AWS IoT **Fleet Provisioning by Claim** is the alternative for fleets that
provision on first boot instead of at a provisioning station; see the
[AWS IoT Fleet Provisioning documentation](https://docs.aws.amazon.com/iot/latest/developerguide/provision-wo-cert.html).

---

## Security considerations

### Certificate storage

Device keys, certificates and WiFi credentials are in the `certs` NVS partition,
**unencrypted**, so anyone with physical access to a board can read them. That is
an accepted risk for the workshop. For production, consider NVS and flash
encryption with secure boot, or a TPM / secure element (for example an ATECC608)
for hardware-protected keys, and check that the chosen storage can hold a
post-quantum key.

### Network security

- The broker connection uses **mutual TLS 1.3**: the device and the broker each
  authenticate with an X.509 certificate, and the device verifies the broker's
  certificate against the CA root for its variant (`WOLFSSL_VERIFY_PEER`).
- **SNI** is set on every connection.
- **OTA downloads from S3 do not yet verify the server certificate and use a
  classical key exchange.** The image's integrity and authenticity are still
  enforced by the SHA-256 and signature checks before the device boots it. Fixing
  the transport (certificate verification and a hybrid ML-KEM key share to S3,
  which supports it) is a planned change.

### PQC readiness

- The device connects to the workshop's Mosquitto broker, which supports the hybrid ML-KEM groups, and the broker bridges to AWS IoT Core. If a server does not support a hybrid group, the TLS 1.3 handshake still succeeds with classical key exchange.
- The hybrid approach means there is **no security downgrade** compared to classical TLS, only an upgrade path.

### wolfSSL licensing

wolfSSL is dual-licensed under GPLv2 and a commercial license. The Kyber/ML-KEM implementation is included free for commercial customers. For open-source projects, GPLv2 applies. See [wolfSSL licensing](https://www.wolfssl.com/license/) for details.

---

## Cleanup and teardown

To avoid ongoing charges, tear down everything the workshop created when you are
done.

- **Full workshop deployment** (the `deploy-workshop.sh` stack — ECS Fargate,
  ECR, CodeBuild, DynamoDB, AppSync, CloudFront, Cognito, AWS Private CA, NLB,
  Secrets Manager, S3, Lambda, Step Functions, SSM): Workshop Studio accounts are
  cleaned automatically when the event ends. Otherwise run the teardown script,
  which deletes the device Things, empties the versioned S3 buckets and ECR
  repositories, and deletes the nested stacks:

  ```bash
  ./cloudformation/teardown-workshop.sh us-east-1 --wait
  ```

  The steps follow the workshop's **Cleanup** page; see
  [`cloudformation/DEPLOYMENT.md` → Cleanup](cloudformation/DEPLOYMENT.md#cleanup-workshop-cleanup-page)
  for what it does, its options, and what is deliberately left in place.

- **Simple single-device stack** (the `esp32-pqc-iot` CloudFormation stack from
  the "AWS IoT Core Setup" section above): delete it with

  ```bash
  aws cloudformation delete-stack --stack-name esp32-pqc-iot
  ```

## References

- [wolfSSL Post-Quantum Key Share on the Espressif ESP32](https://www.wolfssl.com/post-quantum-key-share-on-the-espressif-esp32/) - Official wolfSSL blog post on post-quantum key shares on the ESP32
- [wolfSSL Arduino Library (GitHub)](https://github.com/wolfSSL/Arduino-wolfSSL) - Source repository for the Arduino-packaged wolfSSL
- [AWS IoT Core Developer Guide](https://docs.aws.amazon.com/iot/latest/developerguide/) - Complete AWS IoT documentation
- [AWS Post-Quantum Cryptography Migration](https://aws.amazon.com/security/post-quantum-cryptography/migrating-to-post-quantum-cryptography/) - AWS PQC roadmap and migration guidance
- [AWS IoT ESP32 Arduino Examples (GitHub)](https://github.com/aws-samples/aws-iot-esp32-arduino-examples) - Official AWS sample sketches for ESP32
- [NIST Post-Quantum Cryptography Standards](https://csrc.nist.gov/projects/post-quantum-cryptography) - FIPS 203 (ML-KEM), FIPS 204 (ML-DSA)
- [Arduino-ESP32 documentation](https://docs.espressif.com/projects/arduino-esp32/en/latest/) - The Arduino core the firmware is built on
- [arduino-mqtt (MQTT library by Joel Gaehwiler)](https://github.com/256dpi/arduino-mqtt) - The MQTT client the firmware uses

---

## License

This sample code is provided under the MIT-0 license. See the [LICENSE](LICENSE) file for details.

wolfSSL is licensed separately under GPLv2 or a commercial license. See [wolfssl.com/license](https://www.wolfssl.com/license/).
