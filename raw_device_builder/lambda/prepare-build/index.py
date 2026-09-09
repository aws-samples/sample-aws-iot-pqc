"""
Prepare Build Lambda — first work step of the PQC migration state machine.

Given a thingName, this:
  1. Looks up the device in the DynamoDB registry (keyed by serialNumber).
  2. Checks whether the target PQC firmware already exists in S3.
  3. If it does not, issues a device certificate from the Private CA and starts
     a CodeBuild to compile the PQC firmware variant.
  4. Returns routing data for the state machine:
       { thingName, serialNumber, s3Key, buildNeeded, buildId }

The state machine (not this Lambda) waits for the build to finish, via the
CodeBuild .sync integration — so this Lambda returns quickly and never blocks on
the build.

Environment variables:
  REGISTRY_TABLE          — DynamoDB device registry table
  FIRMWARE_BUCKET         — S3 bucket for firmware
  BUILD_PROJECT           — CodeBuild project name
  IOT_ENDPOINT            — IoT Core data endpoint (passed to the build)
  PRIVATE_CA_ARN          — Private CA ARN (for cert issuance)
  DEFAULT_WIFI_SSID       — fallback WiFi SSID for the PQC build
  DEFAULT_WIFI_PASSWORD   — fallback WiFi password for the PQC build
  DEFAULT_BROKER_ENDPOINT — fallback broker endpoint for the PQC build
"""
import json
import os
import subprocess
import tempfile
import time

import boto3

dynamodb = boto3.resource("dynamodb")
codebuild = boto3.client("codebuild")
acm_pca = boto3.client("acm-pca")

PQC_VERSION = "1.0.0-pqc"


def _table():
    return dynamodb.Table(os.environ["REGISTRY_TABLE"])


def handler(event, context):
    """Look up the device, ensure PQC firmware is (being) built."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get("thingName")
    if not thing_name:
        raise ValueError("Missing thingName")

    serial_number = (
        thing_name.replace("esp32-", "") if thing_name.startswith("esp32-") else thing_name
    )

    # 1. Registry lookup.
    response = _table().get_item(Key={"serialNumber": serial_number})
    if "Item" not in response:
        raise ValueError(f"Device {thing_name} not found in registry")
    device = response["Item"]

    # 2. Does the PQC firmware already exist?
    s3_key = f"{serial_number}/{PQC_VERSION}/firmware.bin"
    if _firmware_exists(s3_key):
        return {
            "thingName": thing_name,
            "serialNumber": serial_number,
            "s3Key": s3_key,
            "buildNeeded": False,
            "buildId": None,
        }

    # 3. Build needed: issue a cert and start CodeBuild.
    certs = issue_device_cert(serial_number)
    build_id = trigger_pqc_build(serial_number, device, certs)
    if not build_id:
        raise RuntimeError("Failed to start PQC firmware build")

    return {
        "thingName": thing_name,
        "serialNumber": serial_number,
        "s3Key": s3_key,
        "buildNeeded": True,
        "buildId": build_id,
    }


def _firmware_exists(s3_key):
    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=os.environ["FIRMWARE_BUCKET"], Key=s3_key)
        return True
    except Exception:
        return False


def issue_device_cert(serial_number):
    """Issue a device certificate from the Private CA (same identity as classical)."""
    ca_arn = os.environ["PRIVATE_CA_ARN"]

    ca_resp = acm_pca.get_certificate_authority_certificate(CertificateAuthorityArn=ca_arn)
    ca_cert = ca_resp["Certificate"]

    with tempfile.TemporaryDirectory() as tmp:
        key_file = f"{tmp}/device.key"
        csr_file = f"{tmp}/device.csr"
        subprocess.run(
            ["openssl", "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", key_file],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["openssl", "req", "-new", "-key", key_file, "-out", csr_file, "-subj",
             f"/CN=esp32-{serial_number}/O=AWS PQC Workshop/OU=IoT Device PQC/C=US"],
            check=True, capture_output=True,
        )
        with open(key_file) as f:
            device_key = f.read()
        with open(csr_file) as f:
            csr_pem = f.read()

    issue_resp = acm_pca.issue_certificate(
        CertificateAuthorityArn=ca_arn,
        Csr=csr_pem.encode(),
        SigningAlgorithm="SHA256WITHECDSA",
        Validity={"Type": "DAYS", "Value": 365},
        IdempotencyToken=f"pqc-{serial_number[:12]}",
    )
    cert_arn = issue_resp["CertificateArn"]

    time.sleep(3)
    for _ in range(10):
        try:
            cert_resp = acm_pca.get_certificate(CertificateAuthorityArn=ca_arn, CertificateArn=cert_arn)
            break
        except acm_pca.exceptions.RequestInProgressException:
            time.sleep(2)
    else:
        raise RuntimeError("PQC cert issuance timed out")

    return {"ca_cert": ca_cert, "device_cert": cert_resp["Certificate"], "device_key": device_key}


def trigger_pqc_build(serial_number, device, certs):
    """Start a CodeBuild for the PQC firmware variant."""
    broker_endpoint = device.get("brokerEndpoint") or os.environ.get("DEFAULT_BROKER_ENDPOINT", "")
    # The registry entry is recreated from telemetry, which does not carry the
    # WiFi password, so fall back to the workshop defaults.
    wifi_ssid = device.get("wifiSsid") or os.environ.get("DEFAULT_WIFI_SSID", "")
    wifi_password = device.get("wifiPassword") or os.environ.get("DEFAULT_WIFI_PASSWORD", "")

    build_resp = codebuild.start_build(
        projectName=os.environ["BUILD_PROJECT"],
        environmentVariablesOverride=[
            {"name": "DEVICE_SERIAL", "value": serial_number, "type": "PLAINTEXT"},
            {"name": "FIRMWARE_VERSION", "value": PQC_VERSION, "type": "PLAINTEXT"},
            {"name": "FIRMWARE_VARIANT", "value": "pqc", "type": "PLAINTEXT"},
            {"name": "WIFI_SSID", "value": wifi_ssid, "type": "PLAINTEXT"},
            {"name": "WIFI_PASSWORD", "value": wifi_password, "type": "PLAINTEXT"},
            {"name": "BROKER_ENDPOINT", "value": broker_endpoint, "type": "PLAINTEXT"},
            {"name": "BROKER_PORT", "value": "8883", "type": "PLAINTEXT"},
            {"name": "IOT_ENDPOINT", "value": os.environ["IOT_ENDPOINT"], "type": "PLAINTEXT"},
            {"name": "CA_CERT", "value": certs["ca_cert"], "type": "PLAINTEXT"},
            {"name": "DEVICE_CERT", "value": certs["device_cert"], "type": "PLAINTEXT"},
            {"name": "DEVICE_KEY", "value": certs["device_key"], "type": "PLAINTEXT"},
        ],
    )
    return build_resp["build"]["id"]
