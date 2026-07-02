# Greengrass + PQC Mosquitto Broker

This folder contains all artifacts to deploy an AWS IoT Greengrass v2 core device on a Raspberry Pi with a PQC-enabled Mosquitto MQTT broker.

## Architecture

```
ESP32 Device                    Raspberry Pi (Greengrass Core)              AWS Cloud
+------------------+            +--------------------------------+          +------------------+
|                  |  TLS 1.3   |                                |          |                  |
|  Classical FW    |  Classical  |  Mosquitto MQTT Broker         | Classical|  AWS IoT Core    |
|  (partition 0)   |----------->|    - OpenSSL 3.5 + ML-KEM      |--------->|    - MQTT Broker  |
|                  |            |    - Port 8883                  |  TLS 1.2 |    - Rules Engine |
|  PQC FW          |  TLS 1.3   |    - mTLS client auth          |  Bridge  |    - CloudWatch   |
|  (partition 1)   |  + ML-KEM  |                                |          |                  |
|                  |----------->|  Greengrass Nucleus             |  TLS 1.2 |                  |
|                  |            |    - Manages Mosquitto lifecycle|--------->|  IoT Greengrass  |
|                  |            |    - Relays commands            |          |    - Deployments  |
|                  |            |    - Dashboard component        |          |    - Components   |
+------------------+            +--------------------------------+          +------------------+
```

### Data Flow

1. **ESP32 → Mosquitto (local, PQC):** The ESP32 connects to Mosquitto on the Pi over TLS 1.3 with hybrid ML-KEM key exchange (`SecP256r1MLKEM768`). Mosquitto authenticates the ESP32 using its X.509 client certificate.

2. **Mosquitto → IoT Core (bridge, classical TLS):** Mosquitto bridges messages to AWS IoT Core using classical TLS 1.2 with the Greengrass device certificate. This is a standard Mosquitto bridge configuration.

3. **IoT Core → Mosquitto → ESP32 (commands):** Commands published to IoT Core (e.g., from the dashboard) are bridged back to Mosquitto, which forwards them to subscribed ESP32 devices.

### Why this design?

- **Greengrass's built-in Moquette broker** runs in the Java JVM and depends on JSSE for TLS. Java's PQC TLS support (JEP 527) only shipped in JDK 27, which Greengrass doesn't officially support.
- **Standalone Mosquitto** linked against OpenSSL 3.5+ gives us full control over TLS 1.3 named groups, including the IANA-standardized `X25519MLKEM768` and `SecP256r1MLKEM768` hybrids.
- **Greengrass still manages everything else:** component deployment, device lifecycle, IoT Core connectivity, and the workshop dashboard.

## Folder Contents

| File | Description |
|------|-------------|
| `README.md` | This file |
| `setup-mosquitto-pqc.sh` | Script to build and install Mosquitto with PQC on Raspberry Pi |
| `mosquitto/mosquitto-pqc.conf` | Mosquitto configuration for PQC TLS |
| `mosquitto/bridge-iotcore.conf` | Mosquitto bridge configuration to AWS IoT Core |
| `mosquitto/acl.conf` | Access control list for ESP32 device topics |
| `certs/generate-broker-certs.sh` | Script to generate PQC-capable broker certificates |
| `greengrass-component/recipe.yaml` | Greengrass component recipe for the PQC Mosquitto broker |
| `greengrass-component/install.sh` | Component install lifecycle script |
| `greengrass-component/run.sh` | Component run lifecycle script |

## Prerequisites

- Raspberry Pi 4 or 5 with **64-bit Raspberry Pi OS** (Bookworm or later)
- Internet connectivity (WiFi or Ethernet)
- An SSH client to connect to the Pi (or physical keyboard/monitor)
- The `pqc-workshop` CloudFormation stack already deployed (see `cloudformation/deploy-workshop.sh`)

The Pi does NOT need AWS CLI pre-installed — the setup script will guide you. However, you need temporary AWS credentials (Access Key + Secret Key) with the following IAM permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:*:*:secret:iot/pqc-workshop-greengrass-core/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iot:DescribeEndpoint",
        "iot:ListAttachedPolicies",
        "iot:AttachPolicy"
      ],
      "Resource": "*"
    }
  ]
}
```

## Raspberry Pi Setup (Facilitator Guide)

The simplest approach: **clone this repo directly on the Pi** and run the scripts in order.

Total setup time: ~30-45 minutes (most is compile time for OpenSSL and Mosquitto).

### Step 1: Clone the repo

```bash
# SSH into the Raspberry Pi
ssh pi@<raspberry-pi-ip>

