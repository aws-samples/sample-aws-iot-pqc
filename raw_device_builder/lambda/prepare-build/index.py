"""
Prepare Build Lambda — first work step of the PQC migration state machine.

Given a thingName (and optional variant), this:
  1. Looks up the device in the DynamoDB registry (keyed by serialNumber).
  2. Checks whether the target firmware for that variant already exists in S3.
  3. If it does not, resolves the per-device build env (WiFi/broker/IoT) so the
     state machine's WaitForBuild step can start the single CodeBuild.
  4. Returns routing data for the state machine:
       { thingName, serialNumber, variant, version, s3Key, buildNeeded, buildEnv }

This Lambda does NOT start a build itself — the state machine's WaitForBuild
(codebuild:startBuild.sync) is the single authoritative build. Starting one here
too previously caused a redundant second build for the same S3 key.

Certificate issuance moved INTO the CodeBuild firmware build (see
raw_device_builder/codebuild/buildspec.yml): the build generates the device
keypair + CSR and issues the cert from the correct AWS Private CA
(classical ECDSA CA for classical/pqc, ML-DSA-44 CA for pqc-mldsa) — so this
Lambda no longer needs OpenSSL and the private key never flows through Lambda
env or Step Function state. This Lambda is now pure orchestration.

The state machine (not this Lambda) waits for the build to finish, via the
CodeBuild .sync integration — so this Lambda returns quickly and never blocks on
the build.

Environment variables:
  REGISTRY_TABLE          — DynamoDB device registry table
  FIRMWARE_BUCKET         — S3 bucket for firmware
  BUILD_PROJECT           — CodeBuild project name
  IOT_ENDPOINT            — IoT Core data endpoint (passed to the build)
  DEFAULT_WIFI_SSID       — fallback WiFi SSID for the build
  DEFAULT_WIFI_PASSWORD   — fallback WiFi password for the build
  DEFAULT_BROKER_ENDPOINT — fallback broker endpoint for the build
"""
import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")

# Firmware variant → (FIRMWARE_VARIANT build value, firmware version / S3 key
# segment). The version is what distinguishes the S3 artifact and the OTA job,
# so each variant gets its own version namespace and can coexist on a device's
# history (enabling one-step rollback under the Option A OTA-slot strategy).
#   classical  → the initial flash variant (rarely re-built via migration)
#   pqc        → hybrid ML-KEM key exchange, classical (ECDSA) device cert
#   pqc-mldsa  → hybrid ML-KEM key exchange + ML-DSA-44 device cert (Step 2)
VARIANT_CONFIG = {
    "classical": {"build_variant": "classical", "version": "1.0.0-classical"},
    "pqc": {"build_variant": "pqc", "version": "2.0.0-pqc"},
    "pqc-mldsa": {"build_variant": "pqc-mldsa", "version": "3.0.0-mldsa"},
}
DEFAULT_VARIANT = "pqc"  # backward-compatible with the original migrate-to-PQC flow

# SSM is the authoritative live source of truth for the workshop WiFi. The
# deploy publishes /pqc-workshop/wifi/ssid (String) + /password (SecureString);
# reading them at migration time means a WiFi change is picked up by the next
# build with a single put-parameter, no redeploy.
WIFI_SSM_PREFIX = os.environ.get("WIFI_SSM_PREFIX", "/pqc-workshop/wifi")
BROKER_SSM_PARAM = os.environ.get("BROKER_SSM_PARAM", "/pqc-workshop/broker/endpoint")


def _table():
    return dynamodb.Table(os.environ["REGISTRY_TABLE"])


def _get_ssm(name):
    """Read one SSM param (decrypted); return None if missing/unreadable so the
    caller can fall back. Never raises — a WiFi lookup must not fail a build."""
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except Exception as e:  # ParameterNotFound, AccessDenied, throttling, etc.
        print(f"SSM read {name} failed ({e}); falling back")
        return None


def wifi_from_ssm():
    """Return (ssid, password) from SSM, each None if unavailable."""
    return (
        _get_ssm(f"{WIFI_SSM_PREFIX}/ssid"),
        _get_ssm(f"{WIFI_SSM_PREFIX}/password"),
    )


