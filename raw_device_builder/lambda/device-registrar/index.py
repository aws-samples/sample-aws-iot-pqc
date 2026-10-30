# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Device Registrar Lambda

Called by the builder-pi GUI when an operator clicks "Register & Flash".
Performs the device registration workflow:
  1. Creates IoT Thing
  2. Creates per-device IoT policy
  3. Stores metadata in DynamoDB device registry
  4. Starts the device-identity CodeBuild job (IDENTITY_PROJECT): issues the
     classical + ML-DSA-44 device certs and builds the device's "certs" NVS
     partition image at identity/<serial>/certs.bin

No firmware is built here. Firmware images carry no device identity or WiFi
(the sketch reads them from the "certs" partition), so every variant is built
once per deployment into shared/<version>/ (build-shared-firmware.sh, chained
off the firmware-builder image build). The Pi flashes the shared classical
image (shared/1.0.0-classical/) with this device's identity image. If that
shared image is not built yet, registration returns 503 instead of leaving the
Pi to fail mid-download. Device keys are generated only inside the identity
job, never in this Lambda. WiFi is not handled here: the identity job reads it
from SSM.

Environment variables:
  REGISTRY_TABLE — DynamoDB table for device registry
  FIRMWARE_BUCKET — S3 bucket for shared firmware and identity images
  IDENTITY_PROJECT — device-identity CodeBuild project name
