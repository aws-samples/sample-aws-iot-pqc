# PQC Workshop — Session History & Context

This file captures the full development history of this project so that future sessions can pick up where we left off.

## Project Overview

An AWS re:Invent Builders Session demonstrating post-quantum cryptography (PQC) for IoT. ESP32 devices connect to a Raspberry Pi running Mosquitto with PQC-enabled TLS (wolfSSL + ML-KEM), which bridges to AWS IoT Core. A React dashboard shows device status, allows firmware migration between classical and PQC, and displays message logs.

## Architecture

```
ESP32 Devices (wolfSSL TLS 1.3 + ML-KEM)
    ↓ MQTT over PQC TLS (port 8883)
Raspberry Pi (Mosquitto broker + Greengrass + SSM + CloudWatch Agent)
    ↓ MQTT bridge to IoT Core
AWS IoT Core
    ↓ IoT Rules
Lambda (telemetry-processor, message-logger, command-publisher, broker-log-reader)
    ↓
DynamoDB (devices table, message-log table)
    ↓
AppSync GraphQL API
    ↓
React Dashboard (S3 + CloudFront + Cognito guest auth)
```

## Key Infrastructure

| Resource | Value |
|----------|-------|
| AWS Account | 087195661091 |
| Region | us-east-1 |
| IoT Endpoint | auxsw1j2tp0z6-ats.iot.us-east-1.amazonaws.com |
| CloudFront Distribution | E35PBBUBSD7O3C |
| Dashboard URL | https://d2yxjskna5y9z3.cloudfront.net |
| S3 Dashboard Bucket | pqc-workshop-dashboard-087195661091 |
| AppSync API ID | ax6cgi3kdfbani34ewakdiowm4 |
| Cognito Identity Pool | us-east-1:589e1d0a-9a61-4021-a7e9-643cd1899375 |
| SSM Instance (Pi) | mi-0edb5bc536ce8b7fc |
| Raspberry Pi IP | 192.168.1.90 |
| WiFi SSID | <redacted — set per deployment> |
| WiFi Password | <redacted — see secrets, never commit> |
| DynamoDB Devices Table | pqc-workshop-devices |
| DynamoDB Message Log Table | pqc-workshop-message-log |
| CloudWatch Broker Log | /pqc-workshop/mosquitto-broker |
| CloudWatch Greengrass Log | /pqc-workshop/greengrass |

## ESP32 Build System

- **FQBN:** `esp32:esp32:XIAO_ESP32S3:USBMode=hwcdc,CDCOnBoot=cdc,DebugLevel=info,PartitionScheme=default_8MB` (CDCOnBoot=cdc so USB-CDC serial is on for the running sketch, not just during flash)
- **Custom partition table:** `esp32-flash/partitions_pqc_workshop.csv` (OTA_0=Classical at 0x10000, OTA_1=PQC at 0x390000)
- **Build property:** `build.partitions=pqc_workshop`
- **MQTT Library:** MQTT by Joel Gaehwiler (NOT PubSubClient)
- **TLS Library:** wolfSSL 5.8.4 with ML-KEM/Kyber support

### Build Commands

```bash
# One-time setup (installs ESP32 core, wolfSSL, MQTT library)
cd arduino-cli && ./setup.sh

# Regenerate bundle (copies latest .ino source + fetches certs from Pi via SSM)
./esp32-flash/bundle-for-esp32.sh --wifi-ssid "<YOUR_SSID>" --wifi-password '<YOUR_WIFI_PASSWORD>' --broker-ip <BROKER_IP>

# Build + flash dual-boot (MUST run from arduino-cli/ directory)
cd arduino-cli && ./build.sh --dual-boot

# Build PQC only
cd arduino-cli && ./build.sh

# Build classical only
cd arduino-cli && ./build.sh --classical

# Serial monitor
cd arduino-cli && ./monitor.sh
```

**IMPORTANT:** The build script compiles from `esp32-arduino-bundle/` (NOT from the project root source). You MUST run `bundle-for-esp32.sh` after editing any `.ino` file to copy changes into the bundle before building.

## Firmware Details

### PQC Firmware (v2.0.0) — `aws_iot_pqc_esp32/`
- wolfSSL TLS 1.3 with SecP256r1MLKEM768 hybrid key exchange
- LED blink at 200ms (GPIO 21)
- Handles `migrate-classical` command on `cmd/<thing>/ota` topic
- Handles `led-on`/`led-off` on `cmd/<thing>/control` topic
- Reports actual negotiated algorithm via `_pqcRequested` runtime flag
- Stack size 32768 for MQTT task (PQC needs extra)

