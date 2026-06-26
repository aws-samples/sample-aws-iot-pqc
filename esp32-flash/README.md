# ESP32 Dual-Boot Flash Guide

Instructions for flashing the XIAO ESP32S3 devices with both firmware images (classical TLS on partition 0, PQC TLS on partition 1) for the workshop.

This is done on **your laptop** before the workshop session. Each device takes about 30 seconds to flash.

## Prerequisites (Laptop Setup)

### 1. Python 3.8+

`esptool.py` requires Python. Check if it's installed:

```bash
python3 --version
```

If not installed:
- **macOS:** `brew install python3` or download from [python.org](https://www.python.org/downloads/)
- **Windows:** Download from [python.org](https://www.python.org/downloads/) (check "Add to PATH")
- **Linux:** `sudo apt install python3 python3-pip`

### 2. esptool.py (ESP32 flash tool)

```bash
pip3 install esptool
```

Verify:
```bash
esptool.py version
# Should show 4.x or later
```

### 3. Arduino IDE (to compile the firmware)

Download from [arduino.cc](https://www.arduino.cc/en/software) (version 2.x recommended).

Install the ESP32 board package:
1. Open **File > Preferences**
2. Add to **Additional Board Manager URLs:**
   ```
   https://espressif.github.io/arduino-esp32/package_esp32_index.json
   ```
3. Open **Tools > Board > Boards Manager**, search "esp32", install

Install required libraries via **Sketch > Include Library > Manage Libraries**:
- **PubSubClient** by Nick O'Leary (2.8+)
- **wolfSSL** by wolfSSL Inc. (5.7.0+)

### 4. Clone this repository

```bash
git clone https://gitlab.aws.dev/Sample-aws-iot-pqc.git
cd Sample-aws-iot-pqc
```

### 5. USB driver (if needed)

The XIAO ESP32S3 uses a USB CDC serial interface. Most systems detect it automatically. If not:
- **macOS/Linux:** Should work out of the box (shows as `/dev/ttyACM0` or `/dev/cu.usbmodem*`)
- **Windows:** Install the [CP210x driver](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers) if the device doesn't appear in Device Manager

## Compilation

You need to compile both firmwares into `.bin` files. The Arduino IDE does this for you.

### Compile the classical firmware (partition 0)

1. Open `aws_iot_pqc_esp32_v1_classical/aws_iot_pqc_esp32_v1_classical.ino` in Arduino IDE
2. Select board: **Tools > Board > esp32 > XIAO_ESP32S3**
3. Select partition scheme: **Tools > Partition Scheme > No OTA (Large APP)**
   (This is just for compilation — we use our own partition table when flashing)
4. Edit `secrets.h`:
   - Set `MQTT_BROKER_ENDPOINT` to the Raspberry Pi's IP address
   - Paste the workshop CA cert, device cert, and device key
   (For batch flashing, you'll create a script that patches secrets.h per device — see below)
5. Click **Sketch > Export Compiled Binary** (or Ctrl+Alt+S)
6. Find the `.bin` file in the sketch folder under `build/`

### Compile the PQC firmware (partition 1)

1. **First:** Configure wolfSSL for PQC (one-time setup):
   - Find wolfSSL's `user_settings.h` in your Arduino libraries folder:
     - macOS: `~/Documents/Arduino/libraries/wolfssl/src/user_settings.h`
     - Linux: `~/Arduino/libraries/wolfssl/src/user_settings.h`
     - Windows: `C:\Users\<you>\Documents\Arduino\libraries\wolfssl\src\user_settings.h`
   - Copy the contents of `aws_iot_pqc_esp32/wolfssl_user_settings.h` into it
2. Open `aws_iot_pqc_esp32/aws_iot_pqc_esp32.ino` in Arduino IDE
3. Select board: **Tools > Board > esp32 > XIAO_ESP32S3**
4. Edit `secrets.h` (same credentials as classical — both firmwares share certs)
5. Click **Sketch > Export Compiled Binary**
6. Find the `.bin` file in the sketch folder under `build/`

### Note on first compilation

The PQC firmware takes **5-10 minutes** on first compile because wolfSSL with ML-KEM is a large library. Subsequent builds are much faster (cached).

## Flashing a Single Device

```bash
cd esp32-flash

# Connect the XIAO ESP32S3 via USB
# Find the serial port:
#   macOS:   ls /dev/cu.usb*
#   Linux:   ls /dev/ttyACM*
#   Windows: Check Device Manager for COM port

./flash-dual-boot.sh /dev/ttyACM0 \
  ../aws_iot_pqc_esp32_v1_classical/build/aws_iot_pqc_esp32_v1_classical.ino.bin \
  ../aws_iot_pqc_esp32/build/aws_iot_pqc_esp32.ino.bin
```

If the device doesn't enter download mode automatically, hold the **BOOT** button while pressing **RESET** (or while plugging in USB).

## Batch Flashing (Workshop Prep)

For flashing 100 devices, each with unique credentials, use the batch script:

### Step 1: Copy device certificates from the Pi

The certificates were generated on the Raspberry Pi by `greengrass/certs/generate-broker-certs.sh`. Copy them to your laptop:

```bash
# From your laptop
scp -r pi@<raspberry-pi-ip>:~/Sample-aws-iot-pqc/greengrass/certs/output/devices ./device-certs/
scp pi@<raspberry-pi-ip>:~/Sample-aws-iot-pqc/greengrass/certs/output/ca.crt ./device-certs/
```

### Step 2: Generate per-device secrets.h files

```bash
# Generate 100 secrets.h files (one per device)
./generate-secrets.sh <raspberry-pi-ip> 100
```

This creates `secrets/esp32-pqc-device-001/secrets.h` through `secrets/esp32-pqc-device-100/secrets.h`, each with the correct device name, certificates, and broker IP.

### Step 3: Compile per-device firmware

For the workshop, all devices share the same firmware binary — only `secrets.h` differs. Since `secrets.h` is compiled into the binary, you need one compilation per device (or use a script):

```bash
# Option A: Single binary with runtime identity (MAC-based)
# If you modify the firmware to derive THINGNAME from the ESP32 MAC address,
# you only need to compile once. All devices share the same binary + certs.

# Option B: Per-device compilation (more control, more effort)
./compile-all.sh 100
# This loops through each device, patches secrets.h, compiles, and saves the .bin
```

### Step 4: Flash all devices

```bash
# Flash devices one at a time (plug in, flash, unplug, repeat)
./flash-dual-boot.sh /dev/ttyACM0 \
  builds/esp32-pqc-device-001/classical.bin \
  builds/esp32-pqc-device-001/pqc.bin

# Or use the batch helper (flashes whatever is plugged in):
./flash-next-device.sh
# (auto-detects port, picks next unflashed device number)
```

## Partition Layout

The custom partition table (`partitions_pqc_workshop.csv`) divides the 8MB flash:

```
┌─────────────────────────────────────────────────┐
│ 0x000000 - 0x008FFF │ Bootloader (36 KB)        │
├─────────────────────────────────────────────────┤
│ 0x009000 - 0x00DFFF │ NVS (20 KB)              │
├─────────────────────────────────────────────────┤
│ 0x00E000 - 0x00FFFF │ OTA Data (8 KB)          │
├─────────────────────────────────────────────────┤
│ 0x010000 - 0x38FFFF │ OTA_0: Classical FW       │
│                      │ (3.5 MB)                  │
├─────────────────────────────────────────────────┤
│ 0x390000 - 0x70FFFF │ OTA_1: PQC FW            │
│                      │ (3.5 MB)                  │
├─────────────────────────────────────────────────┤
│ 0x710000 - 0x710FFF │ NVS Keys (4 KB)          │
├─────────────────────────────────────────────────┤
│ 0x711000 - 0x720FFF │ Coredump (64 KB)         │
└─────────────────────────────────────────────────┘
```

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| `Failed to connect to ESP32-S3` | Device not in download mode | Hold BOOT, press RESET, release BOOT |
| `No serial port found` | USB driver missing or cable issue | Try different cable (data cable, not charge-only) |
| `A fatal error occurred: Packet content transfer incomplete` | Flaky USB connection | Use shorter cable, try different port |
| `Image too large for partition` | Firmware exceeds 3.5 MB | Remove debug symbols, check wolfSSL config |
| Device boots but doesn't connect | Wrong broker IP or certs | Check `secrets.h` values match your Pi setup |
| `WOLFSSL_SECP256R1_MLKEM768 was not declared` | wolfSSL not configured for PQC | Copy `wolfssl_user_settings.h` into library's `user_settings.h` |

## Verification After Flashing

After flashing, the device should:

1. Power on (LED blinks)
2. Connect to WiFi
3. Connect to Mosquitto on the Pi via TLS 1.2 (classical firmware)
4. Appear on the dashboard within 30 seconds

Open the Arduino Serial Monitor (115200 baud) to see connection logs if troubleshooting.
