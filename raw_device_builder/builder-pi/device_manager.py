"""
Device Manager — USB detection, serial identification, and flash operations.

Handles up to 4 simultaneous USB-connected ESP32 devices on the builder-pi.
"""

import subprocess
import serial
import time
import os
import json
import glob
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, List


@dataclass
class DeviceSlot:
    """Represents a USB port slot with an attached ESP32."""
    port: str
    serial_number: Optional[str] = None
    status: str = 'detecting'  # detecting, ready, registered, registering, building, flashing, verifying, done, error
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
    """Manages USB-connected ESP32 devices."""

    def __init__(self, config: dict):
        self.config = config
        self.devices: Dict[str, DeviceSlot] = {}
        self._lock = threading.Lock()
        self._serial_buffers: Dict[str, List[str]] = {}
        self._serial_threads: Dict[str, threading.Thread] = {}

    def scan_ports(self) -> List[str]:
        """Detect connected ESP32 USB serial ports."""
        patterns = [
            '/dev/ttyUSB*',
            '/dev/ttyACM*',
            '/dev/serial/by-id/*CP210*',
            '/dev/serial/by-id/*CH340*',
            '/dev/serial/by-id/*FTDI*',
        ]
        ports = []
        for pattern in patterns:
            ports.extend(glob.glob(pattern))

        # Also check for specific ESP32 USB-CDC ports
        ports.extend(glob.glob('/dev/ttyACM*'))

        return sorted(set(ports))

    def get_serial_number(self, port: str) -> Optional[str]:
        """Read the ESP32 chip MAC/serial via esptool."""
        try:
            result = subprocess.run(
                ['esptool.py', '--port', port, 'chip_id'],
                capture_output=True, text=True, timeout=10
            )
            output = result.stdout + result.stderr

            # Parse chip ID or MAC from esptool output
            for line in output.split('\n'):
                if 'MAC:' in line:
                    mac = line.split('MAC:')[1].strip().replace(':', '')
                    # Use last 6 chars (3 bytes) as serial — matches esp32-XXYYZZ format
                    return mac[-6:].upper()

            # Fallback: try read_mac
            result = subprocess.run(
                ['esptool.py', '--port', port, 'read_mac'],
                capture_output=True, text=True, timeout=10
            )
            output = result.stdout + result.stderr
            for line in output.split('\n'):
                if 'MAC:' in line:
                    mac = line.split('MAC:')[1].strip().replace(':', '')
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
            if self.check_registration(serial_number):
                slot.is_registered = True
                slot.status = 'registered'
                slot.log('Device is already registered in IoT Core')
            else:
                slot.status = 'ready'
                slot.log('New device — ready for registration')

            # Start serial buffer reader
            self._start_serial_reader(port)
        else:
            slot.status = 'error'
            slot.error = 'Could not read device serial number'
            slot.log('ERROR: Failed to read chip ID')

        return slot

    def check_registration(self, serial_number: str) -> bool:
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

    def _start_serial_reader(self, port: str):
        """Start a background thread that continuously reads serial output."""
        if port in self._serial_threads:
            return

        self._serial_buffers[port] = []

        def _reader():
            try:
                ser = serial.Serial(port, self.config.get('serial_baud_rate', 115200), timeout=1)
                while port in self.devices:
                    if ser.in_waiting:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                        if line:
                            buf = self._serial_buffers.get(port, [])
                            buf.append(line)
                            if len(buf) > 200:
                                self._serial_buffers[port] = buf[-200:]
                    time.sleep(0.1)
                ser.close()
            except Exception:
                pass

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        self._serial_threads[port] = thread

    def read_serial_buffer(self, port: str, max_lines: int = 100) -> List[str]:
        """Read the serial buffer for a port."""
        buf = self._serial_buffers.get(port, [])
        return buf[-max_lines:]

    def flash_firmware(self, port: str, firmware_dir: str) -> bool:
        """Flash firmware binaries to ESP32 via esptool.
        firmware_dir should contain: bootloader.bin, partitions.bin, boot_app0.bin, app.bin"""
        slot = self.devices.get(port)
        if not slot:
            return False

        slot.status = 'flashing'
        slot.log(f'Flashing firmware from: {firmware_dir}')

        bootloader = os.path.join(firmware_dir, 'bootloader.bin')
        partitions = os.path.join(firmware_dir, 'partitions.bin')
        boot_app0 = os.path.join(firmware_dir, 'boot_app0.bin')
        app_bin = os.path.join(firmware_dir, 'app.bin')

        # Build flash command with all components at correct offsets
        flash_args = [
            'esptool.py',
            '--port', port,
            '--baud', str(self.config.get('flash_baud_rate', 921600)),
            '--chip', 'esp32s3',
            'write_flash',
            '--flash_mode', 'dio',
            '--flash_freq', '80m',
            '--flash_size', '8MB',
            '0x0', bootloader,
            '0x10000', app_bin,
        ]

        # Add partition table and boot_app0 if they exist
        if os.path.exists(partitions):
            flash_args.extend(['0x8000', partitions])
        if os.path.exists(boot_app0):
            flash_args.extend(['0xe000', boot_app0])

        try:
            slot.log(f'esptool: bootloader@0x0, app@0x10000, partitions@0x8000, ota_data@0xe000')
            result = subprocess.run(
                flash_args,
                capture_output=True, text=True, timeout=120
            )

            if result.returncode == 0:
                slot.log('Flash complete!')
                return True
            else:
                slot.error = result.stderr[-200:] if result.stderr else 'Flash failed'
                slot.log(f'ERROR: Flash failed: {slot.error}')
                return False

        except subprocess.TimeoutExpired:
            slot.error = 'Flash timed out (120s)'
            slot.log('ERROR: Flash timed out')
            return False
        except Exception as e:
            slot.error = str(e)
            slot.log(f'ERROR: {e}')
            return False

    def verify_boot(self, port: str, timeout: int = 30) -> bool:
        """Monitor serial output to verify device boots and connects."""
        slot = self.devices.get(port)
        if not slot:
            return False

        slot.status = 'verifying'
        slot.log('Waiting for device boot...')

        try:
            ser = serial.Serial(port, self.config.get('serial_baud_rate', 115200), timeout=1)
            start = time.time()
            connected = False

            while time.time() - start < timeout:
                if ser.in_waiting:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        slot.log(f'[serial] {line}')
                        # Look for MQTT connected indicator
                        if 'MQTT' in line and 'connect' in line.lower():
                            connected = True
                            break
                        if 'WiFi' in line and 'Connected' in line:
                            slot.log('WiFi connected!')
                time.sleep(0.1)

            ser.close()

            if connected:
                slot.status = 'done'
                slot.log('Device booted and connected successfully!')
                return True
            else:
                slot.status = 'done'
                slot.log('Boot verification timed out — device may still be connecting')
                return False

        except Exception as e:
            slot.log(f'Verification error: {e}')
            return False

    def remove_device(self, port: str):
        """Remove a device slot (when unplugged)."""
        with self._lock:
            self.devices.pop(port, None)

    def get_all_devices(self) -> List[dict]:
        """Return status of all detected devices."""
        return [slot.to_dict() for slot in self.devices.values()]
