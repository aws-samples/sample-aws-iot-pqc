"""
Command Publisher Lambda

Invoked by AppSync when an attendee clicks "Migrate to PQC" on the
dashboard. Publishes the migrate-pqc command to the device's command
topic on IoT Core, which bridges through Mosquitto to the ESP32.

Environment variables:
  IOT_ENDPOINT - IoT Core data endpoint
"""

import boto3
import json
import os

iot_data = boto3.client(
    'iot-data',
    endpoint_url=f"https://{os.environ['IOT_ENDPOINT']}"
)


def handler(event, context):
    """Publish a command to a device via IoT Core."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get('thingName')
    if not thing_name:
        return {
            'success': False,
            'message': 'Missing thingName',
            'thingName': ''
        }

    # Validate thing name format to prevent injection
    if not thing_name.replace('-', '').replace('_', '').isalnum():
        return {
            'success': False,
            'message': 'Invalid thingName format',
            'thingName': thing_name
        }

    # Support custom action and topic suffix (defaults to migrate-pqc on /ota)
    action = event.get('action', 'migrate-pqc')
    topic_suffix = event.get('topicSuffix', 'ota')
    topic = f'cmd/{thing_name}/{topic_suffix}'
    payload = json.dumps({'action': action})

    try:
        iot_data.publish(
            topic=topic,
            qos=1,
            payload=payload
        )
        print(f"Published '{action}' to {topic}")
        return {
            'success': True,
            'message': f'Command "{action}" sent to {thing_name}',
            'thingName': thing_name
        }
    except Exception as e:
        print(f"Error publishing to {topic}: {e}")
        return {
            'success': False,
            'message': str(e),
            'thingName': thing_name
        }
