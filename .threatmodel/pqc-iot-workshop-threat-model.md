# Comprehensive Threat Model Report

**Generated**: 2026-10-28 07:47:49
**Current Phase**: 1 - Business Context Analysis
**Overall Completion**: 100.0%

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Business Context](#business-context)
3. [Classification Profiles](#classification-profiles)
4. [System Architecture](#system-architecture)
5. [Threat Actors](#threat-actors)
6. [Trust Boundaries](#trust-boundaries)
7. [Assets and Flows](#assets-and-flows)
8. [Threats](#threats)
9. [Mitigations](#mitigations)
10. [Assumptions](#assumptions)
11. [Phase Progress](#phase-progress)

## Executive Summary

Post-Quantum Cryptography Migration for IoT workshop (AWS Workshop Studio / re:Invent builders session, also published as the public aws-samples/sample-aws-iot-pqc sample). ESP32-S3 field devices connect over MQTT/TLS 1.3 with a hybrid SecP256r1MLKEM768 key exchange to a containerized Mosquitto PQC broker (ECS Fargate in private subnets behind an internet-facing NLB on 8883 classical / 8884 ML-DSA-44), which bridges over classical mTLS to AWS IoT Core. Two Private CAs (classical ECDSA P-256 and ML-DSA-44) issue broker and device certificates. A cloud pipeline (API Gateway + Lambda registrar, CodeBuild firmware builder container, Step Functions PQC migration, S3 presigned-URL OTA) builds per-device firmware with embedded credentials and migrates devices classical -> PQC key exchange -> PQC certificates over the air. An on-site Raspberry Pi (Builder-Pi, managed via SSM hybrid activation) runs a Flask GUI that registers and USB-flashes devices and hosts the 2.4GHz workshop WiFi AP. A CloudFront + S3 React dashboard backed by AppSync, Cognito, DynamoDB and IoT Rules -> Lambdas shows live telemetry and sends LED/OTA commands. Deployed per event from a single master CloudFormation stack in a throwaway account. In scope: device firmware, broker, NLB, PKI, build/registration/OTA/migration pipeline, Builder-Pi, dashboard and their AWS resources. Out of scope: AWS-managed service internals, the physical venue network beyond the workshop AP, attendee laptops.

### Key Statistics

- **Total Threats**: 23
- **Total Mitigations**: 28
- **Total Assumptions**: 6
- **System Components**: 30
- **Assets**: 10
- **Threat Actors**: 13

## Business Context

**Description**: Post-Quantum Cryptography Migration for IoT workshop (AWS Workshop Studio / re:Invent builders session, also published as the public aws-samples/sample-aws-iot-pqc sample). ESP32-S3 field devices connect over MQTT/TLS 1.3 with a hybrid SecP256r1MLKEM768 key exchange to a containerized Mosquitto PQC broker (ECS Fargate in private subnets behind an internet-facing NLB on 8883 classical / 8884 ML-DSA-44), which bridges over classical mTLS to AWS IoT Core. Two Private CAs (classical ECDSA P-256 and ML-DSA-44) issue broker and device certificates. A cloud pipeline (API Gateway + Lambda registrar, CodeBuild firmware builder container, Step Functions PQC migration, S3 presigned-URL OTA) builds per-device firmware with embedded credentials and migrates devices classical -> PQC key exchange -> PQC certificates over the air. An on-site Raspberry Pi (Builder-Pi, managed via SSM hybrid activation) runs a Flask GUI that registers and USB-flashes devices and hosts the 2.4GHz workshop WiFi AP. A CloudFront + S3 React dashboard backed by AppSync, Cognito, DynamoDB and IoT Rules -> Lambdas shows live telemetry and sends LED/OTA commands. Deployed per event from a single master CloudFormation stack in a throwaway account. In scope: device firmware, broker, NLB, PKI, build/registration/OTA/migration pipeline, Builder-Pi, dashboard and their AWS resources. Out of scope: AWS-managed service internals, the physical venue network beyond the workshop AP, attendee laptops.

### Business Features

- **Industry Sector**: Technology
- **Data Sensitivity**: Confidential
- **User Base Size**: Micro
- **Geographic Scope**: National / Single-Country
- **Regulatory Requirements**: None
- **System Criticality**: Medium
- **Financial Impact**: Low
- **Authentication Requirement**: Basic
- **Deployment Model**: Hybrid cloud
- **User Base Metric**: Concurrent Users
- **Revenue Band**: Enterprise
- **Data Residency**: National / Single-Country
- **Compute Location**: National / Single-Country
- **User Base Location**: National / Single-Country
- **Organizational HQ**: National / Single-Country

## Classification Profiles

### Software Profile

- **Software Type**: Embedded / IoT / Firmware
- **Deployment Model**: Hybrid cloud
- **Architecture Style**: Event-driven
- **Platform / Runtime**: Embedded device
- **User Domain**: Education
- **Licensing / Ownership**: Open source
- **Modern Paradigms**: Cloud-native, Containerized, IoT, Edge computing, Event-driven architecture
- **Description**: ESP32 Arduino firmware (wolfSSL TLS 1.3 + ML-KEM, FreeRTOS WiFi/MQTT tasks, dual-boot OTA) plus an AWS backend: ECS Fargate Mosquitto/OpenSSL 3.5 + oqs-provider broker, NLB, ACM Private CA, Lambda, Step Functions, CodeBuild, API Gateway, AppSync, Cognito, DynamoDB, S3, CloudFront, IoT Core, SSM, Secrets Manager. Raspberry Pi edge host running a Flask GUI and arduino-cli/esptool. MIT-0 licensed sample.

### Data Asset Profiles

| ID | Name | Asset | Category | Content Types | Sensitivity | Compliance | States | Volume | Lifecycle | Business Domain | Description |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DP001 | Device private keys and certificates | DA-DEVICE-CREDS | Secrets and Credentials | Authentication credentials | Restricted | None | At rest, In transit, In use | Small | Active | Engineering | Per-device ECDSA P-256 and ML-DSA-44 key pairs and certificates issued by the shared cert-issuer CodeBuild job, compiled into each device's firmware image, stored in device flash, and used for device-to-broker mTLS. |
| DP002 | Private CA signing keys | DA-CA-KEYS | Secrets and Credentials | Authentication credentials | Restricted | None | At rest, In use | Small | Active | Engineering | Signing keys of the two workshop trust roots (classical ECDSA P-256 Private CA and the ML-DSA-44 CA). Compromise allows minting trusted broker or device identities. |
| DP003 | Broker server and IoT bridge credentials | DA-BROKER-CREDS | Secrets and Credentials | Authentication credentials | Restricted | None | At rest, In use | Small | Active | Engineering | Broker TLS server certificates/keys (classical and ML-DSA) and the AWS IoT Core bridge client certificate/key, stored in Secrets Manager and loaded by the Fargate broker task. |
| DP004 | Workshop WiFi credentials | DA-WIFI | Secrets and Credentials | Authentication credentials | Confidential | None | At rest, In transit, In use | Small | Active | Operations | Workshop AP SSID and PSK. Source of truth is SSM SecureString /pqc-workshop/wifi/password; also compiled into firmware, configured on the Builder-Pi AP, and (known backlog item) still written in plaintext to the pqc-workshop-device-registry DynamoDB table by the registrar. |
| DP005 | Firmware images | DA-FIRMWARE | Unstructured Data | Authentication credentials, Intellectual property / trade secrets | Restricted | None | At rest, In transit | Small | Active | Engineering | Per-device classical / PQC / PQC+ML-DSA firmware binaries in the versioned firmware S3 bucket under <serial>/<version>/, delivered to the Builder-Pi for USB flashing and to devices over OTA via presigned S3 URLs. Each image embeds that device's private key and the WiFi PSK, and integrity determines what code runs on the device. |
| DP006 | Device telemetry and message log | DA-TELEMETRY | Semi-Structured Data | Operational / telemetry data | Internal | None | At rest, In transit | Streaming | Active | Engineering | JSON telemetry on dt/<thing>/telemetry (heap, RSSI, firmware version, negotiated PQC group, TLS version) plus lifecycle events, stored in DynamoDB (pqc-workshop-devices, pqc-workshop-message-log) and pushed to the dashboard. Its integrity is what proves to attendees that a device negotiated ML-KEM. |
| DP007 | Device commands and OTA jobs | DA-COMMANDS | API Data | Operational / telemetry data | Internal | None | In transit | Small | Active | Engineering | Downlink messages on cmd/<thing>/+ (LED control, OTA trigger carrying a presigned firmware URL, revert-to-classical). A forged OTA command can make a device install attacker-chosen firmware. |
| DP008 | Device registry and migration state | DA-REGISTRY | Structured Data | System metadata | Internal | None | At rest | Small | Active | Engineering | DynamoDB device registry (serial, thing name, cert metadata, firmware version, status) and dashboard device rows (migrationStatus), plus IoT Thing / per-device policy records. |
| DP009 | Dashboard user accounts | DA-DASH-USERS | Secrets and Credentials | Authentication credentials, PII | Confidential | None | At rest, In transit | Small | Active | Operations | Cognito user pool identities (email address, password) and issued JWTs used to call the AppSync API that reads telemetry and sends device commands and migrations. |
| DP010 | Infrastructure-as-code and deployment configuration | DA-IAC | Configuration Data | System metadata | Internal | None | At rest | Small | Active | Engineering | CloudFormation templates, buildspecs, Dockerfiles, SSM documents, deploy scripts and the Builder-Pi bundle (contains a 7-day SSM hybrid activation code). Public on GitHub after scrubbing; integrity determines what is deployed. |

### User Personas

| ID | Persona | Name | Privilege | Affiliation | Roles | Intent | Entity Type | Authentication | Threat Actor Overlay | In Scope | Description |
|---|---|---|---|---|---|---|---|---|---|---|---|
| UP001 | System Administrator | Workshop operator | Administrative | Employee | Administrator, Operator | Legitimate | Human | IAM Role | N/A | Yes | Deploys and tears down the master stack with Admin credentials, provisions the Builder-Pi over SSM, and runs readiness checks. |
| UP002 | Authenticated Standard User | Workshop attendee | Low | Customer | End User | Legitimate | Human | Password | N/A | Yes | Plugs in and flashes a device at the Builder-Pi, joins the workshop WiFi, and uses the dashboard (Cognito) to watch telemetry and trigger PQC migrations. Physically present with access to the hardware and the venue network. |
| UP003 | Non-Human Identity / Service Account | ESP32 field device | Low | Customer | Service Account | Legitimate | Non-Human | Certificate | N/A | Yes | Authenticates to the broker with an mTLS client certificate; publishes dt/<thing>/* and subscribes to cmd/<thing>/*; downloads OTA firmware from presigned S3 URLs. |
| UP004 | Non-Human Identity / Service Account | Builder-Pi edge host | Elevated | Employee | Service Account, Operator | Legitimate | Non-Human | IAM Role | N/A | Yes | On-site Raspberry Pi registered as an SSM managed instance (pqc-workshop-builder-pi-ssm role). Calls the builder API to register devices, downloads firmware from S3, flashes over USB, and runs the open Flask GUI on :8080 and the workshop WiFi AP. |
| UP005 | Non-Human Identity / Service Account | Mosquitto broker and IoT bridge | Medium | Employee | Service Account, Integration Endpoint | Legitimate | Non-Human | Certificate | N/A | Yes | Fargate task that terminates device PQC TLS and bridges to AWS IoT Core with its own client certificate (pqc-workshop-broker-bridge Thing). |
| UP006 | Non-Human Identity / Service Account | Cloud workload roles | Elevated | Employee | Service Account | Legitimate | Non-Human | IAM Role | N/A | Yes | Lambda (registrar, deregistrar, prepare-build, publish-ota, start-migration, telemetry-processor, command-publisher, auto-register), CodeBuild (firmware build, image build, cert-issuer) and Step Functions roles that create IoT Things/policies, issue certificates and publish OTA. |
| UP007 | Anonymous / Unauthenticated User | Unauthenticated internet or venue user | None | Public / Unknown | End User | Malicious | Human | None | N/A | Yes | Can reach the internet-facing NLB (8883/8884), the CloudFront dashboard and the builder API endpoint, and anyone in RF range or on the venue LAN can attempt the workshop AP and the Builder-Pi GUI. |
| UP008 | Developer / DevOps User | Sample maintainer | Elevated | Employee | Developer | Legitimate | Human | Multi-factor | N/A | Yes | Commits to the internal GitLab repo and publishes the scrubbed public mirror to GitHub aws-samples and the Workshop Studio source zip. |

### Non-Functional Requirements

- **Resource Utilization**: Constrained — ESP32-S3 RAM/flash budget must fit wolfSSL with ML-KEM and ML-DSA plus dual OTA partitions.
- **Availability**: 99% — Needed only for the duration of a workshop event; not a production service.
- **Recoverability**: Hours — Recovery is a from-scratch redeploy of the master stack (roughly an hour).
- **Installability**: Automated — Must deploy in one attempt from CloudFormation and scripts with minimal documented manual steps.
- **Adaptability**: Multiple environments — Must work in accounts with and without VPC Block Public Access.
- **Interoperability**: Standards-based — MQTT 3.1.1, TLS 1.3, IANA SecP256r1MLKEM768 group, FIPS 203/204 algorithms, X.509.
- **Fault Tolerance**: Partial — Device WiFi/MQTT tasks auto-reconnect; single broker task and NLB.
- **Fail-Safe Behaviour**: Revert to safe state — Dual-boot partitions allow rollback to classical firmware after a failed migration.
- **Capacity**: Small — Tens of devices per workshop room.
- **Safety Criticality**: Non-safety-related — Demo devices drive only an LED.

## System Architecture

### Components

| ID | Name | Type | Service Provider | Description |
|---|---|---|---|---|
| C001 | ESP32 field device firmware | Compute | Other | Three variants: v1 classical (ECDHE + ECDSA cert, port 8883), pqc (SecP256r1MLKEM768 + ECDSA cert, 8883), v3 mldsa (ML-KEM + ML-DSA-44 cert, 8884). mTLS to the broker with WOLFSSL_VERIFY_PEER chain check only (no hostname/SAN check). Publishes dt/<thing>/telemetry and lifecycle, subscribes cmd/<thing>/+. Dual-boot OTA: downloads from the URL in the command over TLS with WOLFSSL_VERIFY_NONE (no server cert check), enforces the sha256 from the same command if present, and verifies an ECDSA-P256 or ML-DSA-44 signature ONLY when the command includes sigAlg; an OTA without sigAlg is installed unsigned in all three variants. |
| C002 | Workshop WiFi access point | Network | On-Premise | 2.4GHz WPA2-PSK AP built by the configure-wifi-ap SSM document from /pqc-workshop/wifi/*. One shared PSK for every device and attendee; the same PSK is compiled into every firmware image. |
| C003 | Builder-Pi edge host and Flask GUI | Compute | On-Premise | Flask GUI bound to 0.0.0.0:8080 with NO authentication or CSRF protection. Routes: /api/register, /api/reflash, /api/deregister, /api/serial/<port>, /api/scan, GET/POST /api/config (POST merges arbitrary JSON into config incl. API endpoint/bucket/region). Calls the builder API with SigV4 using SSM hybrid-activation role credentials, downloads merged firmware from S3, and USB-flashes devices. |
| C004 | Broker Network Load Balancer | Network | AWS | Internet-facing NLB in public subnets. TCP listeners 8883 and 8884 pass TLS through to the broker (no termination). Broker SG allows 0.0.0.0/0 on both ports. VPC BPA exclusion (allow-bidirectional) lets IGW ingress through on BPA accounts. |
| C005 | Mosquitto PQC broker | Container | AWS | Private subnets, no public IP, egress via NAT and VPC endpoints. Listener 8883 trusts the classical CA, 8884 the ML-DSA-44 CA; both require_certificate + use_identity_as_username, TLS 1.3 only. NO acl_file: any client holding any cert from the trusted CA can publish/subscribe to any topic. Loads broker and bridge credentials from Secrets Manager at startup. Logs connect/subscribe/publish events with client IDs and source IPs. |
| C006 | Mosquitto to IoT Core bridge | Messaging | AWS | Bridge client pqc-workshop-broker-bridge connects to IoT Core 8883 with an IoT-issued X.509 cert (TLS 1.2, AmazonRootCA1). Bridges dt/+/telemetry out and cmd/# in; lifecycle is NOT bridged. Its IoT policy allows publish dt/* and shadow, subscribe/receive cmd/* and shadow wildcards: one identity speaks for every device. |
| C007 | AWS IoT Core and rules | Messaging | AWS | Rules: pqc_workshop_telemetry (dt/+/telemetry) and pqc_workshop_lifecycle (dt/+/lifecycle) -> telemetry-processor; pqc_workshop_message_log_dt (dt/+/+) and _cmd (cmd/+/+) -> message-logger; pqc_workshop_auto_register (lifecycle online) -> auto-register. Also hosts the IoT Things, esp32-pqc-device ThingType and per-device policies (devices do not connect to IoT Core directly). |
| C008 | Telemetry processor Lambda | Serverless | AWS | Updates pqc-workshop-devices only when the row already has serialNumber (registered-only guard), reconciles migrationStatus, and calls AppSync. Resource policy allows any IoT rule in the account (rule/*). |
| C009 | Message logger Lambda | Serverless | AWS | Writes dt/+/+ and cmd/+/+ payloads to pqc-workshop-message-log (TTL) for the dashboard log view. cmd payloads include OTA commands with presigned URLs. |
| C010 | Auto-register Lambda | Serverless | AWS | Creates IoT Things on a lifecycle 'online' event. Effectively dormant: lifecycle is not bridged from Mosquitto to IoT Core. |
| C011 | Builder API | Network | AWS | Public edge endpoint. POST /register, /deregister, /firmware, all AuthorizationType AWS_IAM (SigV4). No resource policy, API key or usage plan. Caller is the Builder-Pi. |
| C012 | Device registrar Lambda | Serverless | AWS | Creates the IoT Thing and per-device policy, writes the registry (including the WiFi password in plaintext), seeds the dashboard device row, and starts the firmware CodeBuild with WIFI_PASSWORD as a plaintext env override. Role: iot:Create*/Attach* on *, iot:Publish cmd/*, SSM /pqc-workshop/* read. |
| C013 | Deregistrar and firmware-pusher Lambdas | Serverless | AWS | Behind /deregister (removes Thing/cert/policy/registry/dashboard row) and /firmware. |
| C014 | Start-migration and prepare-build Lambdas | Serverless | AWS | start-migration (invoked by AppSync migrateFirmware) validates the variant and starts the state machine. prepare-build resolves variant/version/signer era and the per-device build env (including the WiFi password). |
| C015 | PQC migration state machine | Serverless | AWS | SetStatusBuilding -> PrepareBuild -> WaitForBuild (codebuild:startBuild.sync, WIFI_PASSWORD passed as PLAINTEXT env, visible in execution history) -> SetStatusPushing -> PublishOta -> SetStatusRebooting; Catch -> SetStatusFailed. |
| C016 | Publish-OTA Lambda | Serverless | AWS | Presigns firmware.bin (+ .sig and signer.crt for signed variants) for 900 s and publishes {action: ota-update, url, sha256, sigUrl, signerCertUrl} to cmd/<thing>/ota at QoS 1 via IoT Core. |
| C017 | Firmware build CodeBuild | Compute | AWS | Generates the device key + CSR in-container, issues the cert from the variant's ACM PCA, writes key, cert, WiFi SSID/PSK and broker endpoint into secrets.h, compiles with arduino-cli, signs firmware.bin with the era signer key from Secrets Manager, uploads all artifacts to s3://firmware/<serial>/<version>/. WIFI_PASSWORD is also a plaintext project env var. |
| C018 | Broker and signer cert provisioner CodeBuild | Compute | AWS | Mints the classical and ML-DSA broker server certs and the classical and ML-DSA OTA signer identities from the two CAs and stores keys/certs in 12 Secrets Manager secrets. |
| C019 | Bridge provisioner Lambda | Serverless | AWS | Creates the pqc-workshop-broker-bridge Thing, IoT keys/cert and wildcard bridge policy, downloads AmazonRootCA1, stores cert/key/root in Secrets Manager. Role has iot Create/Delete/Attach on *. |
| C020 | Classical Private CA | Security | AWS | Trust root for the 8883 listener: issues the classical broker cert, classical/pqc device certs, and the classical OTA signer. |
| C021 | ML-DSA-44 Private CA | Security | AWS | Trust root for the 8884 listener: issues the ML-DSA broker cert, pqc-mldsa device certs and the ML-DSA OTA signer. Key held by ACM PCA (not self-managed). |
| C022 | Dashboard AppSync API | Other | AWS | Queries listDevices/getDevice (devices table), getDeviceLog (message log), getBrokerLog (broker-log-reader). Mutations updateDevice (SETs arbitrary input fields on the device row), migrateFirmware (start-migration -> full build + OTA). sendCommand and deleteDevice are in the schema with NO resolvers. Subscription onDeviceUpdate. |
| C023 | Cognito identity pool | Security | AWS | No user pool. Anyone with the pool ID (shipped in the public dashboard config.json) gets guest credentials for CognitoUnauthRole, which allows appsync:GraphQL on the whole API. |
| C024 | Dashboard CloudFront distribution | Network | AWS | Public HTTPS (redirect-to-https, default cert), GET/HEAD only, no WAF, no access logging. Serves the Vite/React dashboard and its runtime config.json. |
| C025 | Dashboard web client | Compute | Other | Gets guest credentials from Cognito, SigV4-signs AppSync calls, polls every 5 s, offers the firmware picker / migrate button. |
| C026 | Broker log reader Lambda | Serverless | AWS | Reads /pqc-workshop/broker-container for the getBrokerLog query (client IDs, source IPs, topics). |
| C027 | SSM fleet management for the Builder-Pi | Security | AWS | Hybrid activation (7-day code, in the Builder-Pi bundle and Secrets Manager iot/<instance>/*) registers the Pi. Run Command documents install the toolchain, deploy the app, configure services and the WiFi AP (decrypting the PSK). Two candidate Pi roles exist (<instance>-ssm-role and pqc-workshop-builder-pi-ssm). |
| C028 | Operator deploy workstation | Compute | Other | Holds Admin credentials; deploys the master CloudFormation stack, writes /pqc-workshop/* SSM params, starts image builds, generates the Pi bundle and orchestrates SSM provisioning. |
| C029 | Image and dashboard build CodeBuild projects | Compute | AWS | Build the broker, cert-issuer and firmware-builder container images (apt/Ubuntu mirror, oqs-provider) into ECR, and the Vite dashboard into the hosting bucket with a CloudFront invalidation. Supply-chain entry point. |
| C030 | NAT gateway and VPC endpoints | Network | AWS | Egress path for the private broker task to IoT Core, ECR, Secrets Manager and CloudWatch Logs. VPC flow logs enabled (14 days). |

### Connections

| ID | Source | Destination | Protocol | Port | Encrypted | Description |
|---|---|---|---|---|---|---|
| CN001 | ESP32 field device firmware (C001) | Workshop WiFi access point (C002) | Other | N/A | Yes | Device joins the workshop AP with the shared WPA2 PSK baked into its firmware. |
| CN002 | ESP32 field device firmware (C001) | Broker Network Load Balancer (C004) | MQTT | 8883 | Yes | classical / pqc firmware: MQTT over TLS 1.3 (SecP256r1MLKEM768 for pqc, ECDHE for classical) with mTLS client cert from the classical CA. Device verifies the broker chain only, no hostname check. |
| CN003 | ESP32 field device firmware (C001) | Broker Network Load Balancer (C004) | MQTT | 8884 | Yes | pqc-mldsa / signer / verified firmware: MQTT over TLS 1.3 with ML-KEM and an ML-DSA-44 client cert; broker presents an ML-DSA-44 server cert. |
| CN004 | Broker Network Load Balancer (C004) | Mosquitto PQC broker (C005) | TCP | 8883 | Yes | NLB TCP passthrough of 8883 and 8884 to the Fargate task IPs in private subnets; TLS terminates at Mosquitto. |
| CN005 | Mosquitto PQC broker (C005) | Mosquitto to IoT Core bridge (C006) | Other | N/A | No | In-process: broker hands dt/+/telemetry to the bridge client in the same container. |
| CN006 | Mosquitto PQC broker (C005) | ESP32 field device firmware (C001) | MQTT | 8883 | Yes | Downlink to devices over their established TLS sessions via the NLB: cmd/<thing>/ota (presigned firmware URLs), LED/control and revert commands. No per-device topic ACL, so any connected client can also publish to these topics. |
| CN007 | Mosquitto to IoT Core bridge (C006) | NAT gateway and VPC endpoints (C030) | MQTT | 8883 | Yes | Bridge MQTT/TLS 1.2 to IoT Core leaves the private subnet via the NAT gateway. |
| CN008 | NAT gateway and VPC endpoints (C030) | AWS IoT Core and rules (C007) | MQTT | 8883 | Yes | NAT egress to the IoT Core ATS data endpoint; bridge authenticates with its IoT X.509 cert and publishes dt/+/telemetry. |
| CN009 | AWS IoT Core and rules (C007) | Mosquitto to IoT Core bridge (C006) | MQTT | 8883 | Yes | IoT Core delivers cmd/# (OTA and control commands) to the bridge subscription. |
| CN010 | Mosquitto PQC broker (C005) | NAT gateway and VPC endpoints (C030) | HTTPS | 443 | Yes | Broker task reaches ECR, Secrets Manager and CloudWatch Logs via VPC endpoints (or NAT). |
| CN011 | NAT gateway and VPC endpoints (C030) | Secrets Manager secrets (D005) | HTTPS | 443 | Yes | GetSecretValue for broker classical/ML-DSA certs and keys and the bridge cert/key/root CA at task start. |
| CN012 | NAT gateway and VPC endpoints (C030) | ECR repositories (D007) | HTTPS | 443 | Yes | Fargate pulls pqc-workshop-mosquitto-broker:latest. |
| CN013 | NAT gateway and VPC endpoints (C030) | CloudWatch log groups (D010) | HTTPS | 443 | Yes | awslogs driver ships broker logs (client IDs, source IPs, topics) to /pqc-workshop/broker-container. |
| CN014 | AWS IoT Core and rules (C007) | Telemetry processor Lambda (C008) | Other | N/A | Yes | Rules pqc_workshop_telemetry and pqc_workshop_lifecycle invoke the telemetry processor. |
| CN015 | AWS IoT Core and rules (C007) | Message logger Lambda (C009) | Other | N/A | Yes | Rules pqc_workshop_message_log_dt (dt/+/+) and _cmd (cmd/+/+) invoke the message logger. |
| CN016 | AWS IoT Core and rules (C007) | Auto-register Lambda (C010) | Other | N/A | Yes | Rule pqc_workshop_auto_register on dt/+/lifecycle status=online (receives nothing because lifecycle is not bridged). |
| CN017 | Auto-register Lambda (C010) | AWS IoT Core and rules (C007) | HTTPS | 443 | Yes | Auto-register calls iot CreateThing. |
| CN018 | Telemetry processor Lambda (C008) | Dashboard devices table (D003) | HTTPS | 443 | Yes | Conditional UpdateItem (attribute_exists(serialNumber)) and migrationStatus reconcile on pqc-workshop-devices. |
| CN019 | Telemetry processor Lambda (C008) | Dashboard AppSync API (C022) | HTTPS | 443 | Yes | Telemetry processor calls AppSync (IAM) to push updates to subscribers. |
| CN020 | Message logger Lambda (C009) | Message log table (D004) | HTTPS | 443 | Yes | PutItem of MQTT payloads into pqc-workshop-message-log. |
| CN021 | Dashboard web client (C025) | Dashboard CloudFront distribution (C024) | HTTPS | 443 | Yes | Browser loads the SPA and config.json (identity pool ID, AppSync URL). Public, no WAF. |
| CN022 | Dashboard CloudFront distribution (C024) | Dashboard hosting bucket (D008) | HTTPS | 443 | Yes | CloudFront OAC (SigV4) GetObject from the hosting bucket. |
| CN023 | Dashboard web client (C025) | Cognito identity pool (C023) | HTTPS | 443 | Yes | GetId / GetCredentialsForIdentity as an UNAUTHENTICATED guest; no sign-in exists. |
| CN024 | Dashboard web client (C025) | Dashboard AppSync API (C022) | HTTPS | 443 | Yes | SigV4-signed GraphQL with guest credentials: listDevices/getDevice/getDeviceLog/getBrokerLog queries and updateDevice/migrateFirmware mutations. Reachable by anyone holding the public identity pool ID. |
| CN025 | Dashboard AppSync API (C022) | Dashboard devices table (D003) | HTTPS | 443 | Yes | Resolvers listDevices/getDevice/updateDevice (arbitrary field SET) on pqc-workshop-devices. |
| CN026 | Dashboard AppSync API (C022) | Message log table (D004) | HTTPS | 443 | Yes | getDeviceLog resolver queries the message log (includes OTA command payloads). |
| CN027 | Dashboard AppSync API (C022) | Broker log reader Lambda (C026) | HTTPS | 443 | Yes | getBrokerLog resolver invokes the broker log reader. |
| CN028 | Broker log reader Lambda (C026) | CloudWatch log groups (D010) | HTTPS | 443 | Yes | FilterLogEvents on /pqc-workshop/broker-container. |
| CN029 | Dashboard AppSync API (C022) | Start-migration and prepare-build Lambdas (C014) | HTTPS | 443 | Yes | migrateFirmware resolver invokes start-migration with {thingName, variant}. |
| CN030 | Start-migration and prepare-build Lambdas (C014) | PQC migration state machine (C015) | HTTPS | 443 | Yes | start-migration calls StartExecution. |
| CN031 | PQC migration state machine (C015) | Start-migration and prepare-build Lambdas (C014) | HTTPS | 443 | Yes | PrepareBuild state invokes prepare-build (lambda:invoke). |
| CN032 | Start-migration and prepare-build Lambdas (C014) | Device registry table (D002) | HTTPS | 443 | Yes | prepare-build reads the device registry (incl. plaintext wifiPassword fallback). |
| CN033 | Start-migration and prepare-build Lambdas (C014) | SSM Parameter Store /pqc-workshop/* (D006) | HTTPS | 443 | Yes | prepare-build reads /pqc-workshop/wifi/* and broker endpoint (SecureString decrypt). |
| CN034 | PQC migration state machine (C015) | Firmware build CodeBuild (C017) | HTTPS | 443 | Yes | WaitForBuild: codebuild:startBuild.sync with per-device env overrides incl. WIFI_PASSWORD as PLAINTEXT. |
| CN035 | PQC migration state machine (C015) | Publish-OTA Lambda (C016) | HTTPS | 443 | Yes | PublishOta state invokes publish-ota. |
| CN036 | PQC migration state machine (C015) | Dashboard devices table (D003) | HTTPS | 443 | Yes | Status states write migrationStatus building/pushing-ota/rebooting/failed. |
| CN037 | Publish-OTA Lambda (C016) | Firmware S3 bucket (D001) | HTTPS | 443 | Yes | HeadObject and presign (900 s) firmware.bin, firmware.bin.sig, signer.crt. |
| CN038 | Publish-OTA Lambda (C016) | AWS IoT Core and rules (C007) | HTTPS | 443 | Yes | iot-data Publish of the ota-update command to cmd/<thing>/ota at QoS 1. |
| CN039 | ESP32 field device firmware (C001) | Firmware S3 bucket (D001) | HTTPS | 443 | Yes | Device downloads firmware.bin (+ .sig, signer.crt) from the URL in the OTA command. TLS uses WOLFSSL_VERIFY_NONE, so the server certificate is not checked and the host is whatever the command says. sha256 (from the command) is enforced if present; the signature is checked only when the command carries sigAlg. |
| CN040 | ESP32 field device firmware (C001) | ESP32 device flash (D011) | Other | N/A | No | Device private key, certs, signer roots and WiFi PSK are compiled into the app image stored in flash; OTA writes the inactive partition. |
| CN041 | Builder-Pi edge host and Flask GUI (C003) | Builder API (C011) | HTTPS | 443 | Yes | Builder-Pi calls /register, /deregister, /firmware with SigV4 using its SSM-rotated role credentials. |
| CN042 | Builder API (C011) | Device registrar Lambda (C012) | HTTPS | 443 | Yes | /register Lambda proxy integration. |
| CN043 | Builder API (C011) | Deregistrar and firmware-pusher Lambdas (C013) | HTTPS | 443 | Yes | /deregister and /firmware Lambda proxy integrations. |
| CN044 | Device registrar Lambda (C012) | AWS IoT Core and rules (C007) | HTTPS | 443 | Yes | CreateThing, CreatePolicy (pqc-device-<serial>), attach. |
| CN045 | Device registrar Lambda (C012) | Device registry table (D002) | HTTPS | 443 | Yes | PutItem registry record including plaintext wifiPassword. |
| CN046 | Device registrar Lambda (C012) | Dashboard devices table (D003) | HTTPS | 443 | Yes | Seeds the dashboard row with serialNumber (required by the telemetry guard). |
| CN047 | Device registrar Lambda (C012) | SSM Parameter Store /pqc-workshop/* (D006) | HTTPS | 443 | Yes | Reads WiFi SSID/PSK and broker endpoint (SecureString decrypt). |
| CN048 | Device registrar Lambda (C012) | Firmware build CodeBuild (C017) | HTTPS | 443 | Yes | StartBuild for the classical first-flash firmware with WIFI_PASSWORD plaintext override. |
| CN049 | Deregistrar and firmware-pusher Lambdas (C013) | AWS IoT Core and rules (C007) | HTTPS | 443 | Yes | Deregistrar removes the Thing, cert and policy. |
| CN050 | Deregistrar and firmware-pusher Lambdas (C013) | Device registry table (D002) | HTTPS | 443 | Yes | Deregistrar deletes the registry record. |
| CN051 | Firmware build CodeBuild (C017) | Firmware S3 bucket (D001) | HTTPS | 443 | Yes | Reads source/<sketch>.ino; writes <serial>/<version>/ firmware.bin, merged.bin (both embedding the device key and PSK), sig, signer cert, metadata. |
| CN052 | Firmware build CodeBuild (C017) | Classical Private CA (C020) | HTTPS | 443 | Yes | IssueCertificate for classical and pqc device certs. |
| CN053 | Firmware build CodeBuild (C017) | ML-DSA-44 Private CA (C021) | HTTPS | 443 | Yes | IssueCertificate for pqc-mldsa device certs. |
| CN054 | Firmware build CodeBuild (C017) | Secrets Manager secrets (D005) | HTTPS | 443 | Yes | GetSecretValue pqc-broker/signer-* (signer private keys and CA roots) to sign firmware.bin and bake trust anchors. |
| CN055 | Firmware build CodeBuild (C017) | ECR repositories (D007) | HTTPS | 443 | Yes | Pulls pqc-workshop-firmware-builder:latest. |
| CN056 | Builder-Pi edge host and Flask GUI (C003) | Firmware S3 bucket (D001) | HTTPS | 443 | Yes | Downloads <serial>/<version>/firmware.merged.bin for USB flashing; role can read every object in the bucket. |
| CN057 | Builder-Pi edge host and Flask GUI (C003) | Device registry table (D002) | HTTPS | 443 | Yes | GetItem/Scan on the registry (exposes plaintext wifiPassword). |
| CN058 | Builder-Pi edge host and Flask GUI (C003) | SSM Parameter Store /pqc-workshop/* (D006) | HTTPS | 443 | Yes | GetParameter(s)/ByPath /pqc-workshop/* incl. WiFi PSK decrypt. |
| CN059 | Builder-Pi edge host and Flask GUI (C003) | CloudWatch log groups (D010) | HTTPS | 443 | Yes | CloudWatch agent ships flash/serial logs to /pqc-workshop/builder-pi. |
| CN060 | Builder-Pi edge host and Flask GUI (C003) | ESP32 field device firmware (C001) | Other | N/A | No | esptool erase/write of merged firmware and the serial console over USB. No authentication; physical access. |
| CN061 | Workshop WiFi access point (C002) | Builder-Pi edge host and Flask GUI (C003) | HTTP | 8080 | No | Any client on the workshop AP (or a reachable LAN interface) can drive the UNAUTHENTICATED Flask GUI over plain HTTP: register/reflash/deregister devices, read serial output, and POST /api/config to re-point endpoints. |
| CN062 | Builder-Pi edge host and Flask GUI (C003) | SSM fleet management for the Builder-Pi (C027) | HTTPS | 443 | Yes | SSM agent registers with the hybrid activation and polls for Run Command. |
| CN063 | SSM fleet management for the Builder-Pi (C027) | Builder-Pi edge host and Flask GUI (C003) | HTTPS | 443 | Yes | Run Command executes install-toolchain / deploy-app / configure-service / configure-wifi-ap as root on the Pi. |
| CN064 | SSM fleet management for the Builder-Pi (C027) | Secrets Manager secrets (D005) | HTTPS | 443 | Yes | Activation id/code stored in Secrets Manager iot/<instance>/*. |
| CN065 | Operator deploy workstation (C028) | SSM fleet management for the Builder-Pi (C027) | HTTPS | 443 | Yes | Operator orchestrates SSM send-command and generates the Pi bundle (activation code embedded). |
| CN066 | Operator deploy workstation (C028) | SSM Parameter Store /pqc-workshop/* (D006) | HTTPS | 443 | Yes | deploy-workshop.sh PutParameter for iotEndpoint, broker endpoint, WiFi SSID/PSK, dashboard config. |
| CN067 | Operator deploy workstation (C028) | Build source buckets (D009) | HTTPS | 443 | Yes | Uploads broker, cert-issuer and dashboard build sources. |
| CN068 | Operator deploy workstation (C028) | Image and dashboard build CodeBuild projects (C029) | HTTPS | 443 | Yes | Starts the firmware-builder image build and other image builds. |
| CN069 | Image and dashboard build CodeBuild projects (C029) | Build source buckets (D009) | HTTPS | 443 | Yes | Image/dashboard builds read their sources. |
| CN070 | Image and dashboard build CodeBuild projects (C029) | ECR repositories (D007) | HTTPS | 443 | Yes | Push broker, cert-issuer and firmware-builder images (mutable :latest). |
| CN071 | Image and dashboard build CodeBuild projects (C029) | Dashboard hosting bucket (D008) | HTTPS | 443 | Yes | Dashboard build syncs the Vite artifact and config.json, then invalidates CloudFront. |
| CN072 | Broker and signer cert provisioner CodeBuild (C018) | Classical Private CA (C020) | HTTPS | 443 | Yes | IssueCertificate for the classical broker cert and classical OTA signer. |
| CN073 | Broker and signer cert provisioner CodeBuild (C018) | ML-DSA-44 Private CA (C021) | HTTPS | 443 | Yes | IssueCertificate for the ML-DSA broker cert and ML-DSA OTA signer. |
| CN074 | Broker and signer cert provisioner CodeBuild (C018) | Secrets Manager secrets (D005) | HTTPS | 443 | Yes | PutSecretValue for 12 broker and signer CA/cert/key secrets. |
| CN075 | Broker and signer cert provisioner CodeBuild (C018) | Build source buckets (D009) | HTTPS | 443 | Yes | Reads cert-issuer buildspec and pulls the cert-issuer image. |
| CN076 | Bridge provisioner Lambda (C019) | AWS IoT Core and rules (C007) | HTTPS | 443 | Yes | CreateThing, CreateKeysAndCertificate, CreatePolicy for pqc-workshop-broker-bridge. |
| CN077 | Bridge provisioner Lambda (C019) | Secrets Manager secrets (D005) | HTTPS | 443 | Yes | PutSecretValue bridge cert, private key and AmazonRootCA1. |

### Data Stores

| ID | Name | Type | Classification | Encrypted at Rest | Description |
|---|---|---|---|---|---|
| D001 | Firmware S3 bucket | Object Storage | Restricted | Yes | pqc-workshop-firmware-<acct>: SSE-S3, versioning on, public access blocked. Holds sketch sources (source/), Pi app assets, and per device <serial>/<version>/ firmware.bin, firmware.merged.bin, bootloader, partitions, metadata.json, firmware.bin.sig, signer.crt. Every firmware image embeds that device's private key and the WiFi PSK, and old versions persist. Readable in full by the Builder-Pi role. |
| D002 | Device registry table | NoSQL | Confidential | Yes | DynamoDB pqc-workshop-device-registry (SSE on): serial, thing name, cert metadata, firmware version/target, status, broker endpoint, and a PLAINTEXT wifiPassword. Readable by the Builder-Pi role (GetItem/Scan). |
| D003 | Dashboard devices table | NoSQL | Internal | Yes | DynamoDB pqc-workshop-devices (SSE on): per-device dashboard state, latest telemetry, migrationStatus. Read and freely updated through AppSync (updateDevice). |
| D004 | Message log table | NoSQL | Internal | Yes | DynamoDB pqc-workshop-message-log (SSE on, TTL, PITR on): MQTT payloads for dt/+/+ and cmd/+/+, including OTA commands that carry presigned firmware URLs. |
| D005 | Secrets Manager secrets | Other | Restricted | Yes | Default aws/secretsmanager key. pqc-broker/*: classical and ML-DSA broker CA/cert/key, classical and ML-DSA OTA signer CA/cert/key (pqc-broker/signer-*), IoT bridge cert/key/root CA; iot/<instance>/* SSM activation id/code. |
| D006 | SSM Parameter Store /pqc-workshop/* | Other | Confidential | Yes | iotEndpoint, broker/endpoint, wifi/ssid (String), wifi/password (SecureString, aws/ssm key), dashboard config. Written by deploy-workshop.sh. |
| D007 | ECR repositories | Other | Internal | Yes | pqc-workshop-mosquitto-broker, pqc-workshop-cert-issuer, pqc-workshop-firmware-builder (scan on push, mutable latest tag). Images pulled by Fargate and CodeBuild by :latest. |
| D008 | Dashboard hosting bucket | Object Storage | Public | Yes | Auto-named S3 bucket (SSE-S3, versioned), readable only by the CloudFront distribution via OAC. Content is public by design: the SPA and config.json, which includes the Cognito identity pool ID and AppSync endpoint. |
| D009 | Build source buckets | Object Storage | Internal | Yes | pqc-workshop-broker-source-<acct> (broker and cert-issuer build sources, buildspecs) and pqc-workshop-dashboard-source-<acct>. SSE-S3, versioned. Writing here changes what CodeBuild builds. |
| D010 | CloudWatch log groups | Other | Internal | Yes | /pqc-workshop/broker-container (7 d; client IDs, source IPs, topics), /aws/codebuild/pqc-workshop-firmware-build and cert-provisioner, /pqc-workshop/builder-pi, Lambda logs, VPC flow logs (14 d). Default encryption. |
| D011 | ESP32 device flash | File System | Restricted | No | Device private key, certificate, CA roots, OTA signer roots and WiFi PSK compiled into the app image in flash (PROGMEM). No ESP32 flash encryption or secure boot configured in the build FQBN as far as found, so anyone with the board can read them over USB. |

## Threat Actors

### Insider

- **Type**: Insider Threat
- **Motivations**: Financial gain, Revenge / grievance
- **Resources**: Individual
- **Relationship to Target**: Internal
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 8/10
- **Description**: A team member or contractor with repo or account access (GitLab, Workshop Studio, pqc-a/pqc-b) who abuses it. Covers the disgruntled-employee case.

### External Attacker

- **Type**: External Attacker
- **Motivations**: Thrill-seeking / notoriety, Disruption / destruction
- **Resources**: Club / small group
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 3/10
- **Description**: An external attacker who has read the public aws-samples repo, so knows the architecture, topic layout, resource names and the anonymous Cognito/AppSync design, and targets a live workshop deployment during an event window. Can abuse AppSync migrateFirmware to start CodeBuild builds and OTA pushes, tamper with dashboard state, and read message/broker logs.

### Nation-state Actor

- **Type**: Nation-State / APT
- **Motivations**: Espionage / intelligence collection
- **Resources**: Government
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 5 - Nation-state APT / elite
- **State Nexus**: State-executed
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 9/10
- **Description**: Harvest-now-decrypt-later adversary recording TLS traffic for future quantum decryption. This is the workshop's teaching premise rather than a realistic target: the telemetry has little intelligence value. Relevant mainly to confirm classical-only links (bridge TLS 1.2, Builder-Pi HTTPS, OTA download) are understood.

### Hacktivist

- **Type**: Hacktivist
- **Motivations**: Ideological / hacktivism
- **Resources**: Contest / crowd
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Sector-focused
- **Relevant**: Yes
- **Priority**: 7/10
- **Description**: A group wanting a visible stunt at a high-profile event, e.g. defacing or disrupting the live re:Invent PQC demo or showing the 'post-quantum' devices can be hijacked.

### Organized Crime

- **Type**: Financially Motivated Cybercriminal / Organized Crime
- **Motivations**: Financial gain
- **Resources**: Organization
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 3 - Organized cybercrime
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 5/10
- **Description**: Criminals who find the anonymous AppSync API or leaked operator/Pi credentials and abuse the account for cost (repeated CodeBuild firmware builds via migrateFirmware, compute) or pivot using the Admin-level operator role. Workshop data itself has little resale value.

### Competitor

- **Type**: Competitor / Corporate Espionage
- **Motivations**: Competitive advantage, Espionage / intelligence collection
- **Resources**: Organization
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 3 - Organized cybercrime
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: No
- **Description**: No competitive value in an open-source educational sample.

### Script Kiddie

- **Type**: Script Kiddie / Novice
- **Motivations**: Thrill-seeking / notoriety
- **Resources**: Individual
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 1 - Opportunistic / script kiddie
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 2/10
- **Description**: Automated internet scanning and commodity tooling. Finds the internet-facing NLB on 8883/8884, the public builder API and the CloudFront dashboard; can pull the Cognito identity pool ID from the public config.json and call the anonymous AppSync API (list/overwrite devices, trigger migrations). Mostly blocked at the broker by mTLS.

### Disgruntled Employee

- **Type**: Disgruntled Employee
- **Motivations**: Revenge / grievance
- **Resources**: Individual
- **Relationship to Target**: Internal
- **Sophistication Tier**: Tier 1 - Opportunistic / script kiddie
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: No
- **Description**: Covered by TA001 Insider for this system.

### Privileged User

- **Type**: Privileged User
- **Motivations**: Financial gain
- **Resources**: Individual
- **Relationship to Target**: Internal
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 6/10
- **Description**: The workshop operator or sample maintainer making a mistake with Admin credentials: deploying into the wrong account, leaking the Builder-Pi bundle (7-day SSM activation code), committing real account IDs/PSK, or publishing internal content (.kiro/, .threatmodel/) to the public mirror.

### Supply-chain compromise

- **Type**: Third Party
- **Motivations**: Financial gain
- **Resources**: Organization
- **Relationship to Target**: Partner / third-party
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 4/10
- **Description**: A compromised upstream the build trusts: the Ubuntu apt mirror and oqs-provider/OpenSSL sources in the broker and firmware-builder image builds, Arduino ESP32 core and library indexes, npm packages in the dashboard build, uvx/npx-fetched tooling, and mutable :latest ECR tags. Malicious code would run with CodeBuild role access to the CAs, signer keys and firmware bucket.

### Terrorist Organization

- **Type**: Terrorist Organization
- **Motivations**: Ideological / hacktivism, Disruption / destruction
- **Resources**: Club / small group
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Sector-focused
- **Relevant**: No
- **Description**: A group with limited offensive cyber capability, primarily using the internet for propaganda and recruitment

### Private Sector Offensive Actor

- **Type**: Private Sector Offensive Actor / Cyber Mercenary
- **Motivations**: Financial gain
- **Resources**: Organization
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 4 - State-nexus / advanced
- **State Nexus**: State-aligned
- **Targeting Specificity**: Targeted
- **Relevant**: No
- **Description**: No high-value individual targets in this system.

### Curious or malicious on-site attendee

- **Type**: Other
- **Motivations**: Thrill-seeking / notoriety, Disruption / destruction
- **Resources**: Individual
- **Relationship to Target**: Partner / third-party
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 1/10
- **Description**: A workshop attendee (often a security practitioner) in the room with legitimate physical access to an ESP32 board, the Builder-Pi USB station and the workshop WiFi, plus the public dashboard. Can dump their own board's flash over USB to recover its device private key and the shared WiFi PSK, join the AP, drive the unauthenticated Builder-Pi GUI on :8080, connect to the broker with their extracted cert and publish to other devices' cmd/<thing>/ota or dt topics (no broker ACLs), and call the anonymous AppSync API to migrate or overwrite other attendees' devices. Motive is curiosity, a demo of skill, or disrupting the session; impact is a broken workshop in front of an audience.

## Trust Boundaries

### Trust Zones

#### Field devices

- **Trust Level**: Low
- **Architecture Nodes**: ESP32 field device firmware (C001), ESP32 device flash (D011)
- **Description**: ESP32 boards and their flash. Physically in attendees' hands, so device keys and the WiFi PSK must be treated as extractable.

#### Workshop venue network

- **Trust Level**: Low
- **Architecture Nodes**: Workshop WiFi access point (C002)
- **Description**: The 2.4GHz WPA2-PSK workshop AP. Everyone in the room shares one PSK, which is also inside every firmware image.

#### Public internet clients

- **Trust Level**: Untrusted
- **Architecture Nodes**: Dashboard web client (C025)
- **Description**: Browsers running the dashboard and any anonymous internet client.

#### AWS public edge

- **Trust Level**: Low
- **Architecture Nodes**: Broker Network Load Balancer (C004), Builder API (C011), Dashboard AppSync API (C022), Cognito identity pool (C023), Dashboard CloudFront distribution (C024), Dashboard hosting bucket (D008)
- **Description**: Internet-reachable AWS front doors: broker NLB (0.0.0.0/0 on 8883/8884), builder API (SigV4), Cognito identity pool and AppSync (anonymous guest access), CloudFront and its hosting bucket.

#### Broker VPC private subnets

- **Trust Level**: Medium
- **Architecture Nodes**: Mosquitto PQC broker (C005), Mosquitto to IoT Core bridge (C006), NAT gateway and VPC endpoints (C030)
- **Description**: Fargate Mosquitto broker and its IoT Core bridge in private subnets with no public IP; egress via NAT gateway and VPC endpoints.

#### IoT and event processing

- **Trust Level**: Medium
- **Architecture Nodes**: AWS IoT Core and rules (C007), Telemetry processor Lambda (C008), Message logger Lambda (C009), Auto-register Lambda (C010), Broker log reader Lambda (C026), Dashboard devices table (D003), Message log table (D004), CloudWatch log groups (D010)
- **Description**: AWS IoT Core and rules, telemetry/message/auto-register/broker-log Lambdas, the dashboard devices table, message log and CloudWatch logs.

#### Device management backend

- **Trust Level**: Medium
- **Architecture Nodes**: Device registrar Lambda (C012), Deregistrar and firmware-pusher Lambdas (C013), Start-migration and prepare-build Lambdas (C014), PQC migration state machine (C015), Publish-OTA Lambda (C016), Device registry table (D002)
- **Description**: Registrar, deregistrar, firmware-pusher, start-migration, prepare-build and publish-OTA Lambdas, the migration state machine, and the device registry.

#### Build and PKI

- **Trust Level**: High
- **Architecture Nodes**: Firmware build CodeBuild (C017), Broker and signer cert provisioner CodeBuild (C018), Bridge provisioner Lambda (C019), Classical Private CA (C020), ML-DSA-44 Private CA (C021), Image and dashboard build CodeBuild projects (C029), Firmware S3 bucket (D001), Secrets Manager secrets (D005), SSM Parameter Store /pqc-workshop/* (D006), ECR repositories (D007), Build source buckets (D009)
- **Description**: Firmware and image CodeBuild projects, the cert and bridge provisioners, both ACM Private CAs, Secrets Manager (broker, signer and bridge keys), SSM parameters (WiFi PSK), ECR, the firmware bucket and build source buckets. Compromise here lets an attacker mint identities or sign firmware.

#### Operator administration

- **Trust Level**: High
- **Architecture Nodes**: SSM fleet management for the Builder-Pi (C027), Operator deploy workstation (C028)
- **Description**: The operator's deploy workstation with Admin credentials, and SSM fleet management (hybrid activation, Run Command documents) that controls the Builder-Pi.

#### Builder-Pi edge host

- **Trust Level**: Medium
- **Architecture Nodes**: Builder-Pi edge host and Flask GUI (C003)
- **Description**: On-site Raspberry Pi holding SSM-rotated AWS role credentials (whole firmware bucket, registry, WiFi PSK, builder API). Its Flask GUI on :8080 is unauthenticated.

### Trust Boundaries

#### Venue physical and RF boundary

- **Type**: Physical
- **Controls**: Shared WPA2 PSK on the workshop AP, Physical supervision of the Builder-Pi USB station, None on the Builder-Pi GUI (gap)
- **Description**: Everything inside the workshop room: boards in attendees' hands, the shared-PSK AP, and USB access to the Builder-Pi. An attendee crosses this boundary legitimately, so it gives little protection.

#### Internet to AWS public edge

- **Type**: Network
- **Controls**: Device mTLS terminated at the broker, SigV4 on the builder API, HTTPS/OAC on CloudFront, Presigned-URL expiry 900 s plus on-device sha256 and signature check, No WAF, no auth on Cognito guest access (gap)
- **Description**: Internet-reachable AWS endpoints: NLB, builder API, Cognito/AppSync, CloudFront, presigned S3 downloads.

#### Public edge to broker VPC

- **Type**: Network
- **Controls**: Private subnets, no public IP, Security group 8883/8884, require_certificate + per-listener CA, No mosquitto topic ACL (gap), No device hostname/SAN check (gap)
- **Description**: NLB into the private broker subnets and the broker's downlink to devices.

#### Broker VPC to AWS managed services

- **Type**: Network
- **Controls**: IoT X.509 bridge identity, Wildcard bridge IoT policy (gap), Task role scoped to named secrets, VPC endpoints
- **Description**: Bridge to IoT Core and broker task to Secrets Manager, ECR and CloudWatch Logs.

#### Dashboard API to backend

- **Type**: Process
- **Controls**: AppSync AWS_IAM auth (but guest role allows all fields), Service roles per data source, Variant allow-list in start-migration
- **Description**: AppSync resolvers and API Gateway integrations into data and device-management services.

#### Device management to IoT

- **Type**: Process
- **Controls**: Lambda/state-machine IAM roles, Registrar iot:Create*/Attach* on * (broad)
- **Description**: Device-management functions creating Things/policies, publishing OTA commands and writing dashboard state.

#### Build and PKI boundary

- **Type**: Process
- **Controls**: CodeBuild role scoped to both CAs and pqc-broker/signer-*, Signer and device keys scrubbed from build disk, Device key persists in firmware artifacts in S3 (gap), WiFi PSK as plaintext build env (gap)
- **Description**: Entry into the high-trust zone that issues certificates, holds signer keys and stores key-bearing firmware.

#### Builder-Pi to AWS

- **Type**: Network
- **Controls**: SSM hybrid activation and rotating role credentials, Pi role scoped to firmware bucket, registry, /pqc-workshop/*, builder API, Bucket-wide firmware read and registry Scan (broad)
- **Description**: The on-site Pi's AWS identity and SSM management channel.

#### Operator administrative boundary

- **Type**: Account
- **Controls**: Admin IAM via ada/Isengard, Throwaway per-event accounts
- **Description**: Admin-credentialed operator actions into the build, PKI and secrets plane.

## Assets and Flows

### Assets

| ID | Name | Type | Classification | Lifecycle | Data States | Criticality | Owner |
|---|---|---|---|---|---|---|---|
| A001 | Device private key and certificate | Cryptographic Key | Restricted | Active | At rest, In transit, In use | 5 | Firmware build pipeline |
| A002 | OTA firmware signer keys | Cryptographic Key | Restricted | Active | At rest, In use | 5 | Broker cert provisioner |
| A003 | Private CA issuance authority | Process | Restricted | Active | In use | 5 | ACM Private CA |
| A004 | Broker and bridge TLS keys | Cryptographic Key | Restricted | Active | At rest, In use | 4 | Broker stack |
| A005 | Workshop WiFi PSK | Credential | Confidential | Active | At rest, In transit, In use | 3 | Workshop operator |
| A006 | Firmware image | Data | Restricted | Active | At rest, In transit | 5 | Firmware build pipeline |
| A007 | OTA command with presigned URL | Token | Internal | Active | In transit, At rest | 4 | Publish-OTA Lambda |
| A008 | Device telemetry | Data | Internal | Active | In transit, At rest | 2 | Device |
| A009 | Cognito guest credentials | Token | Internal | Active | In transit, In use | 3 | Cognito identity pool |
| A010 | Builder-Pi AWS role credentials | Credential | Confidential | Active | At rest, In use | 4 | SSM |

### Asset Flows

| ID | Asset | Source | Destination | Protocol | Encrypted | Risk Level |
|---|---|---|---|---|---|---|
| F001 | Device private key and certificate | Firmware build CodeBuild (C017) | Classical Private CA (C020) | HTTPS (ACM PCA API) | Yes | 3 |
| F002 | Device private key and certificate | Firmware build CodeBuild (C017) | Firmware S3 bucket (D001) | HTTPS (S3) | Yes | 5 |
| F003 | Device private key and certificate | Builder-Pi edge host and Flask GUI (C003) | ESP32 device flash (D011) | USB serial (esptool) | No | 5 |
| F004 | Device private key and certificate | ESP32 field device firmware (C001) | Mosquitto PQC broker (C005) | MQTT/TLS 1.3 | Yes | 4 |
| F005 | OTA firmware signer keys | Broker and signer cert provisioner CodeBuild (C018) | Secrets Manager secrets (D005) | HTTPS (Secrets Manager) | Yes | 3 |
| F006 | OTA firmware signer keys | Secrets Manager secrets (D005) | Firmware build CodeBuild (C017) | HTTPS (Secrets Manager) | Yes | 4 |
| F007 | Private CA issuance authority | Broker and signer cert provisioner CodeBuild (C018) | ML-DSA-44 Private CA (C021) | HTTPS (ACM PCA API) | Yes | 3 |
| F008 | Private CA issuance authority | Firmware build CodeBuild (C017) | ML-DSA-44 Private CA (C021) | HTTPS (ACM PCA API) | Yes | 3 |
| F009 | Broker and bridge TLS keys | Secrets Manager secrets (D005) | Mosquitto PQC broker (C005) | HTTPS (Secrets Manager) | Yes | 2 |
| F010 | Broker and bridge TLS keys | Mosquitto to IoT Core bridge (C006) | AWS IoT Core and rules (C007) | MQTT/TLS 1.2 | Yes | 3 |
| F011 | Workshop WiFi PSK | Operator deploy workstation (C028) | SSM Parameter Store /pqc-workshop/* (D006) | HTTPS (SSM) | Yes | 2 |
| F012 | Workshop WiFi PSK | Device registrar Lambda (C012) | Device registry table (D002) | HTTPS (DynamoDB) | Yes | 4 |
| F013 | Workshop WiFi PSK | PQC migration state machine (C015) | Firmware build CodeBuild (C017) | HTTPS (CodeBuild/Step Functions) | Yes | 4 |
| F014 | Workshop WiFi PSK | SSM Parameter Store /pqc-workshop/* (D006) | Builder-Pi edge host and Flask GUI (C003) | HTTPS (SSM) | Yes | 3 |
| F015 | Firmware image | Firmware build CodeBuild (C017) | Firmware S3 bucket (D001) | HTTPS (S3) | Yes | 3 |
| F016 | Firmware image | Firmware S3 bucket (D001) | Builder-Pi edge host and Flask GUI (C003) | HTTPS (S3) | Yes | 4 |
| F017 | Firmware image | Firmware S3 bucket (D001) | ESP32 field device firmware (C001) | HTTPS (presigned S3 GET) | Yes | 5 |
| F018 | OTA command with presigned URL | Publish-OTA Lambda (C016) | AWS IoT Core and rules (C007) | HTTPS (iot-data) | Yes | 3 |
| F019 | OTA command with presigned URL | Mosquitto PQC broker (C005) | ESP32 field device firmware (C001) | MQTT | Yes | 4 |
| F020 | OTA command with presigned URL | Message logger Lambda (C009) | Message log table (D004) | HTTPS (DynamoDB) | Yes | 3 |
| F021 | Device telemetry | ESP32 field device firmware (C001) | Mosquitto PQC broker (C005) | MQTT/TLS 1.3 | Yes | 3 |
| F022 | Device telemetry | Telemetry processor Lambda (C008) | Dashboard devices table (D003) | HTTPS (DynamoDB) | Yes | 3 |
| F023 | Cognito guest credentials | Cognito identity pool (C023) | Dashboard web client (C025) | HTTPS | Yes | 4 |
| F024 | Cognito guest credentials | Dashboard web client (C025) | Dashboard AppSync API (C022) | HTTPS (SigV4 GraphQL) | Yes | 5 |
| F025 | Builder-Pi AWS role credentials | SSM fleet management for the Builder-Pi (C027) | Builder-Pi edge host and Flask GUI (C003) | HTTPS (SSM) | Yes | 3 |
| F026 | Builder-Pi AWS role credentials | Builder-Pi edge host and Flask GUI (C003) | Builder API (C011) | HTTPS (SigV4) | Yes | 4 |

## Threats

### Identified Threats

#### T1: anonymous internet user

**Statement**: A anonymous internet user with the identity pool ID from the public dashboard config.json can obtain Cognito guest credentials and call the AppSync migrateFirmware mutation for any thingName, which leads to unrequested firmware builds and OTA pushes to any attendee's device, disrupting the live workshop and spending account resources

- **Prerequisites**: with the identity pool ID from the public dashboard config.json
- **Action**: obtain Cognito guest credentials and call the AppSync migrateFirmware mutation for any thingName
- **Impact**: unrequested firmware builds and OTA pushes to any attendee's device, disrupting the live workshop and spending account resources
- **Impacted Assets**: A009, A006, A007
- **Tags**: appsync, anonymous-access
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: No sign-in exists; any internet user can trigger builds and OTA pushes. Fix with M004 and M006 before the next public event.
- **Assessment State**: Current

#### T2: anonymous internet user

**Statement**: A anonymous internet user with Cognito guest credentials can call the AppSync updateDevice mutation to overwrite any device's firmwareVersion, pqcAlgorithm, tlsVersion or status, which leads to a dashboard that shows false migration results, undermining the workshop's proof that devices negotiated ML-KEM

- **Prerequisites**: with Cognito guest credentials
- **Action**: call the AppSync updateDevice mutation to overwrite any device's firmwareVersion, pqcAlgorithm, tlsVersion or status
- **Impact**: a dashboard that shows false migration results, undermining the workshop's proof that devices negotiated ML-KEM
- **Impacted Assets**: A008, A009
- **Tags**: appsync, anonymous-access, integrity
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Very Likely
- **Residual Risk Rationale**: updateDevice is callable anonymously and falsifies the workshop's PQC evidence. M005 is a low-cost fix.
- **Assessment State**: Current

#### T3: anonymous internet user

**Statement**: A anonymous internet user with Cognito guest credentials within 15 minutes of a migration can read OTA commands through getDeviceLog and use a still-valid presigned URL to download that device's firmware image, which leads to disclosure of the device private key and WiFi PSK embedded in the image, enabling impersonation of that device

- **Prerequisites**: with Cognito guest credentials within 15 minutes of a migration
- **Action**: read OTA commands through getDeviceLog and use a still-valid presigned URL to download that device's firmware image
- **Impact**: disclosure of the device private key and WiFi PSK embedded in the image, enabling impersonation of that device
- **Impacted Assets**: A007, A006, A001, A005
- **Tags**: appsync, presigned-url, key-exposure
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Logged presigned URLs plus key-bearing images give anonymous access to device keys for 15 minutes. Needs M004, M007 or M012.
- **Assessment State**: Current

#### T4: anonymous internet user

**Statement**: A anonymous internet user with Cognito guest credentials can read broker connection logs and all device message payloads through getBrokerLog and getDeviceLog, which leads to disclosure of device client IDs, source IP addresses and full telemetry and command history

- **Prerequisites**: with Cognito guest credentials
- **Action**: read broker connection logs and all device message payloads through getBrokerLog and getDeviceLog
- **Impact**: disclosure of device client IDs, source IP addresses and full telemetry and command history
- **Impacted Assets**: A008, A007
- **Tags**: appsync, anonymous-access
- **Residual Risk Decision**: Open
- **Residual Severity**: Medium
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: Anonymous read of logs and payloads; low data value but closes with M004.
- **Assessment State**: Current

#### T6: workshop attendee

**Statement**: A workshop attendee with a device certificate extracted from their own board can publish an ota-update with no sigAlg, a URL to their own HTTPS server and a matching sha256 to another device's cmd/<thing>/ota, which leads to any device variant installing attacker-chosen firmware, because devices skip signature checks when sigAlg is absent and do not verify the download server's TLS certificate

- **Prerequisites**: with a device certificate extracted from their own board
- **Action**: publish an ota-update with no sigAlg, a URL to their own HTTPS server and a matching sha256 to another device's cmd/<thing>/ota
- **Impact**: any device variant installing attacker-chosen firmware, because devices skip signature checks when sigAlg is absent and do not verify the download server's TLS certificate
- **Impacted Assets**: A007, A006
- **Tags**: broker-acl, ota, signature-downgrade
- **Residual Risk Decision**: Open
- **Residual Severity**: Critical
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: Any attendee can install arbitrary firmware on any device. Highest priority: M001 (fail-closed signature) and M003 (broker ACL), then M002.
- **Assessment State**: Current

#### T7: workshop attendee

**Statement**: A workshop attendee with a device certificate extracted from their own board can publish dt/<other-thing>/telemetry or lifecycle messages for devices they do not own, which leads to fake telemetry and PQC status for other attendees' devices on the dashboard and in the message log

- **Prerequisites**: with a device certificate extracted from their own board
- **Action**: publish dt/<other-thing>/telemetry or lifecycle messages for devices they do not own
- **Impact**: fake telemetry and PQC status for other attendees' devices on the dashboard and in the message log
- **Impacted Assets**: A008
- **Tags**: broker-acl
- **Residual Risk Decision**: Open
- **Residual Severity**: Medium
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: mTLS limits this to cert holders, but without ACLs any attendee can spoof others' telemetry. Closes with M003.
- **Assessment State**: Current

#### T8: attacker

**Statement**: A attacker who obtained a device private key can reuse a deregistered or stolen device certificate to keep connecting to the broker indefinitely, which leads to removed devices staying trusted, because the broker trusts any CA-issued cert, does not check the registry, and both CAs have CRLs disabled

- **Prerequisites**: who obtained a device private key
- **Action**: reuse a deregistered or stolen device certificate to keep connecting to the broker indefinitely
- **Impact**: removed devices staying trusted, because the broker trusts any CA-issued cert, does not check the registry, and both CAs have CRLs disabled
- **Impacted Assets**: A001
- **Tags**: revocation, pki
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: No revocation path; impact bounded by the throwaway account lifetime but extracted keys stay valid for the whole event. M009 needed.
- **Assessment State**: Current

#### T9: user on the workshop WiFi

**Statement**: A user on the workshop WiFi with the shared PSK can drive the unauthenticated Builder-Pi GUI to deregister or reflash other attendees' devices and read their serial consoles, which leads to disruption of other attendees' devices and exposure of serial output

- **Prerequisites**: with the shared PSK
- **Action**: drive the unauthenticated Builder-Pi GUI to deregister or reflash other attendees' devices and read their serial consoles
- **Impact**: disruption of other attendees' devices and exposure of serial output
- **Impacted Assets**: A010
- **Tags**: builder-pi, no-auth
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: Anyone on the AP can manage every device on the station. M010 is low cost.
- **Assessment State**: Current

#### T10: user on the workshop WiFi

**Statement**: A user on the workshop WiFi with the shared PSK can POST to the Builder-Pi /api/config to re-point the firmware bucket or API endpoint to attacker-controlled resources, which leads to the Pi flashing attacker-supplied firmware onto the next boards plugged into the station

- **Prerequisites**: with the shared PSK
- **Action**: POST to the Builder-Pi /api/config to re-point the firmware bucket or API endpoint to attacker-controlled resources
- **Impact**: the Pi flashing attacker-supplied firmware onto the next boards plugged into the station
- **Impacted Assets**: A006, A010
- **Tags**: builder-pi, no-auth, csrf
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Runtime config changes can redirect flashing to attacker firmware. M010.
- **Assessment State**: Current

#### T11: attacker

**Statement**: A attacker with root on the Builder-Pi or its role credentials can use the Pi role to download every device's firmware from the firmware bucket, which leads to disclosure of every device private key and the PSK, allowing impersonation of the whole fleet

- **Prerequisites**: with root on the Builder-Pi or its role credentials
- **Action**: use the Pi role to download every device's firmware from the firmware bucket
- **Impact**: disclosure of every device private key and the PSK, allowing impersonation of the whole fleet
- **Impacted Assets**: A001, A006, A010
- **Tags**: builder-pi, least-privilege
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Pi compromise exposes every device key. M011 and M012.
- **Assessment State**: Current

#### T12: attacker

**Statement**: A attacker with a leaked Builder-Pi bundle within its 7-day validity can register their own machine with the hybrid activation code, which leads to a rogue managed instance holding the Builder-Pi role, with the same bucket, registry and PSK access as the real Pi

- **Prerequisites**: with a leaked Builder-Pi bundle within its 7-day validity
- **Action**: register their own machine with the hybrid activation code
- **Impact**: a rogue managed instance holding the Builder-Pi role, with the same bucket, registry and PSK access as the real Pi
- **Impacted Assets**: A010
- **Tags**: ssm, builder-pi
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Registration limit 5 and multi-day expiry still allow a leaked bundle to add rogue instances. Tighten per M014.
- **Assessment State**: Current

#### T13: user or role with read-only account access

**Statement**: A user or role with read-only account access with CodeBuild, Step Functions or DynamoDB read permissions can read the workshop WiFi PSK from the CodeBuild project environment, build details, Step Functions execution history or the registry table, which leads to disclosure of the PSK outside its SecureString

- **Prerequisites**: with CodeBuild, Step Functions or DynamoDB read permissions
- **Action**: read the workshop WiFi PSK from the CodeBuild project environment, build details, Step Functions execution history or the registry table
- **Impact**: disclosure of the PSK outside its SecureString
- **Impacted Assets**: A005
- **Tags**: secrets-handling
- **Residual Risk Decision**: Open
- **Residual Severity**: Low
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: PSK value is low (shared and compiled into firmware) but plaintext copies are an existing backlog item and a poor pattern for a sample. M013.
- **Assessment State**: Current

#### T14: workshop attendee

**Statement**: A workshop attendee with the extracted PSK and network position in the room can run a rogue AP with the workshop SSID and PSK and present any certificate chaining to the workshop CA, which leads to a man-in-the-middle on devices that associate with it, because the firmware checks the chain but not the broker hostname or SAN

- **Prerequisites**: with the extracted PSK and network position in the room
- **Action**: run a rogue AP with the workshop SSID and PSK and present any certificate chaining to the workshop CA
- **Impact**: a man-in-the-middle on devices that associate with it, because the firmware checks the chain but not the broker hostname or SAN
- **Impacted Assets**: A008, A007
- **Tags**: tls, hostname-check
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Rogue AP plus no hostname check enables MITM in the room. M015 (already a backlog item).
- **Assessment State**: Current

#### T15: compromised upstream package source

**Statement**: A compromised upstream package source such as the Ubuntu apt mirror, oqs-provider source, Arduino ESP32 core or npm registry can inject code into the broker, cert-issuer or firmware-builder image builds, which leads to theft of the OTA signer keys and abuse of CA issuance, so malicious firmware can be signed for the whole fleet

- **Prerequisites**: such as the Ubuntu apt mirror, oqs-provider source, Arduino ESP32 core or npm registry
- **Action**: inject code into the broker, cert-issuer or firmware-builder image builds
- **Impact**: theft of the OTA signer keys and abuse of CA issuance, so malicious firmware can be signed for the whole fleet
- **Impacted Assets**: A002, A003, A006
- **Tags**: supply-chain
- **Residual Risk Decision**: Open
- **Residual Severity**: Critical
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Release-tag pins reduce but do not remove the risk; impact is fleet-wide. Track M016.
- **Assessment State**: Current

#### T16: insider or compromised principal

**Statement**: A insider or compromised principal with ECR push permissions can push a replacement image under the mutable :latest tag in ECR, which leads to Fargate and CodeBuild silently running attacker code with broker secrets or signer-key access on the next start

- **Prerequisites**: with ECR push permissions
- **Action**: push a replacement image under the mutable :latest tag in ECR
- **Impact**: Fargate and CodeBuild silently running attacker code with broker secrets or signer-key access on the next start
- **Impacted Assets**: A002, A004
- **Tags**: supply-chain, ecr
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Requires an insider or compromised principal; scan on push detects known vulnerabilities only. Track M016.
- **Assessment State**: Current

#### T17: anonymous internet user

**Statement**: A anonymous internet user with Cognito guest credentials can repeatedly call migrateFirmware across many devices, which leads to queued or throttled CodeBuild builds that stall real migrations during the session, plus avoidable build cost

- **Prerequisites**: with Cognito guest credentials
- **Action**: repeatedly call migrateFirmware across many devices
- **Impact**: queued or throttled CodeBuild builds that stall real migrations during the session, plus avoidable build cost
- **Impacted Assets**: A009
- **Tags**: appsync, cost
- **Residual Risk Decision**: Open
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Reserved concurrency of 5 limits fan-out but builds still queue. Closes with M004 and M006.
- **Assessment State**: Current

#### T19: anonymous internet user

**Statement**: A anonymous internet user with Cognito guest credentials can migrate or overwrite devices through AppSync, which leads to actions that cannot be attributed to a person, because all callers share the unauthenticated role

- **Prerequisites**: with Cognito guest credentials
- **Action**: migrate or overwrite devices through AppSync
- **Impact**: actions that cannot be attributed to a person, because all callers share the unauthenticated role
- **Impacted Assets**: A009
- **Tags**: audit, anonymous-access
- **Residual Risk Decision**: Open
- **Residual Severity**: Medium
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: No attribution is possible today. Closes with M004 and M019.
- **Assessment State**: Current

#### T20: attacker

**Statement**: A attacker who compromises the broker task, e.g. via a Mosquitto or oqs-provider flaw can publish to dt/* and $aws/things/*/shadow/* and receive cmd/* for every device through the bridge identity, which leads to fleet-wide telemetry spoofing and command interception in IoT Core from one compromised component

- **Prerequisites**: who compromises the broker task, e.g. via a Mosquitto or oqs-provider flaw
- **Action**: publish to dt/* and $aws/things/*/shadow/* and receive cmd/* for every device through the bridge identity
- **Impact**: fleet-wide telemetry spoofing and command interception in IoT Core from one compromised component
- **Impacted Assets**: A004, A008, A007
- **Tags**: broker, least-privilege
- **Residual Risk Decision**: Open
- **Residual Severity**: Medium
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Bridge wildcard policy amplifies a broker compromise; private subnet reduces exposure. M020 is a low-cost fix.
- **Assessment State**: Current

#### T21: maintainer making a mistake

**Statement**: A maintainer making a mistake with publish rights can publish real account IDs, the PSK, .kiro notes or the threat model to the public mirror or the Workshop Studio source zip, which leads to public disclosure of internal details and an attack roadmap

- **Prerequisites**: with publish rights
- **Action**: publish real account IDs, the PSK, .kiro notes or the threat model to the public mirror or the Workshop Studio source zip
- **Impact**: public disclosure of internal details and an attack roadmap
- **Tags**: release-process
- **Residual Risk Decision**: Open
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Public mirror is protected (M021); the Workshop Studio source zip is not (M022).
- **Assessment State**: Current

#### T23: workshop attendee

**Statement**: A workshop attendee with a rogue AP using the workshop SSID and extracted PSK can intercept a device's OTA download, which does not verify the server certificate, and serve or capture the image, which leads to capture of key-bearing firmware images, and installation of a modified image on any device sent an unsigned OTA

- **Prerequisites**: with a rogue AP using the workshop SSID and extracted PSK
- **Action**: intercept a device's OTA download, which does not verify the server certificate, and serve or capture the image
- **Impact**: capture of key-bearing firmware images, and installation of a modified image on any device sent an unsigned OTA
- **Impacted Assets**: A006, A001, A005
- **Tags**: ota, tls, rogue-ap
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Signed pushes are protected by M026, but unverified TLS exposes key-bearing images and unsigned pushes. M001 and M002.
- **Assessment State**: Current

### Resolved Threats

#### T5: workshop attendee

**Statement**: A workshop attendee with physical access to their own ESP32 board can dump the board's flash over USB and recover its device private key, certificate and the WiFi PSK, which leads to the attendee holding a broker-trusted device identity and access to the workshop AP, enabling the broker-level attacks

- **Prerequisites**: with physical access to their own ESP32 board
- **Action**: dump the board's flash over USB and recover its device private key, certificate and the WiFi PSK
- **Impact**: the attendee holding a broker-trusted device identity and access to the workshop AP, enabling the broker-level attacks
- **Impacted Assets**: A001, A005
- **Tags**: physical, no-flash-encryption
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Very Likely
- **Residual Risk Rationale**: Accepted for the workshop: boards are handed to attendees and flash encryption/secure boot would complicate re-flashing. The design must not rely on key secrecy (see T006/T008), and M008 is documented as production guidance.
- **Assessment State**: Current

#### T18: internet attacker

**Statement**: A internet attacker with modest bandwidth can flood the NLB on 8883/8884 with TLS handshakes, which leads to exhaustion of the single Fargate broker task, disconnecting every device

- **Prerequisites**: with modest bandwidth
- **Action**: flood the NLB on 8883/8884 with TLS handshakes
- **Impact**: exhaustion of the single Fargate broker task, disconnecting every device
- **Tags**: availability
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Accepted at workshop scale: a single task is enough for tens of devices and the event is short; M018 recorded for production use.
- **Assessment State**: Current

#### T22: harvest-now-decrypt-later adversary

**Statement**: A harvest-now-decrypt-later adversary with a future cryptographically relevant quantum computer can record and later decrypt the classical-only links: bridge TLS 1.2, Builder-Pi HTTPS and the OTA firmware download from S3, which leads to future disclosure of telemetry and of key-bearing firmware images, contrary to the workshop's PQC message

- **Prerequisites**: with a future cryptographically relevant quantum computer
- **Action**: record and later decrypt the classical-only links: bridge TLS 1.2, Builder-Pi HTTPS and the OTA firmware download from S3
- **Impact**: future disclosure of telemetry and of key-bearing firmware images, contrary to the workshop's PQC message
- **Impacted Assets**: A006, A008
- **Tags**: pqc, hndl
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Accepted: short-lived workshop data has no long-term secrecy value; the classical links are part of the teaching story (M023).
- **Assessment State**: Current

## Mitigations

### Identified Mitigations

#### M1: Make every firmware variant refuse any OTA that does not carry a valid signature chained to the baked signer root (fail closed when sigAlg is missing), and have publish-ota sign every push including the classical era.

**Addresses Threats**: T6, T23

#### M2: Verify the OTA download server: fix the wolfSSL chain issue (load the Amazon intermediate/roots and enable WOLFSSL_VERIFY_PEER with SNI and domain check) and only accept URLs whose host is the workshop firmware bucket's regional S3 endpoint.

**Addresses Threats**: T6, T22, T23

#### M3: Add a Mosquitto ACL that confines each device to its own topics: write dt/%u/#, read cmd/%u/#, using the certificate CN as the username (use_identity_as_username is already on).

**Addresses Threats**: T6, T7, T20

#### M4: Replace anonymous dashboard access: add a Cognito user pool (operator and per-event attendee sign-in), keep the unauthenticated role read-only, and allow mutations only to authenticated users scoped to their own device where possible.

**Addresses Threats**: T1, T2, T3, T4, T17, T19

#### M5: Remove updateDevice from browser-callable access: only the telemetry processor writes device state, so scope the guest role's appsync:GraphQL to specific Query fields.

**Addresses Threats**: T2

#### M7: Redact presigned URLs from logged OTA commands and shorten the presign expiry to the minimum the download needs.

**Addresses Threats**: T3

#### M8: Enable ESP32 flash encryption and secure boot v2 so device keys and the PSK cannot be read back over USB (production guidance; accepted for the workshop).

**Addresses Threats**: T5

#### M9: Make device identity revocable: enable CRLs on both Private CAs, revoke the device cert in the deregistrar, and have Mosquitto check the CRL (or an auth plugin that checks the registry).

**Addresses Threats**: T8

#### M10: Lock down the Builder-Pi GUI: require an operator token, add CSRF protection, make /api/config read-only at runtime, and bind the GUI away from the attendee AP interface.

**Addresses Threats**: T9, T10

#### M11: Least-privilege Builder-Pi role: drop bucket-wide firmware read and registry Scan; the builder API returns a short-lived presigned URL for just the device being flashed.

**Addresses Threats**: T11

#### M12: Stop persisting device private keys in stored firmware: expire per-device images after flashing/OTA (lifecycle rule, short noncurrent-version expiry) or move the key to a separately flashed NVS partition that is never uploaded.

**Addresses Threats**: T3, T11, T23

#### M13: Keep the WiFi PSK only in SSM: drop wifiPassword from the registry and pass it to CodeBuild as a PARAMETER_STORE environment variable instead of PLAINTEXT (project env, registrar override and state machine).

**Addresses Threats**: T13

#### M15: Enforce broker hostname checks on devices: add stable DNS names as SANs on both broker certificates and enable wolfSSL domain-name checking.

**Addresses Threats**: T14

#### M18: Improve broker availability: run at least two broker tasks across AZs, set Mosquitto connection limits, and rely on Shield Standard on the NLB.

**Addresses Threats**: T18

#### M19: Make dashboard actions auditable: enable AppSync request logging and record caller identity once sign-in exists.

**Addresses Threats**: T19

#### M20: Narrow the bridge IoT policy to the device topic shapes (dt/esp32-*/telemetry, cmd/esp32-*/*) and remove the shadow wildcards it does not use.

**Addresses Threats**: T20

#### M22: Apply the same curation to the Workshop Studio source zip (package-workshop-src.sh), which currently ships .kiro/, .threatmodel/ and real account IDs.

**Addresses Threats**: T21

#### M23: Track the remaining classical-only links (IoT Core bridge TLS 1.2, OTA download, Builder-Pi HTTPS) and move them to hybrid ML-KEM where AWS endpoints support it; explain them in the workshop as the next migration step.

**Addresses Threats**: T22

### In Progress Mitigations

#### M6: Throttle migrations: reject migrateFirmware while a device already has a migration in progress, cap start-migration concurrency, and set a CodeBuild concurrent-build limit.

**Addresses Threats**: T1, T17

#### M14: Limit the SSM hybrid activation: registration limit of 1, expiry of hours not 7 days, and auto-deregister stale managed instances.

**Addresses Threats**: T12

#### M16: Harden the build supply chain: pin base images by digest, pin oqs-provider/OpenSSL commits and package versions, verify Arduino index signatures, use npm ci with a lockfile, and deploy images by digest with immutable ECR tags.

**Addresses Threats**: T15, T16

### Resolved Mitigations

#### M17: ECR scan on push for all three images.

**Addresses Threats**: T16

#### M21: Fail-closed public-mirror curation: publish-public-mirror.sh strips .kiro/ and .threatmodel/, scrubs account IDs, the PSK and internal URLs, and aborts if any survive.

**Addresses Threats**: T21

#### M24: Mutual TLS required on both broker listeners, each trusting only its own Private CA, TLS 1.3 only.

**Addresses Threats**: T7

#### M25: Telemetry processor only updates devices that were registered (attribute_exists(serialNumber)), so unknown things cannot create dashboard cards.

**Addresses Threats**: T7

#### M26: Signed OTA verification when the command declares sigAlg: signer leaf must chain to the baked signer root (fails closed without a root), then the ECDSA-P256 or ML-DSA-44 signature must verify; signer keys are scrubbed from the build disk after signing.

**Addresses Threats**: T6, T23

#### M27: Broker runs in private subnets with no public IP; its task role can read only the named broker and bridge secrets.

**Addresses Threats**: T20

#### M28: start-migration validates the requested variant against an allow-list before starting a migration.

**Addresses Threats**: T1

## Assumptions

### A011: Deployment

**Description**: Each workshop runs in a throwaway AWS account (Workshop Studio per-event account, or pqc-a/pqc-b test accounts) that is torn down after the event.

- **Impact**: Bounds the lifetime of leaked credentials, device keys and cost abuse to the event window.
- **Rationale**: Stated deployment model in the project steering and DEPLOYMENT.md.

### A012: Data

**Description**: The system holds no customer data, PII beyond optional dashboard identities, or regulated data; telemetry is synthetic device health data.

- **Impact**: Disruption, not data theft, is the main business impact; this drives the risk ratings.
- **Rationale**: Phase 1 data asset review.

### A013: Users

**Description**: Attendees are semi-trusted: many are security practitioners with physical access to a board, the Builder-Pi station and the workshop WiFi.

- **Impact**: Device keys and the WiFi PSK are treated as extractable; controls must not depend on their secrecy.
- **Rationale**: Room setup in the workshop documentation.

### A014: Risk acceptance

**Description**: Risk tolerance: a disrupted or visibly compromised live session at re:Invent is a reputational failure for a security workshop, so fleet-wide or cross-attendee attacks are not acceptable; single-device or workshop-scale availability issues are tolerable.

- **Impact**: Threats whose only outcome is a degraded session at workshop scale may be accepted; anything that lets one attendee or an internet user take over other devices, or that would be copied into production designs, stays open.
- **Rationale**: Business context: Medium criticality, Low financial impact, high reputational sensitivity.

### A015: Scope

**Description**: The public aws-samples repo is reference material; readers may reuse the design.

- **Impact**: Production readers need the documented hardening (flash encryption, secure boot, sign-in, revocation) rather than copying the workshop defaults.
- **Rationale**: Published as an MIT-0 sample.

### A016: Operations

**Description**: Operator Admin credentials are short-lived Isengard credentials obtained via ada, and upstream sources are pinned to release tags.

- **Impact**: Supply-chain and operator-mistake threats are rated Unlikely/Possible rather than Likely.
- **Rationale**: Observed operator workflow and Dockerfile pins.

## Phase Progress

| Phase | Name | Completion |
|---|---|---|
| 1 | Business Context Analysis | 100% ✅ |
| 2 | Architecture Analysis | 100% ✅ |
| 3 | Threat Actor Analysis | 100% ✅ |
| 4 | Trust Boundary Analysis | 100% ✅ |
| 5 | Asset Flow Analysis | 100% ✅ |
| 6 | Threat Identification | 100% ✅ |
| 7 | Mitigation Planning | 100% ✅ |
| 7.5 | Code Validation Analysis | 100% ✅ |
| 8 | Residual Risk Analysis | 100% ✅ |
| 9 | Output Generation and Documentation | 100% ✅ |

---

*This threat model report was generated automatically by the Threat Modeling MCP Server.*
