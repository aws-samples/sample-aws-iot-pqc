#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
workshop-builder — CLI for Builder-Pi device operations.

Replaces the ad-hoc scripts that were written on the fly and run over SSM.
Every operation is a single non-interactive command line with explicit args,
logs a structured transcript, and exits non-zero on failure — so SSM
send-command invocations are reviewable and reproducible.

Shares its implementation with the Flask GUI via builder_core, so behavior
cannot drift between the two.

Examples:
  workshop-builder scan
  workshop-builder register --port /dev/ttyACM0
  workshop-builder register --serial FC22E0          # register without USB
  workshop-builder reflash --port /dev/ttyACM0
  workshop-builder flash --port /dev/ttyACM0 --bin /tmp/firmware.merged.bin
  workshop-builder deregister --serial FC22E0
  workshop-builder serial --port /dev/ttyACM0 --lines 100
  workshop-builder service {start|stop|status}
"""
import argparse
import sys

from builder_core import operations
from builder_core.config import load_config
from builder_core.logging import OperationLogger


def _device_manager(config):
    """Import DeviceManager lazily (it needs pyserial and may touch hardware)."""
    from device_manager import DeviceManager

    return DeviceManager(config)


def cmd_scan(args, config, log):
    dm = _device_manager(config)
    ports = dm.scan_ports()
    for port in ports:
        if port not in dm.devices:
            dm.detect_device(port)
    devices = dm.get_all_devices()
    for d in devices:
        log.info(f"{d.get('port')}  serial={d.get('serial_number')}  status={d.get('status')}")
    if not devices:
        log.info("No devices detected")
    return {"success": True}


def _resolve_serial(args, config, log):
    """Resolve the device serial from --serial or by detecting on --port."""
    if getattr(args, "serial", None):
        return args.serial
    if getattr(args, "port", None):
        dm = _device_manager(config)
        dm.detect_device(args.port)
        slot = dm.devices.get(args.port)
        if slot and slot.serial_number:
            return slot.serial_number
    return None


def cmd_register(args, config, log):
    serial = _resolve_serial(args, config, log)
    if not serial:
        return {"success": False, "message": "Could not determine device serial (use --serial or a connected --port)"}
    reg = operations.register_device(config, serial, logger=log)
    if not reg["success"]:
        return reg
    if reg.get("buildId") and not args.no_wait:
        log.info("Waiting for firmware build...")
        build = operations.wait_for_build(config, reg["buildId"], logger=log)
        if not build["success"]:
            return build
    # If a port is connected, download + flash.
    if getattr(args, "port", None) and not args.no_flash:
        dm = _device_manager(config)
        # Must detect the device on THIS DeviceManager first — a fresh instance
        # has an empty devices map, so dm.devices.get(port) would be None and
        # download_firmware(None)/flash_firmware would crash with
        # "'NoneType' object has no attribute 'status'".
        dm.detect_device(args.port)
        slot = dm.devices.get(args.port)
        if not slot:
            return {"success": False, "message": f"No device detected on {args.port} for flashing"}
        firmware_dir = dm.download_firmware(slot)
        if not firmware_dir:
            return {"success": False, "message": "Firmware download failed"}
        if not dm.flash_firmware(args.port, firmware_dir):
            return {"success": False, "message": "Flash failed"}
        log.info("Flashed successfully")
    return {"success": True, "message": f"Registered {serial}"}


def cmd_reflash(args, config, log):
    dm = _device_manager(config)
    dm.detect_device(args.port)
    slot = dm.devices.get(args.port)
    if not slot:
        return {"success": False, "message": f"No device on {args.port}"}
    firmware_dir = dm.download_firmware(slot)
    if not firmware_dir:
        return {"success": False, "message": "Firmware download failed"}
    if not dm.flash_firmware(args.port, firmware_dir):
        return {"success": False, "message": "Flash failed"}
    log.info("Reflashed successfully")
    return {"success": True}


def cmd_flash(args, config, log):
    dm = _device_manager(config)
    if not dm.flash_firmware(args.port, args.bin):
        return {"success": False, "message": f"Flash of {args.bin} failed"}
    log.info(f"Flashed {args.bin}")
    return {"success": True}


def cmd_deregister(args, config, log):
    serial = _resolve_serial(args, config, log)
    if not serial:
        return {"success": False, "message": "Could not determine device serial (use --serial or a connected --port)"}
    return operations.deregister_device(config, serial, logger=log)


def cmd_serial(args, config, log):
    dm = _device_manager(config)
    lines = dm.read_serial_buffer(args.port, max_lines=args.lines)
    for line in lines:
        print(line)
    return {"success": True}


def cmd_service(args, config, log):
    import subprocess

    action = args.action
    svc = "pqc-builder-gui.service"
    if action == "status":
        rc = subprocess.run(["systemctl", "is-active", svc]).returncode
        return {"success": rc == 0, "message": f"{svc} active={rc == 0}"}
    rc = subprocess.run(["sudo", "systemctl", action, svc]).returncode
    return {"success": rc == 0, "message": f"systemctl {action} {svc} rc={rc}"}


def build_parser():
    p = argparse.ArgumentParser(prog="workshop-builder", description="Builder-Pi device operations")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="List USB slots and detected serials")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("register", help="Register a device, build + flash firmware")
    s.add_argument("--port", help="USB port (e.g. /dev/ttyACM0)")
    s.add_argument("--serial", help="Device serial (skip USB detection)")
    s.add_argument("--no-wait", action="store_true", help="Do not wait for the build")
    s.add_argument("--no-flash", action="store_true", help="Register/build only, do not flash")
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("reflash", help="Re-flash existing S3 firmware")
    s.add_argument("--port", required=True)
    s.set_defaults(func=cmd_reflash)

    s = sub.add_parser("flash", help="Flash a specific binary")
    s.add_argument("--port", required=True)
    s.add_argument("--bin", required=True, help="Path to firmware binary")
    s.set_defaults(func=cmd_flash)

    s = sub.add_parser("deregister", help="Deregister a device")
    s.add_argument("--port", help="USB port (to detect serial)")
    s.add_argument("--serial", help="Device serial")
    s.set_defaults(func=cmd_deregister)

    s = sub.add_parser("serial", help="Dump recent serial output")
    s.add_argument("--port", required=True)
    s.add_argument("--lines", type=int, default=100)
    s.set_defaults(func=cmd_serial)

    s = sub.add_parser("service", help="Control the builder GUI service")
    s.add_argument("action", choices=["start", "stop", "status"])
    s.set_defaults(func=cmd_service)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    log = OperationLogger(args.command)
    try:
        config = load_config()
        result = args.func(args, config, log)
    except Exception as e:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        log.error(str(e))
        log.close()
        return 1
    if result and result.get("message"):
        (log.info if result.get("success") else log.error)(result["message"])
    log.close()
    return 0 if (result and result.get("success")) else 1


if __name__ == "__main__":
    sys.exit(main())
