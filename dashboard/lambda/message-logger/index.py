# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Message Logger Lambda

Captures all MQTT messages (telemetry, lifecycle, commands) transiting through
IoT Core and stores them in DynamoDB for the dashboard's message log view.

Each message is stored with:
  - thingName (partition key) — extracted from topic
  - timestamp (sort key) — ISO 8601 with milliseconds
  - topic — full MQTT topic
  - direction — "inbound" (device→cloud) or "outbound" (cloud→device)
  - payload — JSON string of the message body
  - ttl — auto-expire after 1 hour (workshop doesn't need long history)

Environment variables:
  LOG_TABLE_NAME — DynamoDB table for message log
"""

import boto3
import json
import os
import time
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['LOG_TABLE_NAME'])

# TTL: messages expire after 1 hour
MESSAGE_TTL_SECONDS = 3600


def extract_thing_name(topic):
    """Extract device name from topic pattern dt/<thing>/... or cmd/<thing>/..."""
    parts = topic.split('/')
    if len(parts) >= 2:
        return parts[1]
    return 'unknown'


def determine_direction(topic):
    """Determine message direction from topic prefix."""
    if topic.startswith('dt/'):
        return 'inbound'   # device → cloud
    elif topic.startswith('cmd/'):
        return 'outbound'  # cloud → device
    return 'unknown'


def handler(event, context):
    """Store an MQTT message in the log table."""
    # The IoT Rule SQL passes the topic via the topic() function
    topic = event.get('mqtttopic', event.get('topic', 'unknown'))
    thing_name = extract_thing_name(topic)
    direction = determine_direction(topic)

    # Skip unknown/unnamed devices
    if thing_name == 'unknown' or not thing_name:
        return {'statusCode': 200, 'body': 'skipped'}

    # Remove internal fields before storing payload
    payload = {k: v for k, v in event.items() if k != 'mqtttopic'}

    now = datetime.now(timezone.utc)
    timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'

    item = {
        'thingName': thing_name,
        'timestamp': timestamp,
        'topic': topic,
        'direction': direction,
        'payload': json.dumps(payload, default=str),
        'ttl': int(time.time()) + MESSAGE_TTL_SECONDS
    }

    try:
        table.put_item(Item=item)
    except Exception as e:
        print(f"Error writing to DynamoDB: {e}")
        # Don't fail — best effort logging
        return {'statusCode': 500, 'body': str(e)}

    return {'statusCode': 200, 'body': f'Logged {direction} message for {thing_name}'}
