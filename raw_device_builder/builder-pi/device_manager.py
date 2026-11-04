# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Device Manager — USB detection, serial identification, and flash operations.

Uses pyserial to list USB serial ports and esptool to read the chip MAC and
flash. The Pi never compiles firmware: it flashes prebuilt images from S3.
Handles up to 4 simultaneous USB-connected ESP32 devices on the builder-pi.

Flashing writes the app pieces plus the per-device identity image (the "certs"
NVS partition) in one esptool call, at the offsets in FLASH_LAYOUT. It does not
use firmware.merged.bin, which is padded to 8 MB and would overwrite "certs".
"""

import subprocess
import sys
import time
import os
import glob
import threading
import serial
from dataclasses import dataclass, field
from typing import Optional, Dict, List


# ─── Constants ───────────────────────────────────────────────────────────────
# USB vendor IDs of the ESP32 boards and USB-UART bridges we flash (the same
# set as the udev rule the configure-service SSM document installs):
# Espressif native USB (XIAO ESP32-S3, 0x303a), Silicon Labs CP210x, WCH CH340,
# FTDI.
ESP32_USB_VIDS = {0x303A, 0x10C4, 0x1A86, 0x0403}
FIRMWARE_DIR = "/opt/pqc-workshop/firmware"

# What is written over USB, as (offset, local file, S3 name under the shared
# firmware prefix shared/<version>/). Offsets match
# esp32-flash/partitions_pqc_workshop.csv.
# The identity image comes from identity/<serial>/certs.bin (S3 name None).
IDENTITY_FILE = "certs.bin"
FLASH_LAYOUT = [
    ("0x0", "bootloader.bin", "bootloader.bin"),
    ("0x8000", "partitions.bin", "partitions.bin"),
    ("0xe000", "boot_app0.bin", "boot_app0.bin"),  # resets otadata -> boot ota_0
    ("0x10000", "app.bin", "firmware.bin"),
    ("0x721000", IDENTITY_FILE, None),             # "certs" NVS partition
]


def flash_args(firmware_dir: str) -> List[str]:
    """The <offset> <file> pairs for esptool write_flash."""
    args = []
    for offset, name, _s3_name in FLASH_LAYOUT:
        args += [offset, os.path.join(firmware_dir, name)]
    return args


def esptool_bin() -> str:
    """The esptool entry point installed alongside the running interpreter.

    On the Pi the app runs from its own virtualenv (/opt/pqc-workshop/venv), so
    esptool lives in that venv's bin/, which is not on a login shell's PATH.
    Resolving it next to sys.executable makes the GUI and the workshop-builder
    CLI use the locked esptool however they were started. Falls back to
    'esptool' on PATH (e.g. a dev machine without a venv).
    """
    candidate = os.path.join(os.path.dirname(sys.executable), 'esptool')
    return candidate if os.access(candidate, os.X_OK) else 'esptool'


def esptool_argv(port: str, address_args: List[str]) -> List[str]:
    """Full esptool command line for a write_flash of address_args."""
    return [
        esptool_bin(), '--port', port,
        '--baud', '921600',
        '--chip', 'esp32s3',
        'write_flash',
        '--flash_mode', 'dio',
        '--flash_freq', '80m',
        '--flash_size', '8MB',
        *address_args,
    ]

# Persistent log directory. Every DeviceSlot.log() line is appended here (in
# addition to the in-memory ring buffer surfaced to the GUI), so the full
# flash/download/serial transcript survives GUI restarts and device unplugs and
# can be shipped to CloudWatch by the CloudWatch agent on the Pi. Overridable
# for dev checkouts via WORKSHOP_BUILDER_LOG_DIR; falls back silently if the
# directory is not writable.
LOG_DIR = os.environ.get("WORKSHOP_BUILDER_LOG_DIR", "/var/log/workshop-builder")


def _append_log_file(name: str, line: str):
    """Best-effort append of a single line to LOG_DIR/<name>.log.

    Never raises — logging must not break a flash operation if the log
    directory is missing/read-only (e.g. a dev checkout).
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"{name}.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def classify_boot_line(line: str) -> Optional[str]:
    """Classify one line of firmware serial output during boot verification.

    Matches the exact success/failure lines the firmware logs (ESP_LOG,
    lowercase tags such as "[wifi]" / "[mqtt]"). Returns 'wifi', 'mqtt',
    'identity' (fail-closed: no valid NVS identity), 'panic', or None. Retry
    lines such as "Connecting to SSID" / "Connecting to PQC Mosquitto broker"
    deliberately do NOT count as connected.
    """
    low = line.lower()
    if 'panic' in low or 'guru meditation' in low:
        return 'panic'
    if '[setup] fatal' in low:
        return 'identity'
    if 'connected to pqc mosquitto broker' in low:
        return 'mqtt'
    if '[wifi] connected!' in low:
        return 'wifi'
    return None


