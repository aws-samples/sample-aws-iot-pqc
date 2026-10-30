# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Builder-Pi device operations — the single implementation shared by the Flask
GUI (app.py) and the workshop-builder CLI (cli.py).

Cloud operations (register/deregister/wait-for-build) are pure functions that
take config + parameters and return structured results, so they are unit
testable with mocked AWS and cannot drift between GUI and CLI. Hardware
operations (flash, serial) are delegated to DeviceManager.

Each function returns a dict: {"success": bool, "message": str, ...}.
"""
import time

from . import aws


def register_device(config, serial_number, identity_only=False, logger=None):
    """
    Register a device via the device-builder API (SigV4).

    Registration starts the device-identity job (identityBuildId), which
    uploads the device's certs NVS partition image to identityKey. The app is
    the shared prebuilt image at firmwarePrefix (e.g. shared/1.0.0-classical),
    built once per deployment, so buildId is None. identity_only=True (reflash
    of an already-registered device) skips the Thing/policy/registry writes.
    A 503 means the shared firmware is not built yet.
    """
    api_url = f"{config['api_endpoint']}/register"
    # Plug-and-flash: only the auto-detected serial is sent. WiFi, broker
    # endpoint and firmware version are resolved server-side by the registrar
    # Lambda from SSM (single source of truth) / its env defaults.
    payload = {
        "serialNumber": serial_number,
    }
    if identity_only:
        payload["identityOnly"] = True
    if logger:
        what = "identity" if identity_only else "registration"
        logger.info(f"Requesting {what} for serial={serial_number}")
    resp = aws.sigv4_post(api_url, payload, config["region"])
    if resp.status_code != 200:
        return {
            "success": False,
            "message": f"Registration API returned {resp.status_code}: {resp.text}",
            "serialNumber": serial_number,
        }
    result = resp.json()
    build_id = result.get("buildId")
    identity_build_id = result.get("identityBuildId")
    if logger:
        logger.info(f"Firmware build: {build_id}; identity build: {identity_build_id}")
    return {
        "success": True,
        "message": f"Registered {serial_number}",
        "serialNumber": serial_number,
        "buildId": build_id,
        "identityBuildId": identity_build_id,
        "identityKey": result.get("identityKey") or identity_key(serial_number),
        # Shared app image the Pi flashes (e.g. shared/1.0.0-classical).
        "firmwarePrefix": result.get("firmwarePrefix"),
    }


def identity_key(serial_number):
    """S3 key of a device's certs NVS partition image (see the identity job)."""
    return f"identity/{serial_number}/certs.bin"


def build_ids(registration):
    """The non-empty CodeBuild ids a registration result needs waiting on."""
    return [b for b in (registration.get("buildId"), registration.get("identityBuildId")) if b]


def deregister_device(config, serial_number, logger=None):
    """Deregister a device via the device-builder API (SigV4)."""
    api_url = f"{config['api_endpoint']}/deregister"
    if logger:
        logger.info(f"Deregistering device serial={serial_number}")
    resp = aws.sigv4_post(api_url, {"serialNumber": serial_number}, config["region"])
    if resp.status_code != 200:
        return {
            "success": False,
            "message": f"Deregister API returned {resp.status_code}: {resp.text}",
            "serialNumber": serial_number,
        }
    return {
        "success": True,
        "message": f"Deregistered {serial_number}",
        "serialNumber": serial_number,
    }


FAILED_STATUSES = ("FAILED", "FAULT", "TIMED_OUT", "STOPPED")


def wait_for_builds(config, ids, timeout=600, poll_interval=15, logger=None,
                    codebuild_client=None):
    """
    Poll CodeBuild until every build in ids completes. Fails as soon as any
    build fails. Returns {"success": bool, "status": <overall status>,
    "statuses": {id: status}}.
    """
    ids = [i for i in ids if i]
    client = codebuild_client or aws.fresh_session(config["region"]).client("codebuild")
    statuses = {i: "UNKNOWN" for i in ids}
    start = time.time()
    while time.time() - start < timeout:
        pending = [i for i in ids if statuses[i] != "SUCCEEDED"]
        for build in client.batch_get_builds(ids=pending).get("builds", []):
            statuses[build["id"]] = build["buildStatus"]
        if logger:
            logger.info("Builds: " + ", ".join(f"{i}={s}" for i, s in statuses.items()))
        failed = {i: s for i, s in statuses.items() if s in FAILED_STATUSES}
        if failed:
            build_id, status = next(iter(failed.items()))
            return {"success": False, "status": status, "buildId": build_id,
                    "statuses": statuses, "message": f"Build {build_id} {status}"}
        if all(s == "SUCCEEDED" for s in statuses.values()):
            return {"success": True, "status": "SUCCEEDED", "statuses": statuses}
        time.sleep(poll_interval)
    return {"success": False, "status": "TIMED_OUT", "statuses": statuses,
            "message": "Build timed out"}


def wait_for_build(config, build_id, timeout=600, poll_interval=15, logger=None,
                   codebuild_client=None):
    """Poll CodeBuild until the given build completes (single-build form)."""
    result = wait_for_builds(config, [build_id], timeout=timeout,
                             poll_interval=poll_interval, logger=logger,
                             codebuild_client=codebuild_client)
    result["buildId"] = build_id
    return result
