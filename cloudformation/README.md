# CloudFormation: AWS IoT Core Setup (standalone single-device template)

> **This is not the workshop deployment.** This page documents
> `iot-core-setup.yaml`, a small **standalone** template that registers **one**
> device straight against **AWS IoT Core** (no PQC broker, no NLB, no Private
> CA) — useful for a quick single-device connectivity test or a home-lab
> direct-to-IoT-Core connection. The full workshop deploys the PQC broker +
> device-builder + dashboard via the master template; see
> [`DEPLOYMENT.md`](DEPLOYMENT.md) for that. In the workshop data path a device
> connects to the Mosquitto broker (which bridges to IoT Core), not to IoT Core
> directly.

This CloudFormation template provisions the AWS IoT Core infrastructure needed to connect a single ESP32 device directly to AWS IoT Core.

## What it creates

| Resource | Type | Purpose |
|----------|------|---------|
| IoT Thing | `AWS::IoT::Thing` | Represents the ESP32 device in AWS IoT |
| IoT Thing Type | `AWS::IoT::ThingType` | Groups PQC-capable ESP32 devices |
| Thing Type Association | `Custom::ThingTypeAssociation` | Associates Thing with Thing Type (via Lambda) |
| IoT Certificate | `Custom::IoTCertificate` | X.509 certificate for mTLS (created via Lambda, stored in Secrets Manager) |
| IoT Policy | `AWS::IoT::Policy` | Least-privilege permissions for the device |
| Policy Attachment | `AWS::IoT::PolicyPrincipalAttachment` | Links policy to certificate |
| Thing Attachment | `AWS::IoT::ThingPrincipalAttachment` | Links certificate to Thing |
| Telemetry Log Group | `AWS::Logs::LogGroup` | Stores telemetry messages |
| Lifecycle Log Group | `AWS::Logs::LogGroup` | Stores online/offline events |
| IoT Rule (Telemetry) | `AWS::IoT::TopicRule` | Routes telemetry to CloudWatch |
| IoT Rule (Lifecycle) | `AWS::IoT::TopicRule` | Routes lifecycle events to CloudWatch |
| IAM Role (Rules) | `AWS::IAM::Role` | Allows IoT Rules to write to CloudWatch |
| IAM Role (Lambda) | `AWS::IAM::Role` | Allows Lambdas to call IoT and Secrets Manager APIs |
| Lambda (Thing Type) | `AWS::Lambda::Function` | Associates Thing with Thing Type |
| Lambda (Certificate) | `AWS::Lambda::Function` | Creates certificate, stores key in Secrets Manager |
| Secrets Manager Secret | Created by Lambda | Stores device certificate PEM and private key |

**Note:** `AWS::IoT::Thing` does not natively support the `ThingTypeName` property, and `AWS::IoT::Certificate` cannot auto-generate key pairs in CloudFormation. Custom Resources (small inline Lambdas) handle both: one associates the Thing with its Type, and another creates the certificate and stores the private key in Secrets Manager for easy retrieval after deployment.

## Prerequisites

- AWS CLI installed and configured with appropriate permissions
- An AWS account with IoT Core access

## Deploy

### Using AWS CLI

```bash
aws cloudformation deploy \
  --template-file iot-core-setup.yaml \
  --stack-name esp32-pqc-iot \
  --parameter-overrides ThingName=my-esp32-pqc-thing \
  --capabilities CAPABILITY_NAMED_IAM
```

### Using a custom thing name

```bash
aws cloudformation deploy \
  --template-file iot-core-setup.yaml \
  --stack-name esp32-pqc-iot \
  --parameter-overrides ThingName=my-custom-device-name \
  --capabilities CAPABILITY_NAMED_IAM
```

### Disable IoT rules (minimal setup)

If you only need the Thing, certificate, and policy without CloudWatch routing:

```bash
aws cloudformation deploy \
  --template-file iot-core-setup.yaml \
  --stack-name esp32-pqc-iot \
  --parameter-overrides \
    ThingName=my-esp32-pqc-thing \
    EnableTelemetryRule=false \
    EnableLifecycleRule=false \
  --capabilities CAPABILITY_NAMED_IAM
```

## After deployment

### 1. Get the IoT endpoint

```bash
aws iot describe-endpoint --endpoint-type iot:Data-ATS --query 'endpointAddress' --output text
```

Copy this value into `AWS_IOT_ENDPOINT` in your device's `secrets.h`.

### 2. Retrieve the device certificate and private key

The stack automatically creates a certificate and stores both the certificate PEM and private key in AWS Secrets Manager. Retrieve them with:

```bash
# Get the secret name from stack outputs
SECRET_NAME=$(aws cloudformation describe-stacks \
  --stack-name esp32-pqc-iot \
  --query 'Stacks[0].Outputs[?OutputKey==`DeviceCredentialsSecret`].OutputValue' \
  --output text)

# Retrieve the credentials
aws secretsmanager get-secret-value \
  --secret-id $SECRET_NAME \
  --query 'SecretString' \
  --output text | python3 -c "
import json, sys
creds = json.load(sys.stdin)
with open('device-certificate.pem.crt', 'w') as f:
    f.write(creds['certificatePem'])
with open('device-private-key.pem', 'w') as f:
    f.write(creds['privateKey'])
print('Files written:')
print('  device-certificate.pem.crt')
print('  device-private-key.pem')
"
```

