# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Builder-Pi Web GUI

Flask application that provides a local web interface for managing
ESP32 device registration and firmware flashing.

Access at http://<builder-pi-ip>:8080

Flow:
  1. Operator plugs ESP32 into builder-pi USB
  2. GUI auto-detects device and shows serial number
  3. Operator clicks "Register & Flash"
  4. Lambda registers device (cert from Private CA) and triggers CodeBuild
  5. CodeBuild compiles classical firmware with device-specific certs
  6. Builder-pi downloads firmware.merged.bin from S3
  7. arduino-cli upload flashes the device
  8. Device boots, connects to broker

Future OTA updates (PQC firmware) delivered via presigned S3 URL over MQTT.
"""

import json
import os
import threading
import time
import requests
import boto3
from flask import Flask, render_template, jsonify, request

from device_manager import DeviceManager
# Shared implementation so the GUI and the workshop-builder CLI cannot drift.
from builder_core import operations
from builder_core.aws import fresh_session
from builder_core.config import load_config, save_config


def get_fresh_boto3_session():
    """Fresh boto3 session with current instance credentials (SSM rotates them)."""
    return fresh_session(load_config().get('region', 'us-east-1'))


config = load_config()

# ─── Flask App ───────────────────────────────────────────────────────────────
app = Flask(__name__)
device_manager = DeviceManager(config)

# Background thread for periodic port scanning
scan_running = True


def port_scanner():
    """Background thread that periodically scans for new USB devices."""
    while scan_running:
        try:
            current_ports = set(device_manager.scan_ports())
            known_ports = set(device_manager.devices.keys())

            # Detect new devices
            for port in current_ports - known_ports:
                device_manager.detect_device(port)

            # Remove disconnected devices
            for port in known_ports - current_ports:
                device_manager.remove_device(port)

        except Exception as e:
            print(f"Port scan error: {e}")

        time.sleep(3)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    """Main GUI page."""
    return render_template('index.html', config=config)


@app.route('/api/devices')
def get_devices():
    """Return current device status for all USB ports."""
    return jsonify(device_manager.get_all_devices())


@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """Get or update configuration."""
    global config
    if request.method == 'POST':
        updates = request.json
        config.update(updates)
        save_config(config)
        return jsonify({'success': True, 'config': config})
    return jsonify(config)


@app.route('/api/system-params')
def get_system_params():
    """Read-only workshop parameters resolved from SSM (the single source of
    truth) for display in the GUI. The operator does not edit these — they are
    baked into firmware server-side by the registrar. Returns the WiFi SSID and
    broker endpoint (and region for context). The WiFi password is intentionally
    NOT returned — it is a secret and not needed for display. Never raises: a
    lookup failure just yields an empty value the GUI shows as unavailable."""
    wifi_ssid = ''
    broker = ''
    try:
        ssm = get_fresh_boto3_session().client('ssm')
        # SSID is a plain String (not a secret); safe to show for reference.
        wifi_ssid = ssm.get_parameter(
            Name='/pqc-workshop/wifi/ssid'
        )['Parameter']['Value']
    except Exception as e:
        print(f"system-params: wifi ssid SSM read failed: {e}")
    try:
        ssm = get_fresh_boto3_session().client('ssm')
        broker = ssm.get_parameter(
            Name='/pqc-workshop/broker/endpoint'
        )['Parameter']['Value']
    except Exception as e:
        print(f"system-params: broker endpoint SSM read failed: {e}")
    return jsonify({
        'wifiSsid': wifi_ssid,
        'brokerEndpoint': broker,
        'region': config.get('region', ''),
    })


@app.route('/api/register', methods=['POST'])
def register_device():
    """Trigger device registration and firmware build+flash pipeline."""
    data = request.json
    port = data.get('port')

    if not port or port not in device_manager.devices:
        return jsonify({'error': 'Invalid or unknown port'}), 400

    slot = device_manager.devices[port]
    if not slot.serial_number:
        return jsonify({'error': 'Device serial not detected'}), 400

    if slot.status not in ('ready', 'registered', 'done', 'error'):
        return jsonify({'error': f'Device is busy: {slot.status}'}), 400

    # Start registration pipeline in background
    thread = threading.Thread(
        target=_registration_pipeline,
        args=(port,),
        daemon=True
    )
    thread.start()

    return jsonify({'success': True, 'message': f'Registration started for {slot.thing_name}'})


@app.route('/api/reflash', methods=['POST'])
def reflash_device():
    """Re-flash a registered device with existing firmware from S3."""
    data = request.json
    port = data.get('port')

    if not port or port not in device_manager.devices:
        return jsonify({'error': 'Invalid or unknown port'}), 400

    slot = device_manager.devices[port]
    if not slot.serial_number:
        return jsonify({'error': 'Device serial not detected'}), 400

    if slot.status not in ('registered', 'ready', 'done', 'error'):
        return jsonify({'error': f'Device is busy: {slot.status}'}), 400

    thread = threading.Thread(
        target=_reflash_pipeline,
        args=(port,),
        daemon=True
    )
    thread.start()

    return jsonify({'success': True, 'message': f'Reflash started for {slot.thing_name}'})


@app.route('/api/deregister', methods=['POST'])
def deregister_device():
    """Deregister a device — calls the API Gateway deregister endpoint."""
    data = request.json
    port = data.get('port')

    if not port or port not in device_manager.devices:
        return jsonify({'error': 'Invalid or unknown port'}), 400

    slot = device_manager.devices[port]
    if not slot.serial_number:
        return jsonify({'error': 'Device serial not detected'}), 400

    slot.log('Deregistering device...')

    try:
        # Shared implementation with the CLI (builder_core.operations).
        result = operations.deregister_device(config, slot.serial_number)
        if result['success']:
            slot.log('Device deregistered successfully')
            slot.status = 'ready'
            slot.is_registered = False
            return jsonify({'success': True, 'message': f'Deregistered {slot.thing_name}'})
        slot.log(f"Deregister failed: {result['message']}")
        return jsonify({'error': result['message']}), 500
    except Exception as e:
        slot.log(f'ERROR: Deregister failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/serial/<path:port>')
def get_serial_output(port):
    """Get recent serial output from a device."""
    port = '/' + port  # Restore leading slash
    if port not in device_manager.devices:
        return jsonify({'error': 'Unknown port'}), 404

    slot = device_manager.devices[port]
    lines = device_manager.read_serial_buffer(port, max_lines=100)
    return jsonify({'port': port, 'thingName': slot.thing_name, 'lines': lines})


@app.route('/api/scan', methods=['POST'])
def force_scan():
    """Force a USB port rescan."""
    ports = device_manager.scan_ports()
    for port in ports:
        if port not in device_manager.devices:
            device_manager.detect_device(port)
    return jsonify({'ports': ports, 'devices': device_manager.get_all_devices()})


# ─── Registration Pipeline ───────────────────────────────────────────────────

def _registration_pipeline(port: str):
    """Execute the full registration -> build -> download -> flash -> verify pipeline."""
    slot = device_manager.devices.get(port)
    if not slot:
        return

    slot.status = 'registered'
    slot.error = None
    slot.log('Starting registration pipeline...')

    # Stop serial monitor during flash
    device_manager.stop_serial_monitor(port)

    try:
        # Step 1: Call the device-registrar API
        slot.log(f'Registering device serial={slot.serial_number}')
        api_url = f"{config['api_endpoint']}/register"

        # Plug-and-flash: the only device-specific input is the auto-detected
        # serial. WiFi, broker endpoint and firmware version are resolved
        # server-side by the registrar Lambda from SSM (the single source of
        # truth) / its env defaults — no operator input, no drift.
        payload = {
            'serialNumber': slot.serial_number,
        }

        # Use SigV4 auth (builder-pi has SSM role with API access)
        session = get_fresh_boto3_session()
        credentials = session.get_credentials().get_frozen_credentials()

        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        aws_request = AWSRequest(method='POST', url=api_url,
                                 data=json.dumps(payload),
                                 headers={'Content-Type': 'application/json'})
        SigV4Auth(credentials, 'execute-api', config['region']).add_auth(aws_request)
        response = requests.post(api_url, json=payload,
                                 headers=dict(aws_request.headers), timeout=30)

        if response.status_code != 200:
            slot.status = 'error'
            slot.error = f'Registration API returned {response.status_code}'
            slot.log(f'ERROR: {slot.error}: {response.text}')
            return

        result = response.json()
        slot.build_id = result.get('buildId')
        slot.log(f'Registered! Build ID: {slot.build_id}')
        slot.is_registered = True

        # Step 2: Wait for CodeBuild to complete
        slot.status = 'building'
        slot.log('Waiting for firmware compilation...')

        if not _wait_for_build(slot):
            slot.status = 'error'
            slot.error = 'Firmware build failed or timed out'
            slot.log(f'ERROR: {slot.error}')
            return

        slot.log('Firmware build complete!')

        # Step 3: Download firmware from S3
        firmware_dir = device_manager.download_firmware(slot)
        if not firmware_dir:
            slot.status = 'error'
            return

        # Step 4: Flash firmware via arduino-cli upload
        success = device_manager.flash_firmware(port, firmware_dir)
        if not success:
            slot.status = 'error'
            return

        # Step 5: Verify boot
        time.sleep(3)  # Wait for device to reset after flash
        device_manager.verify_boot(port, timeout=45)

        # Start serial monitor for ongoing observation
        device_manager.start_serial_monitor(port)

    except Exception as e:
        slot.status = 'error'
        slot.error = str(e)
        slot.log(f'ERROR: Pipeline failed: {e}')


def _wait_for_build(slot, timeout: int = 600) -> bool:
    """Poll CodeBuild until the build completes."""
    if not slot.build_id:
        return False

    session = get_fresh_boto3_session()
    codebuild = session.client('codebuild')
    start = time.time()

    while time.time() - start < timeout:
        try:
            response = codebuild.batch_get_builds(ids=[slot.build_id])
            builds = response.get('builds', [])
            if not builds:
                time.sleep(10)
                continue

            build = builds[0]
            status = build['buildStatus']
            phase = build.get('currentPhase', 'UNKNOWN')

            slot.log(f'Build: {status} (phase: {phase})')

            if status == 'SUCCEEDED':
                return True
            elif status in ('FAILED', 'FAULT', 'TIMED_OUT', 'STOPPED'):
                slot.log(f'Build failed: {status}')
                return False

        except Exception as e:
            slot.log(f'Build poll error: {e}')

        time.sleep(15)

    slot.log('Build timed out waiting')
    return False


def _reflash_pipeline(port: str):
    """Re-flash a device with existing firmware from S3 (no re-registration)."""
    slot = device_manager.devices.get(port)
    if not slot:
        return

    slot.error = None
    slot.log('Starting reflash (using existing firmware in S3)...')

    # Stop serial monitor during flash
    device_manager.stop_serial_monitor(port)

    try:
        # Download firmware
        firmware_dir = device_manager.download_firmware(slot)
        if not firmware_dir:
            slot.status = 'error'
            return

        # Flash
        success = device_manager.flash_firmware(port, firmware_dir)
        if not success:
            slot.status = 'error'
            return

        # Verify boot
        time.sleep(3)
        device_manager.verify_boot(port, timeout=45)

        # Restart serial monitor
        device_manager.start_serial_monitor(port)

    except Exception as e:
        slot.status = 'error'
        slot.error = str(e)
        slot.log(f'ERROR: Reflash failed: {e}')


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Start background port scanner
    scan_thread = threading.Thread(target=port_scanner, daemon=True)
    scan_thread.start()

    print(f"Builder-Pi GUI starting on port {config['gui_port']}...")
    print(f"Access at: http://0.0.0.0:{config['gui_port']}")
    app.run(host='0.0.0.0', port=config['gui_port'], debug=False)
