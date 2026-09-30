"""
Telemetry Processor Lambda

Receives telemetry and lifecycle events from IoT Rules,
stores device state in DynamoDB, and triggers an AppSync
mutation to push real-time updates to the dashboard.

Environment variables:
  TABLE_NAME       - DynamoDB table name
  APPSYNC_ENDPOINT - AppSync GraphQL endpoint URL
"""

import boto3
import os
import json
import time
from datetime import datetime

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['TABLE_NAME'])

# AppSync client for real-time push (optional enhancement)
appsync_endpoint = os.environ.get('APPSYNC_ENDPOINT', '')


def handler(event, context):
    """Process telemetry/lifecycle from IoT Rule and store in DynamoDB."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get('device', event.get('thingName', 'unknown'))
    now = datetime.utcnow().isoformat() + 'Z'

    # Skip messages without a valid device name
    if not thing_name or thing_name == 'unknown':
        print(f"Skipping message with no device name: {json.dumps(event)}")
        return {'statusCode': 200, 'body': 'Skipped (no device name)'}

    # Build update expression — only update fields present in the event
    update_parts = ['#lastSeen = :lastSeen', '#ttl = :ttl']
    attr_names = {'#lastSeen': 'lastSeen', '#ttl': 'ttl'}
    attr_values = {':lastSeen': now, ':ttl': int(time.time()) + 86400}

    # Telemetry fields
    if 'message_id' in event:
        field_map = {
            'messageId': event.get('message_id'),
            'uptimeSec': event.get('uptime_sec'),
            'wifiRssi': event.get('wifi_rssi'),
            'freeHeap': event.get('free_heap'),
            'heapTotal': event.get('heap_total'),
            'minHeap': event.get('min_heap'),
            'maxAllocHeap': event.get('max_alloc_heap'),
            'sketchSize': event.get('sketch_size'),
            'freeSketchSpace': event.get('free_sketch_space'),
            'psramSize': event.get('psram_size'),
            'psramFree': event.get('psram_free'),
            'chipTempC': str(event.get('chip_temp_c', 0)),
            'pqcAlgorithm': event.get('pqc_algorithm'),
            'tlsVersion': event.get('tls_version'),
            'certSigAlg': event.get('cert_sig_alg'),
            'status': 'online',
        }
        # Only include firmware_version if present (don't overwrite with 'unknown')
        if 'firmware_version' in event:
            field_map['firmwareVersion'] = event['firmware_version']

        for k, v in field_map.items():
            if v is not None:
                update_parts.append(f'#{k} = :{k}')
                attr_names[f'#{k}'] = k
                attr_values[f':{k}'] = v

    # Lifecycle fields
    if 'status' in event:
        update_parts.append('#status = :status')
        attr_names['#status'] = 'status'
        attr_values[':status'] = event['status']
        if 'ip' in event:
            update_parts.append('#ipAddress = :ipAddress')
            attr_names['#ipAddress'] = 'ipAddress'
            attr_values[':ipAddress'] = event['ip']
        if 'firmware_version' in event:
            update_parts.append('#firmwareVersion = :firmwareVersion')
            attr_names['#firmwareVersion'] = 'firmwareVersion'
            attr_values[':firmwareVersion'] = event['firmware_version']
        if 'pqc_algorithm' in event:
            update_parts.append('#pqcAlgorithm = :pqcAlgorithm')
            attr_names['#pqcAlgorithm'] = 'pqcAlgorithm'
            attr_values[':pqcAlgorithm'] = event['pqc_algorithm']
        if 'tls_version' in event:
            update_parts.append('#tlsVersion = :tlsVersion')
            attr_names['#tlsVersion'] = 'tlsVersion'
            attr_values[':tlsVersion'] = event['tls_version']
        if 'cert_sig_alg' in event:
            update_parts.append('#certSigAlg = :certSigAlg')
            attr_names['#certSigAlg'] = 'certSigAlg'
            attr_values[':certSigAlg'] = event['cert_sig_alg']

    # Update DynamoDB (only modifies fields present — doesn't clear others).
    #
    # ─── Registered-only guard ───────────────────────────────────────────────
    # The dashboard must ONLY show devices that were registered through the
    # builder-Pi. Without a guard, this bare UpdateItem is an UPSERT: any device
    # that merely publishes to the broker — e.g. a board still flashed from a
    # PREVIOUS deployment that reconnects with a still-valid cert — would create
    # a brand-new row and therefore a ghost card, before it was ever registered
    # on this build.
    #
    # We gate on `attribute_exists(serialNumber)`. The registrar's put_item is
    # the ONLY writer of `serialNumber` into this table (see
    # raw_device_builder/lambda/device-registrar/index.py); the telemetry
    # payload has no serial number, so this processor never sets it. The
    # registrar creates the row BEFORE the device is flashed and starts
    # publishing, so a legitimately-registered device always satisfies the
    # condition — while a leftover/unregistered device has no row (or a
    # telemetry-only row lacking serialNumber), the condition fails, and we skip
    # the write instead of materialising a ghost card. We deliberately use
    # serialNumber rather than registeredAt: both are registrar-only, but
    # serialNumber is the device's stable identity field and cannot be
    # fabricated from a telemetry message.
    try:
        table.update_item(
            Key={'thingName': thing_name},
            UpdateExpression='SET ' + ', '.join(update_parts),
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
            ConditionExpression='attribute_exists(serialNumber)',
        )
        print(f"Updated device: {thing_name}")
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # Device was never registered on this deployment (no serialNumber row) —
        # do not create a ghost card for it.
        print(f"Skipping unregistered device (no serialNumber): {thing_name}")
        return {'statusCode': 200, 'body': f'Skipped unregistered {thing_name}'}

    # ─── Reconcile a finished migration ──────────────────────────────────────
    # The PQC migration state machine's terminal write is migrationStatus =
    # "rebooting"; nothing else ever moves it forward. The dashboard treats
    # "rebooting" (with building / pushing-ota) as "migration in progress" and
    # hides the firmware picker — so once the device reboots into the new
    # firmware the operator is locked out of the NEXT migration until the field
    # is cleared by hand. That is a real workshop blocker (a device cannot be
    # advanced classical -> pqc -> pqc-mldsa without manual DynamoDB surgery).
    #
    # This message IS the device reporting in on the new firmware (it just sent
    # telemetry / an online lifecycle event), so the reboot is complete. Clear
    # migrationStatus + migrationError to return the device to a terminal,
    # pickable state. We ONLY do this when the stored status is "rebooting":
    # during "building" / "pushing-ota" the device is still running the OLD
    # firmware and legitimately publishing telemetry, and clearing then would
    # unhide the picker mid-migration. The conditional write makes this safe
    # under concurrency — if the field changed underneath us, the reconcile is
    # simply skipped.
    is_live_report = ('message_id' in event) or (event.get('status') == 'online')
    if is_live_report:
        try:
            table.update_item(
                Key={'thingName': thing_name},
                UpdateExpression='REMOVE migrationStatus, migrationError',
                ConditionExpression='migrationStatus = :rebooting',
                ExpressionAttributeValues={':rebooting': 'rebooting'},
            )
            print(f"Reconciled finished migration for {thing_name} (cleared rebooting status)")
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            # Not in the rebooting state (the common case) — nothing to reconcile.
            pass

    return {'statusCode': 200, 'body': f'Updated {thing_name}'}
