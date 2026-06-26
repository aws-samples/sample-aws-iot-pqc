# Viewing Your Device's Serial Output

When you plug in your ESP32 device, it outputs its **device name** and connection status to the USB serial port. You can view this output on your laptop without installing any additional software.

## What You'll See

```
══════════════════════════════════════════════════
  ESP32 AWS IoT Core — CLASSICAL TLS (Pre-PQC)
  Firmware v1.0.0 — Awaiting PQC Migration
══════════════════════════════════════════════════

┌──────────────────────────────────────────────┐
│  YOUR DEVICE NAME:  esp32-A1B2C3              │
└──────────────────────────────────────────────┘

Chip: ESP32-S3 Rev 0, 240 MHz
Flash: 8 MB, Heap: 275432 bytes, PSRAM: 8388608 bytes
TLS: Classical (WiFiClientSecure / mbedTLS)
```

The **device name** (`esp32-A1B2C3` in this example) is unique to your device and matches what you'll see on the workshop dashboard.

---

## macOS

macOS has a built-in serial terminal. No installation needed.

### Using `screen` (pre-installed)

```bash
# 1. Find your device's serial port
ls /dev/cu.usb*

# You should see something like:
#   /dev/cu.usbmodem14101

# 2. Connect at 115200 baud
screen /dev/cu.usbmodem14101 115200

# 3. To exit: press Ctrl+A, then K, then Y
```

### Using `cat` (simplest, read-only)

```bash
# Just view the output (no interaction)
cat /dev/cu.usbmodem14101
```

### Permissions

macOS should work without permission changes. If you get "Permission denied":
- Open **System Settings > Privacy & Security > USB Accessories**
- Allow your terminal app to access USB devices

---

## Windows 11

Windows has a built-in serial terminal via PowerShell. No installation needed.

### Using PowerShell (built-in)

```powershell
# 1. Find the COM port
# Open Device Manager (Win+X > Device Manager)
# Look under "Ports (COM & LPT)" for "USB Serial Device (COMx)"
# Note the COM port number (e.g., COM3)

# 2. Connect using PowerShell
$port = New-Object System.IO.Ports.SerialPort "COM3", 115200
$port.Open()

# 3. Read output in a loop
while ($true) {
    if ($port.BytesToRead -gt 0) {
        Write-Host $port.ReadLine()
    }
    Start-Sleep -Milliseconds 100
}

# 4. To stop: press Ctrl+C
# 5. Close the port
$port.Close()
```

### Using Windows Terminal + `mode` (simpler alternative)

```cmd
# In Command Prompt or Windows Terminal:
mode COM3 BAUD=115200 PARITY=n DATA=8 STOP=1
type COM3
```

### Permissions

- The XIAO ESP32S3 should appear automatically as a COM port
- If it doesn't appear in Device Manager:
  1. Try a different USB cable (must be a data cable, not charge-only)
  2. Try a different USB port
  3. The XIAO ESP32S3 uses USB CDC which is natively supported by Windows 11 — no driver installation needed

---

## Linux

Linux has built-in serial tools. No installation needed.

### Using `cat` (simplest)

```bash
# 1. Find the serial port
ls /dev/ttyACM*
# Should show: /dev/ttyACM0

# 2. Set baud rate and read
stty -F /dev/ttyACM0 115200 raw -echo
cat /dev/ttyACM0
```

### Using `screen`

```bash
screen /dev/ttyACM0 115200
# Exit: Ctrl+A, then K, then Y
```

### Permissions

If you get "Permission denied":
```bash
# Add yourself to the dialout group (one-time, requires logout/login)
sudo usermod -a -G dialout $USER
```

Or for immediate access without logout:
```bash
sudo cat /dev/ttyACM0
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| No serial port appears | Try a different USB cable — many cables are charge-only |
| Port shows but no output | Press the RESET button on the XIAO board |
| Garbled characters | Baud rate mismatch — must be **115200** |
| "Permission denied" | See platform-specific permissions section above |
| Port disappears during use | Device rebooted (normal during firmware migration) — reconnect |

## After Firmware Migration

When you click "Migrate to PQC" on the dashboard, the device reboots. The serial output will briefly disconnect, then reconnect with the new firmware banner:

```
══════════════════════════════════════════════════
  ESP32 AWS IoT Core - Post-Quantum Cryptography
  TLS 1.3 + Hybrid ML-KEM Key Exchange
══════════════════════════════════════════════════

┌──────────────────────────────────────────────┐
│  YOUR DEVICE NAME:  esp32-A1B2C3              │
└──────────────────────────────────────────────┘

...
Using PQC key exchange: P-256 + ML-KEM-768 (SecP256r1MLKEM768)
TLS handshake complete in 2847 ms
Connected to PQC Mosquitto broker!
```

Your device name stays the same — it's based on the hardware MAC address and doesn't change between firmwares.
