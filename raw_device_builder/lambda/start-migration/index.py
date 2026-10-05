"""
Start Migration Lambda — the AppSync migrateFirmware target.

Starts the PQC migration Step Functions execution and returns immediately, so
the AppSync resolver responds well within its 30s timeout and the dashboard
never shows a spurious error while the (multi-minute) build runs. Live progress
is surfaced via the migrationStatus field the state machine writes to the device
record, which the dashboard polls.

The dashboard firmware picker sends a `variant` (classical / pqc / pqc-mldsa);
it defaults to "pqc" when absent so the original migrate-to-PQC flow keeps
working. The variant is injected into the execution input so the state machine's
`$.variant` reference always resolves and prepare-build builds the right variant.

Environment variables:
  STATE_MACHINE_ARN — ARN of the pqc-workshop-pqc-migration state machine
"""
import json
import os

import boto3

sfn = boto3.client("stepfunctions")

# Keep in sync with prepare-build VARIANT_CONFIG. Reject anything else so a bad
# picker value fails fast in the UI rather than starting a doomed execution.
# "signer"/"verified" are the ML-DSA-signer-era variants (Workstream 4): same
# pqc-mldsa firmware, signed by the ML-DSA signer (4.0.0-signer / 5.0.0-verified).
VALID_VARIANTS = {"classical", "pqc", "pqc-mldsa", "signer", "verified"}
DEFAULT_VARIANT = "pqc"


def handler(event, context):
    """Start the migration workflow for the given thingName (+ optional variant)."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get("thingName")
    if not thing_name:
        return {"success": False, "message": "Missing thingName", "thingName": ""}

    variant = (event.get("variant") or DEFAULT_VARIANT).strip()
    if variant not in VALID_VARIANTS:
        return {
            "success": False,
            "message": f"Invalid variant '{variant}'. Expected one of: {', '.join(sorted(VALID_VARIANTS))}",
            "thingName": thing_name,
        }

    try:
        sfn.start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            input=json.dumps({"thingName": thing_name, "variant": variant}),
        )
        return {
            "success": True,
            "message": f"Firmware migration to '{variant}' started for {thing_name}.",
            "thingName": thing_name,
            "variant": variant,
        }
    except Exception as e:  # noqa: BLE001 - surface any start failure to the UI
        print(f"Failed to start migration: {e}")
        return {"success": False, "message": str(e), "thingName": thing_name}
