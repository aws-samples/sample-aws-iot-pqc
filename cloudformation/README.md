# CloudFormation: AWS IoT Core Setup

This CloudFormation template provisions all the AWS IoT Core infrastructure needed to work with the ESP32 PQC device sketch.

## What It Creates

| Resource | Type | Purpose |
|----------|------|---------|
| IoT Thing | `AWS::IoT::Thing` | Represents the ESP32 device in AWS IoT |
| IoT Thing Type | `AWS::IoT::ThingType` | Groups PQC-capable ESP32 devices |
| IoT Certificate | `AWS::IoT::Certificate` | X.509 certificate for mTLS authentication |
| IoT Policy | `AWS::IoT::Policy` | Least-privilege permissions for the device |
| Policy Attachment | `AWS::IoT::PolicyPrincipalAttachment` | Links policy to certificate |
| Thing Attachment | `AWS::IoT::ThingPrincipalAttachment` | Links certificate to Thing |
| Telemetry Log Group | `AWS::Logs::LogGroup` | Stores telemetry messages |
| Lifecycle Log Group | `AWS::Logs::LogGroup` | Stores online/offline events |
| IoT Rule (Telemetry) | `AWS::IoT::TopicRule` | Routes telemetry to CloudWatch |
| IoT Rule (Lifecycle) | `AWS::IoT::TopicRule` | Routes lifecycle events to CloudWatch |
| IAM Role | `AWS::IAM::Role` | Allows IoT Rules to write to CloudWatch |

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

### Using a Custom Thing Name

```bash
aws cloudformation deploy \
  --template-file iot-core-setup.yaml \
  --stack-name esp32-pqc-iot \
  --parameter-overrides ThingName=my-custom-device-name \
  --capabilities CAPABILITY_NAMED_IAM
```

### Disable IoT Rules (Minimal Setup)

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

## After Deployment

### 1. Get the IoT Endpoint

```bash
aws iot describe-endpoint --endpoint-type iot:Data-ATS --query 'endpointAddress' --output text
```

Copy this value into `AWS_IOT_ENDPOINT` in your device's `secrets.h`.

### 2. Download the Certificate and Private Key

The CloudFormation stack creates a certificate, but the private key is only available at creation time. Retrieve it using the certificate ID from the stack outputs:

```bash
# Get the certificate ID
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
echo ""
echo "IMPORTANT: The private key was generated at stack creation time."
echo "If you did not capture it, you must delete this certificate and"
echo "create a new one manually in the IoT Console."
```

**Note:** CloudFormation's `AWS::IoT::Certificate` resource generates the private key internally. To retrieve it, you have two options:

1. **Use the IoT Console** — Go to Security > Certificates, find the certificate, and download the key pair (only available immediately after creation).
2. **Create manually** — If you missed the key, delete the stack's certificate and create one manually:
   ```bash
   aws iot create-keys-and-certificate \
     --set-as-active \
     --certificate-pem-outfile device-cert.pem \
     --private-key-outfile device-private-key.pem \
     --public-key-outfile device-public-key.pem
   ```
   Then attach the new certificate to the Thing and policy manually.

### 3. Download Amazon Root CA 1

```bash
curl -o AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
```

### 4. Update secrets.h

Paste the contents of:
- `AmazonRootCA1.pem` → `AWS_CERT_CA`
- `device-certificate.pem.crt` → `AWS_CERT_CRT`
- `device-private-key.pem` → `AWS_CERT_PRIVATE`

And set:
- `AWS_IOT_ENDPOINT` → the endpoint from step 1
- `THINGNAME` → the Thing name you used (default: `my-esp32-pqc-thing`)

### 5. Test the Connection

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
| `ThingArn` | Full ARN of the Thing |
| `IoTEndpoint` | Approximate IoT endpoint (use CLI for exact value) |
| `CertificateArn` | ARN of the created certificate |
| `CertificateId` | ID for CLI certificate operations |
| `PolicyName` | Name of the attached IoT policy |
| `TelemetryTopic` | Topic where device publishes telemetry |
| `LifecycleTopic` | Topic for online/offline lifecycle events |
| `CommandTopic` | Topic for sending commands to the device |
| `TelemetryLogGroup` | CloudWatch Log Group for telemetry |
| `LifecycleLogGroup` | CloudWatch Log Group for lifecycle |

## Clean Up

```bash
aws cloudformation delete-stack --stack-name esp32-pqc-iot
```

This removes all created resources. The certificate will be deactivated and deleted, disconnecting the device.

## IoT Policy Details

The policy follows least-privilege principles:

| Action | Resource | Purpose |
|--------|----------|---------|
| `iot:Connect` | `client/<thingName>` | MQTT connect with Thing name as client ID |
| `iot:Publish` | `topic/dt/<thingName>/telemetry` | Publish sensor data |
| `iot:Publish` | `topic/dt/<thingName>/lifecycle` | Publish online status |
| `iot:RetainPublish` | `topic/dt/<thingName>/lifecycle` | Retained lifecycle messages (LWT) |
| `iot:Subscribe` | `topicfilter/cmd/<thingName>/+` | Subscribe to command topics |
| `iot:Receive` | `topic/cmd/<thingName>/*` | Receive command messages |
