"""
Device Registrar Lambda

Called by the builder-pi GUI when an operator clicks "Register & Flash".
Performs the full device registration workflow:
  1. Generates EC P-256 key + CSR, issues certificate from AWS Private CA
  2. Gets the CA certificate from Private CA
  3. Creates IoT Thing
  4. Creates per-device IoT policy
  5. Stores metadata in DynamoDB device registry
  6. Triggers CodeBuild to compile device-specific firmware (classical TLS)

Certificates are issued by the same Private CA that signed the broker cert,
ensuring the device and broker share a common trust root. Firmware 0 uses
classical TLS — PQC migration happens via OTA (Firmware 1+).

Environment variables:
  REGISTRY_TABLE — DynamoDB table for device registry
  FIRMWARE_BUCKET — S3 bucket for compiled firmware
  BUILD_PROJECT — CodeBuild project name
  IOT_ENDPOINT — AWS IoT Core data endpoint
  DEFAULT_WIFI_SSID — Default WiFi SSID
  DEFAULT_BROKER_ENDPOINT — Default Mosquitto broker IP/NLB endpoint
  PRIVATE_CA_ARN — AWS Private CA ARN (shared with broker-container stack)
"""

import boto3
import json
import os
import time
import subprocess
import tempfile
from datetime import datetime, timezone

iot = boto3.client('iot')
acm_pca = boto3.client('acm-pca')
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
PRIVATE_CA_ARN = os.environ['PRIVATE_CA_ARN']
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
        # ─── Step 1: Generate device certificate from Private CA ──────────
        print(f"Generating device cert for {thing_name} from Private CA...")
        cert_data = issue_device_certificate(serial_number, thing_name)
        if not cert_data:
            return api_response(500, {
                'error': 'Failed to issue device certificate from Private CA',
                'serialNumber': serial_number
            })

        ca_cert = cert_data['ca_cert']
        device_cert = cert_data['device_cert']
        device_key = cert_data['device_key']
        print(f"Certificate issued for {thing_name}")

        # ─── Step 2: Create IoT Thing ─────────────────────────────────────
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
        print(f"Starting classical firmware build for {thing_name}...")
        build_response = codebuild.start_build(
            projectName=BUILD_PROJECT,
            environmentVariablesOverride=[
                {'name': 'DEVICE_SERIAL', 'value': serial_number, 'type': 'PLAINTEXT'},
                {'name': 'FIRMWARE_VERSION', 'value': firmware_version, 'type': 'PLAINTEXT'},
                {'name': 'WIFI_SSID', 'value': wifi_ssid, 'type': 'PLAINTEXT'},
                {'name': 'WIFI_PASSWORD', 'value': wifi_password, 'type': 'PLAINTEXT'},
                {'name': 'BROKER_ENDPOINT', 'value': broker_endpoint, 'type': 'PLAINTEXT'},
                {'name': 'BROKER_PORT', 'value': '8883', 'type': 'PLAINTEXT'},
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


def issue_device_certificate(serial_number, thing_name):
    """
    Generate an EC P-256 key and CSR, issue a certificate from the Private CA.
    Returns dict with ca_cert, device_cert, device_key (all PEM strings).
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            key_file = f'{tmp}/device.key'
            csr_file = f'{tmp}/device.csr'

            # Generate EC P-256 private key
            subprocess.run([
                'openssl', 'ecparam', '-genkey', '-name', 'prime256v1',
                '-noout', '-out', key_file
            ], check=True, capture_output=True)

            # Generate CSR
            subject = f'/CN={thing_name}/O=AWS PQC Workshop/OU=IoT Device/C=US'
            subprocess.run([
                'openssl', 'req', '-new', '-key', key_file,
                '-out', csr_file, '-subj', subject
            ], check=True, capture_output=True)

            with open(key_file) as f:
                device_key = f.read()
            with open(csr_file) as f:
                csr_pem = f.read()

        # Issue certificate from Private CA
        issue_resp = acm_pca.issue_certificate(
            CertificateAuthorityArn=PRIVATE_CA_ARN,
            Csr=csr_pem.encode(),
            SigningAlgorithm='SHA256WITHECDSA',
            Validity={'Type': 'DAYS', 'Value': 365},
            IdempotencyToken=f'dev-{serial_number[:16]}'
        )
        cert_arn = issue_resp['CertificateArn']
        print(f"Certificate issued: {cert_arn}")

        # Wait for certificate to be available (typically < 3 seconds)
        time.sleep(3)
        for attempt in range(10):
            try:
                cert_resp = acm_pca.get_certificate(
                    CertificateAuthorityArn=PRIVATE_CA_ARN,
                    CertificateArn=cert_arn
                )
                break
            except acm_pca.exceptions.RequestInProgressException:
                print(f"  Certificate pending (attempt {attempt + 1})...")
                time.sleep(2)
        else:
            raise Exception('Certificate issuance timed out after 20s')

        device_cert = cert_resp['Certificate']

        # Get the CA certificate (for the trust chain)
        ca_resp = acm_pca.get_certificate_authority_certificate(
            CertificateAuthorityArn=PRIVATE_CA_ARN
        )
        ca_cert = ca_resp['Certificate']

        return {
            'ca_cert': ca_cert,
            'device_cert': device_cert,
            'device_key': device_key
        }

    except Exception as e:
        print(f"Certificate generation failed: {e}")
        import traceback
        traceback.print_exc()
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
