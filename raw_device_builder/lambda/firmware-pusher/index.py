"""
Firmware Pusher Lambda

Handles two operations:
  1. List available firmware versions for a device (GET /firmware?serialNumber=X)
  2. Push OTA update to a device via presigned S3 URL (POST /firmware)

OTA Flow:
  - Generates a presigned S3 URL (5 min expiry) for the firmware.bin
  - Calculates SHA256 checksum for integrity verification
  - Publishes the OTA command to cmd/<thing>/ota via IoT Core MQTT
  - Device downloads firmware.bin from S3 using wolfSSL HTTPS
  - Device verifies SHA256, writes to ota_1 partition, reboots

The firmware.bin is the app-only binary (not the merged binary) since OTA
only updates the application partition — bootloader and partition table
are unchanged from Firmware 0.

Environment variables:
  FIRMWARE_BUCKET — S3 bucket for firmware artifacts
  IOT_ENDPOINT — AWS IoT Core data endpoint
  REGISTRY_TABLE — DynamoDB device registry table
"""

import boto3
import hashlib
import json
import os
import time

s3 = boto3.client('s3')
iot_data = boto3.client('iot-data', endpoint_url=f"https://{os.environ['IOT_ENDPOINT']}")
dynamodb = boto3.resource('dynamodb')

FIRMWARE_BUCKET = os.environ['FIRMWARE_BUCKET']
REGISTRY_TABLE = os.environ['REGISTRY_TABLE']
table = dynamodb.Table(REGISTRY_TABLE)

PRESIGNED_URL_EXPIRY = 300  # 5 minutes


def handler(event, context):
    """Handle firmware listing and OTA push requests."""
    print(f"Event: {json.dumps(event)}")

    http_method = event.get('httpMethod', 'POST')

    if http_method == 'GET':
        return handle_list(event)
    else:
        return handle_push(event)


def handle_list(event):
    """List all registered devices and their firmware versions."""
    query_params = event.get('queryStringParameters') or {}
    serial_number = query_params.get('serialNumber')

    if serial_number:
        # List firmware versions for a specific device
        versions = list_firmware_versions(serial_number)
        return api_response(200, {
            'serialNumber': serial_number,
            'thingName': f'esp32-{serial_number}',
            'versions': versions
        })
    else:
        # List all registered devices with their current firmware version
        result = table.scan()
        devices = []
        for item in result.get('Items', []):
            devices.append({
                'serialNumber': item['serialNumber'],
                'thingName': item.get('thingName', ''),
                'firmwareVersion': item.get('firmwareVersion', ''),
                'targetFirmwareVersion': item.get('targetFirmwareVersion', ''),
                'status': item.get('status', ''),
                'registeredAt': item.get('registeredAt', '')
            })
        return api_response(200, {'devices': devices})


def handle_push(event):
    """Push a firmware update to a device (or all devices) via OTA.

    Request body:
      serialNumber: target device serial (or 'all' for fleet-wide)
      firmwareVersion: version to push (must exist in S3)
    """
    if 'body' in event:
        body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
    else:
        body = event

    serial_number = body.get('serialNumber')
    firmware_version = body.get('firmwareVersion')

    if not serial_number or not firmware_version:
        return api_response(400, {'error': 'Missing serialNumber or firmwareVersion'})

    # Fleet-wide OTA
    if serial_number == 'all':
        return handle_fleet_push(firmware_version)

    thing_name = f'esp32-{serial_number}'

    # Verify firmware exists in S3
    s3_key = f'{serial_number}/{firmware_version}/firmware.bin'
    firmware_meta = get_firmware_metadata(s3_key)
    if not firmware_meta:
        return api_response(404, {
            'error': f'Firmware not found: {s3_key}',
            'hint': 'Compile the firmware version first via CodeBuild'
        })

    # Generate presigned URL
    presigned_url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': FIRMWARE_BUCKET, 'Key': s3_key},
        ExpiresIn=PRESIGNED_URL_EXPIRY
    )

    # Build OTA command payload
    ota_payload = {
        'action': 'ota-update',
        'url': presigned_url,
        'version': firmware_version,
        'size': firmware_meta['size'],
        'sha256': firmware_meta['sha256'],
        'expiresIn': PRESIGNED_URL_EXPIRY
    }

    # Publish to device OTA topic
    topic = f'cmd/{thing_name}/ota'
    try:
        iot_data.publish(topic=topic, qos=1, payload=json.dumps(ota_payload))
        print(f"OTA pushed to {topic}: version={firmware_version}, size={firmware_meta['size']}")

        # Update registry
        table.update_item(
            Key={'serialNumber': serial_number},
            UpdateExpression='SET targetFirmwareVersion = :v, lastOtaPush = :t, #s = :status',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':v': firmware_version,
                ':t': f'{firmware_version}@{int(time.time())}',
                ':status': 'ota-pending'
            }
        )

        return api_response(200, {
            'success': True,
            'thingName': thing_name,
            'firmwareVersion': firmware_version,
            'firmwareSize': firmware_meta['size'],
            'sha256': firmware_meta['sha256'],
            'message': f'OTA v{firmware_version} pushed to {thing_name}'
        })

    except Exception as e:
        print(f"Error publishing OTA: {e}")
        return api_response(500, {'error': f'Failed to publish OTA command: {e}'})


