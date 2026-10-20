# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Publish OTA Lambda — final work step of the PQC migration state machine.

Given { thingName, serialNumber, s3Key, sigAlg } (the build has completed by the
time the state machine reaches this step), this:
  1. Reads the firmware size (head_object) and computes its sha256.
  2. Generates a regional presigned S3 GET URL.
  2b. For SIGNED variants (sigAlg set), presigns the detached signature
      (firmware.bin.sig) and the signer leaf cert (signer.crt) uploaded next to
      the firmware, and adds sigUrl/sigAlg/signerCertUrl to the OTA command.
  3. Publishes an ota-update command to cmd/<thingName>/ota.
  4. Marks the registry item targetFirmwareVersion + status=ota-pending.

Unsigned variants (classical 1.0.0 USB-flash; sigAlg empty/absent) publish the
exact prior payload shape, so older devices and unsigned pushes interoperate.

The device downloads the firmware, writes ota_1, and reboots into PQC.

Environment variables:
  REGISTRY_TABLE   — DynamoDB device registry table
  FIRMWARE_BUCKET  — S3 bucket for firmware
  IOT_ENDPOINT     — IoT Core data endpoint
"""
import hashlib
import json
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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

PQC_VERSION = "2.0.0-pqc"  # canonical pqc version; matches VARIANT_CONFIG["pqc"]
# Presigned URL lifetime. The device may not fetch immediately: it can be
# mid-reboot when the OTA command fires, reconnect, then start the download,
# so a short window meant the URL was already expired by the time the device
# issued the GET — S3 then closed the TLS session with zero bytes, which the
# device saw as `Header read error (r=0, err=6)` / `Invalid content length: 0`.
# 15 minutes comfortably covers a reconnect + a large PQC firmware download.
PRESIGNED_URL_EXPIRY = 900  # 15 minutes

# The migration state machine's WaitForBuild uses the CodeBuild `.sync`
# integration, which returns when the build's BUILD phase completes — but the
# firmware artifacts are uploaded to S3 during POST_BUILD, so firmware.bin can
# lag the .sync return by a few seconds. Poll (short bounded backoff) for the
# object to appear rather than failing the whole migration on a transient miss.
FIRMWARE_WAIT_ATTEMPTS = 10
FIRMWARE_WAIT_SECONDS = 3


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
    # The firmware version being pushed. The state machine passes the resolved
    # per-variant version (e.g. 2.0.0-pqc, 3.0.0-mldsa); fall back to parsing it
    # from the S3 key (<serial>/<version>/firmware.bin), then to PQC_VERSION for
    # the original migrate-to-PQC callers that pre-date variant support.
    version = event.get("version")
    if not version:
        parts = s3_key.split("/")
        version = parts[1] if len(parts) >= 3 else PQC_VERSION

    # Signature era/algorithm for this image. prepare-build forwards sigAlg
    # (ecdsa-p256 | mldsa44) for OTA-delivered variants; empty/absent means an
    # UNSIGNED variant (classical 1.0.0 USB-flash) — publish the legacy payload
    # unchanged so an unsigned push and an older device still interoperate.
    sig_alg = (event.get("sigAlg") or "").strip()
    signed = bool(sig_alg)

    # 1. Size + sha256 (waits out the POST_BUILD upload lag; see constants).
    meta = _firmware_metadata(bucket, s3_key)
    if not meta:
        raise RuntimeError(
            f"PQC firmware not found in S3 after "
            f"{FIRMWARE_WAIT_ATTEMPTS * FIRMWARE_WAIT_SECONDS}s: {s3_key}"
        )

    # 2. Regional presigned URL for the firmware image.
    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )

    # 2b. Signed variants: presign the detached signature + the signer leaf cert
    # (both uploaded by the build next to firmware.bin). The ML-DSA-44 signature
    # is a fixed 2420 B (G2 spike), too large for the device's ~4 KB MQTT buffer
    # with headroom, so BOTH eras deliver the signature by presigned URL rather
    # than inline — one mechanism for both. The signer leaf travels WITH the OTA
    # (same bucket/prefix) and the device validates it against its baked-in CA
    # root; the device never needs the leaf stored durably.
    sig_fields = {}
    if signed:
        prefix = s3_key.rsplit("/", 1)[0]  # <serial>/<version>
        sig_key = f"{prefix}/firmware.bin.sig"
        signer_cert_key = f"{prefix}/signer.crt"
        # Wait out the POST_BUILD upload lag for the sibling artifacts too — they
        # are uploaded in the same post_build phase as firmware.bin, so if the
        # image is present they are almost certainly present, but poll to be safe.
        if not _object_ready(bucket, sig_key):
            raise RuntimeError(
                f"Signed variant '{sig_alg}' but signature not found in S3 after "
                f"{FIRMWARE_WAIT_ATTEMPTS * FIRMWARE_WAIT_SECONDS}s: {sig_key}"
            )
        if not _object_ready(bucket, signer_cert_key):
            raise RuntimeError(
                f"Signed variant '{sig_alg}' but signer cert not found in S3 after "
                f"{FIRMWARE_WAIT_ATTEMPTS * FIRMWARE_WAIT_SECONDS}s: {signer_cert_key}"
            )
        sig_fields = {
            "sigAlg": sig_alg,
            "sigUrl": s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": sig_key},
                ExpiresIn=PRESIGNED_URL_EXPIRY,
            ),
            "signerCertUrl": s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": signer_cert_key},
                ExpiresIn=PRESIGNED_URL_EXPIRY,
            ),
        }
    else:
        print(f"Variant version {version} is unsigned; publishing legacy OTA payload.")

    # 3. Publish OTA command. Signature fields are added only for signed variants
    #    so unsigned pushes and older devices keep the exact prior payload shape.
    ota_payload = {
        "action": "ota-update",
        "url": presigned_url,
        "version": version,
        "size": meta["size"],
        "sha256": meta["sha256"],
        "expiresIn": PRESIGNED_URL_EXPIRY,
        **sig_fields,
    }
    topic = f"cmd/{thing_name}/ota"
    iot_data.publish(topic=topic, qos=1, payload=json.dumps(ota_payload))
    print(f"OTA command published to {topic} (signed={signed}, sigAlg={sig_alg or 'none'})")

    # 4. Mark registry.
    _table().update_item(
        Key={"serialNumber": serial_number},
        UpdateExpression="SET targetFirmwareVersion = :v, #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":v": version, ":status": "ota-pending"},
    )

    return {
        "success": True,
        "message": f"Firmware v{version} pushed to {thing_name}.",
        "thingName": thing_name,
    }


def _firmware_metadata(bucket, s3_key):
    """Fetch firmware size + sha256, polling for the object to appear.

    The object may not be uploaded yet when this step runs (CodeBuild .sync
    returns at BUILD-complete; the artifact lands during POST_BUILD). Treat a
    missing object (404/NoSuchKey) as "not ready yet" and retry with a short
    bounded backoff; surface any other S3 error immediately.
    """
    last_err = None
    for attempt in range(1, FIRMWARE_WAIT_ATTEMPTS + 1):
        try:
            head = s3.head_object(Bucket=bucket, Key=s3_key)
            size = head["ContentLength"]
            body = s3.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
            return {"size": size, "sha256": hashlib.sha256(body).hexdigest()}
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            # A missing object means "not uploaded yet" -> wait and retry. Note
            # this Lambda's role has s3:GetObject but NOT s3:ListBucket, so S3
            # returns 403 Forbidden (not 404) for a key that does not exist yet
            # — it will not disclose non-existence without ListBucket. So treat
            # BOTH 403 and 404 as "not ready yet". A genuine permissions problem
            # degrades to a bounded ~30s wait then a clear failure, which is an
            # acceptable trade for reliably waiting out the POST_BUILD upload lag.
            if code in ("403", "404", "NoSuchKey", "NotFound", "Forbidden") or status in (403, 404):
                last_err = e
                if attempt < FIRMWARE_WAIT_ATTEMPTS:
                    print(
                        f"Firmware not in S3 yet ({s3_key}) [{code or status}], attempt "
                        f"{attempt}/{FIRMWARE_WAIT_ATTEMPTS}; waiting "
                        f"{FIRMWARE_WAIT_SECONDS}s..."
                    )
                    time.sleep(FIRMWARE_WAIT_SECONDS)  # nosemgrep: arbitrary-sleep
                    continue
                print(f"Firmware still absent after {attempt} attempts: {s3_key}")
                return None
            # Any other S3 error is a real failure — do not mask it.
            print(f"Firmware metadata S3 error ({code}): {e}")
            return None
        except Exception as e:  # noqa: BLE001 - unexpected; log and give up
            print(f"Firmware metadata error: {e}")
            return None
    if last_err:
        print(f"Firmware metadata gave up after retries: {last_err}")
    return None


def _object_ready(bucket, s3_key):
    """Return True once the object exists, polling out the POST_BUILD upload lag.

    Presence-only sibling of _firmware_metadata (no body read / hash) for the
    detached signature and signer cert, which are uploaded in the SAME build
    post_build phase as firmware.bin. Same 403-or-404 handling: this Lambda's
    role has s3:GetObject but not s3:ListBucket, so a not-yet-uploaded key comes
    back as 403 Forbidden rather than 404 — treat both as "not ready yet".
    """
    for attempt in range(1, FIRMWARE_WAIT_ATTEMPTS + 1):
        try:
            s3.head_object(Bucket=bucket, Key=s3_key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("403", "404", "NoSuchKey", "NotFound", "Forbidden") or status in (403, 404):
                if attempt < FIRMWARE_WAIT_ATTEMPTS:
                    print(
                        f"Artifact not in S3 yet ({s3_key}) [{code or status}], attempt "
                        f"{attempt}/{FIRMWARE_WAIT_ATTEMPTS}; waiting {FIRMWARE_WAIT_SECONDS}s..."
                    )
                    time.sleep(FIRMWARE_WAIT_SECONDS)  # nosemgrep: arbitrary-sleep
                    continue
                print(f"Artifact still absent after {attempt} attempts: {s3_key}")
                return False
            print(f"Artifact presence S3 error ({code}): {e}")
            return False
        except Exception as e:  # noqa: BLE001
            print(f"Artifact presence error: {e}")
            return False
    return False