# Install git if needed
sudo apt-get update && sudo apt-get install -y git

# Clone the repository
git clone https://gitlab.aws.dev/Sample-aws-iot-pqc.git
cd Sample-aws-iot-pqc/greengrass
```

### Step 2: Install AWS CLI (if not already installed)

```bash
# Install AWS CLI v2 on ARM64
curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip"
unzip -q awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip

# Configure with temporary credentials
aws configure
# Enter: Access Key ID, Secret Access Key, Region (e.g., us-east-1), Output format (json)
```

### Step 3: Install Greengrass v2 nucleus

This installs Java, downloads the Greengrass nucleus, and provisions the core device using the certificate stored in Secrets Manager by the CloudFormation stack.

```bash
sudo -E ./setup-greengrass-core.sh us-east-1 pqc-workshop-greengrass-core
```

**Note:** The `-E` flag preserves your AWS credentials environment so the script can access Secrets Manager as root.

Verify Greengrass is running:
```bash
sudo systemctl status greengrass
```

### Step 4: Build and install PQC Mosquitto

Compiles OpenSSL 3.5 and Mosquitto from source (~15-20 minutes on Pi 4/5):

```bash
sudo ./setup-mosquitto-pqc.sh
```

### Step 5: Generate certificates

```bash
cd certs
chmod +x generate-broker-certs.sh
LD_LIBRARY_PATH=/opt/openssl-pqc/lib ./generate-broker-certs.sh 100
cd ..
```

**Note:** The `LD_LIBRARY_PATH` prefix is required because the PQC OpenSSL binary (`/opt/openssl-pqc/bin/openssl`) needs its own `libssl.so.3` rather than the system default.

This creates `certs/output/` with:
- `ca.crt` / `ca.key` — Workshop CA
- `broker.crt` / `broker.key` — Mosquitto server certificate
- `devices/esp32-pqc-device-001/` through `100/` — Per-device certs

### Step 6: Install certs and config into Mosquitto

```bash
# Copy broker certificates
sudo cp certs/output/ca.crt /opt/mosquitto-pqc/etc/mosquitto/certs/
sudo cp certs/output/broker.crt /opt/mosquitto-pqc/etc/mosquitto/certs/
sudo cp certs/output/broker.key /opt/mosquitto-pqc/etc/mosquitto/certs/

# Copy broker configuration
sudo cp mosquitto/mosquitto-pqc.conf /opt/mosquitto-pqc/etc/mosquitto/
sudo cp mosquitto/acl.conf /opt/mosquitto-pqc/etc/mosquitto/

# Configure the IoT Core bridge (fills in placeholders)
IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
  --query 'endpointAddress' --output text)
GG_THING="pqc-workshop-greengrass-core"

sed "s|REPLACE_WITH_IOT_ENDPOINT|$IOT_ENDPOINT|g; s|REPLACE_WITH_GREENGRASS_THING_NAME|$GG_THING|g" \
  mosquitto/bridge-iotcore.conf | \
  sudo tee /opt/mosquitto-pqc/etc/mosquitto/conf.d/bridge-iotcore.conf > /dev/null

# Get Greengrass device cert for the bridge connection to IoT Core
aws secretsmanager get-secret-value \
  --secret-id "iot/$GG_THING/greengrass-credentials" \
  --query 'SecretString' --output text | sudo python3 -c "
import json, sys
creds = json.load(sys.stdin)
open('/opt/mosquitto-pqc/etc/mosquitto/certs/greengrass-device.crt','w').write(creds['certificatePem'])
open('/opt/mosquitto-pqc/etc/mosquitto/certs/greengrass-device.key','w').write(creds['privateKey'])
print('Greengrass device certs installed')
"

# Download Amazon Root CA (for the bridge → IoT Core connection)
sudo curl -so /opt/mosquitto-pqc/etc/mosquitto/certs/AmazonRootCA1.pem \
  https://www.amazontrust.com/repository/AmazonRootCA1.pem
```

### Step 7: Start the broker

```bash
sudo systemctl start mosquitto-pqc
sudo systemctl enable mosquitto-pqc

# Check status
sudo systemctl status mosquitto-pqc

