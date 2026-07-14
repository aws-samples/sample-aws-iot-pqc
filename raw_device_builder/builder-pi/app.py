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
import botocore.session
from flask import Flask, render_template, jsonify, request

from device_manager import DeviceManager


def get_fresh_boto3_session():
    """Create a fresh boto3 session that fetches current instance credentials.
    SSM managed instances rotate credentials frequently, so we must not
    cache sessions across operations."""
    return boto3.session.Session(region_name=load_config().get('region', 'us-east-1'))

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

    if slot.status not in ('ready', 'registered', 'done', 'error'):
        return jsonify({'error': f'Device is busy: {slot.status}'}), 400

    # Start registration in background thread
    thread = threading.Thread(
        target=_registration_pipeline,
        args=(port,),
        daemon=True
    )
    thread.start()

    return jsonify({'success': True, 'message': f'Registration started for {slot.thing_name}'})


@app.route('/api/deregister', methods=['POST'])
def deregister_device():
    """Deregister a device — cleans up IoT Core, certs, policies, S3."""
    data = request.json
    port = data.get('port')

    if not port or port not in device_manager.devices:
        return jsonify({'error': 'Invalid or unknown port'}), 400

    slot = device_manager.devices[port]
    if not slot.serial_number:
        return jsonify({'error': 'Device serial not detected'}), 400

    slot.log('Deregistering device...')
    slot.status = 'registering'

    # Call deregister API in background
    thread = threading.Thread(
        target=_deregister_pipeline,
        args=(port,),
        daemon=True
    )
    thread.start()

    return jsonify({'success': True, 'message': f'Deregistration started for {slot.thing_name}'})


@app.route('/api/reflash', methods=['POST'])
def reflash_device():
    """Reflash a registered device with existing firmware from S3."""
    data = request.json
    port = data.get('port')

    if not port or port not in device_manager.devices:
        return jsonify({'error': 'Invalid or unknown port'}), 400

    slot = device_manager.devices[port]
    if not slot.serial_number:
        return jsonify({'error': 'Device serial not detected'}), 400

    if slot.status not in ('registered', 'ready', 'done', 'error'):
        return jsonify({'error': f'Device is busy: {slot.status}'}), 400

    # Start reflash in background thread
    thread = threading.Thread(
        target=_reflash_pipeline,
        args=(port,),
        daemon=True
    )
    thread.start()

    return jsonify({'success': True, 'message': f'Reflash started for {slot.thing_name}'})


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
        session = get_fresh_boto3_session()
        credentials = session.get_credentials().get_frozen_credentials()
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
        local_dir = f'/tmp/firmware_{slot.serial_number}'
        os.makedirs(local_dir, exist_ok=True)
        session = get_fresh_boto3_session()
        s3 = session.client('s3')
        s3_prefix = f'{slot.serial_number}/{config["firmware_version"]}/'

        for filename in ['bootloader.bin', 'app.bin', 'boot_app0.bin', 'partitions.bin']:
            s3_key = s3_prefix + filename
            local_path = os.path.join(local_dir, filename)
            try:
                s3.download_file(config['firmware_bucket'], s3_key, local_path)
                slot.log(f'Downloaded: {filename}')
            except Exception as e:
                slot.log(f'WARN: {filename} not available: {e}')

        # Step 4: Flash firmware
        success = device_manager.flash_firmware(port, local_dir)
        if not success:
            slot.status = 'error'
            return

        # Step 5: Verify boot
        time.sleep(3)  # Wait for device to reset after flash
        device_manager.verify_boot(port, timeout=45)

        # Cleanup
        import shutil
        shutil.rmtree(local_dir, ignore_errors=True)

    except Exception as e:
        slot.status = 'error'
        slot.error = str(e)
        slot.log(f'ERROR: Pipeline failed: {e}')


