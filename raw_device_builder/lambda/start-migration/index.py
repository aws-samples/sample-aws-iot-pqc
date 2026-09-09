"""
Start Migration Lambda — the AppSync migrateFirmware target.

Starts the PQC migration Step Functions execution and returns immediately, so
the AppSync resolver responds well within its 30s timeout and the dashboard
never shows a spurious error while the (multi-minute) build runs. Live progress
is surfaced via the migrationStatus field the state machine writes to the device
record, which the dashboard polls.

Environment variables:
  STATE_MACHINE_ARN — ARN of the pqc-workshop-pqc-migration state machine
"""
import json
import os

import boto3

sfn = boto3.client("stepfunctions")


def handler(event, context):
    """Start the migration workflow for the given thingName."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get("thingName")
    if not thing_name:
        return {"success": False, "message": "Missing thingName", "thingName": ""}

    try:
        sfn.start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            input=json.dumps({"thingName": thing_name}),
        )
        return {
            "success": True,
            "message": f"PQC migration started for {thing_name}.",
            "thingName": thing_name,
        }
    except Exception as e:  # noqa: BLE001 - surface any start failure to the UI
        print(f"Failed to start migration: {e}")
        return {"success": False, "message": str(e), "thingName": thing_name}
