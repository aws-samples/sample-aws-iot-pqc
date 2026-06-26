# PQC Workshop Dashboard

Real-time web dashboard for the ESP32 PQC migration workshop. Displays device telemetry, connection status, and provides a one-click firmware migration button.

## Architecture

```
Browser (Dashboard)                     AWS Cloud
+-------------------------+             +------------------------------------------+
|                         |  WebSocket  |                                          |
|  React SPA             |<----------->|  AppSync (GraphQL + Real-time Subs)      |
|    - Device grid        |             |    - Queries: list devices, get device   |
|    - Live telemetry     |             |    - Mutations: migrate firmware         |
|    - Migrate button     |             |    - Subscriptions: onDeviceUpdate       |
|    - PQC status badge   |             |                                          |
|                         |             |  DynamoDB (device-telemetry table)        |
+-------------------------+             |    - PK: thingName                        |
                                        |    - Latest telemetry + status            |
        Hosted on:                      |                                          |
        S3 + CloudFront                 |  IoT Rules                               |
        (or Amplify Hosting)            |    - dt/+/telemetry → Lambda → DynamoDB  |
                                        |    - dt/+/lifecycle → Lambda → DynamoDB  |
                                        |                                          |
                                        |  Lambda (command-publisher)               |
                                        |    - Publishes migrate-pqc to IoT Core   |
                                        |                                          |
                                        |  Cognito Identity Pool                   |
                                        |    - Unauthenticated access for dashboard |
                                        +------------------------------------------+
```

## Data Flow

1. **ESP32 → Mosquitto → IoT Core → IoT Rule → Lambda → DynamoDB**
   Device telemetry arrives at IoT Core via the Mosquitto bridge and is stored in DynamoDB.

2. **DynamoDB → AppSync subscription → Browser**
   When DynamoDB is updated, the Lambda triggers an AppSync mutation which pushes the update to all connected browsers via WebSocket.

3. **Browser → AppSync mutation → Lambda → IoT Core → Mosquitto → ESP32**
   When an attendee clicks "Migrate to PQC", AppSync invokes a Lambda that publishes `{"action":"migrate-pqc"}` to `cmd/<thingName>/ota` on IoT Core, which bridges to the device.

## Folder Contents

| File/Folder | Description |
|-------------|-------------|
| `infrastructure/` | CloudFormation template for all AWS resources |
| `frontend/` | React dashboard application |
| `lambda/` | Lambda function code (telemetry processor + command publisher) |

## Deployment

```bash
# 1. Deploy infrastructure
cd infrastructure
aws cloudformation deploy \
  --template-file dashboard-stack.yaml \
  --stack-name pqc-workshop-dashboard \
  --capabilities CAPABILITY_NAMED_IAM

# 2. Build and deploy frontend
cd ../frontend
npm install
npm run build
aws s3 sync build/ s3://$(aws cloudformation describe-stacks \
  --stack-name pqc-workshop-dashboard \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardBucketName`].OutputValue' \
  --output text)

# 3. Access dashboard
aws cloudformation describe-stacks \
  --stack-name pqc-workshop-dashboard \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardURL`].OutputValue' \
  --output text
```
