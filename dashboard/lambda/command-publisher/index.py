# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Command Publisher Lambda

Invoked by the AppSync sendCommand mutation (dashboard LED toggle, memory
diagnostics). Publishes {"action": <action>} to cmd/<thingName>/<topicSuffix>
on IoT Core, which the Mosquitto bridge forwards to the ESP32.

Only an allowlist of (topicSuffix, action) pairs is accepted, so the dashboard
can never publish to a device's OTA topic through this path (firmware changes
go through migrateFirmware -> the signed-OTA Step Function).

Environment variables:
  IOT_ENDPOINT - IoT Core data endpoint
"""

import json
import os
import re

import boto3

# topicSuffix -> actions the firmware handles on that topic
# (see MqttManager::onMessage / Diagnostics::request in the sketch).
ALLOWED_COMMANDS = {
    'control': {'led-on', 'led-off'},
    'diag': {'diag'},
}
DEFAULT_TOPIC_SUFFIX = 'control'
THING_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

_iot_data = None


def _client():
    global _iot_data
    if _iot_data is None:
        _iot_data = boto3.client('iot-data', endpoint_url=f"https://{os.environ['IOT_ENDPOINT']}")
    return _iot_data


def _result(success, message, thing_name):
    return {'success': success, 'message': message, 'thingName': thing_name or ''}


def handler(event, context):
    """Publish a dashboard command to one device."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get('thingName') or ''
    action = event.get('action') or ''
    topic_suffix = event.get('topicSuffix') or DEFAULT_TOPIC_SUFFIX

    if not THING_NAME_RE.match(thing_name):
        return _result(False, 'Invalid thingName', thing_name)
    allowed = ALLOWED_COMMANDS.get(topic_suffix)
    if allowed is None:
        return _result(False, f"Unsupported topicSuffix '{topic_suffix}'", thing_name)
    if action not in allowed:
        return _result(False, f"Unsupported action '{action}' for topic '{topic_suffix}'", thing_name)

    topic = f'cmd/{thing_name}/{topic_suffix}'
    payload = json.dumps({'action': action})
    try:
        _client().publish(topic=topic, qos=1, payload=payload)
    except Exception as e:  # surface the failure to the dashboard
        print(f"Error publishing to {topic}: {e}")
        return _result(False, 'Publish failed', thing_name)
    print(f"Published {payload} to {topic}")
    return _result(True, f"Command '{action}' sent to {thing_name}", thing_name)