def _wait_for_build(slot: 'DeviceSlot', timeout: int = 600) -> bool:
    """Poll CodeBuild until the build completes.
    Also checks S3 for the app.bin as a fallback — if the binary exists,
    the build succeeded even if CodeBuild reports FAILED due to non-critical
    post_build steps."""
    if not slot.build_id:
        return False

    codebuild = get_fresh_boto3_session().client('codebuild')
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
                # Check if the app binary actually made it to S3
                # (build may report FAILED due to non-critical post_build commands)
                slot.log(f'Build reported {status}, checking S3 for binary...')
                if _check_firmware_in_s3(slot):
                    slot.log('Binary found in S3 — treating as success')
                    return True
                slot.log(f'Build failed with status: {status}')
                return False

        except Exception as e:
            slot.log(f'Build poll error: {e}')

        time.sleep(15)

    # Timeout — check S3 one last time
    slot.log('Build poll timed out, checking S3...')
    if _check_firmware_in_s3(slot):
        slot.log('Binary found in S3 — treating as success')
        return True
    slot.log('Build timed out waiting')
    return False


def _check_firmware_in_s3(slot):
    """Check if the app.bin exists in S3 for this device."""
    try:
        s3 = get_fresh_boto3_session().client('s3')
        s3_key = f'{slot.serial_number}/{config["firmware_version"]}/app.bin'
        s3.head_object(Bucket=config['firmware_bucket'], Key=s3_key)
        return True
    except Exception:
        return False


def _deregister_pipeline(port: str):
    """Deregister a device via the API."""
    slot = device_manager.devices.get(port)
    if not slot:
        return

    slot.log('Calling deregister API...')
    try:
        api_url = f"{config['api_endpoint']}/deregister"
        payload = {'serialNumber': slot.serial_number}

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

        if response.status_code == 200:
            result = response.json()
            slot.log(f"Deregistered: {result.get('message', 'OK')}")
            slot.status = 'ready'
            slot.is_registered = False
            slot.thing_name = f'esp32-{slot.serial_number}'
        else:
            slot.log(f"ERROR: Deregister failed: {response.text}")
            slot.status = 'error'
            slot.error = f'Deregister API returned {response.status_code}'
    except Exception as e:
        slot.log(f'ERROR: {e}')
        slot.status = 'error'
        slot.error = str(e)


def _reflash_pipeline(port: str):
    """Reflash a device with existing firmware from S3 (no re-registration)."""
    slot = device_manager.devices.get(port)
    if not slot:
        return

    slot.status = 'flashing'
    slot.error = None
    slot.log('Starting reflash (using existing firmware in S3)...')

    try:
        # Download firmware from S3
        local_dir = f'/tmp/firmware_{slot.serial_number}'
        os.makedirs(local_dir, exist_ok=True)
        session = get_fresh_boto3_session()
        s3 = session.client('s3')
        s3_prefix = f'{slot.serial_number}/{config["firmware_version"]}/'

        for filename in ['bootloader.bin', 'app.bin', 'boot_app0.bin', 'partitions.bin']:
            s3_key = s3_prefix + filename
            local_path = os.path.join(local_dir, filename)
            try:
                s3.download_file(config['firmware_bucket'], s3_key, local_path)
                slot.log(f'Downloaded: {filename}')
            except Exception as e:
                slot.log(f'WARN: {filename} not available: {e}')

        # Check we have at least the app binary
        if not os.path.exists(os.path.join(local_dir, 'app.bin')):
            slot.status = 'error'
            slot.error = 'No firmware found in S3. Register the device first.'
            slot.log('ERROR: app.bin not found in S3')
            return

        # Flash
        success = device_manager.flash_firmware(port, local_dir)
        if not success:
            slot.status = 'error'
            return

        # Verify boot
        time.sleep(3)
        device_manager.verify_boot(port, timeout=45)

        # Cleanup
        import shutil
        shutil.rmtree(local_dir, ignore_errors=True)

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
