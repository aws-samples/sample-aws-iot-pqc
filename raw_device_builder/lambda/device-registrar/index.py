"""
Device Registrar Lambda

Called by the builder-pi GUI when an operator clicks "Register & Flash".
Performs the full device registration workflow:
  1. Generates PQC-signed certificate via SSM command on the broker-pi
  2. Creates IoT Thing
  3. Creates per-device IoT policy
  4. Stores metadata in DynamoDB device registry
  5. Triggers CodeBuild to compile device-specific firmware

The certificate is generated on the broker-pi using its PQC CA (OpenSSL +
oqs-provider), so the CA signature is quantum-resistant. The device key is
EC P-256 (for ESP32 performance) but the trust chain is PQC.

Environment variables:
  REGISTRY_TABLE — DynamoDB table for device registry
  FIRMWARE_BUCKET — S3 bucket for compiled firmware
  BUILD_PROJECT — CodeBuild project name
  IOT_ENDPOINT — AWS IoT Core data endpoint
  DEFAULT_WIFI_SSID — Default WiFi SSID
  DEFAULT_BROKER_ENDPOINT — Default Mosquitto broker IP
  BROKER_PI_INSTANCE_ID — SSM instance ID of broker-pi
"""

import boto3
import json
import os
import time
from datetime import datetime, timezone

iot = boto3.client('iot')
ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')
codebuild = boto3.client('codebuild')

TABLE_NAME = os.environ['REGISTRY_TABLE']
FIRMWARE_BUCKET = os.environ['FIRMWARE_BUCKET']
BUILD_PROJECT = os.environ['BUILD_PROJECT']
IOT_ENDPOINT = os.environ['IOT_ENDPOINT']
DEFAULT_WIFI_SSID = os.environ.get('DEFAULT_WIFI_SSID', 'workshop-wifi')
DEFAULT_BROKER = os.environ.get('DEFAULT_BROKER_ENDPOINT', '192.168.1.90')
BROKER_PI_INSTANCE = os.environ.get('BROKER_PI_INSTANCE_ID', '')

table = dynamodb.Table(TABLE_NAME)


