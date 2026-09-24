"""
Device Registrar Lambda

Called by the builder-pi GUI when an operator clicks "Register & Flash".
Performs the device registration workflow:
  1. Creates IoT Thing
  2. Creates per-device IoT policy
  3. Stores metadata in DynamoDB device registry
  4. Triggers CodeBuild to compile device-specific firmware (classical TLS)

The device certificate is issued INSIDE the CodeBuild firmware build
(buildspec.yml), which generates the keypair + CSR and issues from the correct
AWS Private CA per FIRMWARE_VARIANT — so this Lambda no longer needs openssl or
a CA ARN, and the private key never flows through Lambda. Firmware 0 uses
classical TLS — PQC migration happens via OTA (Firmware 1+).

Environment variables:
  REGISTRY_TABLE — DynamoDB table for device registry
  FIRMWARE_BUCKET — S3 bucket for compiled firmware
  BUILD_PROJECT — CodeBuild project name
  IOT_ENDPOINT — AWS IoT Core data endpoint
  DEFAULT_WIFI_SSID — Default WiFi SSID
  DEFAULT_BROKER_ENDPOINT — Default Mosquitto broker IP/NLB endpoint
"""

import boto3
import json
import os
from datetime import datetime, timezone

iot = boto3.client('iot')
dynamodb = boto3.resource('dynamodb')
codebuild = boto3.client('codebuild')
ssm = boto3.client('ssm')

TABLE_NAME = os.environ['REGISTRY_TABLE']
FIRMWARE_BUCKET = os.environ['FIRMWARE_BUCKET']
BUILD_PROJECT = os.environ['BUILD_PROJECT']
IOT_ENDPOINT = os.environ['IOT_ENDPOINT']
DEFAULT_WIFI_SSID = os.environ.get('DEFAULT_WIFI_SSID', 'workshop-wifi')
DEFAULT_WIFI_PASSWORD = os.environ.get('DEFAULT_WIFI_PASSWORD', '')
DEFAULT_BROKER = os.environ.get('DEFAULT_BROKER_ENDPOINT', '192.168.1.90')
# Certificate issuance moved into the CodeBuild firmware build (buildspec.yml),
# so this Lambda no longer needs a CA ARN or openssl.
# SSM is the authoritative live source of truth for the workshop WiFi and the
# broker endpoint. The deploy publishes /pqc-workshop/wifi/ssid (String) +
# /password (SecureString) and /pqc-workshop/broker/endpoint (String); reading
# them here means they can be changed with a single put-parameter (every new
# build picks it up) without a redeploy.
WIFI_SSM_PREFIX = os.environ.get('WIFI_SSM_PREFIX', '/pqc-workshop/wifi')
BROKER_SSM_PARAM = os.environ.get('BROKER_SSM_PARAM', '/pqc-workshop/broker/endpoint')

table = dynamodb.Table(TABLE_NAME)


def _get_ssm(name):
    """Read one SSM param (decrypted); return None if missing/unreadable so the
    caller can fall back. Never raises — a WiFi lookup must not fail a build."""
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']
    except Exception as e:  # ParameterNotFound, AccessDenied, throttling, etc.
        print(f"SSM read {name} failed ({e}); falling back")
        return None


def wifi_from_ssm():
    """Return (ssid, password) from SSM, each None if unavailable."""
    ssid = _get_ssm(f'{WIFI_SSM_PREFIX}/ssid')
    password = _get_ssm(f'{WIFI_SSM_PREFIX}/password')
    return ssid, password


