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
                                        |  Cognito user pool + identity pool       |
                                        |    - Workshop-passcode sign-in; no guest |
                                        +------------------------------------------+
```

## Signing in

The dashboard asks for a **workshop passcode** before it shows anything. The
facilitator puts the passcode on screen; attendees type it in (it is upper-cased
for them) and stay signed in for the day.

- The passcode is the password of a single `workshop` user in the dashboard's
  Cognito user pool, so Cognito checks it. There is no sign-up and no guest
  access: the identity pool hands AWS credentials only to signed-in users, and
  that role may call only the read queries plus `sendCommand` and
  `migrateFirmware` (not `updateDevice`, which only the telemetry processor
  writes).
- `deploy-workshop.sh` generates a fresh random passcode (8 characters, no
  look-alike letters or digits) whenever the user pool is new, so every
  from-scratch deploy gets a new one. A redeploy into the same stack keeps it.
  It is printed at the end of the deploy and stored as an SSM SecureString:

  ```bash
  aws ssm get-parameter --name /pqc-workshop/dashboard-auth/passcode \
    --with-decryption --query Parameter.Value --output text
  ```

- Everyone shares the one user, so Cognito's lockout applies to the room as a
  whole: after five wrong passcodes it locks sign-in briefly (from one second,
  doubling up to about 15 minutes). The sign-in page tells people to wait.
- After signing in, **All devices / My device** filters the view. My device
  shows the card for the 6-character code on the attendee's label. It only
  filters what is shown: anyone signed in can still act on any device.

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

A standalone deploy does not create the passcode user (the workshop deploy
script does). Create it once, with a passcode of at least 8 characters:

```bash
POOL=$(aws cloudformation describe-stacks --stack-name pqc-workshop-dashboard \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardUserPoolId`].OutputValue' --output text)
aws cognito-idp admin-create-user --user-pool-id "$POOL" --username workshop --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username workshop \
  --password '<PASSCODE>' --permanent
```

Get the dashboard URL:

```bash
aws cloudformation describe-stacks \
  --stack-name pqc-workshop-dashboard \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardURL`].OutputValue' \
  --output text
```
