# IAM Policies

Reference copies of the key IAM policy documents used in the PQC workshop.

These JSON files are **reference extracts** — they capture the *permission shape*
of the Lambda/role policies so they can be reviewed and audited independently.
The policies that are actually deployed are defined **inline in the
CloudFormation templates** (e.g. `broker-container/cloudformation/broker-container-stack.yaml`,
`raw_device_builder/cloudformation/device-builder-stack.yaml`,
`dashboard/infrastructure/dashboard-stack.yaml`); those are the source of truth.

| Policy | Role | Purpose |
|--------|------|---------|
| `auto-register-policy.json` | Auto-Register Lambda | IoT Thing create/describe/group |
| `cert-provisioner-policy.json` | Cert Provisioner Lambda | IoT cert + Secrets Manager |
| `command-publisher-policy.json` | Command Publisher Lambda | IoT Publish to `cmd/` topics |
| `device-iot-policy.json` | ESP32 devices (via broker bridge) | Connect, publish, subscribe per device |
| `telemetry-processor-policy.json` | Telemetry Processor Lambda | DynamoDB + AppSync |

## Note on Resource ARNs

These reference copies use wildcard ARNs (`*`) for portability. The deployed
policies in the CloudFormation templates scope them further with `Fn::Sub`. The
JSON files here represent the *permission shape*; the deployed policies may be
more restrictive.
