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

    # Build item from telemetry or lifecycle event
    item = {
        'thingName': thing_name,
        'lastSeen': now,
        'ttl': int(time.time()) + 86400  # 24h TTL
    }

    # Telemetry fields
    if 'message_id' in event:
        item['messageId'] = event.get('message_id')
        item['uptimeSec'] = event.get('uptime_sec')
        item['wifiRssi'] = event.get('wifi_rssi')
        item['freeHeap'] = event.get('free_heap')
        item['minHeap'] = event.get('min_heap')
        item['chipTempC'] = str(event.get('chip_temp_c', 0))
        item['pqcAlgorithm'] = event.get('pqc_algorithm', 'unknown')
        item['tlsVersion'] = event.get('tls_version', 'unknown')
        item['firmwareVersion'] = event.get('firmware_version', 'unknown')
        item['status'] = 'online'  # If sending telemetry, device is online

    # Lifecycle fields
    if 'status' in event:
        item['status'] = event['status']
        if 'ip' in event:
            item['ipAddress'] = event['ip']
        if 'firmware_version' in event:
            item['firmwareVersion'] = event['firmware_version']
        if 'pqc_algorithm' in event:
            item['pqcAlgorithm'] = event['pqc_algorithm']
        if 'tls_version' in event:
            item['tlsVersion'] = event['tls_version']

    # Write to DynamoDB
    table.put_item(Item=item)
    print(f"Updated device: {thing_name}")

    return {'statusCode': 200, 'body': f'Updated {thing_name}'}