def handle_fleet_push(firmware_version):
    """Push OTA to all registered devices."""
    result = table.scan()
    devices = result.get('Items', [])

    if not devices:
        return api_response(404, {'error': 'No registered devices found'})

    successes = []
    failures = []

    for device in devices:
        serial_number = device['serialNumber']
        thing_name = device.get('thingName', f'esp32-{serial_number}')
        s3_key = f'{serial_number}/{firmware_version}/firmware.bin'

        firmware_meta = get_firmware_metadata(s3_key)
        if not firmware_meta:
            failures.append({'serialNumber': serial_number, 'reason': 'Firmware not in S3'})
            continue

        presigned_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': FIRMWARE_BUCKET, 'Key': s3_key},
            ExpiresIn=PRESIGNED_URL_EXPIRY
        )

        ota_payload = {
            'action': 'ota-update',
            'url': presigned_url,
            'version': firmware_version,
            'size': firmware_meta['size'],
            'sha256': firmware_meta['sha256'],
            'expiresIn': PRESIGNED_URL_EXPIRY
        }

        topic = f'cmd/{thing_name}/ota'
        try:
            iot_data.publish(topic=topic, qos=1, payload=json.dumps(ota_payload))
            successes.append(serial_number)

            table.update_item(
                Key={'serialNumber': serial_number},
                UpdateExpression='SET targetFirmwareVersion = :v, lastOtaPush = :t, #s = :status',
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={
                    ':v': firmware_version,
                    ':t': f'{firmware_version}@{int(time.time())}',
                    ':status': 'ota-pending'
                }
            )
        except Exception as e:
            failures.append({'serialNumber': serial_number, 'reason': str(e)})

    return api_response(200, {
        'success': True,
        'firmwareVersion': firmware_version,
        'pushed': len(successes),
        'failed': len(failures),
        'successes': successes,
        'failures': failures,
        'message': f'OTA v{firmware_version} pushed to {len(successes)} devices ({len(failures)} failed)'
    })


def get_firmware_metadata(s3_key):
    """Get firmware size and SHA256 checksum from S3."""
    try:
        # Get object metadata (size)
        head = s3.head_object(Bucket=FIRMWARE_BUCKET, Key=s3_key)
        size = head['ContentLength']

        # Check for pre-computed SHA256 in metadata
        user_metadata = head.get('Metadata', {})
        sha256 = user_metadata.get('sha256', '')

        if not sha256:
            # Compute SHA256 by downloading the object
            # For firmware binaries (2-3MB) this is acceptable
            response = s3.get_object(Bucket=FIRMWARE_BUCKET, Key=s3_key)
            body = response['Body'].read()
            sha256 = hashlib.sha256(body).hexdigest()

        return {'size': size, 'sha256': sha256}

    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        print(f"Error getting firmware metadata for {s3_key}: {e}")
        return None


def list_firmware_versions(serial_number):
    """List all firmware versions available in S3 for a device."""
    prefix = f'{serial_number}/'
    versions = []
    try:
        response = s3.list_objects_v2(Bucket=FIRMWARE_BUCKET, Prefix=prefix, Delimiter='/')
        for common_prefix in response.get('CommonPrefixes', []):
            version = common_prefix['Prefix'].strip('/').split('/')[-1]
            versions.append(version)
    except Exception as e:
        print(f"Error listing versions: {e}")
    return sorted(versions, reverse=True)


def api_response(status_code, body):
    """Format response for API Gateway proxy integration."""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body, default=str)
    }
