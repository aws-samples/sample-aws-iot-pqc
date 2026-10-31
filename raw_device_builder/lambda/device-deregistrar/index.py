# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Device Deregistrar Lambda

Cleans up all AWS resources for a device:
  1. Detaches certificate from thing
  2. Detaches policy from certificate
  3. Deactivates and deletes certificate
  4. Deletes IoT policy
  5. Deletes IoT Thing
  6. Removes from the DynamoDB registry AND the dashboard device table
     (pqc-workshop-devices), so its card disappears. A deregistered board that
     keeps publishing cannot bring it back: the telemetry processor only
     updates rows that carry serialNumber, which only the registrar writes.
  7. Removes the device's S3 objects: any not-yet-flashed identity image
     (identity/<serial>/, holds its private keys) and any legacy per-device
     firmware (<serial>/). Shared firmware (shared/<version>/) is not touched.

Environment variables:
  REGISTRY_TABLE — DynamoDB device registry table
  DEVICE_TABLE — dashboard device table (default pqc-workshop-devices)
  FIRMWARE_BUCKET — S3 firmware bucket
"""

import boto3
import json
import os

iot = boto3.client('iot')
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

TABLE_NAME = os.environ['REGISTRY_TABLE']
FIRMWARE_BUCKET = os.environ['FIRMWARE_BUCKET']
table = dynamodb.Table(TABLE_NAME)
device_table = dynamodb.Table(os.environ.get('DEVICE_TABLE', 'pqc-workshop-devices'))


def handler(event, context):
    """Deregister a device and clean up all resources."""
    print(f"Event: {json.dumps(event)}")

    if 'body' in event:
        body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
    else:
        body = event

    serial_number = body.get('serialNumber')
    if not serial_number:
        return api_response(400, {'error': 'Missing serialNumber'})

    thing_name = f'esp32-{serial_number}'
    policy_name = f'pqc-device-{serial_number}'
    errors = []

    try:
        # Step 1: Find and detach certificates from the thing
        cert_arns = []
        try:
            principals = iot.list_thing_principals(thingName=thing_name)
            cert_arns = principals.get('principals', [])
        except Exception as e:
            errors.append(f'list_thing_principals: {e}')

        for cert_arn in cert_arns:
            cert_id = cert_arn.split('/')[-1]
            try:
                # Detach from thing
                iot.detach_thing_principal(thingName=thing_name, principal=cert_arn)
                # Detach policy
                try:
                    iot.detach_policy(policyName=policy_name, target=cert_arn)
                except Exception:
                    pass
                # Deactivate certificate
                iot.update_certificate(certificateId=cert_id, newStatus='INACTIVE')
                # Delete certificate
                iot.delete_certificate(certificateId=cert_id, forceDelete=True)
            except Exception as e:
                errors.append(f'cert cleanup ({cert_id}): {e}')

        # Step 2: Delete IoT policy
        try:
            iot.delete_policy(policyName=policy_name)
        except iot.exceptions.ResourceNotFoundException:
            pass
        except Exception as e:
            errors.append(f'delete_policy: {e}')

        # Step 3: Delete IoT Thing
        try:
            iot.delete_thing(thingName=thing_name)
        except iot.exceptions.ResourceNotFoundException:
            pass
        except Exception as e:
            errors.append(f'delete_thing: {e}')

        # Step 4: Remove from DynamoDB: the registry, and the dashboard row.
        try:
            table.delete_item(Key={'serialNumber': serial_number})
        except Exception as e:
            errors.append(f'dynamodb delete: {e}')
        try:
            device_table.delete_item(Key={'thingName': thing_name})
        except Exception as e:
            errors.append(f'dashboard row delete: {e}')

        # Step 5: Remove the device's S3 objects. The identity image normally
        # disappears after flashing (the Pi deletes it); this covers a device
        # deregistered before it was flashed. Noncurrent versions are expired by
        # the bucket's 1-day lifecycle rule.
        for prefix in (f'identity/{serial_number}/', f'{serial_number}/'):
            try:
                objects = s3.list_objects_v2(Bucket=FIRMWARE_BUCKET, Prefix=prefix)
                for obj in objects.get('Contents', []):
                    s3.delete_object(Bucket=FIRMWARE_BUCKET, Key=obj['Key'])
            except Exception as e:
                errors.append(f's3 cleanup ({prefix}): {e}')

        if errors:
            return api_response(200, {
                'success': True,
                'thingName': thing_name,
                'message': f'Deregistered with {len(errors)} non-critical error(s)',
                'errors': errors
            })

        return api_response(200, {
            'success': True,
            'thingName': thing_name,
            'message': f'Device {thing_name} fully deregistered'
        })

    except Exception as e:
        return api_response(500, {'success': False, 'error': str(e)})


def api_response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
        'body': json.dumps(body)
    }
