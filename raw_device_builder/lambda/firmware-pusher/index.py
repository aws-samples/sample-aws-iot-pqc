"""
Firmware Pusher Lambda

Handles two operations:
  1. List available firmware versions for a device (GET)
  2. Push OTA update to a device via presigned S3 URL (POST)

For OTA push:
  - Generates a presigned S3 URL (5 min expiry) for the selected firmware
  - Publishes the URL to cmd/<thing>/ota via IoT Core
  - Device downloads and flashes using wolfSSL stack

Environment variables:
  FIRMWARE_BUCKET — S3 bucket for firmware artifacts
  IOT_ENDPOINT — AWS IoT Core data endpoint
  REGISTRY_TABLE — DynamoDB device registry table
"""

import boto3
import json
import os

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
            'versions': versions
        })
    else:
        # List all registered devices
        result = table.scan()
        devices = result.get('Items', [])
        return api_response(200, {'devices': devices})


def handle_push(event):
    """Push a firmware update to a device via OTA."""
    if 'body' in event:
        body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
    else:
        body = event

    serial_number = body.get('serialNumber')
    firmware_version = body.get('firmwareVersion')
    thing_name = body.get('thingName', f'esp32-{serial_number}')

    if not serial_number or not firmware_version:
        return api_response(400, {'error': 'Missing serialNumber or firmwareVersion'})

    # Generate presigned URL for the firmware binary
    s3_key = f'{serial_number}/{firmware_version}/firmware.bin'
    try:
        presigned_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': FIRMWARE_BUCKET, 'Key': s3_key},
            ExpiresIn=PRESIGNED_URL_EXPIRY
        )
    except Exception as e:
        return api_response(404, {'error': f'Firmware not found: {s3_key}', 'details': str(e)})

    # Publish OTA command to device
    topic = f'cmd/{thing_name}/ota'
    payload = json.dumps({
        'action': 'ota-update',
        'url': presigned_url,
        'version': firmware_version,
        'size': get_firmware_size(s3_key)
    })

    try:
        iot_data.publish(topic=topic, qos=1, payload=payload)
        print(f"Published OTA update to {topic}: version={firmware_version}")

        # Update registry with target version
        table.update_item(
            Key={'serialNumber': serial_number},
            UpdateExpression='SET targetFirmwareVersion = :v, lastOtaPush = :t',
            ExpressionAttributeValues={
                ':v': firmware_version,
                ':t': json.dumps({'version': firmware_version, 'timestamp': str(int(__import__('time').time()))})
            }
        )

        return api_response(200, {
            'success': True,
            'thingName': thing_name,
            'firmwareVersion': firmware_version,
            'message': f'OTA update v{firmware_version} pushed to {thing_name}'
        })
    except Exception as e:
        return api_response(500, {'error': str(e)})


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


def get_firmware_size(s3_key):
    """Get the size of a firmware binary in S3."""
    try:
        response = s3.head_object(Bucket=FIRMWARE_BUCKET, Key=s3_key)
        return response['ContentLength']
    except Exception:
        return 0


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
