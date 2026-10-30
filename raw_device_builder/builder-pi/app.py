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
  4. Lambda registers the device and starts two CodeBuild jobs: the classical
     firmware build and the device-identity job (device certs + WiFi in a
     "certs" NVS partition image)
  5. Builder-pi waits for both, downloads the app pieces and the identity image
  6. esptool flashes both in one call; the identity image is then deleted
  7. Device boots, connects to broker

Reflash re-issues the identity (identityOnly) and reuses the firmware in S3.

Future OTA updates (PQC firmware) delivered via presigned S3 URL over MQTT.
"""

import threading
import time
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

        time.sleep(3)  # nosemgrep: arbitrary-sleep


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
            slot.is_registered = False  # nosemgrep: is-function-without-parentheses
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
        # Step 1: register. Plug-and-flash: the only input is the auto-detected
        # serial; WiFi, broker endpoint and firmware version are resolved
        # server-side from SSM. Starts the firmware build AND the identity job.
        reg = operations.register_device(config, slot.serial_number, logger=_SlotLogger(slot))
        if not reg['success']:
            slot.status = 'error'
            slot.error = reg['message']
            slot.log(f'ERROR: {slot.error}')
            return
        slot.build_id = reg['buildId']
        slot.identity_build_id = reg['identityBuildId']
        slot.is_registered = True  # nosemgrep: is-function-without-parentheses

        # Steps 2-5: wait for both builds, download, flash, verify.
        _build_and_flash(slot, port, reg, 'Waiting for identity build (firmware is the shared image)...')

    except Exception as e:
        slot.status = 'error'
        slot.error = str(e)
        slot.log(f'ERROR: Pipeline failed: {e}')


class _SlotLogger:
    """Adapts DeviceSlot.log to the logger interface builder_core expects."""

    def __init__(self, slot):
        self.slot = slot

    def info(self, message):
        self.slot.log(message)

    error = info


def _build_and_flash(slot, port, reg, waiting_message):
    """Wait for the registration's builds, then download, flash and verify."""
    slot.status = 'building'
    slot.log(waiting_message)
    build = operations.wait_for_builds(config, operations.build_ids(reg),
                                       logger=_SlotLogger(slot))
    if not build['success']:
        slot.status = 'error'
        slot.error = build.get('message', 'Build failed or timed out')
        slot.log(f'ERROR: {slot.error}')
        return False
    slot.log('Builds complete')

    firmware_dir = device_manager.download_firmware(slot, reg['identityKey'],
                                                   reg.get('firmwarePrefix'))
    if not firmware_dir:
        slot.status = 'error'
        return False

    # Flash app + identity; deletes the identity image on success.
    if not device_manager.flash_device(port, firmware_dir):
        slot.status = 'error'
        return False

    time.sleep(3)  # Wait for device to reset after flash  # nosemgrep: arbitrary-sleep
    device_manager.verify_boot(port, timeout=45)

    # Start serial monitor for ongoing observation
    device_manager.start_serial_monitor(port)
    return True


def _reflash_pipeline(port: str):
    """Re-flash a registered device: existing firmware from S3, new identity.

    The identity image is deleted after every flash, so a reflash asks the
    registrar for a fresh one (identityOnly) — no firmware rebuild.
    """
    slot = device_manager.devices.get(port)
    if not slot:
        return

    slot.error = None
    slot.log('Starting reflash (existing firmware, new identity)...')

    # Stop serial monitor during flash
    device_manager.stop_serial_monitor(port)

    try:
        reg = operations.register_device(config, slot.serial_number, identity_only=True,
                                         logger=_SlotLogger(slot))
        if not reg['success']:
            slot.status = 'error'
            slot.error = reg['message']
            slot.log(f'ERROR: {slot.error}')
            return
        slot.identity_build_id = reg['identityBuildId']
        _build_and_flash(slot, port, reg, 'Waiting for identity build...')

    except Exception as e:
        slot.status = 'error'
        slot.error = str(e)
        slot.log(f'ERROR: Reflash failed: {e}')


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Start background port scanner
    scan_thread = threading.Thread(target=port_scanner, daemon=True)
    scan_thread.start()

    # Bind host is configurable (config.json "gui_host"). It defaults to all
    # interfaces because the GUI is reached from the operator's laptop over the
    # Pi's workshop WiFi access point, not from the Pi itself — so loopback would
    # make it unreachable. Operators who want to narrow the exposure can set
    # "gui_host" to the AP interface IP (e.g. the NetworkManager shared-AP
    # address) or to "127.0.0.1" for local-only access. Sourcing the value from
    # config (rather than a literal in the call) also keeps the bind address a
    # deliberate, documented deployment choice.
    gui_host = config.get('gui_host', '0.0.0.0')  # nosec B104 - all-interfaces default is intentional: the GUI is reached over the Pi's workshop WiFi AP, not loopback; operator-overridable via config "gui_host"
    print(f"Builder-Pi GUI starting on {gui_host}:{config['gui_port']}...")
    print(f"Access at: http://{gui_host}:{config['gui_port']}")
    app.run(host=gui_host, port=config['gui_port'], debug=False)