# Watch logs (look for "bridge connected" message)
sudo tail -f /opt/mosquitto-pqc/var/log/mosquitto/mosquitto.log
```

### Step 8: Verify the setup

```bash
# Test a local publish (simulates an ESP32 connection)
/opt/mosquitto-pqc/bin/mosquitto_pub \
  --cafile /opt/mosquitto-pqc/etc/mosquitto/certs/ca.crt \
  --cert certs/output/devices/esp32-pqc-device-001/device.crt \
  --key certs/output/devices/esp32-pqc-device-001/device.key \
  -h localhost -p 8883 \
  -t "dt/esp32-pqc-device-001/telemetry" \
  -m '{"device":"esp32-pqc-device-001","message_id":0,"pqc_algorithm":"none","tls_version":"TLS 1.2"}'

# Verify in IoT Core: open the MQTT test client in the AWS Console
# and subscribe to dt/# — you should see the test message.
```

### Quick Reference (all commands in sequence)

```bash
# From a fresh Pi with network access:
git clone https://gitlab.aws.dev/Sample-aws-iot-pqc.git
cd Sample-aws-iot-pqc/greengrass

sudo -E ./setup-greengrass-core.sh us-east-1 pqc-workshop-greengrass-core
sudo ./setup-mosquitto-pqc.sh
cd certs && LD_LIBRARY_PATH=/opt/openssl-pqc/lib ./generate-broker-certs.sh 100 && cd ..

sudo cp certs/output/{ca.crt,broker.crt,broker.key} /opt/mosquitto-pqc/etc/mosquitto/certs/
sudo cp mosquitto/mosquitto-pqc.conf /opt/mosquitto-pqc/etc/mosquitto/
sudo cp mosquitto/acl.conf /opt/mosquitto-pqc/etc/mosquitto/

IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query 'endpointAddress' --output text)
sed "s|REPLACE_WITH_IOT_ENDPOINT|$IOT_ENDPOINT|g; s|REPLACE_WITH_GREENGRASS_THING_NAME|pqc-workshop-greengrass-core|g" \
  mosquitto/bridge-iotcore.conf | sudo tee /opt/mosquitto-pqc/etc/mosquitto/conf.d/bridge-iotcore.conf > /dev/null

aws secretsmanager get-secret-value --secret-id "iot/pqc-workshop-greengrass-core/greengrass-credentials" \
  --query 'SecretString' --output text | sudo python3 -c "
import json,sys; c=json.load(sys.stdin)
open('/opt/mosquitto-pqc/etc/mosquitto/certs/greengrass-device.crt','w').write(c['certificatePem'])
open('/opt/mosquitto-pqc/etc/mosquitto/certs/greengrass-device.key','w').write(c['privateKey'])"
sudo curl -so /opt/mosquitto-pqc/etc/mosquitto/certs/AmazonRootCA1.pem \
  https://www.amazontrust.com/repository/AmazonRootCA1.pem

sudo systemctl start mosquitto-pqc && sudo systemctl enable mosquitto-pqc
```

## Interoperability Notes

### Remote Access via SSM (Systems Manager)

The workshop stack includes an SSM hybrid activation that lets you remotely connect to the Raspberry Pi using AWS Systems Manager Session Manager — no SSH keys, VPN, or open ports required.

#### First-Time Setup (Fresh Pi)

After deploying the CloudFormation stack, run the SSM agent setup on the Pi:

```bash
# SSH in (one last time!) or connect physically
ssh pi@<raspberry-pi-ip>

cd ~/Sample-aws-iot-pqc/greengrass

# Set temporary AWS credentials (need SecretsManager read access)
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_SESSION_TOKEN="..."

# Install and register the SSM agent
sudo -E ./setup-ssm-agent.sh us-east-1
```

After this, connect remotely from your laptop:

```bash
# Find the instance ID
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=mi-*" \
  --query 'InstanceInformationList[].{Id:InstanceId,Name:Name,Status:PingStatus}' \
  --output table --region us-east-1

# Start an interactive session
aws ssm start-session --target mi-xxxxxxxxxxxxxxxxx --region us-east-1
```

#### Resetting a Pi from a Previous Workshop / Account

If the Pi was registered to a **different AWS account** (e.g., from a previous workshop dry-run), the SSM agent will try to connect to the old account and fail. You need to reset it:

**Option A: Run the reset script (if you can still SSH in)**

```bash
ssh pi@<raspberry-pi-ip>
cd ~/Sample-aws-iot-pqc/greengrass

export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_SESSION_TOKEN="..."

# --reset flag deregisters from the old account, then re-registers to the new one
sudo -E ./setup-ssm-agent.sh --reset us-east-1
```

**Option B: Manual reset (if SSH works but script isn't available)**

```bash
ssh pi@<raspberry-pi-ip>

# 1. Stop the agent
sudo systemctl stop amazon-ssm-agent