Or retrieve them individually:

```bash
# Just the certificate
aws secretsmanager get-secret-value --secret-id $SECRET_NAME \
  --query 'SecretString' --output text | python3 -c "
import json,sys; print(json.load(sys.stdin)['certificatePem'])" > device-certificate.pem.crt

# Just the private key
aws secretsmanager get-secret-value --secret-id $SECRET_NAME \
  --query 'SecretString' --output text | python3 -c "
import json,sys; print(json.load(sys.stdin)['privateKey'])" > device-private-key.pem
```

### 3. Download Amazon Root CA 1

The Root CA is also stored in SSM Parameter Store automatically. Retrieve it with:

```bash
# From SSM Parameter Store (created by the stack)
aws ssm get-parameter \
  --name /iot/my-esp32-pqc-thing/root-ca-pem \
  --query 'Parameter.Value' \
  --output text > AmazonRootCA1.pem
```

Or download it directly:

```bash
curl -o AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
```

You can also retrieve just the device certificate PEM from SSM (without the private key):

```bash
aws ssm get-parameter \
  --name /iot/my-esp32-pqc-thing/certificate-pem \
  --query 'Parameter.Value' \
  --output text > device-certificate.pem.crt
```

### 4. Update secrets.h

Paste the contents of:
- `AmazonRootCA1.pem` → `AWS_CERT_CA`
- `device-certificate.pem.crt` → `AWS_CERT_CRT`
- `device-private-key.pem` → `AWS_CERT_PRIVATE`

And set:
- `AWS_IOT_ENDPOINT` → the endpoint from step 1
- `THINGNAME` → the Thing name you used (default: `my-esp32-pqc-thing`)

### 5. Test the connection

After uploading the sketch to your ESP32, verify messages in CloudWatch:

```bash
# View telemetry logs
aws logs tail /aws/iot/my-esp32-pqc-thing/telemetry --follow

# View lifecycle events
aws logs tail /aws/iot/my-esp32-pqc-thing/lifecycle --follow
```

Or use the MQTT test client in the IoT Console:
- Subscribe to `dt/my-esp32-pqc-thing/#` to see all device messages
- Publish to `cmd/my-esp32-pqc-thing/request` to send a command

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ThingName` | `my-esp32-pqc-thing` | IoT Thing name (must match device `secrets.h`) |
| `ThingTypeName` | `esp32-pqc-device` | Thing Type for grouping devices |
| `EnableTelemetryRule` | `true` | Create IoT Rule routing telemetry to CloudWatch |
| `EnableLifecycleRule` | `true` | Create IoT Rule routing lifecycle to CloudWatch |
| `TelemetryLogGroupRetentionDays` | `14` | CloudWatch log retention period |

## Outputs

| Output | Description |
|--------|-------------|
| `ThingName` | The created Thing name |
| `ThingTypeName` | The Thing Type name for device grouping |
| `ThingArn` | Full ARN of the Thing |
| `IoTEndpointCommand` | The `aws iot describe-endpoint` command that prints your account's real IoT Data-ATS endpoint |
| `CertificateArn` | ARN of the created certificate |
| `CertificateId` | ID for CLI certificate operations |
| `DeviceCredentialsSecret` | Secrets Manager secret containing cert + private key |
| `RootCaParameter` | SSM Parameter Store name for Amazon Root CA 1 PEM |
| `CertificatePemParameter` | SSM Parameter Store name for device certificate PEM |
| `PolicyName` | Name of the attached IoT policy |
| `TelemetryTopic` | Topic where device publishes telemetry |
| `LifecycleTopic` | Topic for online/offline lifecycle events |
| `CommandTopic` | Topic for sending commands to the device |
| `TelemetryLogGroup` | CloudWatch Log Group for telemetry |
| `LifecycleLogGroup` | CloudWatch Log Group for lifecycle |

## Clean up

```bash
aws cloudformation delete-stack --stack-name esp32-pqc-iot
```

This removes all created resources. The certificate will be deactivated and deleted, disconnecting the device.

## IoT policy details

The policy follows least-privilege principles:

| Action | Resource | Purpose |
|--------|----------|---------|
| `iot:Connect` | `client/<thingName>` | MQTT connect with Thing name as client ID |
| `iot:Publish` | `topic/dt/<thingName>/telemetry` | Publish sensor data |
| `iot:Publish` | `topic/dt/<thingName>/lifecycle` | Publish online status |
| `iot:RetainPublish` | `topic/dt/<thingName>/lifecycle` | Retained lifecycle messages (LWT) |
| `iot:Subscribe` | `topicfilter/cmd/<thingName>/+` | Subscribe to command topics |
| `iot:Receive` | `topic/cmd/<thingName>/*` | Receive command messages |
