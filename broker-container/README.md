# Broker Container — Mosquitto PQC on ECS with NLB

## Overview

A containerized Mosquitto MQTT broker with PQC support (OpenSSL 3.5 + oqs-provider + ML-KEM), running on ECS Fargate behind a Network Load Balancer. Certificates are provisioned automatically by AWS Private CA.

## Architecture

```
ESP32 Devices
    ↓ MQTT over TLS 1.3 + ML-KEM (port 8883)
Network Load Balancer (TCP passthrough)
    ↓
ECS Fargate (single Mosquitto PQC container)
    ├── Broker cert from AWS Private CA
    ├── Device CA trust from Private CA
    ├── Bridge to AWS IoT Core
    └── Logs to CloudWatch
    ↓
AWS IoT Core (via MQTT bridge)
```

## Components

- **Docker image**: Mosquitto 2.0 compiled against OpenSSL 3.5 + oqs-provider
- **NLB**: TCP passthrough on port 8883 (no TLS termination — PQC handshake preserved)
- **ECS Fargate**: Single task, auto-restart on failure
- **Private CA**: Issues broker + device certificates (ECDSA P-256 sigs, PQC key exchange)
- **Secrets Manager**: Stores broker private key
- **EFS**: Shared config and cert storage
- **CloudWatch**: Broker logs

## Certificate Strategy

AWS Private CA issues ECDSA P-256 certificates. PQC protection comes from the TLS 1.3 key exchange (ML-KEM-768), not the certificate signature. This is the NIST-recommended migration path:
1. ✅ PQC key exchange (ML-KEM) — protects data in transit today
2. ⏳ PQC signatures (ML-DSA) — when Private CA supports it

## Deployment

```bash
aws cloudformation deploy \
  --template-file cloudformation/broker-container-stack.yaml \
  --stack-name pqc-workshop-broker-container \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

After deployment:
1. The Private CA and broker cert are created automatically
2. The Docker image is built and pushed to ECR
3. The ECS service starts with the NLB endpoint
4. Devices connect to `<nlb-dns>:8883`

## Files

```
broker-container/
├── README.md
├── docker/
│   ├── Dockerfile              # Mosquitto + OpenSSL 3.5 + oqs-provider
│   ├── entrypoint.sh           # Fetches certs from Secrets Manager on start
│   └── mosquitto.conf          # Base broker config
├── config/
│   └── bridge-iotcore.conf     # IoT Core bridge configuration
├── scripts/
│   └── build-and-push.sh       # Build Docker image and push to ECR
└── cloudformation/
    └── broker-container-stack.yaml  # Full infrastructure
```