### Classical Firmware (v1.0.0) — `aws_iot_pqc_esp32_v1_classical/`
- wolfSSL TLS 1.3 without PQC key share (ECDHE P-256)
- LED blink at 500ms (visual distinction from PQC)
- Handles `migrate-pqc` command on `cmd/<thing>/ota` topic
- Handles `led-on`/`led-off` on `cmd/<thing>/control` topic
- Uses explicit partition lookup (`ESP_PARTITION_SUBTYPE_APP_OTA_1`) for OTA switch

### OTA Partition Switching
Both firmwares use `esp_partition_find_first()` with explicit subtype to find the target partition — NOT `esp_ota_get_next_update_partition()` which doesn't work reliably with direct esptool flashing.

- Classical (app0) → finds OTA_1 via `ESP_PARTITION_SUBTYPE_APP_OTA_1`
- PQC (app1) → finds OTA_0 via `ESP_PARTITION_SUBTYPE_APP_OTA_0`

## Dashboard Features

1. **Device Cards** — Status, algorithm, firmware version, WiFi RSSI, heap, temperature, uptime
2. **PQC/Classical Badge** — Green (PQC) or orange (Classical) based on `pqcAlgorithm` field containing MLKEM/Kyber
3. **Online/Offline Detection** — 90-second staleness check on `lastSeen`
4. **Migrate to PQC Button** — Sends `migrate-pqc` via `cmd/<thing>/ota`
5. **Revert to Classical Button** — Sends `migrate-classical` via `cmd/<thing>/ota`
6. **Blink Toggle Switch** — Sends `led-on`/`led-off` via `cmd/<thing>/control`
7. **Device Filter** — Filter by device name in header
8. **Manage Devices Panel** — Remove individual devices or all (⚙️ button)
9. **Message Log (📋)** — Per-device MQTT message history from DynamoDB (IoT Rules capture)
10. **Broker Log (🦟)** — Mosquitto broker log from CloudWatch, filterable by device

## MQTT Topic Structure

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `dt/<thing>/telemetry` | Device → Cloud | Periodic telemetry (30s) |
| `dt/<thing>/lifecycle` | Device → Cloud | Online/offline status |
| `cmd/<thing>/control` | Cloud → Device | LED control |
| `cmd/<thing>/ota` | Cloud → Device | Firmware partition switch |

## Lambda Functions

| Function | Purpose |
|----------|---------|
| `pqc-workshop-telemetry-processor` | IoT Rule → DynamoDB device state |
| `pqc-workshop-command-publisher` | AppSync → IoT Core publish (commands) |
| `pqc-workshop-message-logger` | IoT Rule → DynamoDB message log |
| `pqc-workshop-broker-log-reader` | CloudWatch Logs Insights → AppSync |

## Raspberry Pi Setup

- SSM hybrid activation (no AWS credentials needed on Pi)
- Mosquitto PQC broker (OpenSSL 3.5 + oqs-provider for ML-KEM)
- Greengrass v2 (nucleus + bridge component)
- CloudWatch agent (ships broker + Greengrass logs)
- Bridge client ID: `pqc-workshop-greengrass-core-bridge` (avoids conflict with Greengrass nucleus)

### Pi Scripts
- `greengrass/bundle-for-pi.sh` — Generates self-contained zip for Pi setup
- `greengrass/setup-all.sh` — Idempotent setup (SSM, Greengrass, Mosquitto, CloudWatch)
- `greengrass/check_rpi_setup_status.sh` — Validates 12 components on Pi

## Known Issues & Fixes Applied

1. **`while(!Serial)` causes infinite hang** on ESP32-S3 — use 10s non-blocking wait
2. **NTP sync must happen before loading certs** — otherwise ASN_BEFORE_DATE_E
3. **Greengrass + bridge same client ID** → SESSION_TAKEN_OVER loop — bridge appends `-bridge`
4. **`esp_ota_get_next_update_partition` unreliable** with esptool flashing — use `esp_partition_find_first()` with explicit subtype
5. **Telemetry processor was overwriting firmwareVersion** with 'unknown' — changed to `update_item` that skips unknown values
6. **Dashboard `isPqc` check** — must look for MLKEM/Kyber in algorithm name (runtime detection)
7. **Build script compiles from bundle dir** — must regenerate bundle after source edits

## CloudFormation Stacks

