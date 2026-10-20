# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
PQC Upgrader Lambda

Triggered when an attendee clicks "Upgrade to PQC" on the dashboard.
Performs:
  1. Looks up device cert material from DynamoDB registry
  2. Triggers CodeBuild to compile PQC firmware with device-specific certs
  3. Waits for build to complete (up to 5 minutes)
  4. Generates presigned S3 URL for the firmware binary
  5. Publishes OTA command to cmd/<thing>/ota via IoT Core

The device downloads the PQC firmware, writes to ota_1, and reboots.

Environment variables:
  REGISTRY_TABLE   — DynamoDB device registry table
  FIRMWARE_BUCKET  — S3 bucket for firmware
  BUILD_PROJECT    — CodeBuild project name
  IOT_ENDPOINT     — IoT Core data endpoint
  PRIVATE_CA_ARN   — Private CA ARN (for cert retrieval)
"""

import boto3
from botocore.config import Config
import hashlib
import json
import os
import time

dynamodb = boto3.resource('dynamodb')
codebuild = boto3.client('codebuild')
# Use a region-specific S3 endpoint with SigV4 so presigned URLs point at
# s3.<region>.amazonaws.com. The global s3.amazonaws.com endpoint can return a
# 307 redirect on GET for a regional bucket, which the device's minimal HTTP
# client does not follow (it saw Content-Length: 0 from the redirect).
_S3_REGION = os.environ.get('AWS_REGION', 'us-east-1')
s3 = boto3.client(
    's3',
    region_name=_S3_REGION,
    endpoint_url=f'https://s3.{_S3_REGION}.amazonaws.com',
    config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'})
)
iot_data = boto3.client('iot-data', endpoint_url=f"https://{os.environ['IOT_ENDPOINT']}")

TABLE_NAME = os.environ['REGISTRY_TABLE']
FIRMWARE_BUCKET = os.environ['FIRMWARE_BUCKET']
BUILD_PROJECT = os.environ['BUILD_PROJECT']
PQC_VERSION = '2.0.0-pqc'  # canonical pqc version (retired lambda; kept in sync)
PRESIGNED_URL_EXPIRY = 300  # 5 minutes

table = dynamodb.Table(TABLE_NAME)


def handler(event, context):
    """Build PQC firmware and push OTA to device."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get('thingName')
    if not thing_name:
        return {'success': False, 'message': 'Missing thingName', 'thingName': ''}

    # Derive serial number
    serial_number = thing_name.replace('esp32-', '') if thing_name.startswith('esp32-') else thing_name

    try:
        # Step 1: Look up device in registry
        print(f"Looking up device: {serial_number}")
        response = table.get_item(Key={'serialNumber': serial_number})
        if 'Item' not in response:
            return {'success': False, 'message': f'Device {thing_name} not found in registry', 'thingName': thing_name}

        device = response['Item']

        # Step 2: Check if PQC firmware already exists in S3
        s3_key = f'{serial_number}/{PQC_VERSION}/firmware.bin'
        firmware_exists = check_firmware_exists(s3_key)

        if not firmware_exists:
            # Need to build — but we need the device certs.
            # For the workshop, devices share the same cert from registration.
            # The PQC firmware uses the same certs as classical (same CA trust chain).
            # Trigger CodeBuild with the device's broker endpoint and certs.
            print(f"PQC firmware not found, triggering build...")

            # Get device certs from the original build (stored in S3 from registration)
            # The certs are the same for both classical and PQC — only the TLS config changes
            build_id = trigger_pqc_build(serial_number, device)

            if not build_id:
                return {'success': False, 'message': 'Failed to trigger PQC build', 'thingName': thing_name}

            # Wait for build (up to 5 min)
            print(f"Waiting for build: {build_id}")
            if not wait_for_build(build_id, timeout=300):
                return {'success': False, 'message': f'PQC build failed or timed out: {build_id}', 'thingName': thing_name}

            print("PQC build complete!")

        # Step 3: Generate presigned URL and get file metadata
        firmware_meta = get_firmware_metadata(s3_key)
        if not firmware_meta:
            return {'success': False, 'message': 'PQC firmware not found in S3 after build', 'thingName': thing_name}

        presigned_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': FIRMWARE_BUCKET, 'Key': s3_key},
            ExpiresIn=PRESIGNED_URL_EXPIRY
        )

        # Step 4: Publish OTA command to device
        ota_payload = {
            'action': 'ota-update',
            'url': presigned_url,
            'version': PQC_VERSION,
            'size': firmware_meta['size'],
            'sha256': firmware_meta['sha256'],
            'expiresIn': PRESIGNED_URL_EXPIRY
        }

        topic = f'cmd/{thing_name}/ota'
        iot_data.publish(topic=topic, qos=1, payload=json.dumps(ota_payload))
        print(f"OTA command published to {topic}")

        # Update registry
        table.update_item(
            Key={'serialNumber': serial_number},
            UpdateExpression='SET targetFirmwareVersion = :v, #s = :status',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':v': PQC_VERSION, ':status': 'ota-pending'}
        )

        return {
            'success': True,
            'message': f'PQC firmware v{PQC_VERSION} pushed to {thing_name}. Device will download and reboot.',
            'thingName': thing_name
        }

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'message': str(e), 'thingName': thing_name}