@dataclass
class DeviceSlot:
    """Represents a USB port slot with an attached ESP32."""
    port: str
    serial_number: Optional[str] = None
    status: str = 'detecting'  # detecting, ready, registered, building, downloading, flashing, verifying, done, error
    thing_name: Optional[str] = None
    build_id: Optional[str] = None
    error: Optional[str] = None
    progress: List[str] = field(default_factory=list)
    is_registered: bool = False
    identity_build_id: Optional[str] = None
    # True once the registry has answered for this serial. A lookup that fails
    # (e.g. at boot, before the network or the clock is up) leaves it False and
    # the slot in 'detecting', so the scanner retries instead of offering
    # "Register" for a device that is already registered.
    registry_checked: bool = False
    last_registry_try: float = 0.0
    # S3 key + version of the downloaded identity image, deleted after flashing.
    identity_key: Optional[str] = None
    identity_version_id: Optional[str] = None

    def log(self, message: str):
        timestamp = time.strftime('%H:%M:%S')
        line = f'[{timestamp}] {message}'
        # In-memory ring buffer for the GUI (last 50, surfaced 20 to the browser).
        self.progress.append(line)
        if len(self.progress) > 50:
            self.progress = self.progress[-50:]
        # Persistent, full transcript per device (keyed by serial when known,
        # else the tty port). Survives GUI restart/unplug; shipped to CloudWatch.
        device_key = (self.serial_number or self.port or 'unknown').replace('/', '_')
        full = f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} [{device_key}] {message}'
        _append_log_file(f'device-{device_key}', full)
        # Also to a single combined stream so `journalctl`/agent tailing is simple.
        _append_log_file('flash', full)

    def to_dict(self):
        return {
            'port': self.port,
            'serialNumber': self.serial_number,
            'status': self.status,
            'thingName': self.thing_name,
            'buildId': self.build_id,
            'error': self.error,
            'isRegistered': self.is_registered,  # nosemgrep: is-function-without-parentheses
            'progress': self.progress[-20:]
        }


