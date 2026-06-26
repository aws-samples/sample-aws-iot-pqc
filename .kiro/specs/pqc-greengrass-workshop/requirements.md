# Requirements Document

## Introduction

This document specifies the requirements for a re:Invent workshop that guides participants through enabling Post-Quantum Cryptography (PQC) on an AWS IoT Greengrass v2 core device, performing an over-the-air firmware migration on an ESP32 IoT device from classical TLS to PQC TLS, and reconnecting the ESP32 to AWS IoT Core via the Greengrass core device acting as a local MQTT broker/proxy.

The workshop demonstrates a realistic PQC migration path for constrained IoT devices in an edge computing topology: ESP32 → Greengrass Core (local MQTT broker) → AWS IoT Core (cloud).

## Glossary

- **Workshop_System**: The complete set of infrastructure, firmware, configuration, and documentation that participants deploy and interact with during the workshop
- **Greengrass_Core**: An AWS IoT Greengrass v2 core device (EC2 instance or local machine) that runs the Greengrass nucleus, MQTT broker component, and PQC-enabled connection to AWS IoT Core
- **ESP32_Device**: The ESP32 microcontroller running either the v1 classical firmware or the v2 PQC firmware, connecting to MQTT via the Greengrass Core
- **Participant**: A re:Invent workshop attendee following the step-by-step instructions
- **OTA_Migration**: The process of switching the ESP32 boot partition from OTA_0 (classical firmware v1.0) to OTA_1 (PQC firmware v2.0) via an MQTT command
- **Local_MQTT_Broker**: The Greengrass Moquette MQTT broker component that accepts connections from local IoT devices and bridges messages to AWS IoT Core
- **PQC_Connection**: A TLS 1.3 connection using hybrid key exchange combining classical ECDHE with post-quantum ML-KEM (Kyber)
- **Classical_Connection**: A TLS 1.2 connection using standard ECDHE key exchange without post-quantum algorithms
- **CloudFormation_Template**: An AWS CloudFormation YAML template that provisions all required AWS resources in a single deployment
- **Workshop_Guide**: The step-by-step markdown document that participants follow to complete the workshop

## Requirements

### Requirement 1: Greengrass Core Device Provisioning

**User Story:** As a Participant, I want to deploy a Greengrass v2 core device with a single CloudFormation command, so that I can quickly set up the edge infrastructure without manual configuration.

#### Acceptance Criteria

1. WHEN the Participant deploys the CloudFormation template, THE CloudFormation_Template SHALL create an IoT Greengrass v2 core device with an IAM role, Thing, certificate, and IoT policy
2. WHEN the Greengrass core device starts, THE Greengrass_Core SHALL establish a PQC_Connection to AWS IoT Core using TLS 1.3 with hybrid Kyber/ML-KEM key exchange
3. THE CloudFormation_Template SHALL output the Greengrass core device IP address, Thing name, and connection instructions for the ESP32
4. IF the CloudFormation deployment fails, THEN THE CloudFormation_Template SHALL provide a descriptive error message identifying the failing resource

### Requirement 2: Greengrass Local MQTT Broker Configuration

**User Story:** As a Participant, I want the Greengrass core device to act as a local MQTT broker, so that the ESP32 can connect to it instead of directly to AWS IoT Core.

#### Acceptance Criteria

1. WHEN the Greengrass_Core starts, THE Local_MQTT_Broker SHALL accept MQTT connections from local devices on port 8883 using mutual TLS authentication
2. THE Local_MQTT_Broker SHALL bridge messages published by the ESP32_Device on local topics to the corresponding topics on AWS IoT Core
3. THE Local_MQTT_Broker SHALL forward messages received from AWS IoT Core command topics to the subscribed ESP32_Device
4. WHEN the ESP32_Device connects to the Local_MQTT_Broker, THE Greengrass_Core SHALL authenticate the device using its X.509 client certificate
5. THE Greengrass_Core SHALL provide a Greengrass component configuration that enables the MQTT broker with client device authentication

### Requirement 3: ESP32 Firmware Modification for Greengrass Connectivity

**User Story:** As a Participant, I want the ESP32 firmware to connect to the Greengrass core device instead of directly to AWS IoT Core, so that the device communicates through the edge gateway.

#### Acceptance Criteria

1. THE ESP32_Device SHALL connect to the Local_MQTT_Broker on the Greengrass_Core using the Greengrass core device IP address and port 8883
2. WHEN the ESP32_Device connects via the Greengrass_Core, THE ESP32_Device SHALL authenticate using the same X.509 device certificate used for direct AWS IoT Core connections
3. THE ESP32_Device SHALL publish telemetry messages to the same topic structure (dt/<thingName>/telemetry) regardless of whether it connects directly or via Greengrass
4. THE ESP32_Device SHALL subscribe to command topics (cmd/<thingName>/+) through the Local_MQTT_Broker and receive commands forwarded from AWS IoT Core
5. WHEN the connection to the Local_MQTT_Broker is lost, THE ESP32_Device SHALL attempt reconnection using exponential backoff with jitter
6. THE Workshop_System SHALL provide a secrets.h configuration that allows the Participant to set the Greengrass core IP address as the MQTT broker endpoint

### Requirement 4: OTA Firmware Migration from Classical to PQC