def broker_from_ssm():
    """Return the broker endpoint from SSM, or None if unavailable."""
    return _get_ssm(BROKER_SSM_PARAM)


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

    # WiFi resolution order: explicit GUI override -> SSM (live source of truth)
    # -> env-var fallback. SSM lets the workshop WiFi be rotated without a
    # redeploy; the env vars are only a last resort if SSM is unset/unreadable.
    ssm_ssid, ssm_password = wifi_from_ssm()
    wifi_ssid = body.get('wifiSsid') or ssm_ssid or DEFAULT_WIFI_SSID
    wifi_password = body.get('wifiPassword') or ssm_password or DEFAULT_WIFI_PASSWORD
    # Broker endpoint resolution: explicit override -> SSM (live source of
    # truth, published from the broker stack output) -> env fallback.
    broker_endpoint = body.get('brokerEndpoint') or broker_from_ssm() or DEFAULT_BROKER
    firmware_version = body.get('firmwareVersion', '0.1.0')

    thing_name = f'esp32-{serial_number}'
    account_id = context.invoked_function_arn.split(':')[4]
    region = os.environ.get('AWS_REGION', 'us-east-1')

    try:
        # ─── Step 1: Create IoT Thing ─────────────────────────────────────
        # NOTE: the device certificate is NOT issued here anymore. It is issued
        # inside the CodeBuild firmware build (see buildspec.yml), which
        # generates the keypair + CSR and issues from the correct Private CA per
        # FIRMWARE_VARIANT — so the private key never flows through this Lambda.
        # The device cert authenticates to the self-managed Mosquitto broker, not
        # to IoT Core, so nothing here needs the cert in hand (it is never
        # attached as an IoT Thing principal).
        print(f"Creating IoT Thing: {thing_name}")
        try:
            registered_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')
            iot.create_thing(
                thingName=thing_name,
                # Must match the thing type created by the deployment
                # (device-provisioning-stack.yaml AWS::IoT::ThingType) and used
                # by the auto-register Lambda: 'esp32-pqc-device'. The old value
                # 'ESP32-PQC' was never created -> CreateThing ResourceNotFound.
                thingTypeName='esp32-pqc-device',
                attributePayload={
                    'attributes': {
                        'serialNumber': serial_number,
                        'registeredAt': registered_at,
                        'firmwareVersion': firmware_version,
                        'certType': 'PrivateCA-ECDSA'
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
            'certType': 'PrivateCA-ECDSA',
            'firmwareVersion': firmware_version,
            'status': 'registered',
            'registeredAt': datetime.now(timezone.utc).isoformat(),
            'wifiSsid': wifi_ssid,
            # Persist the WiFi password so the PQC migration (prepare-build)
            # rebuilds firmware with the SAME credentials the device was flashed
            # with. Without this the registry entry (recreated from telemetry,
            # which does not carry the password) lacks wifiPassword and migration
            # falls back to the deploy default — a mismatch strands the device on
            # a WPA2 4-way-handshake timeout after it reboots into PQC firmware.
            'wifiPassword': wifi_password,
            'brokerEndpoint': broker_endpoint
        })

        # ─── Step 5: Trigger CodeBuild (classical firmware) ───────────────
        # The build issues the classical device cert itself (FIRMWARE_VARIANT
        # defaults to 'classical' in the build project); no cert material is
        # passed from here. BROKER_PORT is left unset so the build applies the
        # variant default (8883 for classical).
        print(f"Starting classical firmware build for {thing_name}...")
        build_response = codebuild.start_build(
            projectName=BUILD_PROJECT,
            environmentVariablesOverride=[
                {'name': 'DEVICE_SERIAL', 'value': serial_number, 'type': 'PLAINTEXT'},
                {'name': 'FIRMWARE_VERSION', 'value': firmware_version, 'type': 'PLAINTEXT'},
                {'name': 'FIRMWARE_VARIANT', 'value': 'classical', 'type': 'PLAINTEXT'},
                {'name': 'WIFI_SSID', 'value': wifi_ssid, 'type': 'PLAINTEXT'},
                {'name': 'WIFI_PASSWORD', 'value': wifi_password, 'type': 'PLAINTEXT'},
                {'name': 'BROKER_ENDPOINT', 'value': broker_endpoint, 'type': 'PLAINTEXT'},
                {'name': 'IOT_ENDPOINT', 'value': IOT_ENDPOINT, 'type': 'PLAINTEXT'},
            ]
        )
        build_id = build_response['build']['id']

        return api_response(200, {
            'success': True,
            'thingName': thing_name,
            'serialNumber': serial_number,
            'certType': 'PrivateCA-ECDSA',
            'buildId': build_id,
            'firmwareBucket': FIRMWARE_BUCKET,
            'firmwareKey': f'{serial_number}/{firmware_version}/firmware.bin',
            'message': f'Device {thing_name} registered. Classical firmware build: {build_id}'
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
