"""
Device Manager — USB detection, serial identification, and flash operations.

Uses arduino-cli for upload and serial monitoring (matching the proven working
flow from arduino-cli/build.sh). Handles up to 4 simultaneous USB-connected
ESP32 devices on the builder-pi.

Key change from previous version: uses `arduino-cli upload` with the merged
binary instead of esptool with manual offsets. This ensures the bootloader,
partition table, and app are all flashed correctly.
"""

import subprocess
import time
import os
import json
import glob
import threading
import serial
from dataclasses import dataclass, field
from typing import Optional, Dict, List


# ─── Constants ───────────────────────────────────────────────────────────────
FQBN = "esp32:esp32:XIAO_ESP32S3:USBMode=hwcdc,CDCOnBoot=default,DebugLevel=info,PartitionScheme=default_8MB"
ARDUINO_CLI = "arduino-cli"
FIRMWARE_DIR = "/opt/pqc-workshop/firmware"


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

    def log(self, message: str):
        timestamp = time.strftime('%H:%M:%S')
        self.progress.append(f'[{timestamp}] {message}')
        if len(self.progress) > 50:
            self.progress = self.progress[-50:]

    def to_dict(self):
        return {
            'port': self.port,
            'serialNumber': self.serial_number,
            'status': self.status,
            'thingName': self.thing_name,
            'buildId': self.build_id,
            'error': self.error,
            'isRegistered': self.is_registered,
            'progress': self.progress[-20:]
        }