- `dashboard/infrastructure/dashboard-stack.yaml` — All dashboard infra (DynamoDB, AppSync, Lambdas, IoT Rules, Cognito, S3, CloudFront, message log, broker log)
- `cloudformation/pqc-workshop-main.yaml` — Main stack (references SSM stack)
- `cloudformation/ssm-hybrid-activation-stack.yaml` — SSM hybrid activation for Pi

## Deployment Commands

```bash
# Deploy dashboard frontend
cd dashboard/frontend && npm run build
aws s3 sync build/ s3://pqc-workshop-dashboard-087195661091/ --delete --region us-east-1
aws cloudfront create-invalidation --distribution-id E35PBBUBSD7O3C --paths "/*" --region us-east-1

# Update a Lambda (example: message-logger)
cd dashboard/lambda/message-logger
zip -j /tmp/fn.zip index.py
aws lambda update-function-code --function-name pqc-workshop-message-logger --zip-file fileb:///tmp/fn.zip --region us-east-1
```

## Git Conventions

- Conventional commits (feat/fix/docs/chore)
- Never commit real WiFi creds or secrets (secrets.h is in .gitignore, bundle dir is in .gitignore)
- Device names: `esp32-XXYYZZ` format (MAC-derived)

## What's Left / Future Work

- Dual-boot "Revert to Classical" — firmware updated with explicit partition lookup, rebuilt and flashed with `--dual-boot`. Needs live testing (press "Revert to Classical" button on dashboard while PQC device is online). If it still doesn't work, check serial output for "OTA topic detected" log.
- Architecture diagram (`architecture.drawio`) may need updating for broker log + message log features
- Consider adding AppSync subscriptions for real-time dashboard updates (currently polling every 5s)
- Workshop attendee guide / step-by-step instructions document

## TASK 19: Broker Container (ECS Fargate + NLB + Private CA)

**STATUS:** In progress — Docker image building in CodeBuild

**Location:** `broker-container/`

**What it is:** Containerized Mosquitto MQTT broker with OpenSSL 3.5 + oqs-provider (ML-KEM PQC), running on ECS Fargate behind an NLB (TCP passthrough). Certificates from AWS Private CA.

**Current state:**
- CloudFormation stack `pqc-workshop-broker-container` — CREATE_IN_PROGRESS (ECS service failing because no image yet)
- Docker image build `pqc-workshop-broker-image:077e7870-5efa-4153-ad7c-cec272e221eb` — building (compiling OpenSSL 3.5 + liboqs + Mosquitto)
- ECR repo `pqc-workshop-mosquitto-broker` exists but empty
- Private CA created and activated
- Secrets Manager populated with CA cert, broker cert, broker key
- NLB created

**When you check in tomorrow:**

1. Check if the Docker image build succeeded:
```bash
aws codebuild batch-get-builds --ids "pqc-workshop-broker-image:077e7870-5efa-4153-ad7c-cec272e221eb" --region us-east-1 --query 'builds[0].buildStatus' --output text
```

2. Check if image is in ECR:
```bash
aws ecr describe-images --repository-name pqc-workshop-mosquitto-broker --region us-east-1
```

3. Check CloudFormation stack status:
```bash
aws cloudformation describe-stacks --stack-name pqc-workshop-broker-container --region us-east-1 --query 'Stacks[0].StackStatus' --output text
```

**If image build SUCCEEDED but stack rolled back:**
```bash
# Redeploy stack with DesiredCount: 0 (template already fixed)
cd broker-container
aws cloudformation deploy \
  --template-file cloudformation/broker-container-stack.yaml \
  --stack-name pqc-workshop-broker-container \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides VpcId=vpc-0e2090cf34c4dd5df SubnetIds=subnet-08e67aa7ab02cd95f,subnet-09d47ce3ddab3b738 \
  --region us-east-1

# Then scale to 1
aws ecs update-service --cluster pqc-workshop-broker --service pqc-workshop-broker --desired-count 1 --region us-east-1
```

**If image build FAILED:**
- Check logs: `aws logs filter-log-events --log-group-name /aws/codebuild/pqc-workshop-broker-image --region us-east-1`
- The issue was liboqs CMake not finding OpenSSL — fixed by adding explicit `-DOPENSSL_INCLUDE_DIR` and `-DOPENSSL_CRYPTO_LIBRARY` paths and `--libdir=lib` to the OpenSSL configure
- May need another iteration on the Dockerfile