"""

import boto3
import json
import os
import re
from datetime import datetime, timezone

SERIAL_RE = re.compile(r'^[A-Z0-9]{4,16}$')

iot = boto3.client('iot')
dynamodb = boto3.resource('dynamodb')
codebuild = boto3.client('codebuild')
s3 = boto3.client('s3')

TABLE_NAME = os.environ['REGISTRY_TABLE']
# Dashboard device table (pqc-workshop-devices), keyed by thingName. The
# registrar SEEDS a row here at registration so (a) the device appears on the
# dashboard immediately, before its first telemetry, and (b) the row carries
# serialNumber, which the telemetry-processor's registered-only guard
# (ConditionExpression attribute_exists(serialNumber)) requires before it will
# accept telemetry. The dashboard table is otherwise only ever written by
# telemetry, so without this seed a genuinely-registered device could never
# surface (its first telemetry would be rejected by the guard).
DEVICE_TABLE_NAME = os.environ.get('DEVICE_TABLE', 'pqc-workshop-devices')
FIRMWARE_BUCKET = os.environ['FIRMWARE_BUCKET']
IDENTITY_PROJECT = os.environ['IDENTITY_PROJECT']
# First-flash firmware: the shared classical image (variants.json "classical").
CLASSICAL_VERSION = '1.0.0-classical'
SHARED_PREFIX = 'shared'

table = dynamodb.Table(TABLE_NAME)
device_table = dynamodb.Table(DEVICE_TABLE_NAME)


def _shared_firmware_ready(version):
    """True if shared/<version>/firmware.bin exists. Any error (missing key, or
    403 because the role has no ListBucket) counts as not ready."""
    try:
        s3.head_object(Bucket=FIRMWARE_BUCKET, Key=f'{SHARED_PREFIX}/{version}/firmware.bin')
        return True
    except Exception:
        return False


def _start_identity_build(serial_number):
    """Start the device-identity job for one serial; return its build id.

    The job issues both device certs (classical + ML-DSA-44) and uploads the
    device's certs NVS partition image. It reads the WiFi SSID and password
    from Parameter Store itself, so only the serial is passed.
    """
    response = codebuild.start_build(
        projectName=IDENTITY_PROJECT,
        environmentVariablesOverride=[
            {'name': 'DEVICE_SERIAL', 'value': serial_number, 'type': 'PLAINTEXT'},
        ]
    )
    return response['build']['id']


def handler(event, context):
    """Register a device and start its identity job.

    With {"identityOnly": true} (reflash), only re-issue the device identity.
    """
    print(f"Event: {json.dumps(event)}")

    # Parse request
    if 'body' in event:
        body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
    else:
        body = event

    serial_number = body.get('serialNumber')
    if not serial_number:
        return api_response(400, {'error': 'Missing serialNumber'})
    # Same rule as the identity job (issue_device_identity.validate_serial):
    # the serial becomes a cert CN and an S3 key, so reject anything odd here.
    if not SERIAL_RE.match(serial_number):
        return api_response(400, {'error': 'Invalid serialNumber (expected 4-16 of A-Z0-9)'})

    # First flash is always the shared classical image.
    firmware_version = CLASSICAL_VERSION
    firmware_prefix = f'{SHARED_PREFIX}/{firmware_version}'

    thing_name = f'esp32-{serial_number}'
    # Where the identity job uploads the device's certs NVS partition image.
    identity_key = f'identity/{serial_number}/certs.bin'
    account_id = context.invoked_function_arn.split(':')[4]
    region = os.environ.get('AWS_REGION', 'us-east-1')

    # Both paths flash the shared classical image, so it must exist first.
    if not _shared_firmware_ready(firmware_version):
        return api_response(503, {
            'success': False,
            'serialNumber': serial_number,
            'error': (f'Shared firmware {firmware_version} is not built yet. It is built '
                      f'automatically after the firmware-builder image build '
                      f'(pqc-workshop-build-image); wait for that build to SUCCEED, '
                      f'then try again.'),
        })

    # Reflash path: the device is already registered; it only needs a fresh
    # identity image (the Pi deletes the previous one after each flash). Start
    # the identity job alone — no Thing/policy/registry writes.
    if body.get('identityOnly'):
        try:
            if 'Item' not in table.get_item(Key={'serialNumber': serial_number}):
                return api_response(404, {'error': f'Device {serial_number} is not registered'})
            identity_build_id = _start_identity_build(serial_number)
        except Exception as e:
            print(f"Error starting identity build for {serial_number}: {e}")
            return api_response(500, {'success': False, 'error': str(e),
                                      'serialNumber': serial_number})
        return api_response(200, {
            'success': True,
            'thingName': thing_name,
            'serialNumber': serial_number,
            'identityBuildId': identity_build_id,
            'firmwareBucket': FIRMWARE_BUCKET,
            'firmwarePrefix': firmware_prefix,
            'identityKey': identity_key,
            'message': f'Identity build for {thing_name}: {identity_build_id}',
        })

    try:
        # ─── Step 1: Create IoT Thing ─────────────────────────────────────
        # NOTE: the device certificates are issued by the identity job (Step 5),
        # not here, so no private key flows through this Lambda. They
        # authenticate to the self-managed Mosquitto broker, not to IoT Core, so
        # nothing here needs them (they are never attached as Thing principals).
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
            # No WiFi here (the password used to be stored in plaintext). SSM
            # /pqc-workshop/wifi/* is the single source of truth; the identity
            # job reads it from Parameter Store into the certs NVS image.
            'identityKey': identity_key,
        })

        # ─── Step 4b: Seed the dashboard device table ─────────────────────
        # The dashboard reads pqc-workshop-devices (keyed by thingName), which is
        # otherwise only ever written by the telemetry-processor. Seed the row
        # here so the device shows on the dashboard the moment it is registered
        # (before any telemetry), and — critically — so the row carries
        # serialNumber, which the telemetry-processor's registered-only guard
        # requires before it will accept the device's telemetry. Use UpdateItem
        # with SET (an upsert that only sets these fields) so we never clobber
        # telemetry fields if a row somehow already exists (e.g. a re-register of
        # a device that has since reported in).
        try:
            device_table.update_item(
                Key={'thingName': thing_name},
                UpdateExpression=(
                    'SET serialNumber = :sn, registeredAt = :ra, '
                    '#st = :st, firmwareVersion = :fw'
                ),
                ExpressionAttributeNames={'#st': 'status'},
                ExpressionAttributeValues={
                    ':sn': serial_number,
                    ':ra': datetime.now(timezone.utc).isoformat(),
                    ':st': 'registered',
                    ':fw': firmware_version,
                },
            )
            print(f"Seeded dashboard row for {thing_name}")
        except Exception as e:
            # Non-fatal: registration + build still succeed; the device will
            # appear once seeded. Log loudly so this is visible.
            print(f"WARNING: failed to seed dashboard row for {thing_name}: {e}")

        # ─── Step 5: Start the device-identity job ────────────────────────
        # Issues both device certs (classical + ML-DSA-44) and builds the
        # device's certs NVS partition image. The job reads the WiFi SSID and
        # password from Parameter Store itself, so only the serial is passed.
        print(f"Starting device identity build for {thing_name}...")
        identity_build_id = _start_identity_build(serial_number)

        # No firmware build: the Pi flashes the shared classical image.
        return api_response(200, {
            'success': True,
            'thingName': thing_name,
            'serialNumber': serial_number,
            'certType': 'PrivateCA-ECDSA',
            'buildId': None,
            'identityBuildId': identity_build_id,
            'firmwareBucket': FIRMWARE_BUCKET,
            'firmwarePrefix': firmware_prefix,
            'firmwareKey': f'{firmware_prefix}/firmware.bin',
            'identityKey': identity_key,
            'message': (f'Device {thing_name} registered; identity build: '
                        f'{identity_build_id}; firmware: {firmware_prefix}/')
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