def trigger_pqc_build(serial_number, device):
    """Trigger CodeBuild for PQC firmware variant."""
    try:
        # The PQC firmware needs the same certs as classical.
        # Retrieve them from the original firmware's secrets.h in S3,
        # or re-issue from Private CA. For simplicity, we pass the
        # same env vars that the classical build used.
        # The key difference: we compile aws_iot_pqc_esp32.ino instead of classical.

        broker_endpoint = device.get('brokerEndpoint', os.environ.get('DEFAULT_BROKER_ENDPOINT', ''))
        # WiFi credentials: the registry entry is recreated from telemetry, which
        # does not include the WiFi password, so device.get('wifiPassword') is
        # usually empty. Fall back to the workshop defaults from env vars so the
        # PQC firmware can still associate to WiFi after the OTA reboot.
        wifi_ssid = device.get('wifiSsid') or os.environ.get('DEFAULT_WIFI_SSID', '')
        wifi_password = device.get('wifiPassword') or os.environ.get('DEFAULT_WIFI_PASSWORD', '')

        # Get certs from Private CA (same cert works for PQC — the CA is the same)
        acm_pca = boto3.client('acm-pca')
        ca_arn = os.environ.get('PRIVATE_CA_ARN', '')

        # Get CA cert
        ca_resp = acm_pca.get_certificate_authority_certificate(CertificateAuthorityArn=ca_arn)
        ca_cert = ca_resp['Certificate']

        # For PQC build, reuse the existing device cert (same identity, different firmware)
        # The cert/key were generated at registration and are already in the device.
        # We need them for the PQC build too. They should be in S3 from the original build.
        # Check if we have metadata that includes the cert info.
        # If not, re-issue a cert for the PQC build.
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            key_file = f'{tmp}/device.key'
            csr_file = f'{tmp}/device.csr'

            subprocess.run(['openssl', 'ecparam', '-genkey', '-name', 'prime256v1', '-noout', '-out', key_file],
                          check=True, capture_output=True)
            subprocess.run(['openssl', 'req', '-new', '-key', key_file, '-out', csr_file, '-subj',
                          f'/CN=esp32-{serial_number}/O=AWS PQC Workshop/OU=IoT Device PQC/C=US'],
                          check=True, capture_output=True)

            with open(key_file, encoding="utf-8") as f:
                device_key = f.read()
            with open(csr_file, encoding="utf-8") as f:
                csr_pem = f.read()

        # Issue cert from Private CA
        issue_resp = acm_pca.issue_certificate(
            CertificateAuthorityArn=ca_arn,
            Csr=csr_pem.encode(),
            SigningAlgorithm='SHA256WITHECDSA',
            Validity={'Type': 'DAYS', 'Value': 365},
            IdempotencyToken=f'pqc-{serial_number[:12]}'
        )
        cert_arn = issue_resp['CertificateArn']

        time.sleep(3)  # nosemgrep: arbitrary-sleep
        for _ in range(10):
            try:
                cert_resp = acm_pca.get_certificate(CertificateAuthorityArn=ca_arn, CertificateArn=cert_arn)
                break
            except acm_pca.exceptions.RequestInProgressException:
                time.sleep(2)  # nosemgrep: arbitrary-sleep
        else:
            raise Exception('PQC cert issuance timed out')

        device_cert = cert_resp['Certificate']

        # Trigger CodeBuild with PQC firmware variant
        build_resp = codebuild.start_build(
            projectName=BUILD_PROJECT,
            environmentVariablesOverride=[
                {'name': 'DEVICE_SERIAL', 'value': serial_number, 'type': 'PLAINTEXT'},
                {'name': 'FIRMWARE_VERSION', 'value': PQC_VERSION, 'type': 'PLAINTEXT'},
                {'name': 'FIRMWARE_VARIANT', 'value': 'pqc', 'type': 'PLAINTEXT'},
                {'name': 'WIFI_SSID', 'value': wifi_ssid, 'type': 'PLAINTEXT'},
                {'name': 'WIFI_PASSWORD', 'value': wifi_password, 'type': 'PLAINTEXT'},
                {'name': 'BROKER_ENDPOINT', 'value': broker_endpoint, 'type': 'PLAINTEXT'},
                {'name': 'BROKER_PORT', 'value': '8883', 'type': 'PLAINTEXT'},
                {'name': 'IOT_ENDPOINT', 'value': os.environ['IOT_ENDPOINT'], 'type': 'PLAINTEXT'},
                {'name': 'CA_CERT', 'value': ca_cert, 'type': 'PLAINTEXT'},
                {'name': 'DEVICE_CERT', 'value': device_cert, 'type': 'PLAINTEXT'},
                {'name': 'DEVICE_KEY', 'value': device_key, 'type': 'PLAINTEXT'},
            ]
        )
        return build_resp['build']['id']

    except Exception as e:
        print(f"Build trigger failed: {e}")
        return None


def wait_for_build(build_id, timeout=300):
    """Poll CodeBuild until complete."""
    start = time.time()
    while time.time() - start < timeout:
        builds = codebuild.batch_get_builds(ids=[build_id])
        status = builds['builds'][0]['buildStatus']
        if status == 'SUCCEEDED':
            return True
        if status in ('FAILED', 'FAULT', 'TIMED_OUT', 'STOPPED'):
            print(f"Build failed: {status}")
            return False
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    return False


def check_firmware_exists(s3_key):
    """Check if firmware binary exists in S3."""
    try:
        s3.head_object(Bucket=FIRMWARE_BUCKET, Key=s3_key)
        return True
    except Exception:
        return False


def get_firmware_metadata(s3_key):
    """Get firmware size and SHA256."""
    try:
        head = s3.head_object(Bucket=FIRMWARE_BUCKET, Key=s3_key)
        size = head['ContentLength']
        # Download and hash
        response = s3.get_object(Bucket=FIRMWARE_BUCKET, Key=s3_key)
        body = response['Body'].read()
        sha256 = hashlib.sha256(body).hexdigest()
        return {'size': size, 'sha256': sha256}
    except Exception as e:
        print(f"Firmware metadata error: {e}")
        return None