class DeviceManager:
    """Manages USB-connected ESP32 devices using arduino-cli."""

    def __init__(self, config: dict):
        self.config = config
        self.devices: Dict[str, DeviceSlot] = {}
        self._lock = threading.Lock()
        self._serial_buffers: Dict[str, List[str]] = {}
        self._serial_threads: Dict[str, threading.Thread] = {}
        self._serial_stop_events: Dict[str, threading.Event] = {}

    def scan_ports(self) -> List[str]:
        """Detect connected ESP32 USB serial ports using arduino-cli."""
        ports = []

        # Primary method: arduino-cli board list (most reliable)
        try:
            result = subprocess.run(
                [ARDUINO_CLI, 'board', 'list', '--format', 'json'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                boards = json.loads(result.stdout)
                for entry in boards:
                    port_info = entry.get('port', {})
                    address = port_info.get('address', '')
                    protocol = port_info.get('protocol', '')
                    if protocol == 'serial' and address:
                        ports.append(address)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            print(f"arduino-cli board list failed: {e}")

        # Fallback: glob for known ESP32 USB paths
        if not ports:
            patterns = [
                '/dev/ttyUSB*',
                '/dev/ttyACM*',
                '/dev/serial/by-id/*CP210*',
                '/dev/serial/by-id/*CH340*',
            ]
            for pattern in patterns:
                ports.extend(glob.glob(pattern))

        return sorted(set(ports))

    def get_serial_number(self, port: str) -> Optional[str]:
        """Read the ESP32 chip MAC/serial via esptool."""
        try:
            result = subprocess.run(
                ['esptool.py', '--port', port, 'read_mac'],
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

            # Check if already registered
            if self._check_registration(serial_number):
                slot.is_registered = True
                slot.status = 'registered'
                slot.log('Device is already registered')
            else:
                slot.status = 'ready'
                slot.log('New device — ready for registration')
        else:
            slot.status = 'error'
            slot.error = 'Could not read device serial number'
            slot.log('ERROR: Failed to read chip ID via esptool')

        return slot

    def _check_registration(self, serial_number: str) -> bool:
        """Check if a device is already registered in DynamoDB."""
        try:
            import boto3
            session = boto3.session.Session(region_name=self.config.get('region', 'us-east-1'))
            dynamodb = session.resource('dynamodb')
            table = dynamodb.Table('pqc-workshop-device-registry')
            response = table.get_item(Key={'serialNumber': serial_number})
            return 'Item' in response
        except Exception:
            return False

    def download_firmware(self, slot: DeviceSlot) -> Optional[str]:
        """Download the compiled firmware from S3.
        Returns the local directory containing the binary, or None on failure.
        
        arduino-cli upload --input-file expects companion files in the same
        directory with specific naming:
          <name>.merged.bin           — the full flash image
          <name>.merged.bootloader.bin — bootloader (derived from merged name)
        We download and rename to match this convention.
        """
        import boto3

        slot.status = 'downloading'
        slot.log('Downloading firmware from S3...')

        firmware_version = self.config.get('firmware_version', '0.1.0')
        s3_prefix = f'{slot.serial_number}/{firmware_version}'
        local_dir = os.path.join(FIRMWARE_DIR, slot.serial_number, firmware_version)
        os.makedirs(local_dir, exist_ok=True)

        try:
            session = boto3.session.Session(region_name=self.config.get('region', 'us-east-1'))
            s3 = session.client('s3')
            bucket = self.config['firmware_bucket']

            # Download the merged binary
            merged_key = f'{s3_prefix}/firmware.merged.bin'
            merged_path = os.path.join(local_dir, 'firmware.merged.bin')
            s3.download_file(bucket, merged_key, merged_path)
            slot.log(f'Downloaded: firmware.merged.bin ({os.path.getsize(merged_path)} bytes)')

            # Download the bootloader and create the companion file that
            # arduino-cli expects (firmware.merged.bootloader.bin)
            try:
                boot_key = f'{s3_prefix}/bootloader.bin'
                boot_companion = os.path.join(local_dir, 'firmware.merged.bootloader.bin')
                s3.download_file(bucket, boot_key, boot_companion)
                slot.log(f'Downloaded: bootloader → firmware.merged.bootloader.bin')
            except Exception as e:
                slot.log(f'WARN: bootloader not available: {e}')

            # Download partitions — arduino-cli expects firmware.merged.partitions.bin
            try:
                part_key = f'{s3_prefix}/partitions.bin'
                part_companion = os.path.join(local_dir, 'firmware.merged.partitions.bin')
                s3.download_file(bucket, part_key, part_companion)
                slot.log(f'Downloaded: partitions → firmware.merged.partitions.bin')
            except Exception as e:
                slot.log(f'WARN: partitions.bin not available: {e}')

            # Also download the app-only binary (for OTA reference)
            try:
                app_key = f'{s3_prefix}/firmware.bin'
                app_path = os.path.join(local_dir, 'firmware.bin')
                s3.download_file(bucket, app_key, app_path)
                slot.log(f'Downloaded: firmware.bin ({os.path.getsize(app_path)} bytes)')
            except Exception:
                slot.log('WARN: firmware.bin (OTA binary) not available yet')

            return local_dir

        except Exception as e:
            slot.error = f'Download failed: {e}'
            slot.log(f'ERROR: {slot.error}')
            return None

    def flash_firmware(self, port: str, firmware_dir: str) -> bool:
        """Flash firmware to ESP32 using esptool with the merged binary.

        The merged binary contains bootloader + partition table + app at the
        correct offsets, so we flash it at address 0x0 to cover the entire flash.
        This matches what arduino-cli does internally during a full upload.
        """
        slot = self.devices.get(port)
        if not slot:
            return False

        slot.status = 'flashing'
        merged_bin = os.path.join(firmware_dir, 'firmware.merged.bin')

        if not os.path.exists(merged_bin):
            slot.error = 'firmware.merged.bin not found'
            slot.log(f'ERROR: {slot.error}')
            return False

        slot.log(f'Flashing via esptool to {port}...')
        slot.log(f'Binary: {merged_bin} ({os.path.getsize(merged_bin)} bytes)')

        try:
            # Use esptool to write the merged binary at offset 0x0.
            # The merged binary already contains:
            #   0x0000 — bootloader
            #   0x8000 — partition table
            #   0xe000 — ota data (boot_app0)
            #   0x10000 — application
            result = subprocess.run(
                [
                    'esptool', '--port', port,
                    '--baud', '921600',
                    '--chip', 'esp32s3',
                    'write_flash',
                    '--flash_mode', 'dio',
                    '--flash_freq', '80m',
                    '--flash_size', '8MB',
                    '0x0', merged_bin,
                ],
                capture_output=True, text=True, timeout=120
            )

            if result.returncode == 0:
                slot.log('Flash complete!')
                if result.stdout.strip():
                    for line in result.stdout.strip().split('\n')[-5:]:
                        slot.log(f'  {line.strip()}')
                return True
            else:
                error_output = result.stderr.strip() or result.stdout.strip()
                slot.error = error_output[-300:] if error_output else 'Flash failed (no output)'
                slot.log(f'ERROR: esptool flash failed')
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

                        # Detect WiFi connection
                        if 'WiFi' in line and 'connect' in line.lower():
                            wifi_connected = True
                            slot.log('WiFi connected!')

                        # Detect MQTT connection
                        if 'MQTT' in line and 'connect' in line.lower():
                            mqtt_connected = True
                            slot.log('MQTT connected to broker!')
                            break

                        # Detect fatal errors
                        if 'panic' in line.lower() or 'guru meditation' in line.lower():
                            slot.log('FATAL: Device crashed during boot')
                            ser.close()
                            slot.status = 'error'
                            slot.error = 'Device panic during boot'
                            return False

                time.sleep(0.05)

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
            while not stop_event.is_set():
                try:
                    ser = serial.Serial(port, baud_rate, timeout=1)
                    while not stop_event.is_set():
                        if ser.in_waiting:
                            line = ser.readline().decode('utf-8', errors='ignore').strip()
                            if line:
                                buf = self._serial_buffers.get(port, [])
                                buf.append(line)
                                if len(buf) > 200:
                                    self._serial_buffers[port] = buf[-200:]
                        time.sleep(0.05)
                    ser.close()
                    break
                except serial.SerialException:
                    # Port may be in use by upload — wait and retry
                    time.sleep(2)
                except Exception:
                    time.sleep(2)

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
        """Read the serial buffer for a port."""
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
