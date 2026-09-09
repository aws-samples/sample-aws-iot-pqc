"""
Publish OTA Lambda — final work step of the PQC migration state machine.

Given { thingName, serialNumber, s3Key } (the build has completed by the time
the state machine reaches this step), this:
  1. Reads the firmware size (head_object) and computes its sha256.
  2. Generates a regional presigned S3 GET URL.
  3. Publishes an ota-update command to cmd/<thingName>/ota.
  4. Marks the registry item targetFirmwareVersion + status=ota-pending.

The device downloads the firmware, writes ota_1, and reboots into PQC.

Environment variables:
  REGISTRY_TABLE   — DynamoDB device registry table
  FIRMWARE_BUCKET  — S3 bucket for firmware
  IOT_ENDPOINT     — IoT Core data endpoint
"""
import hashlib
import json
import os

import boto3
from botocore.config import Config

dynamodb = boto3.resource("dynamodb")
iot_data = boto3.client("iot-data", endpoint_url=f"https://{os.environ['IOT_ENDPOINT']}")

# Region-specific S3 endpoint with SigV4 so presigned URLs point at
# s3.<region>.amazonaws.com. The global s3.amazonaws.com endpoint can 307-redirect
# on GET for a regional bucket, which the device's minimal HTTP client does not
# follow (it saw Content-Length: 0 from the redirect). This is a required OTA fix.
_S3_REGION = os.environ.get("AWS_REGION", "us-east-1")
s3 = boto3.client(
    "s3",
    region_name=_S3_REGION,
    endpoint_url=f"https://s3.{_S3_REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

PQC_VERSION = "1.0.0-pqc"
PRESIGNED_URL_EXPIRY = 300  # 5 minutes


def _table():
    return dynamodb.Table(os.environ["REGISTRY_TABLE"])


def handler(event, context):
    """Presign the firmware, publish the OTA command, mark the registry."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event["thingName"]
    serial_number = event.get("serialNumber") or (
        thing_name.replace("esp32-", "") if thing_name.startswith("esp32-") else thing_name
    )
    s3_key = event.get("s3Key") or f"{serial_number}/{PQC_VERSION}/firmware.bin"
    bucket = os.environ["FIRMWARE_BUCKET"]

    # 1. Size + sha256.
    meta = _firmware_metadata(bucket, s3_key)
    if not meta:
        raise RuntimeError(f"PQC firmware not found in S3: {s3_key}")

    # 2. Regional presigned URL.
    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )

    # 3. Publish OTA command.
    ota_payload = {
        "action": "ota-update",
        "url": presigned_url,
        "version": PQC_VERSION,
        "size": meta["size"],
        "sha256": meta["sha256"],
        "expiresIn": PRESIGNED_URL_EXPIRY,
    }
    topic = f"cmd/{thing_name}/ota"
    iot_data.publish(topic=topic, qos=1, payload=json.dumps(ota_payload))
    print(f"OTA command published to {topic}")

    # 4. Mark registry.
    _table().update_item(
        Key={"serialNumber": serial_number},
        UpdateExpression="SET targetFirmwareVersion = :v, #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":v": PQC_VERSION, ":status": "ota-pending"},
    )

    return {
        "success": True,
        "message": f"PQC firmware v{PQC_VERSION} pushed to {thing_name}.",
        "thingName": thing_name,
    }


def _firmware_metadata(bucket, s3_key):
    try:
        head = s3.head_object(Bucket=bucket, Key=s3_key)
        size = head["ContentLength"]
        body = s3.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
        return {"size": size, "sha256": hashlib.sha256(body).hexdigest()}
    except Exception as e:
        print(f"Firmware metadata error: {e}")
        return None
