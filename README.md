# ESP32 AWS IoT Core with Post-Quantum Cryptography (PQC)

An Arduino sketch that connects any ESP32 to AWS IoT Core using **post-quantum cryptography** via wolfSSL. The connection uses TLS 1.3 with a hybrid Kyber/ML-KEM key exchange, protecting data against both classical and future quantum computer attacks.

WiFi and MQTT run as independent FreeRTOS tasks with automatic failure recovery.

## Table of Contents

- [Why Post-Quantum Cryptography?](#why-post-quantum-cryptography)
- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Supported Hardware](#supported-hardware)
- [Prerequisites](#prerequisites)
- [Installation: Arduino Cloud IDE](#installation-arduino-cloud-ide)
- [Installation: Arduino Desktop IDE](#installation-arduino-desktop-ide)
- [AWS IoT Core Setup](#aws-iot-core-setup)
- [Configure the Sketch](#configure-the-sketch)
- [Upload and Run](#upload-and-run)
- [Expected Serial Output](#expected-serial-output)
- [Code Walkthrough](#code-walkthrough)
- [PQC Key Exchange Details](#pqc-key-exchange-details)
- [Memory and Performance](#memory-and-performance)
- [Troubleshooting](#troubleshooting)
- [Security Considerations](#security-considerations)
- [References](#references)
- [License](#license)

---

## Why Post-Quantum Cryptography?

Current public-key algorithms (RSA, ECDSA, ECDHE) are vulnerable to future large-scale quantum computers. The "harvest now, decrypt later" (HNDL) threat means an adversary can capture encrypted traffic today and decrypt it later once a cryptographically relevant quantum computer (CRQC) exists.

Hybrid PQC key exchange addresses this by combining a classical algorithm (ECDHE) with a quantum-resistant one (ML-KEM / Kyber) in a single TLS handshake. If either algorithm is broken, the other still protects the session. AWS is actively migrating its services to PQC, and this sample shows how IoT devices can participate in that migration today.

For more detail, see the [AWS Post-Quantum Cryptography Migration Plan](https://aws.amazon.com/security/post-quantum-cryptography/migrating-to-post-quantum-cryptography/).

---

## How It Works

```
ESP32 Device                                    AWS Cloud
+---------------------------+                   +----------------------------+
|                           |     WiFi          |                            |
|  [WiFi FreeRTOS Task]     |===================|                            |
|    - Auto connect         |                   |                            |
|    - Auto reconnect       |   TLS 1.3         |   AWS IoT Core             |
|    - Health monitoring    |   Hybrid PQC      |                            |
|                           |   (P521 + Kyber)  |   - MQTT Broker            |
|  [MQTT FreeRTOS Task]     |<================>|   - Rules Engine           |
|    - wolfSSL TLS client   |   mTLS (X.509)    |   - Device Shadow          |
|    - PubSubClient MQTT    |   MQTT 3.1.1      |   - Lambda, DynamoDB, ...  |
|    - Publish telemetry    |                   |                            |
|    - Subscribe commands   |                   |                            |
|                           |                   |                            |
|  [Main Loop]              |                   |                            |
|    - Status reporting     |                   |                            |
|    - Watchdog             |                   |                            |
+---------------------------+                   +----------------------------+
```

1. The **WiFi task** manages the wireless connection on FreeRTOS core 0. It connects to the configured network, monitors the link, and automatically reconnects on failure.

2. The **MQTT task** waits for WiFi, initializes wolfSSL with your device certificates, then opens a TLS 1.3 connection to AWS IoT Core. During the TLS handshake, it requests a hybrid PQC key exchange (P-521 + Kyber Level 5). Once connected, it publishes JSON telemetry every 30 seconds and subscribes to a topic for inbound commands.

3. The **main loop** prints a system status report every 60 seconds (uptime, WiFi RSSI, MQTT state, free heap).

If WiFi drops, the WiFi task reconnects automatically. The MQTT task detects the loss, waits for WiFi to return, then re-establishes the TLS and MQTT connection from scratch.

---

## Project Structure

```
Sample-aws-iot-pqc/
+-- README.md                              This file
+-- aws_iot_pqc_esp32/
    +-- aws_iot_pqc_esp32.ino              Main sketch
    +-- secrets.h                          WiFi + AWS IoT credentials (you edit this)
    +-- wolfssl_user_settings.h            Reference wolfSSL PQC configuration
```

| File | Description |
|------|-------------|
| `aws_iot_pqc_esp32.ino` | Main Arduino sketch containing the WolfSSLClient class, FreeRTOS tasks, wolfSSL context setup, and MQTT logic. |
| `secrets.h` | Placeholder file for WiFi SSID/password, AWS IoT endpoint, Thing name, MQTT topics, and PEM-encoded certificates. |
| `wolfssl_user_settings.h` | Reference `#define` values that must be copied into the wolfSSL library's own `user_settings.h` to enable TLS 1.3 and Kyber/ML-KEM on ESP32. |

---

## Supported Hardware

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

## Prerequisites

Before starting, you need:

1. An **ESP32 development board** with WiFi and a USB cable
2. An **AWS account** with access to AWS IoT Core
3. The **Arduino Cloud Agent** installed (for the Cloud IDE) or the **Arduino Desktop IDE** installed
4. A **WiFi network** the ESP32 can reach

---

## Installation: Arduino Cloud IDE

The [Arduino Cloud Editor](https://app.arduino.cc/sketches/) is a browser-based IDE that stores your sketches online. It includes most popular libraries and all board packages without downloading. However, wolfSSL requires a custom configuration for PQC, so you need to import a modified version of the library.

### Step 1: Install the Arduino Cloud Agent

The Cloud Agent is a browser plugin that lets the editor communicate with your USB-connected board.

1. Go to [https://create.arduino.cc/getting-started/plugin/welcome](https://create.arduino.cc/getting-started/plugin/welcome)
2. Download and install the agent for your operating system
3. After installation, the agent runs in the background (look for its icon in your system tray)

### Step 2: Select Your ESP32 Board

1. Open the [Cloud Editor](https://app.arduino.cc/sketches/)
2. Connect your ESP32 board via USB
3. The board and port should appear automatically in the board selector at the top of the editor
4. If it does not appear, click the board selector and choose your ESP32 variant manually

### Step 3: Prepare the wolfSSL Library with PQC Support

The Cloud Editor includes wolfSSL in its library index, but the default configuration does not enable PQC. You need to import a modified version:

1. **Download the wolfSSL Arduino library** from [https://github.com/wolfSSL/Arduino-wolfSSL/releases](https://github.com/wolfSSL/Arduino-wolfSSL/releases) as a `.zip` file
2. **Extract the zip** to a temporary folder on your computer
3. **Open the file** `src/user_settings.h` inside the extracted folder
4. **Add the PQC defines** from the `wolfssl_user_settings.h` file in this project. The critical lines to add are:

```c
#define WOLFSSL_TLS13
#define WOLFSSL_EXPERIMENTAL_SETTINGS
#define WOLFSSL_HAVE_KYBER
#define WOLFSSL_WC_KYBER
#define WOLFSSL_SHA3
#define HAVE_ECC
#define HAVE_ECC521
#define HAVE_SNI
#define HAVE_HKDF
#define HAVE_AESGCM
#define HAVE_SUPPORTED_CURVES
#define HAVE_TLS_EXTENSIONS
```

See `wolfssl_user_settings.h` in this project for the full recommended set of defines including ESP32 hardware acceleration and memory tuning.

5. **Re-zip the modified folder** (the zip must contain a single top-level folder with `library.properties` inside it)
6. **Import into the Cloud Editor:**
   - In the Cloud Editor, click the **Libraries** icon (book icon) in the left sidebar
   - Click the **upward arrow** (import) icon at the top of the Libraries panel
   - Select your modified `.zip` file
   - Wait for the import to complete

The custom library will be stored under your Arduino Cloud account and will override the default wolfSSL library for your sketches.

### Step 4: Install PubSubClient

1. In the Cloud Editor, click the **Libraries** icon
2. Search for **PubSubClient** by Nick O'Leary
3. Click **Include** to add it to your sketch (or it will be resolved automatically at compile time)

### Step 5: Import the Sketch

1. Download this repository as a `.zip` file
2. In the Cloud Editor, click **Create** (top right) then **Import local file**
3. Select the `.zip` file
4. The sketch will appear in your sketchbook

Alternatively, create a new sketch and copy the contents of each file manually:
- Copy `aws_iot_pqc_esp32.ino` into the main editor
- Click the **+** tab button to add a new file named `secrets.h`, then paste its contents

### Step 6: Configure and Upload

Follow the [Configure the Sketch](#configure-the-sketch) and [Upload and Run](#upload-and-run) sections below.

---

## Installation: Arduino Desktop IDE

### Step 1: Install the Arduino IDE

Download and install from [https://www.arduino.cc/en/software](https://www.arduino.cc/en/software) (version 2.x recommended).

### Step 2: Install the ESP32 Board Package

1. Open **File > Preferences**
2. In **Additional Board Manager URLs**, add:
   ```
   https://espressif.github.io/arduino-esp32/package_esp32_index.json
   ```
3. Open **Tools > Board > Boards Manager**
4. Search for **esp32** by Espressif Systems
5. Click **Install**

### Step 3: Install Required Libraries

Open **Sketch > Include Library > Manage Libraries** and install:

| Library | Author | Version |
|---------|--------|---------|
| **wolfSSL** | wolfSSL Inc. | 5.7.0 or later |
| **PubSubClient** | Nick O'Leary | 2.8 or later |

### Step 4: Configure wolfSSL for PQC

After installing wolfSSL, you must edit its `user_settings.h` to enable PQC. The file location:

| OS | Path |
|----|------|
| Windows | `C:\Users\<you>\Documents\Arduino\libraries\wolfssl\src\user_settings.h` |
| macOS | `~/Documents/Arduino/libraries/wolfssl/src/user_settings.h` |
| Linux | `~/Arduino/libraries/wolfssl/src/user_settings.h` |

Open the file and add the defines from `wolfssl_user_settings.h` in this project. At minimum, add these inside the header guard:

```c
// TLS 1.3
#define WOLFSSL_TLS13
#define HAVE_TLS_EXTENSIONS
#define HAVE_SUPPORTED_CURVES
#define HAVE_HKDF

// Post-Quantum Cryptography (Kyber / ML-KEM)
#define WOLFSSL_EXPERIMENTAL_SETTINGS
#define WOLFSSL_HAVE_KYBER
#define WOLFSSL_WC_KYBER
#define WOLFSSL_SHA3

// ECC for hybrid key exchange
#define HAVE_ECC
#define HAVE_ECC256
#define HAVE_ECC521

// Required for AWS IoT
#define HAVE_SNI
#define HAVE_AESGCM

// ESP32 platform
#define WOLFSSL_ESPIDF
#define WOLFSSL_ESP32
#define WOLFSSL_ESP32_CRYPT
```

For the full recommended configuration (including memory tuning, disabled legacy features, and hardware acceleration), copy the entire contents of `wolfssl_user_settings.h`.

After editing, do a **clean build**: in the Arduino IDE, hold Shift and click the Upload button, or delete the build cache folder.

### Step 5: Open the Sketch

1. Open the Arduino IDE
2. Go to **File > Open** and navigate to `aws_iot_pqc_esp32/aws_iot_pqc_esp32.ino`
3. The IDE will open all files in the sketch folder (`.ino`, `secrets.h`)

---

## AWS IoT Core Setup

### Step 1: Create a Thing

1. Open the [AWS IoT Console](https://console.aws.amazon.com/iot/)
2. In the left navigation, go to **Manage > All devices > Things**
3. Click **Create things > Create single thing**
4. Enter the Thing name: `my-esp32-pqc-thing` (must match `THINGNAME` in `secrets.h`)
5. Leave other settings as default and click **Next**

### Step 2: Generate Certificates

1. On the certificate page, select **Auto-generate a new certificate**
2. Click **Next**
3. You will be prompted to download three files. **Download all three now** (you cannot download the private key again later):

| File | Description | Used in `secrets.h` as |
|------|-------------|----------------------|
| `xxxxxxxxxx-certificate.pem.crt` | Device certificate | `AWS_CERT_CRT` |
| `xxxxxxxxxx-private.pem.key` | Device private key | `AWS_CERT_PRIVATE` |
| `AmazonRootCA1.pem` | Amazon Root CA 1 | `AWS_CERT_CA` |

4. Click **Activate** to activate the certificate
5. Click **Done** (you will attach a policy next)

If you missed the Root CA download, get it from: [https://www.amazontrust.com/repository/AmazonRootCA1.pem](https://www.amazontrust.com/repository/AmazonRootCA1.pem)

### Step 3: Create and Attach an IoT Policy

1. In the IoT Console, go to **Security > Policies**
2. Click **Create policy**
3. Name it `esp32-pqc-policy`
4. Switch to **JSON** view and paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "iot:Connect",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:client/my-esp32-pqc-thing"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Subscribe",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topicfilter/esp32/pqc/sub"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Receive",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topic/esp32/pqc/sub"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Publish",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topic/esp32/pqc/pub"
    }
  ]
}
```

Replace `REGION` with your AWS region (e.g., `us-east-1`) and `ACCOUNT_ID` with your 12-digit AWS account ID.

5. Click **Create**
6. Go to **Security > Certificates**, find the certificate you created, click on it
7. Click **Actions > Attach policy** and select `esp32-pqc-policy`

### Step 4: Find Your IoT Endpoint

1. In the IoT Console, go to **Settings** (bottom of the left navigation)
2. Copy the **Device data endpoint** (looks like `xxxxxxxxxxxxxx-ats.iot.us-east-1.amazonaws.com`)

Or use the AWS CLI:
```bash
aws iot describe-endpoint --endpoint-type iot:Data-ATS
```

---

## Configure the Sketch

Open `secrets.h` and fill in your values:

### WiFi Credentials

```c
#define WIFI_SSID     "MyHomeNetwork"
#define WIFI_PASSWORD "MyWiFiPassword123"
```

### AWS IoT Configuration

```c
#define AWS_IOT_ENDPOINT "a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com"
#define AWS_IOT_PORT     8883
#define THINGNAME        "my-esp32-pqc-thing"
```

### Certificates

Open each downloaded `.pem` file in a text editor and paste the full contents (including the `-----BEGIN` and `-----END` lines) into the corresponding variable in `secrets.h`:

- `AWS_CERT_CA` - paste the contents of `AmazonRootCA1.pem`
- `AWS_CERT_CRT` - paste the contents of your device certificate file
- `AWS_CERT_PRIVATE` - paste the contents of your private key file

Example (abbreviated):
```c
static const char AWS_CERT_CA[] PROGMEM = R"EOF(
-----BEGIN CERTIFICATE-----
MIIDQTCCAimgAwIBAgITBmyfz5m/jAo54vB4ikPmljZbyjANBgkqhkiG9w0BAQsF
ADA5MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6
...full certificate content...
-----END CERTIFICATE-----
)EOF";
```

### MQTT Topics (Optional)

The default topics are:
```c
#define AWS_IOT_PUBLISH_TOPIC   "esp32/pqc/pub"    // Device publishes here
#define AWS_IOT_SUBSCRIBE_TOPIC "esp32/pqc/sub"     // Device listens here
```

Change these if needed, but remember to update your IoT policy to match.

---

## Upload and Run

### Arduino Cloud IDE

1. Make sure your ESP32 is connected and selected in the board dropdown
2. Click the **Upload** button (right arrow icon)
3. Wait for compilation and upload to complete (first build with wolfSSL PQC takes several minutes)
4. Click the **Serial Monitor** button to open it in a new window
5. Set baud rate to **115200**

### Arduino Desktop IDE

1. Select your board: **Tools > Board > esp32 > [your ESP32 variant]**
2. Select the port: **Tools > Port > [your ESP32's COM/tty port]**
3. Click **Upload** (or press Ctrl+U / Cmd+U)
4. Open **Tools > Serial Monitor** and set baud rate to **115200**

The first compilation will take several minutes because wolfSSL with PQC is a large library. Subsequent builds are faster due to caching.

---

## Expected Serial Output

```
╔══════════════════════════════════════════════════╗
║  ESP32 AWS IoT Core - Post-Quantum Cryptography ║
║  TLS 1.3 + Hybrid Kyber/ML-KEM Key Exchange     ║
╚══════════════════════════════════════════════════╝

Chip:       ESP32-S3 Rev 0
CPU Freq:   240 MHz
Flash:      8 MB
Free Heap:  275432 bytes
PSRAM:      8388608 bytes

[Setup] All tasks created. System running.
[WiFi] Task started
[WiFi] Connecting...
......
[WiFi] Connected! IP: 192.168.1.42, RSSI: -45 dBm
[MQTT] Task started, waiting for WiFi...
[MQTT] WiFi is up, initializing wolfSSL...
[wolfSSL] Library initialized
[wolfSSL] CA certificate loaded
[wolfSSL] Device certificate loaded
[wolfSSL] Private key loaded
[wolfSSL] TLS 1.3 PQC context ready
[MQTT] Connecting to AWS IoT Core with PQC TLS...
[wolfSSL] TCP connected to a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com:8883
[wolfSSL] Using PQC key exchange: P-521 + Kyber Level 5
[wolfSSL] Starting TLS 1.3 PQC handshake...
[wolfSSL] TLS handshake complete in 2847 ms
[wolfSSL] Protocol: TLSv1.3
[wolfSSL] Cipher:   TLS_AES_128_GCM_SHA256
[MQTT] Connected to AWS IoT Core!
[MQTT] Subscribed to: esp32/pqc/sub
[MQTT] Published message #1 to esp32/pqc/pub
--- System Status ---------------------------------------------
  Uptime:     60 sec
  WiFi:       Connected (RSSI: -45 dBm)
  MQTT:       Connected
  Free Heap:  198432 bytes
  Min Heap:   142108 bytes
---------------------------------------------------------------
[MQTT] Published message #2 to esp32/pqc/pub
```

### Verifying the Connection in AWS

1. In the AWS IoT Console, go to **Test > MQTT test client**
2. Subscribe to `esp32/pqc/pub`
3. You should see JSON messages arriving every 30 seconds:

```json
{
  "device": "my-esp32-pqc-thing",
  "message_id": 1,
  "uptime_sec": 45,
  "wifi_rssi": -45,
  "free_heap": 198432,
  "pqc_algorithm": "P521_KYBER_LEVEL5",
  "tls_version": "TLS 1.3"
}
```

4. To send a message to the device, publish to `esp32/pqc/sub`:
```json
{"command": "hello from the cloud"}
```

---

## Code Walkthrough

### WolfSSLClient Class (TLS Layer)

The core of this sketch is the `WolfSSLClient` class, which wraps a plain Arduino `WiFiClient` (TCP only) with wolfSSL to add TLS 1.3 + PQC. It implements the Arduino `Client` interface so that `PubSubClient` can use it transparently.

**Connection flow inside `WolfSSLClient::connect()`:**

1. **TCP connect** - `WiFiClient::connect()` opens a raw TCP socket to the AWS IoT endpoint on port 8883
2. **Create SSL session** - `wolfSSL_new()` creates a new TLS session from the shared context
3. **Set I/O callbacks** - Custom send/receive callbacks bridge wolfSSL to the Arduino `WiFiClient`
4. **Request PQC key share** - `wolfSSL_UseKeyShare(ssl, WOLFSSL_P521_KYBER_LEVEL5)` tells wolfSSL to offer a hybrid P-521 + Kyber Level 5 key exchange during the TLS handshake. If this fails, it falls back to P-256 + Kyber Level 1.
5. **Set SNI** - Server Name Indication is required for AWS IoT to route to the correct endpoint
6. **TLS handshake** - `wolfSSL_connect()` performs the full TLS 1.3 handshake with a 30-second timeout. The handshake loop handles non-blocking I/O (`WANT_READ`/`WANT_WRITE`) by yielding to FreeRTOS.
7. **Verify** - On success, the protocol version and cipher suite are printed to Serial

### wolfSSL I/O Callbacks

wolfSSL does not know about Arduino's `WiFiClient`. The `wolfssl_send_cb` and `wolfssl_recv_cb` functions bridge the gap:

- **Send callback**: Takes a buffer from wolfSSL and writes it to `WiFiClient::write()`
- **Receive callback**: Reads from `WiFiClient::read()` into wolfSSL's buffer. Returns `WOLFSSL_CBIO_ERR_WANT_READ` when no data is available (non-blocking behavior).

### wolfSSL Context Setup

`setupWolfSSLContext()` runs once and creates a shared `WOLFSSL_CTX` that all connections use:

1. Initializes the wolfSSL library
2. Creates a TLS 1.3 client method context
3. Loads the Amazon Root CA 1 for server verification
4. Loads the device certificate and private key for client authentication (mTLS)
5. Enables peer verification (`WOLFSSL_VERIFY_PEER`)

### WiFi FreeRTOS Task

`wifiTask()` runs on core 0 in an infinite loop:

1. Checks `WiFi.status()` every second
2. If disconnected, calls `WiFi.disconnect(true)` then `WiFi.begin()` with a 15-second timeout
3. Updates the `g_wifiConnected` flag (protected by a mutex for thread safety)
4. On success, prints the IP address and RSSI

### MQTT FreeRTOS Task

`mqttTask()` runs on core 1 (or core 0 on single-core chips):

1. Waits for `g_wifiConnected` to become true
2. Calls `setupWolfSSLContext()` once
3. Creates a `WolfSSLClient` and wraps it in a `PubSubClient`
4. Connects to AWS IoT Core using the Thing name as the MQTT client ID
5. Subscribes to the inbound topic
6. In the main loop: calls `mqttClient.loop()` to process incoming messages, publishes telemetry every 30 seconds
7. If WiFi drops, disconnects MQTT and waits for WiFi to return before reconnecting

### Main Loop

`loop()` prints a status report every 60 seconds showing uptime, WiFi/MQTT state, and heap usage. All real work happens in the FreeRTOS tasks.

---

## PQC Key Exchange Details

The sketch uses **hybrid key exchange**, combining a classical and a post-quantum algorithm in a single TLS 1.3 handshake:

| Component | Algorithm | NIST Security Level | Key Size | Purpose |
|-----------|-----------|-------------------|----------|---------|
| Classical | ECDHE P-521 | ~256-bit equivalent | 132 bytes | Protection against classical attacks |
| Post-Quantum | Kyber Level 5 (ML-KEM-1024) | Level 5 (highest) | 1568 bytes public key | Protection against quantum attacks |

### Why Hybrid?

The hybrid approach provides defense in depth:
- If Kyber is found to have a weakness, ECDHE P-521 still protects the session
- If a quantum computer breaks ECDHE, Kyber still protects the session
- Both algorithms must be broken simultaneously to compromise the key exchange

### Fallback Behavior

The sketch tries two PQC key exchange options in order:

1. **P-521 + Kyber Level 5** (strongest, larger keys, slower handshake)
2. **P-256 + Kyber Level 1** (smaller keys, faster handshake, still quantum-resistant)

If the server does not support either hybrid option, the TLS 1.3 handshake will still succeed using classical key exchange only (the server selects the best mutually-supported option). The device is "PQC-ready" regardless.

### wolfSSL Key Share Constants

| Constant | Classical Curve | Kyber Level | ML-KEM Variant |
|----------|----------------|-------------|----------------|
| `WOLFSSL_P256_KYBER_LEVEL1` | NIST P-256 | Level 1 | ML-KEM-512 |
| `WOLFSSL_P384_KYBER_LEVEL3` | NIST P-384 | Level 3 | ML-KEM-768 |
| `WOLFSSL_P521_KYBER_LEVEL5` | NIST P-521 | Level 5 | ML-KEM-1024 |

---

## Memory and Performance

### RAM Usage

The PQC handshake requires significantly more memory than classical TLS due to larger key sizes.

| Phase | Approximate Heap Usage |
|-------|----------------------|
| Idle (WiFi connected, no TLS) | ~5 KB |
| During TLS 1.3 PQC handshake | ~40-60 KB peak |
| Steady state (TLS + MQTT connected) | ~15-20 KB |
| WiFi task stack | 4 KB |
| MQTT task stack | 16 KB |

**Minimum recommended free heap before connecting: 80 KB**

### Flash Usage

| Component | Approximate Size |
|-----------|-----------------|
| wolfSSL with PQC (Kyber + TLS 1.3) | ~350-450 KB |
| PubSubClient | ~15 KB |
| Sketch code | ~30 KB |
| Certificates (in PROGMEM) | ~5 KB |
| **Total** | **~400-500 KB** |

Most ESP32 boards have 4 MB flash, so this fits comfortably.

### Handshake Time

| Key Exchange | Typical Handshake Time |
|-------------|----------------------|
| Classical ECDHE P-256 only | ~800-1200 ms |
| Hybrid P-256 + Kyber Level 1 | ~1500-2500 ms |
| Hybrid P-521 + Kyber Level 5 | ~2500-4000 ms |

Times depend on WiFi latency, server load, and ESP32 clock speed (240 MHz recommended).

### Reducing Memory Usage

If you are tight on memory:

1. Use `WOLFSSL_P256_KYBER_LEVEL1` instead of Level 5 (saves ~20 KB during handshake)
2. Add `#define WOLFSSL_NO_TLS12` to disable TLS 1.2 support (saves ~30 KB flash)
3. Reduce `MQTT_TASK_STACK_SIZE` to 12288 if stable
4. Reduce `mqttClient.setBufferSize()` from 1024 to 512
5. Use a board with PSRAM and enable it in the Arduino board settings

---

## Troubleshooting

### Build Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `#error "wolfSSL TLS 1.3 support is required"` | wolfSSL `user_settings.h` not configured | Add `#define WOLFSSL_TLS13` to wolfSSL's `user_settings.h` |
| `#error "wolfSSL Kyber/ML-KEM support is required"` | PQC not enabled in wolfSSL | Add `#define WOLFSSL_HAVE_KYBER` and related defines |
| `wolfssl.h: No such file or directory` | wolfSSL library not installed | Install wolfSSL via Library Manager |
| `PubSubClient.h: No such file or directory` | PubSubClient not installed | Install PubSubClient via Library Manager |
| `WOLFSSL_P521_KYBER_LEVEL5 was not declared` | wolfSSL version too old or Kyber not enabled | Update wolfSSL to 5.7.0+ and verify `user_settings.h` |
| Compilation takes very long (5+ minutes) | Normal for first build with wolfSSL PQC | Subsequent builds use cache and are faster |

### Runtime Errors

| Symptom | Cause | Solution |
|---------|-------|----------|
| `[WiFi] Connection failed` | Wrong SSID/password, or network out of range | Check `WIFI_SSID` and `WIFI_PASSWORD` in `secrets.h`. Move closer to the router. |
| `[wolfSSL] TCP connection failed` | Cannot reach AWS IoT endpoint | Check `AWS_IOT_ENDPOINT` is correct. Verify port 8883 is not blocked by firewall. |
| `[wolfSSL] Failed to load CA cert` | Malformed Root CA in `secrets.h` | Re-download `AmazonRootCA1.pem` and paste the full contents including BEGIN/END lines. |
| `[wolfSSL] Failed to load device cert` | Malformed device certificate | Re-download from AWS IoT Console. Ensure no extra whitespace or missing lines. |
| `[wolfSSL] Failed to load private key` | Wrong or malformed private key | Ensure you are using the private key (not the public key). You can only download it once during Thing creation. |
| `[wolfSSL] Handshake error: -188` | Certificate verification failed | Ensure the Root CA matches your endpoint. Check that the device certificate is activated and has a policy attached. |
| `[wolfSSL] Handshake timed out` | Network too slow or endpoint unreachable | Check WiFi signal strength. Try a closer access point. Increase `handshakeTimeout` if needed. |
| `[wolfSSL] Failed to set PQC key share` | wolfSSL compiled without Kyber | Verify all PQC defines are in `user_settings.h`. Do a clean rebuild. |
| `[MQTT] Connection failed, rc=-2` | TLS connection dropped | Usually a certificate or policy issue. Check the AWS IoT Console logs. |
| `[MQTT] Connection failed, rc=-4` | MQTT keepalive timeout | Increase `MQTT_KEEPALIVE_SEC` or check for network instability. |
| Stack overflow / crash / reboot loop | MQTT task stack too small | Increase `MQTT_TASK_STACK_SIZE` to 20480 or higher. |
| `Guru Meditation Error: Core 1 panic'ed (LoadProhibited)` | Out of heap memory | Use Kyber Level 1 instead of Level 5. Use a board with PSRAM. |

### Debugging Tips

1. **Enable wolfSSL debug logging**: Uncomment `wolfSSL_Debugging_ON()` in `setupWolfSSLContext()` and add `#define DEBUG_WOLFSSL` to `user_settings.h`. This produces verbose TLS handshake output.

2. **Monitor heap usage**: The status report in `loop()` shows free and minimum heap. If minimum heap drops below 20 KB, you are at risk of crashes.

3. **Test with the MQTT test client**: In the AWS IoT Console, use **Test > MQTT test client** to subscribe to `esp32/pqc/pub` and verify messages are arriving.

4. **Check CloudWatch logs**: If the device connects via TCP but the TLS handshake fails, check the AWS IoT Core logs in CloudWatch for server-side error details.

5. **Simplify first**: If you cannot get PQC working, temporarily comment out the `wolfSSL_UseKeyShare()` call. The connection will use classical key exchange, which confirms your certificates and network are correct. Then re-enable PQC.

---

## Security Considerations

### Certificate Storage

The certificates in `secrets.h` are stored in flash memory (`PROGMEM`). For production deployments, consider:

- Using the ESP32's **eFuse** or **NVS encrypted partition** for key storage
- Implementing **certificate rotation** via AWS IoT Jobs or Fleet Provisioning
- Using an **ATECC608** secure element for hardware-protected private keys

### Network Security

- The sketch uses **mutual TLS (mTLS)**: both the device and server authenticate each other with X.509 certificates
- **Server certificate verification** is enabled (`WOLFSSL_VERIFY_PEER`), preventing man-in-the-middle attacks
- **SNI** (Server Name Indication) is set, which is required by AWS IoT Core

### PQC Readiness

- AWS IoT Core is progressively adding PQC support. If your endpoint does not yet support hybrid PQC key exchange, the TLS handshake will still succeed using classical algorithms. The device is "PQC-ready" and will automatically use PQC once the server supports it.
- The hybrid approach means there is **no security downgrade** compared to classical TLS, only an upgrade path.

### wolfSSL Licensing

wolfSSL is dual-licensed under GPLv2 and a commercial license. The Kyber/ML-KEM implementation is included free for commercial customers. For open-source projects, GPLv2 applies. See [wolfSSL licensing](https://www.wolfssl.com/license/) for details.

---

## References

- [wolfSSL Post-Quantum Key Share on the Espressif ESP32](https://www.wolfssl.com/post-quantum-key-share-on-the-espressif-esp32/) - Official wolfSSL blog post on enabling Kyber on ESP32
- [wolfSSL Arduino Library (GitHub)](https://github.com/wolfSSL/Arduino-wolfSSL) - Source repository for the Arduino-packaged wolfSSL
- [AWS IoT Core Developer Guide](https://docs.aws.amazon.com/iot/latest/developerguide/) - Complete AWS IoT documentation
- [AWS Post-Quantum Cryptography Migration](https://aws.amazon.com/security/post-quantum-cryptography/migrating-to-post-quantum-cryptography/) - AWS PQC roadmap and migration guidance
- [AWS IoT ESP32 Arduino Examples (GitHub)](https://github.com/aws-samples/aws-iot-esp32-arduino-examples) - Official AWS sample sketches for ESP32
- [NIST Post-Quantum Cryptography Standards](https://csrc.nist.gov/projects/post-quantum-cryptography) - FIPS 203 (ML-KEM), FIPS 204 (ML-DSA)
- [Arduino Cloud Editor Documentation](https://docs.arduino.cc/arduino-cloud/guides/cloud-editor/) - Guide to using the Arduino Cloud IDE
- [PubSubClient Library](https://pubsubclient.knolleary.net/) - MQTT client documentation

---

## License

This sample code is provided under the MIT-0 license. See the [LICENSE](LICENSE) file for details.

wolfSSL is licensed separately under GPLv2 or a commercial license. See [wolfssl.com/license](https://www.wolfssl.com/license/).