**Key resources:**
- CodeBuild project: `pqc-workshop-broker-image`
- ECR repo: `pqc-workshop-mosquitto-broker`
- CloudFormation stack: `pqc-workshop-broker-container`
- VPC: `vpc-0e2090cf34c4dd5df`
- Subnets: `subnet-08e67aa7ab02cd95f`, `subnet-09d47ce3ddab3b738`
- Private CA ARN: check stack outputs once created

---

## Pending Tasks Summary

| Task | Status | Next Action |
|------|--------|-------------|
| Broker container image build | Building in CodeBuild | Check if succeeded tomorrow |
| CloudFormation stack | Likely rolled back (no image) | Redeploy with DesiredCount:0 |
| Device FC22E0 cleanup | ✅ Done | - |
| Builder-pi GUI updates | ✅ Deployed | Ready to test register & flash |
| Dashboard v2 (firmware manager) | ✅ Deployed | https://d21c2wyzdgiex7.cloudfront.net |
| PQC cert gen via SSM | ✅ Working | - |
| Firmware build (pre-built ECR image) | ✅ Working | ~30s builds |
| OTA updater firmware | ✅ Code complete | Needs successful flash test |

**STATUS:** Code complete, not yet deployed

**Location:** `raw_device_builder/`

**What it is:** A production-line simulation where a "builder-pi" (second Raspberry Pi) auto-detects raw ESP32 devices via USB, registers them in IoT Core with unique certificates, compiles device-specific firmware in CodeBuild, flashes via serial (Firmware 0), and supports subsequent OTA updates via presigned S3 URLs (Firmware 0+).

**Components built:**
- `cloudformation/device-builder-stack.yaml` — API Gateway, Lambda x2, CodeBuild, S3, DynamoDB, IAM
- `lambda/device-registrar/index.py` — Creates IoT Thing + cert + policy, triggers CodeBuild
- `lambda/firmware-pusher/index.py` — Lists firmware versions, generates presigned URLs, publishes OTA command
- `codebuild/buildspec.yml` — Installs arduino-cli + ESP32 toolchain, compiles with injected secrets.h
- `builder-pi/app.py` — Flask web GUI (port 8080), device detection, registration trigger, progress logs
- `builder-pi/device_manager.py` — USB detection, esptool serial read, flash, boot verification
- `builder-pi/templates/index.html` — Web UI with settings panel + device cards
- `builder-pi/static/styles.css` — Dark theme UI
- `builder-pi/setup-builder-pi.sh` — One-time Pi setup (installs deps, creates systemd service)
- `builder-pi/config.json` — Default settings (WiFi, endpoints, bucket)
- `firmware/ota_updater/ota_updater.ino` — Production firmware template with OTA download capability

**Design decisions:**
- CodeBuild for compilation (not on Pi — too slow)
- Per-device unique X.509 certificates (not fleet-shared)
- wolfSSL for OTA HTTPS download (PQC-ready for future S3 PQC endpoints)
- Single OTA partition (not dual-boot — simpler for production)
- Separate Dashboard v2 (not merged with existing workshop dashboard)
- Git source is configurable (GitLab by default, can switch later)
- Builder-pi uses SSM (same pattern as broker-pi, no long-term credentials)

**NOT yet built:**
- Dashboard v2 frontend (folder structure ready, needs React app)
- Full wolfSSL HTTPS OTA download implementation (placeholder uses esp_https_ota)
- Actual deployment / testing

**Next steps when back at office:**
1. Deploy CloudFormation: `aws cloudformation deploy --template-file raw_device_builder/cloudformation/device-builder-stack.yaml --stack-name pqc-workshop-device-builder --capabilities CAPABILITY_NAMED_IAM`
2. Get API Gateway URL from stack outputs
3. Set up the second Pi with SSM agent (follow broker-pi pattern)
4. Copy `builder-pi/` to second Pi, run `setup-builder-pi.sh`
5. Update `config.json` with API Gateway URL
6. Connect ESP32 via USB and test the full pipeline
7. Build Dashboard v2 for OTA firmware management

---

## Session Credentials Tip

Kiro's `execute_bash` tool runs in a separate process from the terminal. To share AWS credentials:
```bash
aws configure set aws_access_key_id $AWS_ACCESS_KEY_ID
aws configure set aws_secret_access_key $AWS_SECRET_ACCESS_KEY
aws configure set aws_session_token $AWS_SESSION_TOKEN
aws configure set region us-east-1
```
This writes to `~/.aws/credentials` which all processes can read.
