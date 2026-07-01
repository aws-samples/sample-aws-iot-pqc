# Lambda Functions

Extracted Lambda function code for the PQC workshop CloudFormation stacks.

Each subdirectory is a self-contained Lambda deployment package.

| Function | Trigger | Purpose |
|----------|---------|---------|
| `auto-register/` | IoT Rule (lifecycle events) | Auto-registers new devices on first connection |
| `cert-provisioner/` | CloudFormation Custom Resource | Creates IoT certificates, stores in Secrets Manager |
| `command-publisher/` | AppSync Mutation | Publishes migrate-pqc command to IoT Core |
| `telemetry-processor/` | IoT Rule (telemetry + lifecycle) | Stores device state in DynamoDB |

## Packaging

Lambda code is packaged into .zip files by `cloudformation/package.sh` and uploaded to S3 before CloudFormation deployment. The CloudFormation templates reference these S3 locations using the `Code.S3Bucket` and `Code.S3Key` properties.

## Local Testing

```bash
# Test telemetry processor locally
cd telemetry-processor
python -c "
import index
event = {'device': 'esp32-AABBCC', 'message_id': 1, 'pqc_algorithm': 'none'}
index.handler(event, None)
"
```
