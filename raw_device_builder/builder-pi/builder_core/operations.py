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


def register_device(config, serial_number, logger=None):
    """
    Register a device via the device-builder API (SigV4). Returns the build id
    so the caller can wait for the firmware build.
    """
    api_url = f"{config['api_endpoint']}/register"
    # Plug-and-flash: only the auto-detected serial is sent. WiFi, broker
    # endpoint and firmware version are resolved server-side by the registrar
    # Lambda from SSM (single source of truth) / its env defaults.
    payload = {
        "serialNumber": serial_number,
    }
    if logger:
        logger.info(f"Registering device serial={serial_number}")
    resp = aws.sigv4_post(api_url, payload, config["region"])
    if resp.status_code != 200:
        return {
            "success": False,
            "message": f"Registration API returned {resp.status_code}: {resp.text}",
            "serialNumber": serial_number,
        }
    result = resp.json()
    build_id = result.get("buildId")
    if logger:
        logger.info(f"Registered. Build ID: {build_id}")
    return {
        "success": True,
        "message": f"Registered {serial_number}",
        "serialNumber": serial_number,
        "buildId": build_id,
    }


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


def wait_for_build(config, build_id, timeout=600, poll_interval=15, logger=None,
                   codebuild_client=None):
    """
    Poll CodeBuild until the given build completes.
    Returns {"success": bool, "status": <final status>}.
    """
    client = codebuild_client or aws.fresh_session(config["region"]).client("codebuild")
    start = time.time()
    while time.time() - start < timeout:
        builds = client.batch_get_builds(ids=[build_id]).get("builds", [])
        if not builds:
            time.sleep(poll_interval)
            continue
        status = builds[0]["buildStatus"]
        if logger:
            logger.info(f"Build {build_id}: {status}")
        if status == "SUCCEEDED":
            return {"success": True, "status": status, "buildId": build_id}
        if status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
            return {"success": False, "status": status, "buildId": build_id,
                    "message": f"Build {status}"}
        time.sleep(poll_interval)
    return {"success": False, "status": "TIMED_OUT", "buildId": build_id,
            "message": "Build timed out"}
