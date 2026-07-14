"""
Builder-Pi Web GUI

Flask application that provides a local web interface for managing
ESP32 device registration and firmware flashing.

Access at http://<builder-pi-ip>:8080

Features:
  - Auto-detects USB-connected ESP32 devices (up to 4)
  - Shows device serial number and status
  - Allows operator to trigger registration + flash pipeline
  - Displays real-time progress log per device
  - Configurable WiFi, broker, and endpoint settings
"""

import json
import os
import threading
import time
import requests
import boto3
from flask import Flask, render_template, jsonify, request

from device_manager import DeviceManager

# ─── Configuration ───────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

config = load_config()

# ─── Flask App ───────────────────────────────────────────────────────────────
app = Flask(__name__)
device_manager = DeviceManager(config)

# Background thread for periodic port scanning
scan_thread = None
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


@app.route('/api/register', methods=['POST'])
def register_device():
    """Trigger device registration for a specific port."""
    data = request.json
    port = data.get('port')

    if not port or port not in device_manager.devices:
        return jsonify({'error': 'Invalid or unknown port'}), 400

    slot = device_manager.devices[port]
    if not slot.serial_number:
        return jsonify({'error': 'Device serial not detected'}), 400

    if slot.status not in ('ready', 'done', 'error'):
        return jsonify({'error': f'Device is busy: {slot.status}'}), 400

    # Start registration in background thread
    thread = threading.Thread(
        target=_registration_pipeline,
        args=(port,),
        daemon=True
    )
    thread.start()

    return jsonify({'success': True, 'message': f'Registration started for {slot.thing_name}'})


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
    """Execute the full registration → build → flash → verify pipeline."""
    slot = device_manager.devices.get(port)
    if not slot:
        return

    slot.status = 'registering'
    slot.error = None
    slot.log('Starting registration pipeline...')

    try:
        # Step 1: Call the registration API
        slot.log(f'Calling registration API for serial={slot.serial_number}')
        api_url = f"{config['api_endpoint']}/register"

        payload = {
            'serialNumber': slot.serial_number,
            'wifiSsid': config['wifi_ssid'],
            'wifiPassword': config['wifi_password'],
            'brokerEndpoint': config['broker_endpoint'],
            'firmwareVersion': config['firmware_version']
        }

        # Use IAM auth via SigV4 (builder-pi has SSM role with API access)
        session = boto3.session.Session()
        credentials = session.get_credentials()
        if credentials:
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest

            aws_request = AWSRequest(method='POST', url=api_url,
                                     data=json.dumps(payload),
                                     headers={'Content-Type': 'application/json'})
            SigV4Auth(credentials, 'execute-api', config['region']).add_auth(aws_request)
            response = requests.post(api_url,
                                     json=payload,
                                     headers=dict(aws_request.headers),
                                     timeout=30)
        else:
            # Fallback: direct call (for testing without IAM)
            response = requests.post(api_url, json=payload, timeout=30)

        if response.status_code != 200:
            slot.status = 'error'
            slot.error = f'Registration API returned {response.status_code}'
            slot.log(f'ERROR: {slot.error}: {response.text}')
            return

        result = response.json()
        slot.build_id = result.get('buildId')
        slot.log(f'Registration successful! Build ID: {slot.build_id}')

        # Step 2: Wait for CodeBuild to complete
        slot.status = 'building'
        slot.log('Waiting for firmware build...')

        firmware_path = _wait_for_build(slot)
        if not firmware_path:
            slot.status = 'error'
            slot.error = 'Build failed or timed out'
            slot.log(f'ERROR: {slot.error}')
            return

        # Step 3: Download firmware from S3
        slot.log(f'Downloading firmware from S3...')
        local_firmware = f'/tmp/firmware_{slot.serial_number}.bin'
        s3 = boto3.client('s3')
        s3_key = f'{slot.serial_number}/{config["firmware_version"]}/firmware.bin'
        s3.download_file(config['firmware_bucket'], s3_key, local_firmware)
        slot.log(f'Downloaded: {s3_key}')

        # Step 4: Flash firmware
        success = device_manager.flash_firmware(port, local_firmware)
        if not success:
            slot.status = 'error'
            return

        # Step 5: Verify boot
        time.sleep(3)  # Wait for device to reset after flash
        device_manager.verify_boot(port, timeout=45)

        # Cleanup
        os.remove(local_firmware)

    except Exception as e:
        slot.status = 'error'
        slot.error = str(e)
        slot.log(f'ERROR: Pipeline failed: {e}')


def _wait_for_build(slot: 'DeviceSlot', timeout: int = 300) -> bool:
    """Poll CodeBuild until the build completes."""
    if not slot.build_id:
        return False

    codebuild = boto3.client('codebuild', region_name=config['region'])
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

            slot.log(f'Build status: {status} (phase: {phase})')

            if status == 'SUCCEEDED':
                return True
            elif status in ('FAILED', 'FAULT', 'TIMED_OUT', 'STOPPED'):
                slot.log(f'Build failed with status: {status}')
                return False

        except Exception as e:
            slot.log(f'Build poll error: {e}')

        time.sleep(15)

    slot.log('Build timed out waiting')
    return False


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Start background port scanner
    scan_thread = threading.Thread(target=port_scanner, daemon=True)
    scan_thread.start()

    print(f"Builder-Pi GUI starting on port {config['gui_port']}...")
    print(f"Access at: http://0.0.0.0:{config['gui_port']}")
    app.run(host='0.0.0.0', port=config['gui_port'], debug=False)
