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
                                        |    - dt/+/telemetry → Lambda → DynamoDB  |
                                        |    - dt/+/lifecycle → Lambda → DynamoDB  |
                                        |                                          |
                                        |  Lambda (command-publisher)               |
                                        |    - Publishes cmd/<thing>/... to IoT Core|
                                        |                                          |
                                        |  Cognito Identity Pool                   |
                                        |    - Unauthenticated access for dashboard |
                                        +------------------------------------------+
```

## Data Flow

1. **ESP32 → broker → IoT Core → IoT Rule → Lambda → DynamoDB**
   Device telemetry reaches AWS IoT Core via the ECS Fargate Mosquitto broker's
   MQTT bridge, and the `telemetry-processor` Lambda stores it in DynamoDB.

2. **DynamoDB → AppSync subscription → Browser**
   When the device record is updated, the Lambda triggers an AppSync mutation
   which pushes the update to all connected browsers over the WebSocket
   subscription.

3. **Browser → AppSync `migrateFirmware` mutation → Step Function → OTA → ESP32**
   When an attendee clicks **Flash** / **Migrate** in the dashboard, the
   `migrateFirmware` AppSync mutation starts the `pqc-workshop-pqc-migration`
   Step Function (in the device-builder stack) with the chosen `variant`. It
   builds the firmware in CodeBuild, then publishes an OTA command
   (`{"action":"ota-update","url":...}`) to `cmd/<thingName>/ota` on IoT Core,
   which the broker bridges down to the device. LED/on-off control commands go
   through the `command-publisher` Lambda to the same `cmd/<thing>/...` topics.

## Folder Contents

| File/Folder | Description |
|-------------|-------------|
| `infrastructure/` | CloudFormation template for the dashboard backend (AppSync, DynamoDB, Cognito, S3/CloudFront, IoT Rules) |
| `lambda/` | Backend Lambdas: `telemetry-processor`, `message-logger`, `broker-log-reader`, `command-publisher` |

> **The dashboard frontend is not in this directory.** The current v2 "PQC
> Firmware Manager" frontend (Vite/React) lives in
> [`raw_device_builder/dashboard-v2/frontend/`](../raw_device_builder/dashboard-v2/frontend/).
> This `dashboard/` directory provides the backend infrastructure and Lambdas it
> talks to.

## Deployment

In the workshop, this backend is deployed as the `DashboardStack` nested under
the master template, and the v2 frontend
(`raw_device_builder/dashboard-v2/frontend/`) is built and synced to the
dashboard S3 bucket by `cloudformation/deploy-workshop.sh`. See
[`cloudformation/DEPLOYMENT.md`](../cloudformation/DEPLOYMENT.md) for the full
flow.

To deploy just this backend stack standalone:

```bash
cd infrastructure
aws cloudformation deploy \
  --template-file dashboard-stack.yaml \
  --stack-name pqc-workshop-dashboard \
  --capabilities CAPABILITY_NAMED_IAM
```

Then build the v2 frontend and sync it to the dashboard bucket:

```bash
cd ../raw_device_builder/dashboard-v2/frontend
npm install
npm run build
aws s3 sync build/ "s3://$(aws cloudformation describe-stacks \
  --stack-name pqc-workshop-dashboard \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardBucketName`].OutputValue' \
  --output text)"
```

Get the dashboard URL:

```bash
aws cloudformation describe-stacks \
  --stack-name pqc-workshop-dashboard \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardURL`].OutputValue' \
  --output text
```