def handler(event, context):
    """Register a new device and trigger firmware build."""
    print(f"Event: {json.dumps(event)}")

    # Parse request
    if 'body' in event:
        body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
    else:
        body = event

    serial_number = body.get('serialNumber')
    if not serial_number:
        return api_response(400, {'error': 'Missing serialNumber'})

    # Allow override of defaults from GUI
    wifi_ssid = body.get('wifiSsid', DEFAULT_WIFI_SSID)
    wifi_password = body.get('wifiPassword', '')
    broker_endpoint = body.get('brokerEndpoint', DEFAULT_BROKER)
    firmware_version = body.get('firmwareVersion', '0.1.0')

    thing_name = f'esp32-{serial_number}'
    account_id = context.invoked_function_arn.split(':')[4]
    region = os.environ.get('AWS_REGION', 'us-east-1')

    try:
        # ─── Step 1: Generate PQC certificate via SSM on broker-pi ────────
        print(f"Generating PQC cert for {thing_name} via SSM...")
        cert_data = generate_pqc_cert_via_ssm(serial_number)
        if not cert_data:
            return api_response(500, {
                'error': 'Failed to generate PQC certificate on broker-pi',
                'serialNumber': serial_number
            })

        ca_cert = cert_data['ca_cert']
        device_cert = cert_data['device_cert']
        device_key = cert_data['device_key']
        print(f"PQC cert generated for {thing_name}")

        # ─── Step 2: Create IoT Thing ─────────────────────────────────────
        print(f"Creating IoT Thing: {thing_name}")
        try:
            # IoT Thing attributes only allow [a-zA-Z0-9_.,@/:#=\[\]-]
            registered_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')
            iot.create_thing(
                thingName=thing_name,
                thingTypeName='ESP32-PQC',
                attributePayload={
                    'attributes': {
                        'serialNumber': serial_number,
                        'registeredAt': registered_at,
                        'firmwareVersion': firmware_version,
                        'certType': 'PQC-signed'
                    }
                }
            )
        except iot.exceptions.ResourceAlreadyExistsException:
            print(f"Thing {thing_name} already exists, continuing...")

        # ─── Step 3: Create per-device IoT policy ─────────────────────────
        policy_name = f'pqc-device-{serial_number}'
        print(f"Creating policy: {policy_name}")

        policy_document = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "iot:Connect",
                    "Resource": f"arn:aws:iot:{region}:{account_id}:client/{thing_name}"
                },
                {
                    "Effect": "Allow",
                    "Action": "iot:Publish",
                    "Resource": [
                        f"arn:aws:iot:{region}:{account_id}:topic/dt/{thing_name}/*"
                    ]
                },
                {
                    "Effect": "Allow",
                    "Action": "iot:Subscribe",
                    "Resource": [
                        f"arn:aws:iot:{region}:{account_id}:topicfilter/cmd/{thing_name}/*"
                    ]
                },
                {
                    "Effect": "Allow",
                    "Action": "iot:Receive",
                    "Resource": [
                        f"arn:aws:iot:{region}:{account_id}:topic/cmd/{thing_name}/*"
                    ]
                }
            ]
        }

        try:
            iot.create_policy(
                policyName=policy_name,
                policyDocument=json.dumps(policy_document)
            )
        except iot.exceptions.ResourceAlreadyExistsException:
            print(f"Policy {policy_name} already exists, continuing...")

        # ─── Step 4: Store in DynamoDB ────────────────────────────────────
        table.put_item(Item={
            'serialNumber': serial_number,
            'thingName': thing_name,
            'policyName': policy_name,
            'certType': 'PQC-signed',
            'firmwareVersion': firmware_version,
            'status': 'registered',
            'registeredAt': datetime.now(timezone.utc).isoformat(),
            'wifiSsid': wifi_ssid,
            'brokerEndpoint': broker_endpoint
        })

        # ─── Step 5: Trigger CodeBuild ────────────────────────────────────
        print(f"Starting firmware build for {thing_name}...")
        build_response = codebuild.start_build(
            projectName=BUILD_PROJECT,
            environmentVariablesOverride=[
                {'name': 'DEVICE_SERIAL', 'value': serial_number, 'type': 'PLAINTEXT'},
                {'name': 'FIRMWARE_VERSION', 'value': firmware_version, 'type': 'PLAINTEXT'},
                {'name': 'WIFI_SSID', 'value': wifi_ssid, 'type': 'PLAINTEXT'},
                {'name': 'WIFI_PASSWORD', 'value': wifi_password, 'type': 'PLAINTEXT'},
                {'name': 'BROKER_ENDPOINT', 'value': broker_endpoint, 'type': 'PLAINTEXT'},
                {'name': 'IOT_ENDPOINT', 'value': IOT_ENDPOINT, 'type': 'PLAINTEXT'},
                {'name': 'CA_CERT', 'value': ca_cert, 'type': 'PLAINTEXT'},
                {'name': 'DEVICE_CERT', 'value': device_cert, 'type': 'PLAINTEXT'},
                {'name': 'DEVICE_KEY', 'value': device_key, 'type': 'PLAINTEXT'},
            ]
        )
        build_id = build_response['build']['id']

        return api_response(200, {
            'success': True,
            'thingName': thing_name,
            'serialNumber': serial_number,
            'certType': 'PQC-signed',
            'buildId': build_id,
            'firmwarePath': f's3://{FIRMWARE_BUCKET}/{serial_number}/{firmware_version}/firmware.bin',
            'message': f'Device {thing_name} registered with PQC cert. Build: {build_id}'
        })

    except Exception as e:
        print(f"Error registering device: {e}")
        import traceback
        traceback.print_exc()
        return api_response(500, {
            'success': False,
            'error': str(e),
            'serialNumber': serial_number
        })


def generate_pqc_cert_via_ssm(serial_number):
    """Generate a PQC-signed device certificate on the broker-pi via SSM."""
    if not BROKER_PI_INSTANCE:
        print("ERROR: BROKER_PI_INSTANCE_ID not configured")
        return None

    try:
        # Send command to broker-pi to generate cert
        cmd_response = ssm.send_command(
            InstanceIds=[BROKER_PI_INSTANCE],
            DocumentName='AWS-RunShellScript',
            Parameters={
                'commands': [
                    f'/opt/mosquitto-pqc/etc/mosquitto/certs/generate-device-cert.sh {serial_number}'
                ]
            },
            TimeoutSeconds=30
        )
        command_id = cmd_response['Command']['CommandId']

        # Poll for completion (up to 20 seconds)
        for _ in range(20):
            time.sleep(1)
            result = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=BROKER_PI_INSTANCE
            )
            if result['Status'] in ('Success', 'Failed', 'TimedOut', 'Cancelled'):
                break

        if result['Status'] != 'Success':
            print(f"SSM command failed: {result.get('StandardErrorContent', 'unknown error')}")
            return None

        # Parse JSON output from the script
        output = result['StandardOutputContent'].strip()
        cert_data = json.loads(output)
        return cert_data

    except Exception as e:
        print(f"SSM cert generation failed: {e}")
        return None


def api_response(status_code, body):
    """Format response for API Gateway proxy integration."""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body)
    }
