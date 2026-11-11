# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
from datetime import datetime, timezone

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
            # OTA firmware-signing badge: the signature era that protected the
            # running image (ECDSA-P256 / ML-DSA-44 / unsigned) and the SHA-256
            # the device computed of its own running app partition at boot.
            'otaSigAlg': event.get('ota_sig_alg'),
            'firmwareSha256': event.get('firmware_sha256'),
            # Duration (ms) of the device's last TLS handshake with the broker:
            # classical vs hybrid ML-KEM vs ML-DSA certificate. Older firmware
            # does not send it, so it is simply skipped (None).
            'handshakeMs': event.get('handshake_ms'),
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
        if 'ota_sig_alg' in event:
            update_parts.append('#otaSigAlg = :otaSigAlg')
            attr_names['#otaSigAlg'] = 'otaSigAlg'
            attr_values[':otaSigAlg'] = event['ota_sig_alg']
        if 'firmware_sha256' in event:
            update_parts.append('#firmwareSha256 = :firmwareSha256')
            attr_names['#firmwareSha256'] = 'firmwareSha256'
            attr_values[':firmwareSha256'] = event['firmware_sha256']
        if 'handshake_ms' in event:
            update_parts.append('#handshakeMs = :handshakeMs')
            attr_names['#handshakeMs'] = 'handshakeMs'
            attr_values[':handshakeMs'] = event['handshake_ms']

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
    #
    # "Back on the new firmware" must mean the report comes FROM the new
    # firmware. The state machine sets "rebooting" as soon as it has published
    # the OTA command, but the board keeps running (and publishing telemetry
    # from) the OLD image for the ~30-60 s it spends downloading. Measured with a
    # 32-board burst: clearing on that telemetry returned the picker ~30 s
    # before the reboot, with the old version still showing. So the state
    # machine also stores migrationTargetVersion, and we clear only when the
    # report's firmware_version matches it. A row without a target (written by
    # an older state machine) keeps the previous behaviour so it cannot get
    # stuck. Limitation: an OTA to the version already running cannot be told
    # apart from the old image, so it clears as early as before.
    is_live_report = ('message_id' in event) or (event.get('status') == 'online')
    reported_fw = event.get('firmware_version')
    if is_live_report:
        condition = 'migrationStatus = :rebooting AND attribute_not_exists(migrationTargetVersion)'
        values = {':rebooting': 'rebooting'}
        if reported_fw:
            condition = ('migrationStatus = :rebooting AND (attribute_not_exists(migrationTargetVersion)'
                         ' OR migrationTargetVersion = :fw)')
            values[':fw'] = reported_fw
        try:
            table.update_item(
                Key={'thingName': thing_name},
                UpdateExpression='REMOVE migrationStatus, migrationError, migrationTargetVersion, migrationRebootingSince',
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
            print(f"Reconciled finished migration for {thing_name} (cleared rebooting status)")
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            # Not in the rebooting state (the common case) — nothing to reconcile.
            if reported_fw:
                _fail_stalled_migration(thing_name, reported_fw)

    return {'statusCode': 200, 'body': f'Updated {thing_name}'}


# A board that is still reporting its OLD version this long after the OTA was
# sent did not install it: the download or the signature check failed on the
# board, which then carries on running the old image without telling the cloud.
# The download takes ~60-90 s even in a 32-board burst, so 5 minutes is well
# clear of a slow success.
REBOOT_TIMEOUT_SEC = int(os.environ.get('MIGRATION_REBOOT_TIMEOUT_SEC', '300'))


def _iso_ms(dt):
    """Same format as Step Functions' $$.State.EnteredTime (UTC, ms, 'Z'), so
    the two compare correctly as strings."""
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f"{dt.microsecond // 1000:03d}Z"


def _fail_stalled_migration(thing_name, reported_fw):
    """Mark a migration failed when the board reports a version other than the
    target REBOOT_TIMEOUT_SEC after the OTA was sent. Without this, the card
    would wait for the target version forever and lock the board out of a retry."""
    cutoff = _iso_ms(datetime.fromtimestamp(time.time() - REBOOT_TIMEOUT_SEC, timezone.utc))
    try:
        table.update_item(
            Key={'thingName': thing_name},
            UpdateExpression=('SET migrationStatus = :failed, migrationError = :err '
                              'REMOVE migrationTargetVersion, migrationRebootingSince'),
            ConditionExpression=('migrationStatus = :rebooting AND migrationTargetVersion <> :fw '
                                 'AND migrationRebootingSince < :cutoff'),
            ExpressionAttributeValues={
                ':failed': 'failed', ':rebooting': 'rebooting', ':fw': reported_fw, ':cutoff': cutoff,
                ':err': (f"The board is still running {reported_fw} {REBOOT_TIMEOUT_SEC // 60} minutes after "
                         "the update was sent, so the download or its signature check failed on the "
                         "board. Try the migration again."),
            },
        )
        print(f"Marked stalled migration failed for {thing_name} (still on {reported_fw})")
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        pass