class DeviceManager:
    """Manages the USB-connected ESP32 devices on the builder-Pi."""

    def __init__(self, config: dict):
        self.config = config
        self.devices: Dict[str, DeviceSlot] = {}
        self._lock = threading.Lock()
        self._serial_buffers: Dict[str, List[str]] = {}
        self._serial_threads: Dict[str, threading.Thread] = {}
        self._serial_stop_events: Dict[str, threading.Event] = {}

    def scan_ports(self) -> List[str]:
        """Detect connected ESP32 USB serial ports.

        Primary: pyserial's port list, filtered to the ESP32 USB vendor IDs.
        Fallback (no pyserial metadata, e.g. an unusual kernel): glob the tty
        nodes the boards appear as.
        """
        ports = []
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports()
                     if p.vid in ESP32_USB_VIDS]
        except Exception as e:
            print(f"pyserial port listing failed: {e}")

        if not ports:
            for pattern in ('/dev/ttyUSB*', '/dev/ttyACM*'):
                ports.extend(glob.glob(pattern))

        return sorted(set(ports))

    def get_serial_number(self, port: str) -> Optional[str]:
        """Read the ESP32 chip MAC/serial via esptool."""
        try:
            result = subprocess.run(
                [esptool_bin(), '--port', port, 'read_mac'],
                capture_output=True, text=True, timeout=15
            )
            output = result.stdout + result.stderr

            for line in output.split('\n'):
                if 'MAC:' in line:
                    mac = line.split('MAC:')[1].strip().replace(':', '')
                    # Use last 6 chars (3 bytes) as serial — matches esp32-XXYYZZ format
                    return mac[-6:].upper()

            return None
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"Error reading serial from {port}: {e}")
            return None

    def detect_device(self, port: str) -> Optional[DeviceSlot]:
        """Detect and identify a device on a USB port."""
        with self._lock:
            if port in self.devices:
                return self.devices[port]

            slot = DeviceSlot(port=port)
            slot.log(f'Detecting device on {port}...')
            self.devices[port] = slot

        serial_number = self.get_serial_number(port)
        if serial_number:
            slot.serial_number = serial_number
            slot.thing_name = f'esp32-{serial_number}'
            slot.log(f'Device identified: {slot.thing_name} (serial: {serial_number})')

            self._apply_registration(slot, self._check_registration(serial_number))
        else:
            slot.status = 'error'
            slot.error = 'Could not read device serial number'
            slot.log('ERROR: Failed to read chip ID via esptool')

        # Begin capturing serial as soon as a device is identified, so the GUI
        # Serial view / CLI have data regardless of whether a flash pipeline has
        # run. (The monitor is stopped during flash by the pipelines and
        # restarted after.) Only for identified devices — a port we couldn't
        # read a serial from is likely mid-flash/bootloader.
        if slot.serial_number:
            self.start_serial_monitor(port)

        return slot

    def _check_registration(self, serial_number: str) -> Optional[bool]:
        """Is the device in the registry? True / False, or None if the lookup
        failed (network, credentials or clock not ready) and must be retried."""
        try:
            import boto3
            session = boto3.session.Session(region_name=self.config.get('region', 'us-east-1'))
            dynamodb = session.resource('dynamodb')
            table = dynamodb.Table('pqc-workshop-device-registry')
            response = table.get_item(Key={'serialNumber': serial_number})
            return 'Item' in response
        except Exception as e:
            print(f"Registry lookup for {serial_number} failed: {e}")
            return None

    def _apply_registration(self, slot: DeviceSlot, registered: Optional[bool]):
        slot.last_registry_try = time.time()
        if registered is None:
            if slot.status != 'detecting':
                slot.log('Registry lookup failed; retrying (network or clock not ready?)')
            slot.status = 'detecting'
            return
        slot.registry_checked = True
        if registered:
            slot.is_registered = True  # nosemgrep: is-function-without-parentheses
            slot.status = 'registered'
            slot.log('Device is already registered')
        else:
            slot.status = 'ready'
            slot.log('New device — ready for registration')

    def recheck_registrations(self, interval: float = 10.0):
        """Retry the registry lookup for identified devices whose lookup failed.
        Called by the GUI's port scanner loop."""
        now = time.time()
        with self._lock:
            pending = [s for s in self.devices.values()
                       if s.serial_number and not s.registry_checked
                       and s.status == 'detecting' and now - s.last_registry_try >= interval]
        for slot in pending:
            self._apply_registration(slot, self._check_registration(slot.serial_number))

    def _s3(self):
        import boto3
        session = boto3.session.Session(region_name=self.config.get('region', 'us-east-1'))
        return session.client('s3')

    def download_firmware(self, slot: DeviceSlot, identity_key: Optional[str] = None,
                          firmware_prefix: Optional[str] = None) -> Optional[str]:
        """Download the app pieces and the device identity image from S3.

        The app pieces are the SHARED image for the version (one build per
        variant per deployment, identical for every device); only the identity
        image is per device. firmware_prefix comes from the registrar's
        response, defaulting to shared/<firmware_version>.

        Writes FLASH_LAYOUT's files into FIRMWARE_DIR/<serial>/<version>/ and
        returns that directory, or None on failure. Every piece is required:
        a device flashed without its identity cannot connect.

        The identity image (identity/<serial>/certs.bin) is fetched with
        get_object so its S3 VersionId is recorded; delete_identity() removes
        exactly that version once the flash succeeds.
        """
        slot.status = 'downloading'
        slot.log('Downloading firmware and identity from S3...')

        firmware_version = self.config.get('firmware_version', '1.0.0-classical')
        s3_prefix = (firmware_prefix or f'shared/{firmware_version}').rstrip('/')
        local_dir = os.path.join(FIRMWARE_DIR, slot.serial_number, firmware_version)
        os.makedirs(local_dir, exist_ok=True)
        identity_key = identity_key or f'identity/{slot.serial_number}/certs.bin'

        try:
            s3 = self._s3()
            bucket = self.config['firmware_bucket']

            for _offset, local_name, s3_name in FLASH_LAYOUT:
                if s3_name is None:
                    continue  # the identity image, fetched below
                path = os.path.join(local_dir, local_name)
                s3.download_file(bucket, f'{s3_prefix}/{s3_name}', path)
                slot.log(f'Downloaded: {s3_name} ({os.path.getsize(path)} bytes)')

            obj = self._get_identity(s3, bucket, identity_key, slot)
            path = os.path.join(local_dir, IDENTITY_FILE)
            # Owner-only: the image holds the device private keys + WiFi PSK.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(obj['Body'].read())
            slot.identity_key = identity_key
            slot.identity_version_id = obj.get('VersionId')
            slot.log(f'Downloaded: {identity_key} ({os.path.getsize(path)} bytes)')

            return local_dir

        except Exception as e:
            slot.error = f'Download failed: {e}'
            slot.log(f'ERROR: {slot.error}')
            return None

    @staticmethod
    def _get_identity(s3, bucket, key, slot, attempts=4, delay=3):
        """get_object with a short retry: a just-finished identity build's
        object can lag the SUCCEEDED status by a moment."""
        for attempt in range(1, attempts + 1):
            try:
                return s3.get_object(Bucket=bucket, Key=key)
            except s3.exceptions.NoSuchKey:
                if attempt == attempts:
                    raise
                slot.log(f'Identity image not in S3 yet; retrying ({attempt}/{attempts - 1})')
                time.sleep(delay)  # nosemgrep: arbitrary-sleep

    @staticmethod
    def _identity_versions(s3, bucket, key):
        """All version ids (incl. delete markers) of exactly this key."""
        ids = []
        for page in s3.get_paginator('list_object_versions').paginate(Bucket=bucket, Prefix=key):
            for v in page.get('Versions', []) + page.get('DeleteMarkers', []):
                if v['Key'] == key:
                    ids.append(v['VersionId'])
        return ids

    def delete_identity(self, slot: DeviceSlot, firmware_dir: str) -> bool:
        """Delete the device's identity image from S3 — EVERY version, not just
        the one flashed — and the local copy, so the keys exist only on the
        device. Older versions are identities issued but never flashed (e.g. a
        registration whose flash failed, then a Reflash); each holds a valid
        device key, so none may linger as a noncurrent version.

        Falls back to deleting just the downloaded version if versions cannot
        be listed. Best effort: failures are logged, and the S3 lifecycle rule
        expires identity objects after a day anyway.
        """
        ok = True
        if slot.identity_key:
            bucket = self.config['firmware_bucket']
            s3 = self._s3()
            try:
                targets = self._identity_versions(s3, bucket, slot.identity_key)
            except Exception as e:
                slot.log(f'WARN: cannot list versions of {slot.identity_key} ({e}); '
                         'deleting only the flashed version')
                targets = [slot.identity_version_id] if slot.identity_version_id else [None]
            for version_id in targets:
                try:
                    args = {'Bucket': bucket, 'Key': slot.identity_key}
                    if version_id:
                        args['VersionId'] = version_id
                    s3.delete_object(**args)
                except Exception as e:
                    ok = False
                    slot.log(f'WARN: could not delete {slot.identity_key} '
                             f'version {version_id}: {e}')
            if ok:
                slot.log(f'Deleted {slot.identity_key} from S3 ({len(targets)} version(s))')
        try:
            os.remove(os.path.join(firmware_dir, IDENTITY_FILE))
        except FileNotFoundError:
            pass
        except OSError as e:
            ok = False
            slot.log(f'WARN: could not delete local identity image: {e}')
        slot.identity_key = None
        slot.identity_version_id = None
        return ok

    def flash_firmware(self, port: str, firmware_dir: str) -> bool:
        """Flash the app pieces and the identity image in one esptool call.

        Writes each FLASH_LAYOUT file at its offset. Not firmware.merged.bin:
        the build pads that to the full 8 MB, so it would overwrite the
        certs partition at 0x721000.
        """
        slot = self.devices.get(port)
        if not slot:
            return False

        slot.status = 'flashing'
        missing = [name for _o, name, _s in FLASH_LAYOUT
                   if not os.path.exists(os.path.join(firmware_dir, name))]
        if missing:
            slot.error = f'Missing flash files: {", ".join(missing)}'
            slot.log(f'ERROR: {slot.error}')
            return False

        slot.log(f'Flashing via esptool to {port}...')
        return self._run_esptool(slot, port, flash_args(firmware_dir))

    def flash_device(self, port: str, firmware_dir: str) -> bool:
        """Flash firmware + identity; on success delete the identity image.

        On failure the identity image is kept (locally and in S3) so a retry
        can flash it without re-issuing.
        """
        if not self.flash_firmware(port, firmware_dir):
            slot = self.devices.get(port)
            if slot:
                slot.log('Flash failed; keeping the identity image for a retry')
            return False
        self.delete_identity(self.devices[port], firmware_dir)
        return True

    def flash_image(self, port: str, image: str, offset: str = '0x0') -> bool:
        """Flash a single binary at one offset (CLI `flash --bin`, debugging)."""
        slot = self.devices.get(port) or DeviceSlot(port=port)
        if not os.path.exists(image):
            slot.error = f'{image} not found'
            slot.log(f'ERROR: {slot.error}')
            return False
        slot.status = 'flashing'
        return self._run_esptool(slot, port, [offset, image])

    def _run_esptool(self, slot: DeviceSlot, port: str, address_args: List[str]) -> bool:
        """Run esptool write_flash with the given <offset> <file> pairs."""
        for offset, path in zip(address_args[::2], address_args[1::2]):
            slot.log(f'  {offset}  {os.path.basename(path)} ({os.path.getsize(path)} bytes)')
        try:
            result = subprocess.run(
                esptool_argv(port, address_args),
                capture_output=True, text=True, timeout=120
            )

            # Persist the FULL esptool output to the device log file (not just
            # the GUI tail), so a failed flash is fully diagnosable from the
            # shipped logs rather than a truncated 300-char tail.
            device_key = (slot.serial_number or slot.port or 'unknown').replace('/', '_')
            if result.stdout:
                for ln in result.stdout.rstrip().split('\n'):
                    _append_log_file(f'device-{device_key}', f'  [esptool] {ln}')
            if result.stderr:
                for ln in result.stderr.rstrip().split('\n'):
                    _append_log_file(f'device-{device_key}', f'  [esptool:err] {ln}')

            if result.returncode == 0:
                slot.log('Flash complete!')
                if result.stdout.strip():
                    for line in result.stdout.strip().split('\n')[-5:]:
                        slot.log(f'  {line.strip()}')
                return True
            else:
                error_output = result.stderr.strip() or result.stdout.strip()
                slot.error = error_output[-300:] if error_output else 'Flash failed (no output)'
                slot.log('ERROR: esptool flash failed (full output in device log)')
                for line in error_output.split('\n')[-5:]:
                    slot.log(f'  {line.strip()}')
                return False

        except subprocess.TimeoutExpired:
            slot.error = 'Flash timed out (120s)'
            slot.log('ERROR: Flash timed out')
            return False
        except Exception as e:
            slot.error = str(e)
            slot.log(f'ERROR: {e}')
            return False

    def verify_boot(self, port: str, timeout: int = 45) -> bool:
        """Monitor serial output to verify device boots and connects to broker.
        Looks for WiFi connection and MQTT connection indicators."""
        slot = self.devices.get(port)
        if not slot:
            return False

        slot.status = 'verifying'
        slot.log('Waiting for device boot...')

        baud_rate = self.config.get('serial_baud_rate', 115200)

        try:
            ser = serial.Serial(port, baud_rate, timeout=1)
            start = time.time()
            wifi_connected = False
            mqtt_connected = False

            while time.time() - start < timeout:
                if ser.in_waiting:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        slot.log(f'[serial] {line}')
                        event = classify_boot_line(line)

                        if event == 'wifi' and not wifi_connected:
                            wifi_connected = True
                            slot.log('WiFi connected!')

                        if event == 'mqtt':
                            mqtt_connected = True
                            slot.log('MQTT connected to broker!')
                            break

                        if event in ('panic', 'identity'):
                            ser.close()
                            slot.status = 'error'
                            if event == 'panic':
                                slot.error = 'Device panic during boot'
                                slot.log('FATAL: Device crashed during boot')
                            else:
                                slot.error = 'Device has no valid identity (certs partition); Reflash to re-issue it'
                                slot.log(f'FATAL: {slot.error}')
                            return False

                time.sleep(0.05)  # nosemgrep: arbitrary-sleep

            ser.close()

            if mqtt_connected:
                slot.status = 'done'
                slot.log('Device booted and connected successfully!')
                return True
            elif wifi_connected:
                slot.status = 'done'
                slot.log('WiFi connected but MQTT not confirmed (may still be connecting)')
                return True
            else:
                slot.status = 'done'
                slot.log('Boot verification timed out — check serial monitor')
                return False

        except serial.SerialException as e:
            slot.log(f'Serial port error: {e}')
            slot.status = 'done'
            slot.log('Could not open serial port for verification (device may be fine)')
            return False
        except Exception as e:
            slot.log(f'Verification error: {e}')
            slot.status = 'done'
            return False

    def start_serial_monitor(self, port: str):
        """Start a background thread that continuously reads serial output."""
        if port in self._serial_threads:
            return

        self._serial_buffers[port] = []
        stop_event = threading.Event()
        self._serial_stop_events[port] = stop_event

        def _reader():
            baud_rate = self.config.get('serial_baud_rate', 115200)
            busy_noted = False
            while not stop_event.is_set():
                try:
                    # exclusive=True so we fail fast (and visibly) if another
                    # reader holds the port, rather than both getting partial
                    # data. This is the contention that made the GUI Serial
                    # view appear empty.
                    ser = serial.Serial(port, baud_rate, timeout=1, exclusive=True)
                    busy_noted = False
                    while not stop_event.is_set():
                        if ser.in_waiting:
                            line = ser.readline().decode('utf-8', errors='ignore').strip()
                            if line:
                                buf = self._serial_buffers.get(port, [])
                                buf.append(line)
                                if len(buf) > 200:
                                    self._serial_buffers[port] = buf[-200:]
                        time.sleep(0.05)  # nosemgrep: arbitrary-sleep
                    ser.close()
                    break
                except serial.SerialException as e:
                    # Port busy (e.g. mid-flash, or another reader). Surface it
                    # ONCE into the buffer so "empty" is distinguishable from
                    # "can't open", then keep retrying.
                    if not busy_noted:
                        buf = self._serial_buffers.get(port, [])
                        buf.append(f'[monitor] serial port unavailable ({e}); retrying...')
                        self._serial_buffers[port] = buf[-200:]
                        busy_noted = True
                    time.sleep(2)  # nosemgrep: arbitrary-sleep
                except Exception:
                    time.sleep(2)  # nosemgrep: arbitrary-sleep

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        self._serial_threads[port] = thread

    def stop_serial_monitor(self, port: str):
        """Stop the serial monitor for a port (e.g., before flashing)."""
        stop_event = self._serial_stop_events.get(port)
        if stop_event:
            stop_event.set()
            thread = self._serial_threads.get(port)
            if thread:
                thread.join(timeout=5)
            self._serial_threads.pop(port, None)
            self._serial_stop_events.pop(port, None)

    def read_serial_buffer(self, port: str, max_lines: int = 100) -> List[str]:
        """Read the serial buffer for a port.

        Starts the background monitor on demand if it isn't already running, so
        the GUI 'Serial' view / CLI `serial` always begin capturing even when no
        flash pipeline has run yet (the monitor was previously only started by
        register/reflash). The very first read may return few/no lines while the
        monitor spins up; subsequent polls fill in.
        """
        if port not in self._serial_threads:
            self.start_serial_monitor(port)
        buf = self._serial_buffers.get(port, [])
        return buf[-max_lines:]

    def remove_device(self, port: str):
        """Remove a device slot (when unplugged)."""
        self.stop_serial_monitor(port)
        with self._lock:
            self.devices.pop(port, None)
            self._serial_buffers.pop(port, None)

    def get_all_devices(self) -> List[dict]:
        """Return status of all detected devices."""
        return [slot.to_dict() for slot in self.devices.values()]