**User Story:** As a Participant, I want to trigger the firmware migration from classical TLS to PQC TLS via an MQTT command through Greengrass, so that I can observe the device upgrading its cryptographic capabilities.

#### Acceptance Criteria

1. WHEN the Participant publishes {"action":"migrate-pqc"} to cmd/<thingName>/ota via the AWS IoT Core MQTT test client, THE Local_MQTT_Broker SHALL forward the command to the ESP32_Device
2. WHEN the ESP32_Device receives the migrate-pqc command, THE ESP32_Device SHALL switch the boot partition from OTA_0 to OTA_1 and reboot within 5 seconds
3. WHEN the ESP32_Device reboots with the PQC firmware, THE ESP32_Device SHALL establish a PQC_Connection to the Local_MQTT_Broker using TLS 1.3 with hybrid Kyber/ML-KEM key exchange
4. WHEN the PQC firmware connects successfully, THE ESP32_Device SHALL publish a lifecycle event indicating firmware_version "2.0.0" and pqc_algorithm "P521_KYBER_LEVEL5"
5. IF the OTA_1 partition does not contain valid firmware, THEN THE ESP32_Device SHALL log an error message and remain on the current firmware

### Requirement 5: Workshop Narrative and Step-by-Step Guide

**User Story:** As a Participant, I want a clear workshop guide with a narrative story, so that I understand the security motivation and can follow each step without prior Greengrass experience.

#### Acceptance Criteria

1. THE Workshop_Guide SHALL contain an introduction explaining the quantum computing threat, harvest-now-decrypt-later attacks, and why PQC migration matters for IoT
2. THE Workshop_Guide SHALL organize steps into three acts: (1) Deploy Greengrass with PQC, (2) Migrate ESP32 firmware from classical to PQC, (3) Verify end-to-end PQC connectivity
3. THE Workshop_Guide SHALL include verification checkpoints after each major step where the Participant confirms expected serial output or AWS Console state
4. WHEN a step requires the Participant to run a CLI command, THE Workshop_Guide SHALL provide the exact command with placeholder values clearly marked
5. THE Workshop_Guide SHALL include a troubleshooting section addressing common failures: WiFi connectivity, certificate mismatch, Greengrass deployment errors, and OTA partition issues
6. THE Workshop_Guide SHALL specify an estimated completion time of 45-60 minutes for the entire workshop

### Requirement 6: End-to-End PQC Verification

**User Story:** As a Participant, I want to verify that the entire communication path uses post-quantum cryptography after migration, so that I can confirm the workshop objective is achieved.

#### Acceptance Criteria

1. WHEN the ESP32_Device connects to the Local_MQTT_Broker after PQC migration, THE ESP32_Device SHALL report the TLS cipher suite and key exchange algorithm in its serial output
2. THE Workshop_Guide SHALL instruct the Participant to verify PQC connectivity by checking the telemetry messages in the AWS IoT Core MQTT test client for pqc_algorithm field value "P521_KYBER_LEVEL5"
3. WHEN the Greengrass_Core connects to AWS IoT Core, THE Greengrass_Core SHALL use a PQC-enabled TLS connection and log the negotiated cipher suite
4. THE Workshop_System SHALL provide a comparison view showing telemetry before migration (pqc_algorithm: "none", tls_version: "TLS 1.2") and after migration (pqc_algorithm: "P521_KYBER_LEVEL5", tls_version: "TLS 1.3")

### Requirement 7: CloudFormation Infrastructure for Workshop

**User Story:** As a Participant, I want all AWS resources provisioned via a single CloudFormation template, so that setup is repeatable and cleanup is simple.

#### Acceptance Criteria

1. THE CloudFormation_Template SHALL create all required resources: IoT Thing for ESP32, IoT Thing for Greengrass core, certificates, IoT policies, Greengrass core definition, IAM roles, and CloudWatch log groups
2. THE CloudFormation_Template SHALL accept parameters for ThingName, GreengrassCoreThingName, and AWS region
3. WHEN the Participant deletes the CloudFormation stack, THE CloudFormation_Template SHALL remove all created resources including IoT Things, certificates, policies, and log groups
4. THE CloudFormation_Template SHALL store device credentials in AWS Secrets Manager so the Participant can retrieve them after deployment
5. THE CloudFormation_Template SHALL configure IoT policies that allow the ESP32 to connect via both direct and Greengrass-brokered paths during the migration transition

### Requirement 8: Greengrass PQC Component Deployment

**User Story:** As a Participant, I want to deploy a Greengrass component that enables PQC on the core device's connection to AWS IoT Core, so that the entire path from device to cloud uses quantum-resistant cryptography.

#### Acceptance Criteria

1. THE Workshop_System SHALL provide a Greengrass component recipe that configures the Greengrass nucleus to use TLS 1.3 with PQC for its connection to AWS IoT Core
2. WHEN the PQC component is deployed, THE Greengrass_Core SHALL negotiate a hybrid key exchange (ECDHE + ML-KEM) on its next connection to AWS IoT Core
3. THE Workshop_Guide SHALL include steps for the Participant to deploy the PQC component using the Greengrass CLI or AWS Console
4. WHEN the PQC component deployment completes, THE Greengrass_Core SHALL log a message confirming the PQC-enabled connection is active