def broker_from_ssm():
    """Return the broker endpoint from SSM, or None if unavailable."""
    return _get_ssm(BROKER_SSM_PARAM)


def resolve_variant(event):
    """Pick the firmware variant from the event, defaulting to pqc. Raises on an
    unknown variant so a bad dashboard/picker value fails loudly instead of
    silently building the wrong firmware."""
    variant = (event.get("variant") or DEFAULT_VARIANT).strip()
    if variant not in VARIANT_CONFIG:
        raise ValueError(
            f"Unknown firmware variant '{variant}'. "
            f"Expected one of: {', '.join(sorted(VARIANT_CONFIG))}"
        )
    return variant


def handler(event, context):
    """Look up the device, ensure the requested variant's firmware is (being) built."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get("thingName")
    if not thing_name:
        raise ValueError("Missing thingName")

    variant = resolve_variant(event)
    cfg = VARIANT_CONFIG[variant]
    version = cfg["version"]

    serial_number = (
        thing_name.replace("esp32-", "") if thing_name.startswith("esp32-") else thing_name
    )

    # 1. Registry lookup.
    response = _table().get_item(Key={"serialNumber": serial_number})
    if "Item" not in response:
        raise ValueError(f"Device {thing_name} not found in registry")
    device = response["Item"]

    # 2. Does this variant's firmware already exist?
    s3_key = f"{serial_number}/{version}/firmware.bin"
    if _firmware_exists(s3_key):
        return {
            "thingName": thing_name,
            "serialNumber": serial_number,
            "variant": variant,
            "version": version,
            "s3Key": s3_key,
            "buildNeeded": False,
            # buildEnv is unused when buildNeeded is False, but always present so
            # the state machine's ResultSelector never references a missing path.
            "buildEnv": {"wifiSsid": "", "wifiPassword": "", "brokerEndpoint": "", "iotEndpoint": ""},
        }

    # 3. Build needed. The state machine's WaitForBuild step starts + waits for
    #    the single CodeBuild (codebuild:startBuild.sync); this Lambda does NOT
    #    start a build itself (that previously caused a redundant second build).
    #    We resolve the per-device build env (WiFi/broker/IoT) here and return it
    #    so WaitForBuild builds with the CORRECT per-device WiFi.
    build_env = resolve_build_env(device)
    return {
        "thingName": thing_name,
        "serialNumber": serial_number,
        "variant": variant,
        "version": version,
        "s3Key": s3_key,
        "buildNeeded": True,
        "buildEnv": build_env,
    }


def _firmware_exists(s3_key):
    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=os.environ["FIRMWARE_BUCKET"], Key=s3_key)
        return True
    except Exception:
        return False


def resolve_build_env(device):
    """Resolve the per-device build environment for the requested firmware variant.

    Returns the fully-resolved build environment (WiFi, broker endpoint) so the
    state machine's WaitForBuild step can start the single authoritative build
    with the CORRECT per-device WiFi. Resolving here (SSM -> registry -> env) and
    passing it through the state machine avoids both (a) a redundant second build
    and (b) the build silently using the deploy-default WiFi, which would strand
    a device whose WiFi differs from the default.
    """
    # Broker resolution: SSM (live source of truth) -> registry value -> env.
    broker_endpoint = (
        broker_from_ssm()
        or device.get("brokerEndpoint")
        or os.environ.get("DEFAULT_BROKER_ENDPOINT", "")
    )
    # WiFi resolution: SSM (live source of truth) -> registry value (what the
    # device was flashed with) -> env fallback.
    ssm_ssid, ssm_password = wifi_from_ssm()
    wifi_ssid = ssm_ssid or device.get("wifiSsid") or os.environ.get("DEFAULT_WIFI_SSID", "")
    wifi_password = (
        ssm_password or device.get("wifiPassword") or os.environ.get("DEFAULT_WIFI_PASSWORD", "")
    )
    return {
        "wifiSsid": wifi_ssid,
        "wifiPassword": wifi_password,
        "brokerEndpoint": broker_endpoint,
        "iotEndpoint": os.environ.get("IOT_ENDPOINT", ""),
    }