# 2. Remove the old registration
sudo rm -f /var/lib/amazon/ssm/registration

# 3. Clear the credential vault
sudo rm -rf /var/lib/amazon/ssm/Vault
sudo mkdir -p /var/lib/amazon/ssm/Vault

# 4. Re-register with new activation credentials
# Get activation-id and activation-code from Secrets Manager (on your laptop):
#   aws secretsmanager get-secret-value \
#     --secret-id iot/pqc-workshop-greengrass-core/ssm-activation \
#     --query 'SecretString' --output text --region us-east-1

# Then on the Pi:
sudo amazon-ssm-agent -register \
  -code "<new-activation-code>" \
  -id "<new-activation-id>" \
  -region us-east-1 \
  -y

# 5. Start the agent
sudo systemctl start amazon-ssm-agent
```

**Option C: Full nuclear reset (wipe all SSM state)**

Use this if the agent is completely stuck or corrupted:

```bash
ssh pi@<raspberry-pi-ip>

# Remove the agent entirely
sudo systemctl stop amazon-ssm-agent
sudo systemctl disable amazon-ssm-agent
sudo dpkg -r amazon-ssm-agent

# Clean all SSM data
sudo rm -rf /var/lib/amazon/ssm
sudo rm -rf /etc/amazon/ssm

# Re-run setup from scratch
cd ~/Sample-aws-iot-pqc/greengrass
sudo -E ./setup-ssm-agent.sh us-east-1
```

#### How It Works Under the Hood

1. The CloudFormation stack creates an **SSM hybrid activation** (stored in Secrets Manager as `iot/pqc-workshop-greengrass-core/ssm-activation`)
2. The activation includes an **activation ID** and **activation code** (like a one-time password)
3. `setup-ssm-agent.sh` retrieves these from Secrets Manager and uses them to register the Pi
4. Once registered, the Pi gets an instance ID (`mi-xxxxxxxxxxxxxxxxx`) and maintains an outbound HTTPS connection to the SSM service
5. Session Manager, Run Command, and other SSM features work through this connection

#### Network Requirements

The Pi needs **outbound HTTPS (port 443)** to these endpoints:
- `ssm.{region}.amazonaws.com`
- `ssmmessages.{region}.amazonaws.com`
- `ec2messages.{region}.amazonaws.com`

No inbound ports are required. Works through NAT, firewalls, and on workshop WiFi.

#### Running Commands Remotely

```bash
# Interactive shell
aws ssm start-session --target mi-xxxxxxxxxxxxxxxxx --region us-east-1

# Run a single command
aws ssm send-command \
  --instance-ids mi-xxxxxxxxxxxxxxxxx \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["sudo systemctl status mosquitto-pqc"]' \
  --region us-east-1

# Run the full Greengrass setup remotely
aws ssm send-command \
  --instance-ids mi-xxxxxxxxxxxxxxxxx \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["cd ~/Sample-aws-iot-pqc/greengrass && sudo -E ./setup-greengrass-core.sh us-east-1"]' \
  --region us-east-1
```

---

### Key Exchange Agreement

Both the ESP32 (wolfSSL) and the Mosquitto broker (OpenSSL 3.5) must agree on the same TLS 1.3 named group for the PQC handshake:

| Side | Library | Named Group | IANA Code Point |
|------|---------|-------------|-----------------|
| ESP32 (client) | wolfSSL 5.7+ | `SecP256r1MLKEM768` | 4587 |
| Mosquitto (server) | OpenSSL 3.5+ | `SecP256r1MLKEM768` | 4587 |

The ESP32 firmware must be updated from the legacy `WOLFSSL_P521_KYBER_LEVEL5` (code point 12093) to the standard `SecP256r1MLKEM768` (code point 4587) for interoperability.

### wolfSSL Configuration Changes

In the ESP32's `wolfssl_user_settings.h`, replace:
```c
// OLD (Kyber Round 3 legacy — won't interop with OpenSSL)
#define WOLFSSL_HAVE_KYBER
#define WOLFSSL_WC_KYBER

// NEW (IANA-standard ML-KEM — interops with OpenSSL 3.5)
#define WOLFSSL_HAVE_MLKEM
#define HAVE_PQC
#define WOLFSSL_PQC_HYBRIDS
```

In the firmware, replace:
```c
// OLD
wolfSSL_UseKeyShare(ssl, WOLFSSL_P521_KYBER_LEVEL5);

// NEW
wolfSSL_UseKeyShare(ssl, WOLFSSL_SECP256R1_MLKEM768);
```
