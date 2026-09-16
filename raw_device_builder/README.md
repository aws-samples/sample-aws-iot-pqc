# Raw Device Builder — Production Device Registration Pipeline

## Overview

This module simulates a real-world IoT production line scenario. A "builder-pi" (Raspberry Pi) is connected to raw ESP32 devices via USB. An operator uses a local web GUI to register each device with AWS IoT Core, compile device-specific firmware, and flash it over the serial connection (Firmware 0). Subsequent firmware updates (Firmware 0+) are delivered over-the-air via MQTT using presigned S3 URLs and the device's wolfSSL PQC stack.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Builder-Pi (Raspberry Pi + SSM Agent)                               │
│                                                                      │
│  ┌────────────────────────────────┐    USB (up to 4 ports)          │
│  │  Web GUI (Flask, port 8080)    │◄──────────────────────┐         │
│  │  - Device detection            │                        │         │
│  │  - Registration trigger        │    ┌─────┐ ┌─────┐   │         │
│  │  - Flash progress              │    │ESP32│ │ESP32│ ...│         │
│  │  - Boot verification           │    └─────┘ └─────┘   │         │
│  └────────────┬───────────────────┘                        │         │
│               │ API calls                                  │ esptool │
│               ▼                                            │         │
│  ┌────────────────────────┐   S3 download                  │         │
│  │  device_manager.py     │────────────────────────────────┘         │
│  └────────────────────────┘                                          │
└───────────────────────┬──────────────────────────────────────────────┘
                        │ HTTPS (via SSM / direct)
                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  AWS Cloud                                                           │
│                                                                      │
│  API Gateway ──► Lambda (device-registrar)                           │
│                    ├── IoT Core: Create Thing + Certificate + Policy  │
│                    ├── DynamoDB: Store device metadata                │
│                    └── CodeBuild: Trigger firmware compilation        │
│                                                                      │
│  CodeBuild ──► Pulls source from Git repo                            │
│                Generates secrets.h with device cert/key              │
│                Compiles via arduino-cli                               │
│                Uploads binary to S3                                   │
│                                                                      │
│  S3 (firmware bucket)                                                │
│    └── <serial>/<version>/firmware.bin                                │
│                                                                      │
│  Dashboard v2 (CloudFront + AppSync)                                 │
│    ├── List registered devices                                       │
│    ├── List firmware versions per device                             │
│    └── Push OTA update (presigned URL via cmd/<thing>/ota)           │
└──────────────────────────────────────────────────────────────────────┘
```

## Firmware Lifecycle

| Phase | Mechanism | Description |
|-------|-----------|-------------|
| Firmware 0 | USB serial (esptool) | Initial flash on production line via builder-pi |
| Firmware 0+ | OTA via MQTT + S3 | Device downloads from presigned S3 URL using wolfSSL |

## Prerequisites

1. **Builder-Pi** — Raspberry Pi with:
   - SSM Agent registered via the bundle (`bundle-for-builder-pi.sh` →
     `setup-ssm.sh`); the toolchain (Python/Flask/esptool/arduino-cli), GUI app,
     and systemd service are then installed remotely by the SSM documents the
     orchestrator runs (`setup-builder-pi-remote.sh`)
   - USB ports available for ESP32 connection
   - Network access to AWS (for API calls and S3 downloads)

2. **AWS Account** with permissions to deploy CloudFormation stack

3. **Git repository** containing firmware source (this GitLab repo by default)

## Setup

### 1. Deploy AWS Infrastructure

```bash
aws cloudformation deploy \
  --template-file cloudformation/device-builder-stack.yaml \
  --stack-name pqc-workshop-device-builder \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    GitRepoUrl=<your-gitlab-repo-url> \
    FirmwareBucketName=pqc-workshop-firmware-087195661091
```

### 2. Setup Builder-Pi

Two steps (see `cloudformation/DEPLOYMENT.md` §5 for detail):

```bash
# On the deploy host: generate the SSM-bootstrap bundle
raw_device_builder/bundle-for-builder-pi.sh --region us-east-1

# On the Pi: register the SSM agent
unzip builder-pi-bundle.zip && cd builder-pi-bundle && sudo ./setup-ssm.sh

# Back on the deploy host: provision the Pi via SSM documents (installs the
# toolchain + GUI app; the GUI auto-starts)
./raw_device_builder/setup-builder-pi-remote.sh --region us-east-1
```

### 3. Access the GUI

```
http://<builder-pi-ip>:8080
```

## Directory Structure

```
raw_device_builder/
├── README.md                          # This file
├── cloudformation/
│   └── device-builder-stack.yaml      # All AWS infra (API GW, Lambda, CodeBuild, S3, DDB)
├── lambda/
│   ├── device-registrar/index.py      # Register device in IoT Core, trigger build
│   └── firmware-pusher/index.py       # Generate presigned URL for OTA push
├── codebuild/
│   ├── buildspec.yml                  # Build spec for firmware compilation
│   └── Dockerfile                     # arduino-cli + ESP32 toolchain image
├── bundle-for-builder-pi.sh           # Deploy host: mint SSM bundle (bootstrap only)
├── setup-builder-pi-remote.sh         # Deploy host: run the 3 SSM provisioning docs
├── builder-pi/
│   ├── app.py                         # Flask web GUI (deployed via SSM deploy-app doc)
│   ├── templates/index.html           # Web UI template
│   ├── static/styles.css              # UI styles
│   ├── device_manager.py              # USB detection, serial read, flash
│   └── config.json                    # Defaults (WiFi, endpoints, etc.)
├── dashboard-v2/
│   ├── frontend/src/                  # React dashboard for OTA management
│   └── infrastructure/
│       └── dashboard-v2-stack.yaml    # Dashboard v2 CloudFormation
└── firmware/
    └── ota_updater/                   # OTA-capable firmware (single partition)
        └── ota_updater.ino            # Firmware with S3 OTA download via wolfSSL
```

## Security Notes

- Each device gets a unique X.509 certificate (not fleet-shared)
- IoT policies are scoped per-device (connect/pub/sub only their topics)
- S3 presigned URLs expire after 5 minutes
- OTA uses wolfSSL stack (PQC-ready for when S3 enables PQC endpoints)
- Builder-pi never stores long-term AWS credentials (SSM managed)
