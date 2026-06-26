"""
Auto-Register Device Lambda

Triggered by IoT Rule when a lifecycle "online" event arrives from
a new device (MAC-derived name like "esp32-AABBCC").
Creates the IoT Thing and adds it to the workshop group.
"""

import boto3
import json

iot = boto3.client('iot')


def handler(event, context):
    """Auto-register a device when its first lifecycle event arrives."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get('device', '')
    if not thing_name or not thing_name.startswith('esp32-'):
        print(f"Ignoring non-device message: {thing_name}")
        return

    # Check if Thing already exists
    try:
        iot.describe_thing(thingName=thing_name)
        print(f"Thing {thing_name} already exists")
        return
    except iot.exceptions.ResourceNotFoundException:
        pass

    # Create the Thing
    print(f"Auto-registering new device: {thing_name}")
    iot.create_thing(
        thingName=thing_name,
        thingTypeName='esp32-pqc-device',
        attributePayload={
            'attributes': {
                'workshop': 'pqc-reinvent',
                'registered_by': 'auto-registration',
                'pqc_algorithm': event.get('pqc_algorithm', 'unknown'),
                'firmware_version': event.get('firmware_version', 'unknown')
            }
        }
    )

    # Add to workshop group
    try:
        iot.add_thing_to_thing_group(
            thingGroupName='pqc-workshop-devices',
            thingName=thing_name
        )
    except Exception as e:
        print(f"Could not add to group: {e}")

    print(f"Device {thing_name} registered successfully")
