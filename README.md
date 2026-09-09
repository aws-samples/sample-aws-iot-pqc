# ESP32 AWS IoT Core with Post-Quantum Cryptography (PQC)

An Arduino sketch that connects any ESP32 to AWS IoT Core using **post-quantum cryptography** via wolfSSL. The connection uses TLS 1.3 with a hybrid Kyber/ML-KEM key exchange, protecting data against both classical and future quantum computer attacks.

WiFi and MQTT run as independent FreeRTOS tasks with automatic failure recovery.

## Table of Contents

- [Why Post-Quantum Cryptography?](#why-post-quantum-cryptography)
- [How It Works](#how-it-works)
- [Workshop Architecture (Containerized PQC Broker)](#workshop-architecture-containerized-pqc-broker)
- [Project Structure](#project-structure)
- [Supported Hardware](#supported-hardware)
- [Prerequisites](#prerequisites)
- [Installation: Arduino Cloud IDE](#installation-arduino-cloud-ide)
- [Installation: Arduino Desktop IDE](#installation-arduino-desktop-ide)
- [AWS IoT Core Setup](#aws-iot-core-setup)
- [Validate Environment Readiness](#validate-environment-readiness)
- [Configure the Sketch](#configure-the-sketch)
- [Upload and Run](#upload-and-run)
- [Expected Serial Output](#expected-serial-output)
- [Code Walkthrough](#code-walkthrough)
- [MQTT Best Practices](#mqtt-best-practices)
- [PQC Migration Techniques](#pqc-migration-techniques)
- [PQC Key Exchange Details](#pqc-key-exchange-details)
- [Memory and Performance](#memory-and-performance)
- [Troubleshooting](#troubleshooting)
- [Automatic Registration and Certificate Provisioning](#automatic-registration-and-certificate-provisioning)
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

2. The **MQTT task** waits for WiFi, initializes wolfSSL with your device certificates, then opens a TLS 1.3 connection to the containerized Mosquitto PQC broker (ECS Fargate, reached via a Network Load Balancer on port 8883). During the TLS handshake, it requests a hybrid PQC key exchange (P-256 + ML-KEM-768). Once connected, it publishes JSON telemetry every 30 seconds and subscribes to a topic for inbound commands. The broker bridges this traffic up to AWS IoT Core.

3. The **main loop** prints a system status report every 60 seconds (uptime, WiFi RSSI, MQTT state, free heap).

If WiFi drops, the WiFi task reconnects automatically. The MQTT task detects the loss, waits for WiFi to return, then re-establishes the TLS and MQTT connection from scratch.

---

## Workshop Architecture (Containerized PQC Broker)

For the re:Invent builders session, the PQC-enabled Mosquitto broker runs as a **container on ECS Fargate** behind a Network Load Balancer — there is no Raspberry Pi gateway and no AWS IoT Greengrass in the data path. ESP32 devices connect straight to the load balancer over PQC TLS 1.3, and the broker bridges their traffic up to AWS IoT Core. A separate Raspberry Pi (the "builder-pi") is used only to flash devices, not to route MQTT.

```
ESP32 Devices (x8)          AWS Cloud (us-east-1)
+-------------------+       +--------------------------------------------------------------+
|                   |       |                                                              |
| [OTA_0 / app0]    | TLS   |   Network Load        ECS Fargate                            |
|  Classical FW     | 1.3   |   Balancer            +--------------------------+           |
|  (wolfSSL)        |=====> |   pqc-broker-nlb ====> |  Mosquitto PQC broker    |           |
|                   | :8883 |   (TCP passthrough)   |   - OpenSSL 3.5 + ML-KEM |           |
| [OTA_1 / app1]    |       |                       |   - port 8883, mTLS      |           |
|  PQC FW           | TLS   |                       |   - certs from Private CA|           |
|  (wolfSSL)        | 1.3   |                       +-----------+--------------+           |
|                   | +ML-  |                                   | MQTT bridge (mTLS)       |
|                   | KEM   |                                   v                          |
+-------------------+       |   AWS IoT Core <----------- (pqc-workshop-broker-bridge)      |
        ^                   |     |  IoT Rules                                             |
        | flash (USB)       |     +--> telemetry-processor / message-logger Lambdas        |
        |                   |            |                                                 |
+-------+---------+         |            v                                                 |
| Builder-Pi      |         |   DynamoDB (pqc-workshop-devices, -message-log)              |
| workshop-builder|-- API ->|     |                                                        |
| CLI + Flask GUI |  (IAM)  |     v                                                        |
| (SSM instance)  |         |   AppSync GraphQL API  <-- Cognito (unauth)                  |
+-----------------+         |     |                                                        |
                            |     v                                                        |
                            |   CloudFront + S3  (v2 "PQC Firmware Manager" dashboard)      |
                            +--------------------------------------------------------------+
```

### PQC Key Exchange: SecP256r1MLKEM768

Both the ESP32 and Mosquitto negotiate the **IANA-standardized** hybrid key exchange `SecP256r1MLKEM768` (code point 4587). This combines classical ECDHE P-256 with quantum-resistant ML-KEM-768 in a single TLS 1.3 handshake. The broker gets ML-KEM support from OpenSSL 3.5 with the oqs-provider.

| Component | TLS Library | Named Group | Code Point |
|-----------|-------------|-------------|-----------|
| ESP32 (PQC firmware) | wolfSSL 5.7+ | SecP256r1MLKEM768 | 4587 |
| Mosquitto broker (container) | OpenSSL 3.5+ | SecP256r1MLKEM768 | 4587 |
| Mosquitto → IoT Core bridge | OpenSSL 3.5 | secp256r1 (classical) | 23 |

### Certificate trust

An **AWS Private CA** (ACM Private CA) created by the broker stack is the single trust root for the workshop. It issues both the broker's server certificate and every device certificate, so device ↔ broker mTLS validates against one CA. The broker's cert/key and the IoT Core bridge credentials are stored in Secrets Manager.

### Workshop Flow

1. **Pre-session:** Facilitator deploys the single master CloudFormation stack, runs the one-time firmware image build, and registers the builder-pi. Devices are flashed with classical firmware (OTA_0).
2. **Attendee plugs in ESP32:** Device boots OTA_0 (classical TLS 1.3), connects to the container broker via the NLB, and appears on the dashboard.
3. **Code walkthrough:** Facilitator explains the PQC firmware differences.
4. **Firmware migration:** Attendee clicks **Migrate to PQC** on the dashboard → an asynchronous Step Functions workflow builds the PQC firmware, pushes it over the air, and the device reboots into OTA_1 (PQC). Live status (`building → pushing-ota → rebooting`) is shown on the dashboard.
5. **Verification:** Dashboard shows the device now negotiating `SecP256r1MLKEM768` over TLS 1.3.

See [`cloudformation/DEPLOYMENT.md`](cloudformation/DEPLOYMENT.md) for the full deployment guide, and the [PQC Migration Techniques](#pqc-migration-techniques) section for migration details.

---

## Project Structure

```
Sample-aws-iot-pqc/
+-- README.md                              This file
+-- attendee_experience.md                 Workshop attendee flow document
+-- check_setup_status.sh                  Validates AWS environment readiness
+-- architecture.drawio                    Architecture + deployment-pipeline diagrams
+-- aws_iot_pqc_esp32/
|   +-- aws_iot_pqc_esp32.ino              PQC firmware (OTA_1 / app1)
|   +-- secrets.h                          WiFi + credentials (build-time generated)
|   +-- wolfssl_user_settings.h            Reference wolfSSL PQC configuration
+-- aws_iot_pqc_esp32_v1_classical/
|   +-- aws_iot_pqc_esp32_v1_classical.ino Classical firmware (OTA_0 / app0)
|   +-- secrets.h                          WiFi + credentials
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
    +-- pqc-workshop-main.yaml             Master template (nests all 5 child stacks)
    +-- device-provisioning-stack.yaml     JIT device auto-registration
    +-- ssm-hybrid-activation-stack.yaml   Builder-Pi SSM hybrid activation
    +-- deploy-workshop.sh                 One-command deploy (self-hosted)
    +-- package.sh                         Stage assets for Workshop Studio
    +-- DEPLOYMENT.md                      Deployment guide + manual steps
```

| Path | Description |
|------|-------------|
| `aws_iot_pqc_esp32/` | PQC Arduino sketch (OTA_1) with wolfSSL TLS 1.3, FreeRTOS tasks, and MQTT logic. |
| `aws_iot_pqc_esp32_v1_classical/` | Classical TLS sketch (OTA_0) with OTA migration handler. |
| `secrets.h` | WiFi SSID/password, broker endpoint, Thing name, and PEM certificates. For workshop builds this is generated at compile time by CodeBuild; the checked-in copy holds placeholders. |
| `wolfssl_user_settings.h` | Reference `#define` values for wolfSSL with ML-KEM PQC on ESP32. |
| `broker-container/` | The containerized PQC Mosquitto broker: CloudFormation (ECS Fargate, NLB, Private CA, ECR/CodeBuild image build) and the Docker image sources. |
| `raw_device_builder/` | Device build/registration pipeline: builder API + firmware CodeBuild, the PQC-migration Step Functions workflow, the builder-pi CLI/GUI, and the v2 dashboard frontend. |
| `dashboard/` | Dashboard backend infrastructure and Lambdas (telemetry, message log, broker log, command publisher). |
| `cloudformation/pqc-workshop-main.yaml` | Single master template that nests all five child stacks for one-command / Workshop Studio deployment. |

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

You can set up the AWS IoT Core infrastructure either automatically using CloudFormation or manually through the console.

### Option A: Automated Setup with CloudFormation (Recommended)

The `cloudformation/` folder contains a template that creates everything you need in one command.

**Prerequisites:** AWS CLI installed and configured with permissions for IoT, IAM, and CloudWatch.

#### Step 1: Deploy the Stack

```bash
aws cloudformation deploy \
  --template-file cloudformation/iot-core-setup.yaml \
  --stack-name esp32-pqc-iot \
  --parameter-overrides ThingName=my-esp32-pqc-thing \
  --capabilities CAPABILITY_NAMED_IAM
```

This creates:
- IoT Thing (`my-esp32-pqc-thing`)
- IoT Thing Type (`esp32-pqc-device`)
- X.509 certificate (active)
- IoT Policy with least-privilege permissions for the structured topic hierarchy
- IoT Rules routing telemetry and lifecycle events to CloudWatch Logs
- IAM Role for the IoT Rules

#### Step 2: Get Your IoT Endpoint

```bash
aws iot describe-endpoint --endpoint-type iot:Data-ATS --query 'endpointAddress' --output text
```

Save this value — you will paste it into `secrets.h` as `AWS_IOT_ENDPOINT`.

#### Step 3: Download the Certificate and Private Key

The CloudFormation stack creates a certificate, but you need to retrieve the credentials:

```bash
# Get the certificate ID from stack outputs
CERT_ID=$(aws cloudformation describe-stacks \
  --stack-name esp32-pqc-iot \
  --query 'Stacks[0].Outputs[?OutputKey==`CertificateId`].OutputValue' \
  --output text)

# Download the certificate PEM
aws iot describe-certificate \
  --certificate-id $CERT_ID \
  --query 'certificateDescription.certificatePem' \
  --output text > device-certificate.pem.crt

echo "Certificate saved to device-certificate.pem.crt"
```

**Important:** The private key is generated internally by CloudFormation and cannot be retrieved after creation. If you need the private key, create the certificate manually instead:

```bash
# Create a new certificate with downloadable private key
aws iot create-keys-and-certificate \
  --set-as-active \
  --certificate-pem-outfile device-cert.pem \
  --private-key-outfile device-private-key.pem \
  --public-key-outfile device-public-key.pem

# Get the certificate ARN from the output
CERT_ARN=$(aws iot list-certificates --query 'certificates[-1].certificateArn' --output text)

# Attach the policy and thing
aws iot attach-policy --policy-name my-esp32-pqc-thing-policy --target $CERT_ARN
aws iot attach-thing-principal --thing-name my-esp32-pqc-thing --principal $CERT_ARN
```

#### Step 4: Download Amazon Root CA 1

```bash
curl -o AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
```

#### Step 5: View Stack Outputs

```bash
aws cloudformation describe-stacks \
  --stack-name esp32-pqc-iot \
  --query 'Stacks[0].Outputs' \
  --output table
```

This shows all the topics, ARNs, and log groups created.

#### Cleanup

To remove all AWS resources:
```bash
aws cloudformation delete-stack --stack-name esp32-pqc-iot
```

---

### Option B: Manual Setup via AWS Console

#### Step 1: Create a Thing

1. Open the [AWS IoT Console](https://console.aws.amazon.com/iot/)
2. In the left navigation, go to **Manage > All devices > Things**
3. Click **Create things > Create single thing**
4. Enter the Thing name: `my-esp32-pqc-thing` (must match `THINGNAME` in `secrets.h`)
5. Leave other settings as default and click **Next**

#### Step 2: Generate Certificates

1. On the certificate page, select **Auto-generate a new certificate**
2. Click **Next**
3. **Download all three files now** (you cannot download the private key again later):

| File | Description | Used in `secrets.h` as |
|------|-------------|----------------------|
| `xxxxxxxxxx-certificate.pem.crt` | Device certificate | `AWS_CERT_CRT` |
| `xxxxxxxxxx-private.pem.key` | Device private key | `AWS_CERT_PRIVATE` |
| `AmazonRootCA1.pem` | Amazon Root CA 1 | `AWS_CERT_CA` |

4. Click **Activate** to activate the certificate
5. Click **Done** (you will attach a policy next)

If you missed the Root CA download, get it from: [https://www.amazontrust.com/repository/AmazonRootCA1.pem](https://www.amazontrust.com/repository/AmazonRootCA1.pem)

#### Step 3: Create and Attach an IoT Policy

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
      "Action": "iot:Publish",
      "Resource": [
        "arn:aws:iot:REGION:ACCOUNT_ID:topic/dt/my-esp32-pqc-thing/telemetry",
        "arn:aws:iot:REGION:ACCOUNT_ID:topic/dt/my-esp32-pqc-thing/lifecycle"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "iot:RetainPublish",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topic/dt/my-esp32-pqc-thing/lifecycle"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Subscribe",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topicfilter/cmd/my-esp32-pqc-thing/+"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Receive",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topic/cmd/my-esp32-pqc-thing/*"
    }
  ]
}
```

Replace `REGION` with your AWS region (e.g., `us-east-1`) and `ACCOUNT_ID` with your 12-digit AWS account ID.

5. Click **Create**
6. Go to **Security > Certificates**, find the certificate you created, click on it
7. Click **Actions > Attach policy** and select `esp32-pqc-policy`

#### Step 4: Find Your IoT Endpoint

1. In the IoT Console, go to **Settings** (bottom of the left navigation)
2. Copy the **Device data endpoint** (looks like `xxxxxxxxxxxxxx-ats.iot.us-east-1.amazonaws.com`)

Or use the AWS CLI:
```bash
aws iot describe-endpoint --endpoint-type iot:Data-ATS
```

---

## Validate Environment Readiness

After deploying the CloudFormation stacks (or at any time before/during the workshop), run the readiness check script to confirm all AWS resources are correctly provisioned:

### Prerequisites

- AWS CLI v2 installed
- Valid AWS credentials in the session (environment variables or named profile)
- Read-only access to: IoT, Lambda, DynamoDB, AppSync, CloudFormation, CloudFront, S3, Cognito, ECS, ELB, ACM-PCA, SSM, SecretsManager

### Run the Check

```bash
# From the project root
./check_setup_status.sh
```

### Options

```bash
# Specify a different region
./check_setup_status.sh --region us-west-2

# Specify a custom stack name
./check_setup_status.sh --stack-name my-pqc-workshop

# Show help
./check_setup_status.sh --help
```

### What It Checks

| # | Component | What's Validated |
|---|-----------|-----------------|
| 1 | AWS Credentials | Valid session with account access |
| 2 | CloudFormation | Main + nested stacks deployed successfully |
| 3 | IoT Core | Endpoint, Thing, device policy (esp32-* wildcard), certificates, Rules |
| 4 | Lambda Functions | Dashboard + builder + migration functions exist and are Active |
| 5 | DynamoDB | `pqc-workshop-devices` and `-device-registry` tables ACTIVE |
| 6 | AppSync API | GraphQL endpoint responding |
| 7 | Cognito | Identity pool allows unauthenticated dashboard access |
| 8 | Dashboard | S3 files deployed, CloudFront distribution active |
| 9 | PQC Broker | ECS Fargate service running behind the NLB; Private CA active |
| 10 | Secrets Manager | Broker cert/key + IoT bridge + SSM activation stored |
| 11 | SSM Hybrid Instance | Builder-Pi registered and online via Systems Manager |
| 12 | ESP32 Devices | Auto-registered devices (warning if none yet — expected pre-workshop) |

### Expected Output (Pre-Workshop)

A fully deployed environment that hasn't yet connected the builder-Pi or ESP32 devices will show:

```
  ✅ Passed:   15
  ⚠️  Warnings: 2
  ❌ Failed:   0

  👍 Environment is MOSTLY READY (review warnings above).

  Common warnings before the session:
    - Builder-Pi not registered: Pi not yet provisioned via SSM
    - No ESP32 devices: They auto-register when powered on
```

Once the builder-Pi is online and devices connect, all checks pass with zero warnings.

---

## Configure the Sketch

Open `secrets.h` and fill in your values:

### WiFi Credentials

```c
#define WIFI_SSID     "MyHomeNetwork"
#define WIFI_PASSWORD "MyWiFiPassword123"
```

### Log Level

```c
#define LOG_LEVEL  3   // 0=NONE, 1=ERROR, 2=WARN, 3=INFO, 4=DEBUG, 5=VERBOSE
```

Set to `4` (DEBUG) during initial setup to see detailed diagnostics. Switch to `3` (INFO) for normal operation.

### AWS IoT Configuration

```c
#define AWS_IOT_ENDPOINT "a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com"
#define AWS_IOT_PORT     8883
#define THINGNAME        "my-esp32-pqc-thing"
```

The Thing name is used to construct MQTT topics automatically:
- Telemetry: `dt/my-esp32-pqc-thing/telemetry`
- Lifecycle: `dt/my-esp32-pqc-thing/lifecycle`
- Commands: `cmd/my-esp32-pqc-thing/+`

### Certificates

Open each downloaded `.pem` file in a text editor and paste the full contents (including the `-----BEGIN` and `-----END` lines) into the corresponding variable in `secrets.h`:

- `AWS_CERT_CA` — paste the contents of `AmazonRootCA1.pem`
- `AWS_CERT_CRT` — paste the contents of your device certificate file
- `AWS_CERT_PRIVATE` — paste the contents of your private key file

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

With `LOG_LEVEL` set to 3 (INFO):

```
I (main) ══════════════════════════════════════════════════
I (main)   ESP32 AWS IoT Core - Post-Quantum Cryptography
I (main)   TLS 1.3 + Hybrid Kyber/ML-KEM Key Exchange
I (main) ══════════════════════════════════════════════════

I (main) Chip:       ESP32-S3 Rev 0
I (main) CPU Freq:   240 MHz
I (main) Flash:      8 MB
I (main) Free Heap:  275432 bytes
I (main) PSRAM:      8388608 bytes
I (main) Log Level:  3
I (main) All tasks created. System running.
I (wifi) Task started on core 0
I (wifi) Connecting to SSID 'MyNetwork' (attempt 1)...
I (wifi) Connected! IP: 192.168.1.42, RSSI: -45 dBm, Channel: 6
I (mqtt) Task started on core 1, waiting for WiFi...
I (mqtt) WiFi is up, initializing wolfSSL...
I (wolfssl) Initializing wolfSSL library...
I (wolfssl) CA certificate loaded
I (wolfssl) Device certificate loaded
I (wolfssl) Private key loaded
I (wolfssl) TLS 1.3 PQC context ready (heap free: 215000 bytes)
I (mqtt) Connecting to AWS IoT Core (thing='my-esp32-pqc-thing', attempt=1)...
I (tls) TCP connected to a1b2c3d4e5-ats.iot.us-east-1.amazonaws.com:8883
I (tls) Using PQC key exchange: P-256 + ML-KEM-768 (SecP256r1MLKEM768)
I (tls) Starting TLS 1.3 PQC handshake...
I (tls) TLS handshake complete in 2847 ms
I (tls) Protocol: TLSv1.3, Cipher: TLS_AES_128_GCM_SHA256
I (mqtt) Connected to AWS IoT Core!
I (mqtt) Published lifecycle: online → dt/my-esp32-pqc-thing/lifecycle
I (mqtt) Subscribed to commands: cmd/my-esp32-pqc-thing/+ (QoS 1)
I (mqtt) Published telemetry #1 → dt/my-esp32-pqc-thing/telemetry
I (main) ─── System Status ─────────────────────────────
I (main)   Uptime:     60 sec
I (main)   WiFi:       Connected (RSSI: -45 dBm)
I (main)   MQTT:       Connected
I (main)   Free Heap:  198432 bytes
I (main)   Min Heap:   142108 bytes
I (main) ───────────────────────────────────────────────
I (mqtt) Published telemetry #2 → dt/my-esp32-pqc-thing/telemetry
```

### Verifying the Connection in AWS

#### Using the MQTT Test Client

1. In the AWS IoT Console, go to **Test > MQTT test client**
2. Subscribe to `dt/my-esp32-pqc-thing/#` (wildcard for all device topics)
3. You should see telemetry messages arriving every 30 seconds:

```json
{
  "device": "my-esp32-pqc-thing",
  "message_id": 1,
  "timestamp": 45,
  "uptime_sec": 45,
  "wifi_rssi": -45,
  "free_heap": 198432,
  "min_heap": 142108,
  "chip_temp_c": 48.3,
  "pqc_algorithm": "SECP256R1_MLKEM768",
  "tls_version": "TLS 1.3"
}
```

4. You should also see the lifecycle event (published once on connect):

```json
{
  "status": "online",
  "device": "my-esp32-pqc-thing",
  "firmware_version": "2.0.0",
  "pqc_algorithm": "SECP256R1_MLKEM768",
  "ip": "192.168.1.42",
  "uptime_sec": 15
}
```

5. To send a command to the device, publish to `cmd/my-esp32-pqc-thing/request`:
```json
{"action": "reboot", "delay_sec": 5}
```

#### Using CloudWatch Logs (if IoT Rules are enabled)

```bash
# Stream telemetry logs in real time
aws logs tail /aws/iot/my-esp32-pqc-thing/telemetry --follow

# Stream lifecycle events
aws logs tail /aws/iot/my-esp32-pqc-thing/lifecycle --follow
```

#### Testing the LWT (Last Will)

1. Subscribe to `dt/my-esp32-pqc-thing/lifecycle` in the MQTT test client
2. Power off the ESP32 abruptly (pull the USB cable)
3. After the keepalive timeout (60 seconds), the broker publishes:
```json
{"status": "offline", "reason": "unexpected"}
```

---

## Code Walkthrough

### WolfSSLClient Class (TLS Layer)

The core of this sketch is the `WolfSSLClient` class, which wraps a plain Arduino `WiFiClient` (TCP only) with wolfSSL to add TLS 1.3 + PQC. It implements the Arduino `Client` interface so that `PubSubClient` can use it transparently.

**Connection flow inside `WolfSSLClient::connect()`:**

1. **TCP connect** - `WiFiClient::connect()` opens a raw TCP socket to the AWS IoT endpoint on port 8883
2. **Create SSL session** - `wolfSSL_new()` creates a new TLS session from the shared context
3. **Set I/O callbacks** - Custom send/receive callbacks bridge wolfSSL to the Arduino `WiFiClient`
4. **Request PQC key share** - `wolfSSL_UseKeyShare(ssl, WOLFSSL_SECP256R1_MLKEM768)` tells wolfSSL to offer a hybrid P-256 + ML-KEM-768 key exchange during the TLS handshake. If this fails, it falls back to X25519 + ML-KEM-768.
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

## MQTT Best Practices

This sketch implements several IoT MQTT best practices that are essential for production-quality device firmware.

### Topic Structure

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
  "Resource": "arn:aws:iot:*:*:topic/dt/my-esp32-pqc-thing/*"
}
```

Topics are built dynamically from `THINGNAME` at runtime, so changing the Thing name in `secrets.h` automatically updates all topics.

### Last Will and Testament (LWT)

The MQTT LWT is a message the broker holds and publishes automatically if the device disconnects unexpectedly (network loss, crash, keepalive timeout). This sketch configures:

- **LWT Topic:** `dt/<thingName>/lifecycle`
- **LWT Payload:** `{"status":"offline","reason":"unexpected"}`
- **LWT QoS:** 1 (at least once delivery)
- **LWT Retain:** true (persists so new subscribers see the last state)

This means cloud services always know when a device goes offline, even if the device cannot send a graceful disconnect message.

### Lifecycle Events

On successful MQTT connection, the device publishes a retained "online" event:

```json
{
  "status": "online",
  "device": "my-esp32-pqc-thing",
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

### Exponential Backoff with Jitter

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

### Updated IoT Policy

With the new topic structure, your IoT policy should be:

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
      "Action": "iot:Publish",
      "Resource": [
        "arn:aws:iot:REGION:ACCOUNT_ID:topic/dt/my-esp32-pqc-thing/telemetry",
        "arn:aws:iot:REGION:ACCOUNT_ID:topic/dt/my-esp32-pqc-thing/lifecycle"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "iot:Subscribe",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topicfilter/cmd/my-esp32-pqc-thing/+"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Receive",
      "Resource": "arn:aws:iot:REGION:ACCOUNT_ID:topic/cmd/my-esp32-pqc-thing/*"
    }
  ]
}
```

---

## PQC Migration Techniques

This section describes three approaches for migrating an IoT device from classical TLS to post-quantum TLS via over-the-air (OTA) firmware update. The project includes a pre-PQC firmware (v1.0) and the PQC firmware (v2.0) to demonstrate the migration live.

### Overview

The migration demonstrates a real-world scenario: devices already deployed in the field running classical TLS need to be upgraded to PQC without physical access. The telemetry payload includes `pqc_algorithm` and `tls_version` fields, making the transition visible in real time on a dashboard.

```
Before (v1.0):  {"pqc_algorithm":"none", "tls_version":"TLS 1.2", "firmware_version":"1.0.0"}
After  (v2.0):  {"pqc_algorithm":"SECP256R1_MLKEM768", "tls_version":"TLS 1.3", "firmware_version":"2.0.0"}
```

### Option A: HTTPS OTA Pull (Simple) ← Used in Builders Session

The device listens for an OTA command on its MQTT command topic, then downloads the new firmware binary from an HTTPS URL (e.g., S3 bucket).

> **How the workshop implements this:** the dashboard **Migrate to PQC** button starts an asynchronous **Step Functions** workflow (`pqc-workshop-pqc-migration`). It builds the device's PQC firmware with CodeBuild (via the `startBuild.sync` integration so nothing blocks), then the `publish-ota` Lambda generates a **regional presigned S3 URL** and publishes `{"action":"ota-update","url":...}` to `cmd/<thing>/ota`. The device downloads to OTA_1 and reboots into PQC. Live progress (`building → pushing-ota → rebooting`) is written to the `pqc-workshop-devices` table and shown on the dashboard, which polls every 5 seconds.

**Flow:**
1. Device runs v1.0 (classical TLS), publishes telemetry normally
2. Cloud publishes `{"url":"https://s3.amazonaws.com/bucket/firmware-v2.bin"}` to `cmd/<thing>/ota`
3. Device downloads the binary using `httpUpdate`, writes to OTA partition
4. Device reboots into v2.0 (PQC TLS)
5. Telemetry now shows PQC algorithm and TLS 1.3

**Pros:**
- Simple implementation (~50 lines of OTA code)
- No extra AWS services beyond S3
- Works with Arduino Cloud IDE
- Realistic production scenario

**Cons:**
- Requires network download during demo (risk of conference WiFi issues)
- Download takes 10-30 seconds depending on binary size and network speed
- No built-in rollback or fleet tracking

**Best for:** Demos where showing the actual download process is important.

### Option B: AWS IoT Jobs (Production-Grade)

AWS IoT Jobs provides a managed OTA framework with status tracking, rollback, and fleet-wide deployment.

**Flow:**
1. Device runs v1.0 with an IoT Jobs agent polling `$aws/things/<thing>/jobs/notify-next`
2. Operator creates a Job targeting the Thing with a Job Document containing the S3 firmware URL
3. Device receives the job, downloads firmware, writes to OTA partition
4. Device reboots into v2.0, reports job completion to `$aws/things/<thing>/jobs/<jobId>/update`
5. IoT Jobs dashboard shows successful migration

**Pros:**
- Production-ready fleet management
- Tracks success/failure per device
- Supports staged rollout (percentage-based)
- Built-in timeout and retry
- Audit trail

**Cons:**
- More setup (IAM roles, Job Document, S3 presigned URLs, Jobs agent code)
- Still requires network download during demo
- More complex to explain in a short session

**Best for:** Showing the production architecture in slides/diagrams, or longer workshops.

### Option C: Dual-Partition Switch (Instant Demo)

Both firmware versions are pre-flashed to the ESP32's two OTA partitions before the demo. A command on the MQTT topic triggers an instant boot partition switch — no download needed. (This was an earlier approach; the builders session now uses the real OTA pull in Option A.)

**Flow:**
1. Pre-flash v1.0 to OTA_0 and v2.0 to OTA_1 using `esptool.py`
2. Device boots into v1.0 (OTA_0), connects with classical TLS
3. Presenter publishes `{"action":"migrate-pqc"}` to `cmd/<thing>/ota`
4. Device calls `esp_ota_set_boot_partition(ota_1)` and reboots (~3 seconds)
5. Device boots into v2.0 (OTA_1), connects with PQC TLS
6. Dashboard instantly shows the PQC fields change

**Pros:**
- Instant switch (~3 seconds total)
- No network download — zero risk of WiFi failure on stage
- Deterministic timing for live demos
- Visually identical to a real OTA from the audience perspective

**Cons:**
- Requires pre-staging both images before the demo
- Not a "real" OTA download (but the mechanism is the same as post-download)
- Requires `esptool.py` for initial dual-flash setup

**Best for:** Live stage demos where reliability and speed are critical.

### Pre-Flashing for Option C

```bash
# Build both sketches and export their binaries from Arduino IDE
# (Sketch > Export Compiled Binary)

# Flash v1.0 to OTA_0 partition and v2.0 to OTA_1 partition
esptool.py --chip esp32s3 --port /dev/ttyUSB0 \
  write_flash 0x10000 firmware_v1_classical.bin \
               0x200000 firmware_v2_pqc.bin

# The device boots from OTA_0 (v1.0) by default
```

The exact partition offsets depend on your partition table. The default ESP32 Arduino partition scheme with OTA uses:
- `0x10000` — OTA_0 (primary)
- `0x200000` — OTA_1 (secondary)

Verify with: `esptool.py --chip esp32s3 read_flash_status`

### Project Structure for Migration Demo

```
aws_iot_pqc_esp32/              ← v2.0 (PQC) - the main sketch
aws_iot_pqc_esp32_v1_classical/ ← v1.0 (Classical TLS) - pre-PQC baseline
cloudformation/                 ← AWS IoT Core infrastructure
```

---

The sketch uses **hybrid key exchange**, combining a classical and a post-quantum algorithm in a single TLS 1.3 handshake:

| Component | Algorithm | NIST Security Level | Key Size | Purpose |
|-----------|-----------|-------------------|----------|---------|
| Classical | ECDHE P-256 | ~128-bit equivalent | 65 bytes | Protection against classical attacks |
| Post-Quantum | ML-KEM-768 (FIPS 203) | Level 3 | 1184 bytes public key | Protection against quantum attacks |

### Why Hybrid?

The hybrid approach provides defense in depth:
- If Kyber is found to have a weakness, ECDHE P-521 still protects the session
- If a quantum computer breaks ECDHE, Kyber still protects the session
- Both algorithms must be broken simultaneously to compromise the key exchange

### Fallback Behavior

The sketch tries two PQC key exchange options in order:

1. **P-256 + ML-KEM-768** (SecP256r1MLKEM768, IANA code point 4587, recommended)
2. **X25519 + ML-KEM-768** (X25519MLKEM768, IANA code point 4588, fallback)

If the server does not support either hybrid option, the TLS 1.3 handshake will still succeed using classical key exchange only (the server selects the best mutually-supported option). The device is "PQC-ready" regardless.

### wolfSSL Key Share Constants

This project uses the **IANA-standardized ML-KEM hybrid groups** (section B-8 of the wolfSSL manual). These are interoperable with OpenSSL 3.5+.

| Constant | Classical Curve | ML-KEM Variant | IANA Code Point |
|----------|----------------|----------------|-----------------|
| `WOLFSSL_SECP256R1_MLKEM768` | NIST P-256 | ML-KEM-768 | 4587 |
| `WOLFSSL_X25519_MLKEM768` | X25519 | ML-KEM-768 | 4588 |
| `WOLFSSL_SECP384R1_MLKEM1024` | NIST P-384 | ML-KEM-1024 | 4589 |

The legacy Kyber Round 3 constants (`WOLFSSL_P521_KYBER_LEVEL5` etc.) use experimental code points (120xx) that are not interoperable with OpenSSL. They are deprecated in favor of the standard groups above.

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
| Hybrid P-256 + ML-KEM-768 | ~1500-2500 ms |
| Hybrid X25519 + ML-KEM-768 | ~1500-2500 ms |

Times depend on WiFi latency, server load, and ESP32 clock speed (240 MHz recommended).

### Reducing Memory Usage

If you are tight on memory:

1. Use `WOLFSSL_X25519_MLKEM768` as a lighter alternative (slightly smaller key shares)
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
| `WOLFSSL_SECP256R1_MLKEM768 was not declared` | wolfSSL version too old or ML-KEM not enabled | Update wolfSSL to 5.7.0+ and verify `user_settings.h` |
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

3. **Test with the MQTT test client**: In the AWS IoT Console, use **Test > MQTT test client** to subscribe to `dt/my-esp32-pqc-thing/#` and verify messages are arriving.

4. **Check CloudWatch logs**: If the device connects via TCP but the TLS handshake fails, check the AWS IoT Core logs in CloudWatch for server-side error details.

5. **Simplify first**: If you cannot get PQC working, temporarily comment out the `wolfSSL_UseKeyShare()` call. The connection will use classical key exchange, which confirms your certificates and network are correct. Then re-enable PQC.

---

## Automatic Registration and Certificate Provisioning

This project uses manually-created certificates for simplicity (appropriate for a 60-minute builders session). However, for production deployments or larger workshops, AWS IoT supports **Fleet Provisioning by Claim** — a mechanism where devices automatically register themselves and obtain unique certificates on first boot.

### How Fleet Provisioning Works

Instead of creating a certificate per device in the AWS Console, you bake a shared "claim certificate" into the firmware. On first boot, the device uses this claim cert to connect with limited permissions, requests its own unique certificate, registers as a Thing, and then reconnects with its permanent identity.

```
First Boot (one-time, ~5-10 seconds):
┌──────────┐                              ┌─────────────────┐
│  Device   │── Connect with claim cert ──▶│  AWS IoT Core   │
│           │                              │                 │
│           │── CreateCertificate ────────▶│  Returns new    │
│           │◀── cert + private key ───────│  device cert    │
│           │                              │                 │
│           │── RegisterThing ───────────▶│  Creates Thing,  │
│           │◀── thing name ───────────────│  attaches policy│
│           │                              │                 │
│           │── Save cert to NVS ──────────│                 │
│           │── Reconnect with device cert─│                 │
└──────────┘                              └─────────────────┘

Subsequent Boots:
┌──────────┐                              ┌─────────────────┐
│  Device   │── Load cert from NVS ────────│                 │
│           │── Connect directly ─────────▶│  AWS IoT Core   │
└──────────┘                              └─────────────────┘
```

### When to Use Fleet Provisioning

| Scenario | Approach | Reason |
|----------|----------|--------|
| Builders session (60 min) | Manual certs | Simpler, fewer failure points, focus stays on PQC |
| Workshop (2+ hours) | Fleet Provisioning | "Just flash and go" experience, no cert copy/paste |
| Production (100+ devices) | Fleet Provisioning | Scales without per-device manual steps |
| Manufacturing line | Fleet Provisioning | Same firmware image for all units |

### AWS Resources Required

To enable Fleet Provisioning, you would add these to the CloudFormation template:

```yaml
# Provisioning Template — defines what to create for each new device
IoTProvisioningTemplate:
  Type: AWS::IoT::ProvisioningTemplate
  Properties:
    TemplateName: esp32-pqc-provisioning
    Description: Auto-provisions ESP32 PQC devices on first connection
    Enabled: true
    ProvisioningRoleArn: !GetAtt ProvisioningRole.Arn
    TemplateBody: |
      {
        "Parameters": {
          "SerialNumber": {"Type": "String"},
          "AWS::IoT::Certificate::Id": {"Type": "String"}
        },
        "Resources": {
          "thing": {
            "Type": "AWS::IoT::Thing",
            "Properties": {
              "ThingName": {"Fn::Join": ["-", ["esp32-pqc", {"Ref": "SerialNumber"}]]},
              "ThingTypeName": "esp32-pqc-device",
              "AttributePayload": {
                "Attributes": {
                  "pqc_algorithm": "SECP256R1_MLKEM768",
                  "provisioned_by": "fleet-provisioning"
                }
              }
            }
          },
          "certificate": {
            "Type": "AWS::IoT::Certificate",
            "Properties": {
              "CertificateId": {"Ref": "AWS::IoT::Certificate::Id"},
              "Status": "ACTIVE"
            }
          },
          "policy": {
            "Type": "AWS::IoT::Policy",
            "Properties": {
              "PolicyName": "esp32-pqc-device-policy"
            }
          }
        }
      }

# Claim Certificate Policy — very restrictive, only allows provisioning topics
ClaimPolicy:
  Type: AWS::IoT::Policy
  Properties:
    PolicyName: esp32-pqc-claim-policy
    PolicyDocument:
      Version: '2012-10-17'
      Statement:
        - Effect: Allow
          Action: iot:Connect
          Resource: '*'
        - Effect: Allow
          Action:
            - iot:Publish
            - iot:Receive
          Resource:
            - !Sub 'arn:aws:iot:${AWS::Region}:${AWS::AccountId}:topic/$aws/certificates/create/*'
            - !Sub 'arn:aws:iot:${AWS::Region}:${AWS::AccountId}:topic/$aws/provisioning-templates/esp32-pqc-provisioning/provision/*'
        - Effect: Allow
          Action: iot:Subscribe
          Resource:
            - !Sub 'arn:aws:iot:${AWS::Region}:${AWS::AccountId}:topicfilter/$aws/certificates/create/*'
            - !Sub 'arn:aws:iot:${AWS::Region}:${AWS::AccountId}:topicfilter/$aws/provisioning-templates/esp32-pqc-provisioning/provision/*'

# IAM Role for the provisioning template
ProvisioningRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Version: '2012-10-17'
      Statement:
        - Effect: Allow
          Principal:
            Service: iot.amazonaws.com
          Action: sts:AssumeRole
    ManagedPolicyArns:
      - arn:aws:iam::aws:policy/service-role/AWSIoTThingsRegistration
```

### Device Firmware Changes (Sample Code)

Below is a reference implementation of the provisioning flow for the ESP32. This would run once on first boot, before the normal MQTT task starts.

```cpp
#include <Preferences.h>  // ESP32 NVS (Non-Volatile Storage)

// NVS namespace for storing provisioned credentials
#define NVS_NAMESPACE    "iot_creds"
#define NVS_KEY_CERT     "device_cert"
#define NVS_KEY_KEY      "device_key"
#define NVS_KEY_THING    "thing_name"
#define NVS_KEY_PROVISIONED "provisioned"

// Fleet Provisioning MQTT topics
#define FP_CREATE_CERT_TOPIC     "$aws/certificates/create/json"
#define FP_CREATE_CERT_ACCEPTED  "$aws/certificates/create/json/accepted"
#define FP_CREATE_CERT_REJECTED  "$aws/certificates/create/json/rejected"
#define FP_REGISTER_TOPIC        "$aws/provisioning-templates/esp32-pqc-provisioning/provision/json"
#define FP_REGISTER_ACCEPTED     "$aws/provisioning-templates/esp32-pqc-provisioning/provision/json/accepted"
#define FP_REGISTER_REJECTED     "$aws/provisioning-templates/esp32-pqc-provisioning/provision/json/rejected"

Preferences nvs;

// State machine for provisioning
enum ProvisionState {
    PROV_CHECK_NVS,
    PROV_CONNECT_CLAIM,
    PROV_CREATE_CERT,
    PROV_WAIT_CERT,
    PROV_REGISTER_THING,
    PROV_WAIT_REGISTER,
    PROV_SAVE_AND_RECONNECT,
    PROV_DONE
};

static ProvisionState g_provState = PROV_CHECK_NVS;
static String g_newCertPem;
static String g_newKeyPem;
static String g_certOwnershipToken;
static String g_thingName;

// Check if device is already provisioned
bool isProvisioned() {
    nvs.begin(NVS_NAMESPACE, true);  // Read-only
    bool provisioned = nvs.getBool(NVS_KEY_PROVISIONED, false);
    nvs.end();
    return provisioned;
}

// Load provisioned credentials from NVS
bool loadProvisionedCredentials(String& cert, String& key, String& thingName) {
    nvs.begin(NVS_NAMESPACE, true);
    cert = nvs.getString(NVS_KEY_CERT, "");
    key = nvs.getString(NVS_KEY_KEY, "");
    thingName = nvs.getString(NVS_KEY_THING, "");
    nvs.end();
    return (cert.length() > 0 && key.length() > 0 && thingName.length() > 0);
}

// Save new credentials to NVS
void saveProvisionedCredentials(const String& cert, const String& key,
                                 const String& thingName) {
    nvs.begin(NVS_NAMESPACE, false);  // Read-write
    nvs.putString(NVS_KEY_CERT, cert);
    nvs.putString(NVS_KEY_KEY, key);
    nvs.putString(NVS_KEY_THING, thingName);
    nvs.putBool(NVS_KEY_PROVISIONED, true);
    nvs.end();
    ESP_LOGI("provision", "Credentials saved to NVS (cert=%d bytes, key=%d bytes, thing='%s')",
             cert.length(), key.length(), thingName.c_str());
}

// Callback for provisioning MQTT messages
void provisioningCallback(char* topic, byte* payload, unsigned int length) {
    char buf[4096];
    unsigned int copyLen = (length < sizeof(buf) - 1) ? length : sizeof(buf) - 1;
    memcpy(buf, payload, copyLen);
    buf[copyLen] = '\0';

    if (strstr(topic, "certificates/create/json/accepted")) {
        ESP_LOGI("provision", "Certificate created successfully");
        // Parse JSON response to extract:
        //   certificatePem, privateKey, certificateOwnershipToken
        // (Use ArduinoJson or simple string parsing)
        //
        // Example response structure:
        // {
        //   "certificateId": "...",
        //   "certificatePem": "-----BEGIN CERTIFICATE-----\n...",
        //   "privateKey": "-----BEGIN RSA PRIVATE KEY-----\n...",
        //   "certificateOwnershipToken": "..."
        // }
        //
        // Store in g_newCertPem, g_newKeyPem, g_certOwnershipToken
        g_provState = PROV_REGISTER_THING;

    } else if (strstr(topic, "certificates/create/json/rejected")) {
        ESP_LOGE("provision", "Certificate creation REJECTED: %s", buf);
        g_provState = PROV_CONNECT_CLAIM;  // Retry

    } else if (strstr(topic, "provision/json/accepted")) {
        ESP_LOGI("provision", "Thing registered successfully");
        // Parse response to extract thingName
        // Example: {"deviceConfiguration":{"thingName":"esp32-pqc-ABC123"}}
        // Store in g_thingName
        g_provState = PROV_SAVE_AND_RECONNECT;

    } else if (strstr(topic, "provision/json/rejected")) {
        ESP_LOGE("provision", "Thing registration REJECTED: %s", buf);
    }
}

// Run the provisioning flow (call from mqttTask before normal operation)
void runProvisioningFlow(PubSubClient& mqttClient) {
    switch (g_provState) {
        case PROV_CREATE_CERT:
            // Request a new certificate
            ESP_LOGI("provision", "Requesting new device certificate...");
            mqttClient.subscribe(FP_CREATE_CERT_ACCEPTED);
            mqttClient.subscribe(FP_CREATE_CERT_REJECTED);
            mqttClient.publish(FP_CREATE_CERT_TOPIC, "{}");
            g_provState = PROV_WAIT_CERT;
            break;

        case PROV_REGISTER_THING: {
            // Register the thing with the new certificate
            ESP_LOGI("provision", "Registering thing...");
            mqttClient.subscribe(FP_REGISTER_ACCEPTED);
            mqttClient.subscribe(FP_REGISTER_REJECTED);

            // Build registration payload with device serial number
            String serialNumber = String((uint32_t)(ESP.getEfuseMac() >> 16), HEX);
            char regPayload[512];
            snprintf(regPayload, sizeof(regPayload),
                     "{\"certificateOwnershipToken\":\"%s\","
                     "\"parameters\":{\"SerialNumber\":\"%s\"}}",
                     g_certOwnershipToken.c_str(),
                     serialNumber.c_str());
            mqttClient.publish(FP_REGISTER_TOPIC, regPayload);
            g_provState = PROV_WAIT_REGISTER;
            break;
        }

        case PROV_SAVE_AND_RECONNECT:
            // Save credentials and reconnect with permanent identity
            saveProvisionedCredentials(g_newCertPem, g_newKeyPem, g_thingName);
            ESP_LOGI("provision", "Provisioning complete! Restarting with permanent cert...");
            vTaskDelay(pdMS_TO_TICKS(1000));
            esp_restart();
            break;

        default:
            // PROV_WAIT_CERT, PROV_WAIT_REGISTER — just call mqttClient.loop()
            break;
    }
}
```

### Integration with the Main Sketch

The provisioning flow would be integrated into the MQTT task like this:

```cpp
void mqttTask(void* pvParameters) {
    // ... wait for WiFi ...

    if (isProvisioned()) {
        // Normal path: load cert from NVS, connect with wolfSSL + PQC
        String cert, key, thingName;
        loadProvisionedCredentials(cert, key, thingName);
        // Use cert/key for wolfSSL context...
        // Use thingName for MQTT client ID and topics...
    } else {
        // First boot: connect with claim cert (classical TLS),
        // run provisioning flow, then restart
        ESP_LOGI("provision", "First boot — starting Fleet Provisioning...");
        // Connect with WiFiClientSecure using CLAIM_CERT from PROGMEM
        // Set callback to provisioningCallback
        // Run provisioning state machine
        // Device will restart after provisioning completes
    }

    // ... normal MQTT loop (only reached after provisioning) ...
}
```

### Attendee Experience with Fleet Provisioning

If implemented, the builders session flow would simplify to:

1. Attendee opens Arduino Cloud IDE
2. Flashes the firmware (identical binary for everyone — contains claim cert + WiFi creds)
3. Device powers on, auto-provisions in ~5-10 seconds
4. Telemetry appears on the dashboard — no certificate copy/paste needed

### Why We Don't Use It in This Session

For a 60-minute builders session, manual certificate provisioning is preferred because:

- Fewer moving parts to debug in a room of attendees
- Attendees learn the certificate model explicitly (educational value)
- The PQC migration story stays front and center
- Fleet Provisioning adds ~200 lines of code unrelated to PQC
- If provisioning fails for one attendee, it's harder to troubleshoot than a cert paste error

Fleet Provisioning is the right choice for production deployments or workshops longer than 2 hours where the "just flash and go" experience justifies the additional infrastructure setup.

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
