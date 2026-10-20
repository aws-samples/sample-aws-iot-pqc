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
- **NLB**: TCP passthrough on ports 8883 (classical cert) and 8884 (ML-DSA-44 cert) — no TLS termination, so the PQC handshake runs end to end
- **ECS Fargate**: Single task, auto-restart on failure
- **Private CAs**: Two — a classical CA (ECDSA P-256 certs) and an ML-DSA-44 CA
  (FIPS 204 PQC signatures) — issuing the broker's and devices' certificates
- **Secrets Manager**: Stores broker private keys and the IoT Core bridge credentials
- **CloudWatch**: Broker logs

## Certificate Strategy

This follows the NIST-recommended staged migration path, and **both stages are
deployed in this workshop**:

1. ✅ **PQC key exchange (ML-KEM)** — the classical CA issues ECDSA P-256
   certificates, and quantum resistance comes from the TLS 1.3 hybrid ML-KEM-768
   key exchange on the **8883** listener. This closes "harvest now, decrypt
   later" today.
2. ✅ **PQC signatures (ML-DSA-44)** — a second Private CA issues ML-DSA-44
   (FIPS 204) certificates, presented on a parallel **8884** listener. Devices
   migrated to the `pqc-mldsa` firmware variant connect here and present an
   ML-DSA-44 certificate, so the certificate signature is post-quantum too.

Both listeners run in parallel under the same broker/NLB, so devices migrate
from 8883 (classical cert) to 8884 (ML-DSA-44 cert) gradually over OTA.

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
│   ├── entrypoint.sh           # Fetches certs from Secrets Manager + writes the
│   │                           #   IoT Core bridge config at runtime
│   ├── mosquitto.conf          # Broker config (8883 + 8884 listeners)
│   └── buildspec.yml           # CodeBuild spec for the image build
├── scripts/
│   └── build-and-push.sh       # Build Docker image and push to ECR
└── cloudformation/
    └── broker-container-stack.yaml  # Full infrastructure
```
