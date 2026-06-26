# IAM Policies

Externalized IAM policy documents for the PQC workshop.

These JSON files are referenced by CloudFormation templates and uploaded to S3 during the packaging step. They are kept as separate files so policies can be reviewed, audited, and modified independently of the infrastructure templates.

| Policy | Used By | Purpose |
|--------|---------|---------|
| `auto-register-policy.json` | Auto-Register Lambda Role | IoT Thing create/describe/group |
| `cert-provisioner-policy.json` | Cert Provisioner Lambda Role | IoT cert + Secrets Manager |
| `command-publisher-policy.json` | Command Publisher Lambda Role | IoT Publish to cmd topics |
| `device-iot-policy.json` | ESP32 devices (via bridge) | Connect, publish, subscribe per device |
| `greengrass-bridge-iot-policy.json` | Greengrass core bridge | Bridge all dt/cmd topics |
| `greengrass-token-exchange-policy.json` | Greengrass Token Exchange Role | Logs + S3 + IoT |
| `telemetry-processor-policy.json` | Telemetry Processor Lambda Role | DynamoDB + AppSync |

## Note on Resource ARNs

These policies use wildcard ARNs (`*`) for portability across accounts and regions. The CloudFormation templates scope them further using `Fn::Sub` when creating the actual IAM roles. The JSON files here represent the *permission shape*; the actual deployed policies may be more restrictive.
