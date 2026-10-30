# PQC workshop — deployment guide

The workshop deploys from a **single master CloudFormation template**,
`cloudformation/pqc-workshop-main.yaml`, which nests the workshop stacks:

```
pqc-workshop-main.yaml (master)
├── NetworkStack         — self-contained VPC (2 public subnets, 2 AZs, IGW)
│                          (only when CreateVpc=true — the default)
├── BrokerStack          — Private CA + ECS Fargate PQC broker + NLB + VPC endpoints
├── DeviceBuilderStack   — builder API, CodeBuild, PQC migration (Step Functions)
│                          (consumes BrokerStack's Private CA + NLB endpoint via
│                           native cross-stack references)
├── DeviceStack          — device auto-registration (IoT rule + Lambda)
├── SSMActivationStack   — Builder-Pi SSM hybrid activation
└── DashboardStack       — AppSync/DynamoDB/Cognito backend + v2 dashboard hosting
```

![Deployment pipeline: the master CloudFormation stack nests the network, broker, device-builder, device, SSM-activation, and dashboard stacks; CodeBuild builds the broker and firmware container images and the dashboard frontend; the Builder-Pi is provisioned over SSM and flashes devices](../docs/images/deployment-pipeline.png)

CloudFormation owns the ordering: the broker consumes the VPC from
`NetworkStack` (when creating one), and `DeviceBuilderStack` references
`BrokerStack.Outputs.PrivateCAArn` and `BrokerStack.Outputs.BrokerEndpoint`, so
the broker is fully created before the device-builder stack runs.

---

> **The workshop is the source of truth for installation and teardown.** The
> steps below follow the Workshop Studio operator chapter (section **99**, pages
> 99.1–99.8) and its **Cleanup** page, in the same order and with the same
> commands; this file adds reference detail for self-hosted deploys and for
> maintainers. If they ever disagree, the workshop wins and this file is wrong.
> A deploy has passed only when those steps were followed as written **and**
> worked.

## Deployment sequence

Run these steps in order; each heading names the matching workshop page. The
workshop runs everything from a browser-based **Code Editor** IDE in the
workshop account, in `us-east-1`. For a self-hosted deploy, run the same
commands from any machine with the prerequisites below, and add
`--profile <name>` if you use a named AWS CLI profile.

Plan about **30 minutes** end to end. Most of it is unattended, and the firmware
builder image (step 4) can build while you provision the Builder-Pi (step 6).

### 1. Operator environment (workshop 99.1)

In Workshop Studio the Code Editor IDE is already provisioned with everything
the deploy needs: AWS CLI v2, Node.js LTS, an instance role that can deploy the
stack, and the region set to `us-east-1`. Confirm with:

```bash
aws --version
node --version
aws sts get-caller-identity
```

Self-hosted: AWS CLI v2 with credentials for the target account (Admin or
equivalent — the stacks create IAM roles, ECS, NLB, Private CA, VPC, etc.).
Docker and Node.js are **not** required locally — the container images and the
dashboard are built in CodeBuild.

### 2. Get the source (workshop 99.2)

In Workshop Studio the IDE is pre-loaded with the source in
`~/environment/Sample-aws-iot-pqc`, from the `pqc-workshop-src.zip` in the event
assets bucket. That zip is built from this repository with
`cloudformation/package-workshop-src.sh` (it archives the committed HEAD) and
uploaded before the event, so rebuild and re-upload it whenever the shipped
source changes. Self-hosted: clone this repository.

Every command below runs from the repository root or from `cloudformation/`:

```bash
cd ~/environment/Sample-aws-iot-pqc/cloudformation
```

### 3. Deploy the central stack (workshop 99.3)

> ⚠️ **The workshop WiFi credentials are supplied at deploy time.** The script
> stores them in SSM Parameter Store (`/pqc-workshop/wifi/ssid`, and
> `/pqc-workshop/wifi/password` as a SecureString). The device-identity job
> writes them into each device's `certs` partition at registration, and the
> Builder-Pi WiFi AP (brought up automatically by the `configure-wifi-ap` SSM
> document) broadcasts the same SSID. They are **never committed** to the repo,
> never passed to CloudFormation, and never compiled into firmware. Provide them
> by **one of three methods**, resolved in this precedence (per value, first
> match wins):
>
> 1. **CLI flags:** `--wifi-ssid <ssid> --wifi-password <pw>`
> 2. **Env vars:** `WORKSHOP_WIFI_SSID` / `WORKSHOP_WIFI_PASSWORD`
> 3. **Interactive prompt** (when run from a terminal): the SSID prompt defaults
>    to `reinvent-pqc-iot-wifi`; the password is read hidden and is **required**
>    (re-prompts until non-empty). There is no password default, ever.
>
> Non-interactive with nothing supplied: the SSID falls back to the workshop
> default (`reinvent-pqc-iot-wifi`, not a secret) and the password falls back to
> `changeme` (the deploy still succeeds but devices won't join a real network —
> the script prints a WARN). SSM is the single source of truth: the
> device-identity job and the Builder-Pi AP setup both read it.
>
> **Security:** a `--wifi-password` flag is visible in shell history and `ps`.
> Prefer the env var or the interactive prompt for the real password.

```bash
cd cloudformation

# Simplest: run interactively and let the script prompt for the WiFi (SSID
# defaults to reinvent-pqc-iot-wifi, password entered hidden):
./deploy-workshop.sh <region> [--profile <name>]

# Or supply the WiFi non-interactively via env vars:
export WORKSHOP_WIFI_SSID='reinvent-pqc-iot-wifi'
export WORKSHOP_WIFI_PASSWORD='@reinvent2026'   # WPA2 PSK, 8–63 chars
./deploy-workshop.sh <region> [--profile <name>]

# Or via CLI flags (SSID is fine on the CLI; avoid the real password here):
./deploy-workshop.sh <region> [--profile <name>] --wifi-ssid reinvent-pqc-iot-wifi

# Option A — let the stack create its own VPC (recommended; required for
# Workshop Studio / Control Tower accounts that have no usable default VPC):
./deploy-workshop.sh <region> [--profile <name>] [--device-count 100]

# Option B — bring your own VPC (pass BOTH flags; needs >=2 subnets in >=2 AZs):
./deploy-workshop.sh <region> [--profile <name>] --vpc-id <vpc-xxxx> --subnets <subnet-a,subnet-b>
```

Pass NEITHER `--vpc-id`/`--subnets` (Option A) or BOTH (Option B); passing only
one is rejected. The script confirms the target account before any writes (set
`CONFIRM_DEPLOY=yes` to skip the prompt in automation).

`deploy-workshop.sh` performs the full sequence automatically:

1. package each child template's Lambda code and upload the children to the
   templates bucket;
2. deploy the master stack (with `BrokerTriggerBuild=false`, so the broker's
   in-stack image build is skipped and its ECS service stays at `DesiredCount:
   0`). The stack creates **both** ACM Private CAs here: the classical EC CA
   (8883 path) and the **ML-DSA-44 CA** (8884 path, Step 2);
3. stage the broker + firmware Docker build contexts to S3, build the broker
   container image in CodeBuild, then build the **shared cert-issuer image**
   (`pqc-workshop-cert-issuer-image-build`, OpenSSL 3.5) and run the **broker
   cert provisioner** (`pqc-workshop-broker-cert-provisioner`) in it — this mints
   **both** broker server certs (classical + ML-DSA-44) into Secrets Manager
   **before** scaling the broker ECS service to 1 task (the broker's entrypoint
   fetches those secrets on start, so the order matters);
4. build + sync the v2 dashboard and invalidate CloudFront;
5. print the outputs (dashboard URL, broker endpoint) and the remaining steps.

Approximate duration: ~13–15 minutes (CloudFormation create ~6 min + broker
image build ~3–4 min + cert-issuer image build + broker cert provisioning
~3–4 min).

It prints the stack outputs, including the **dashboard URL** and the
**broker endpoint** — leave them on screen. When it finishes, the cloud side is fully up: broker running behind the NLB with
**both** the classical (8883) and ML-DSA-44 (8884) listeners, and the dashboard
served from CloudFront.

> **Step 2 (ML-DSA-44 certificate migration) is deployed by the same one-shot
> flow above** — no extra self-hosted steps. The second CA is created with the
> stack; the cert-issuer image build + broker cert provisioner (which the script
> runs automatically) produce the ML-DSA-44 broker cert for the 8884 listener;
> and the `pqc-mldsa` device variant is built on demand by the migration Step
> Function when an operator picks it in the dashboard (see the ML-DSA reference
> section). Verify both CAs and the 8884 listener with `check_setup_status.sh` (§5).

### 4. Build the firmware builder image (workshop 99.4 — ONE-TIME, MANDATORY BEFORE ANY FLASH)

> ⚠️ **You MUST run this before registering/flashing the first device.** The
> per-device firmware compile (`pqc-workshop-firmware-build`) runs *inside* a
> custom CodeBuild container image that lives in the `pqc-workshop-firmware-builder`
> ECR repo. A fresh deploy leaves that repo EMPTY, so if you flash before running
> this step the device build fails at PROVISIONING with:
>
> ```
> CannotPullImageManifestError: Requested image not found
> ```
>
> The device Thing/cert still get created, but no firmware is produced. After this
> image build succeeds, just re-run register/flash — no need to re-register.

`deploy-workshop.sh` has already staged the build source, so start the build (the
project name is the `ImageBuilderProjectName` stack output —
`pqc-workshop-build-image`):

```bash
aws codebuild start-build --project-name pqc-workshop-build-image [--profile <name>]
```

Wait for it to reach `SUCCEEDED` before flashing any device (~10–15 min). To
watch it to completion:

```bash
aws codebuild batch-get-builds [--profile <name>] \
  --ids "$(aws codebuild list-builds-for-project --project-name pqc-workshop-build-image \
           --query 'ids[0]' --output text [--profile <name>])" \
  --query 'builds[0].buildStatus' --output text
```

Confirm the image landed: `aws ecr describe-images --repository-name pqc-workshop-firmware-builder`
should list at least one image.

You should see at least one image tagged `latest`:

```bash
aws ecr describe-images --repository-name pqc-workshop-firmware-builder \
  --query 'imageDetails[].imageTags' --output table
```

### 5. Verify the environment (workshop 99.5)

Before touching hardware, from the repository root:

```bash
./check_setup_status.sh --region us-east-1 [--profile <name>]
```

It prints a pass/warn/fail line per check and a summary. A healthy deploy is
almost entirely passes; the Builder-Pi shows offline until step 6, and
device-dependent checks warn until step 8. The check also validates the ML-DSA
resources: **two** ACTIVE Private CAs (an `EC_prime256v1` classical CA and an
`ML_DSA_44` CA) and the broker NLB's **8884** listener + `pqc-broker-mldsa-tg`
target group, alongside the classical 8883 path.

Open the dashboard (no devices yet) and keep the tab open:

```bash
aws cloudformation describe-stacks --stack-name pqc-workshop \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue | [0]" \
  --output text
```

Optionally confirm the broker task is running (`1`):

```bash
aws ecs list-tasks --cluster pqc-workshop-broker --desired-status RUNNING \
  --query 'length(taskArns)' --output text
```

### 6. Provision the Builder-Pi (workshop 99.6)

The Builder-Pi is provisioned in two phases. The **bundle only registers the
SSM agent**; everything else (arduino-cli, the ESP32 core, the GUI app, the
systemd service) is installed *remotely* by SSM documents run from the deploy
host. This is deliberate: running the provisioning via SSM (as root, with a
consistent environment, output captured to SSM + CloudWatch, each step
independently re-runnable) is far more reliable and debuggable than a big
self-contained script run under `sudo` on the Pi.

**Step 6.1 — register the Pi as an SSM managed instance.** From this repo,
generate the (now tiny) bundle and copy it to the Pi:

```bash
raw_device_builder/bundle-for-builder-pi.sh            # add --region if not us-east-1
# copy raw_device_builder/builder-pi-bundle.zip to the Pi (USB / scp)
```

Then on the Pi:

```bash
unzip builder-pi-bundle.zip
cd builder-pi-bundle
sudo ./setup-ssm.sh
```

After ~30s the Pi appears as an online managed instance:

```bash
aws ssm describe-instance-information \
  --query "InstanceInformationList[?Name=='pqc-workshop-builder-pi'].{id:InstanceId,ping:PingStatus,ip:IPAddress}" \
  --output table
```

> **Rebuilding an environment always includes reimaging the Pi.** The reimaged
> Pi registers with a **new** instance ID. Run the orchestrator below against
> it (`--instance-id <new-mi-id>` if more than one instance shows), then
> deregister the old one once it shows `ConnectionLost`:
> `aws ssm deregister-managed-instance --instance-id <old-mi-id>`.

**Step 6.2 — provision the Pi from the deploy host.** Back on the machine with
AWS credentials, run the orchestrator. It waits for the Pi to be Online, then
runs the SSM documents in order (install-awscli → install-toolchain →
deploy-app → configure-service → configure-wifi-ap):

```bash
./raw_device_builder/setup-builder-pi-remote.sh            # add --region / --profile if needed
```

The GUI **auto-starts** at the end (systemd `enable`d + started), reachable at
`http://<pi-ip>:8080`, **and the workshop 2.4 GHz WiFi AP is brought up on the
Pi's USB radio** by the final `configure-wifi-ap` document — so the Pi ends fully
networked with no manual step (see §7). The toolchain step is slow (~10-15 min:
the ~5 GB ESP32 core). Every step's output is captured by SSM and shipped to the
`/pqc-workshop/builder-pi` CloudWatch log group.

**If a step fails**, the orchestrator prints the failing document's output and a
console link, then stops. Fix the cause and re-run just that document without
repeating the slow toolchain install, e.g.:

```bash
./raw_device_builder/setup-builder-pi-remote.sh --only deploy-app
```

The app + assets the deploy-app document installs are staged to
`s3://pqc-workshop-firmware-<acct>/builder-pi/` by `deploy-workshop.sh`. To ship
an updated GUI to an already-provisioned Pi: re-run `deploy-workshop.sh` (or just
re-stage `builder-pi/app.tgz`), then `setup-builder-pi-remote.sh --only deploy-app`.

> **Plug-and-flash — no GUI configuration.** The Builder-Pi GUI takes no WiFi,
> broker, or firmware-version input. Every device value comes from the workshop
> **single source of truth in SSM**: the device-identity job reads the WiFi from
> `/pqc-workshop/wifi/{ssid,password}`, and the registrar passes the broker
> endpoint from `/pqc-workshop/broker/endpoint` to the firmware build. The
> operator just plugs in an ESP32 and clicks **Register & Flash** — the only
> per-device input is the auto-detected serial number. To change the WiFi or
> broker later, update the SSM parameter; the next registration (or **Reflash**,
> for WiFi) picks it up, no redeploy.

### 7. The Builder-Pi as the workshop WiFi AP (workshop 99.7)

The ESP32 devices are **2.4 GHz only** and must join the workshop SSID (e.g.
`reinvent-pqc-iot-wifi`) — the SSID written into their identity partition. The Builder-Pi
itself hosts that network: it becomes a high-density WiFi access point for the
devices on its USB radio and bridges their traffic up to the venue backbone via
its onboard uplink. It is sized for a full workshop room (up to ~150 devices
across one or two Pis; the deploy default is 100).

> **This is now AUTOMATED — no manual steps.** The AP is brought up by the
> `configure-wifi-ap` SSM document, which runs as the final step (5/5) of the Pi
> provisioning chain in `setup-builder-pi-remote.sh` (Step 6.2). It pulls the
> SSID/PSK from SSM (`/pqc-workshop/wifi/{ssid,password}` — the same source the
> device identities are built from, so the AP PSK always matches the devices), sets the
> WiFi regulatory domain, detects the AP-capable USB radio by its `mt76*` driver
> (not by interface name), and builds the NetworkManager `shared`-mode AP. So on
> a normally-provisioned Pi there is **nothing to do here** — this section is
> reference material (hardware, radio roles, monitoring, troubleshooting) plus a
> manual fallback.
>
> Re-run just the AP step at any time with:
> ```bash
> ./raw_device_builder/setup-builder-pi-remote.sh --only configure-wifi-ap
> ```

**Hardware specification.** This is the exact Builder-Pi hardware/OS the workshop
is validated on (a dedicated second Pi may be used instead — see the SPOF note).
The dual-band USB WiFi adapter is required: the ESP32s are 2.4 GHz-only, and the
Pi's onboard radio is a weak AP, so a capable USB radio hosts the workshop AP
(see the radio-role split below).

| Component | Specification | Notes | Where to buy (The Pi Hut) |
|---|---|---|---|
| Single-board computer | Raspberry Pi 5 Model B (Rev 1.1), 16 GB RAM | 64-bit quad-core Arm Cortex-A76 @ 2.4 GHz; 16 GB gives ample headroom for the ESP32 toolchain + concurrent flashing | [Raspberry Pi 5 (16GB variant)](https://thepihut.com/products/raspberry-pi-5) |
| Operating system | Raspberry Pi OS (Debian 13 "Trixie", 64-bit), kernel `6.18.50+rpt-rpi-2712` | NetworkManager-based network stack (used for the AP); check yours with `cat /etc/os-release` | (ships on Raspberry Pi OS media) |
| USB WiFi adapter | AC1300 Dual-Band USB WiFi Adapter — MediaTek **MT7612U** (`0e8d:7612`), 802.11a/b/g/n/ac, USB 3.0 | 2.4 GHz + 5 GHz; the `mt76x2u` driver ships with Raspberry Pi OS and has strong AP-mode support. Hosts the workshop 2.4 GHz AP. | [AC1300 Dual-Band USB WiFi Adapter (MT7612U)](https://thepihut.com/collections/raspberry-pi-store/products/dual-band-usb-wifi-adapter-for-raspberry-pi-5ghz-2-4ghz-mt7612u) |

> The AC1300 adapter enumerates as a MediaTek `MT7612U` — the `configure-wifi-ap`
> SSM document detects the AP radio by its `mt76*` driver (not by interface name),
> so any MT7612U-based dual-band adapter works, not only this specific model.

**Radio role assignment (this split is deliberate — do not swap it):**

| Role | Interface | Radio | Band | Mode | Why |
|---|---|---|---|---|---|
| Workshop IoT AP (`reinvent-pqc-iot-wifi`) | `wlan1` | **AC1300 USB adapter (MT7612U)** | 2.4 GHz | NetworkManager AP (shared) | ESP32s are 2.4 GHz-only; the MT7612U `mt76x2u` driver has strong AP-mode support for a table of devices. |
| Uplink to venue backbone | `wlan0` | Pi onboard (brcmfmac) | 5 GHz (preferred) | station (client) | The onboard radio is a solid *client* but a weak AP; putting the uplink on 5 GHz dodges the brutal 2.4 GHz congestion at re:Invent. |

Putting the demanding AP job on the capable USB MT7612U radio and the easy
station job on the onboard radio — **two physical radios, one job each** — is
what makes the dual-band bridge robust. A single radio doing AP+station tends to
force both onto the same channel/band, which would defeat the 2.4↔5 split.

> **Interface naming.** The table above assumes the USB adapter is `wlan1` and
> the onboard radio is `wlan0`, but USB adapters don't get a stable name across
> reboots. The `configure-wifi-ap` SSM document sidesteps this by picking the AP
> radio by its `mt76*` driver rather than by name — so you don't need to hardcode
> `wlan1`.

#### What the automated `configure-wifi-ap` document does

The document (run automatically as step 5/5 of Pi provisioning) performs exactly
what used to be a manual `nmcli` runbook, using **NetworkManager** (`nmcli`) —
the default network stack on current Raspberry Pi OS — in a single
`ipv4.method shared` connection profile (`workshop-ap`) that owns the AP mode,
DHCP (NM's own `dnsmasq`), the interface IP, and NAT/masquerade to the uplink. In
order it:

1. **Resolves SSID/PSK from SSM** (`/pqc-workshop/wifi/{ssid,password}`) — the
   single source of truth the device identities are built from, so the AP PSK always
   matches the devices. (This eliminates the hand-typed-PSK mistake that once
   caused a `4WAY_HANDSHAKE_TIMEOUT`.)
2. **Sets the WiFi regulatory domain** so the self-managed MediaTek radio will
   actually start a 2.4 GHz AP.
3. **Detects the AP radio by its `mt76*` driver** (not by the `wlan1` name, which
   is not stable for USB adapters across reboots) and never picks the onboard
   uplink radio.
4. **Builds the `workshop-ap` profile** (`802-11-wireless.mode ap`, `band bg`,
   `channel 6`, `wpa-psk`) with `autoconnect yes` so it returns after a reboot,
   and applies the density (`dhcp-lease-max=150`, `10.42.0.10-250` lease pool) and
   power-save (`wifi.powersave=2`) drop-ins for a full room.
5. **Brings the AP up** and verifies `wlan1` reaches the `connected` (AP) state.

An ESP32 flashed for the workshop then associates to the SSID, picks up a
`10.42.0.x` lease, and reaches AWS IoT / the broker through the Pi's `wlan0`
uplink (NM masquerades the AP subnet onto whichever interface has the default
route).

> **Manual fallback.** If you ever need to build the AP by hand (e.g. debugging
> off the SSM path), the equivalent `nmcli` recipe — `dnsmasq-base` install,
> `nmcli connection add/modify ... ipv4.method shared`, the density + power-save
> drop-ins, and `nmcli connection up workshop-ap` — is preserved in git history
> (see the pre-automation revision of this section). Prefer
> `setup-builder-pi-remote.sh --only configure-wifi-ap` over doing it by hand.

**Pre-event checklist (all flagged during design):**

- **Outbound MQTT (8883) must be allowed** from the `wlan0` uplink to the broker
  NLB and AWS IoT. The whole chain dies if the venue blocks it — test end to end
  before the room fills.
- **The onboard `wlan0` must actually associate to the venue network.** Enterprise
  auth / captive portals are painful on a headless Pi — do a dry run on the real
  venue SSID, not just your home network.
- **SPOF:** if this AP Pi dies, every device drops. Keep a spare Pi imaged and
  ready, and prefer running the AP on a *dedicated* Pi rather than the Builder-Pi
  so a flashing hiccup and a WiFi hiccup can't take each other down.

#### Monitoring & troubleshooting

Handy commands for watching the AP during the workshop:

```bash
# Live count of stations authenticated to the workshop AP
sudo iw dev wlan1 station dump | grep -c Station

# DHCP leases: match MAC ↔ IP ↔ hostname
cat /var/lib/misc/dnsmasq.leases

# Per-client signal strength (worse than ~-80 dBm → expect MQTT packet loss)
sudo iw dev wlan1 station dump | grep -E "Station|signal:"

# Watch devices register and claim leases as they boot
watch -n 1 cat /var/lib/misc/dnsmasq.leases

# Raw interface packet/error counters
ip -s link show wlan1
```

> **Architecture note — NAT conntrack vs. MQTT KeepAlive.** Because the Pi routes
> via IP masquerade (NAT), the Linux firewall keeps a conntrack entry per ESP32's
> outbound flow. If a device goes silent too long, that entry is evicted and the
> broker's downstream packets get dropped. Set the ESP32 firmware's **MQTT
> KeepAlive to 15–30 s** so each client pings often enough to keep its conntrack
> slot alive.

### 8. Register and flash the first device (workshop 99.8)

The smoke test that proves the whole environment works end to end. The firmware
builder image (step 4) must have reached `SUCCEEDED` first.

1. Open the Builder-Pi GUI at `http://<pi-ip>:8080` (the IP from step 6.1).
2. Plug an ESP32 into a USB port on the Pi. The GUI auto-detects it and shows
   its serial number.
3. Click **Register & Flash**.

On the cloud side that one click:

- **registers** the device: the IoT Thing (`esp32-<serial>`), a per-device
  policy and the registry entry;
- **issues the device identity**: the `pqc-workshop-device-identity` CodeBuild
  job generates the keys, gets a classical (ECDSA P-256) and an ML-DSA-44 device
  certificate from the two Private CAs, and packs them, both CA roots and the
  WiFi into a 64 KB image for the device's `certs` partition
  (`identity/<serial>/certs.bin`, expired by an S3 lifecycle rule after a day);
- **builds** the classical firmware (`pqc-workshop-firmware-build`). The image
  holds no keys or WiFi, only the broker endpoint, so OTA updates carry no
  secrets.

The Pi then downloads the firmware pieces and the identity image, flashes them
in one `esptool` call (`0x0` bootloader, `0x8000` partitions, `0xe000`
boot_app0, `0x10000` app, `0x721000` certs — never the 8 MB
`firmware.merged.bin`, which would overwrite `certs`), deletes the identity
image from S3, and verifies the boot. The first build pulls the ~3.5 GB builder
image, so the first flash takes a few minutes.

Confirm the device connected: on the dashboard it appears **online** with its
firmware version and the classical TLS algorithm, and in the broker log:

```bash
aws logs filter-log-events \
  --log-group-name /pqc-workshop/broker-container \
  --start-time $(( ($(date +%s) - 600) * 1000 )) \
  --filter-pattern '"esp32"' \
  --query 'events[*].message' --output text
```

you should see the client connect (`CONNACK`), publish its lifecycle "birth"
message, subscribe to `cmd/esp32-<serial>/+`, then publish telemetry.

> **Reflash** re-issues a fresh identity and flashes it with the firmware already
> in S3. A device whose serial console shows `[Setup] FATAL: no valid device
> identity` has a missing, out-of-date or wrong-board `certs` partition and
> deliberately refuses to start WiFi; Reflash fixes it. To pick up a change to the
> *firmware* build itself, Deregister and Register the device instead (Reflash
> reuses the firmware in S3).

---

## Cleanup (workshop Cleanup page)

Workshop Studio reclaims and cleans its accounts when the event ends. For a
self-hosted or test deployment, from `cloudformation/`:

```bash
./teardown-workshop.sh us-east-1 --wait [--profile <name>]
```

It confirms the target account (type `y`, or set `CONFIRM_TEARDOWN=yes`), then,
in order: deletes every `esp32-*` Thing (detaching certificates and policies)
and deprecates the `esp32-pqc-device` Thing Type; scales the broker service to 0;
deletes all images in the `pqc-workshop-firmware-builder`,
`pqc-workshop-mosquitto-broker` and `pqc-workshop-cert-issuer` repositories;
empties every object version and delete-marker from the workshop S3 buckets
(never the Workshop Studio assets bucket); deletes the `pqc-workshop` master
stack (10–15 min with `--wait`); and finally empties and deletes the
CloudFormation artifacts bucket. It is safe to re-run.

Options: `--prune-ssm` deregisters Builder-Pi instances already showing
`ConnectionLost`; `--delete-thing-type` also deletes the deprecated Thing Type
after the required ~5-minute wait (otherwise the next deploy reuses it).

Left in place on purpose (a redeploy reuses or overwrites them): the
`/pqc-workshop/*` SSM parameters, the Builder-Pi's SSM registration unless
pruned, and the devices' `certs` partitions (useless against a new deployment —
their CAs are gone; re-register each device). To delete the SSM parameters when
you are finished with the account:

```bash
aws ssm get-parameters-by-path --path /pqc-workshop --recursive \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | \
  xargs -n 10 aws ssm delete-parameters --names
```

If you cannot run the script, empty the versioned buckets (all versions and
delete-markers) and the three ECR repositories, delete the `esp32-*` Things,
then `aws cloudformation delete-stack --stack-name pqc-workshop`.

---

## Reference: migrating a device's certificate to ML-DSA-44

Step 2 lets a device move its **certificate** from classical ECDSA to a
post-quantum **ML-DSA-44** signature (FIPS 204), on top of the Step 1 ML-KEM key
exchange. The classical and PQC paths run in parallel — nothing is a flag-day
(a single all-at-once cutover) — so a device migrates over the existing OTA process.

There are three firmware variants, each built on demand and delivered over OTA:

| Variant | Key exchange | Device cert | Broker port | Reported firmware |
|---|---|---|---|---|
| `classical` | ECDHE | ECDSA P-256 (classical CA) | 8883 | `1.0.0` |
| `pqc` | hybrid ML-KEM | ECDSA P-256 (classical CA) | 8883 | `2.0.0` |
| `pqc-mldsa` | hybrid ML-KEM | **ML-DSA-44 (ML-DSA CA)** | **8884** | `3.0.0` |

**Operator flow (from the dashboard):**

1. Open the dashboard (the CloudFront URL printed by the deploy / the
   `DashboardURL` output). Each connected device shows a card with its current
   key exchange, TLS version, and firmware — and a **"Running: …"** line derived
   from live telemetry.
2. In the device card's **firmware picker**, choose the target variant
   (`Classical (ECDSA)` / `PQC key exchange (ML-KEM)` / `PQC + ML-DSA-44 cert`).
   The picker pre-selects the next logical step for the device.
3. Click **🚀 Flash**. This calls the `migrateFirmware` AppSync mutation with the
   chosen `variant`, which starts the `pqc-workshop-pqc-migration` Step Function:
   it builds the selected variant (if not already built), pushes the image
   over OTA, and the device reboots into it. Live progress appears on the card
   (Building… → Pushing OTA… → Rebooting…).
4. When the device reconnects, its telemetry reports the new state. For
   `pqc-mldsa` it connects on **8884**, still negotiates hybrid ML-KEM, and now
   presents an ML-DSA-44 certificate. The card flips to reflect it.

Rollback is the same flow in reverse — pick an earlier variant and Flash. The
OTA writes the inactive slot and the device's slot-content-aware migrate handler
targets the resident firmware, so a one-version rollback is preserved.

**Verifying the broker's ML-DSA-44 listener** (independent of any device):

```bash
# The broker NLB DNS is the BrokerEndpoint output; 8884 is the ML-DSA-44 port.
openssl s_client -connect <broker-nlb-dns>:8884 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -noout -text | grep -A1 'Signature Algorithm'
```

The broker server certificate on 8884 should show an ML-DSA-44 signature
(OID `2.16.840.1.101.3.4.3.17`), whereas 8883 shows a classical ECDSA cert.

---

## Updating a running environment (`--stage-only`)

A full `deploy-workshop.sh` run rebuilds the broker and cert-issuer images,
re-provisions the broker certs and rebuilds the dashboard every time, about
20 minutes even when only one sketch changed. When the stack already exists and
you changed only shipped **source**, use the fast path instead:

```bash
cd cloudformation
./deploy-workshop.sh us-east-1 --stage-only [--profile <name>]
```

It needs an existing `pqc-workshop` stack in a stable state, takes about
2–3 minutes, and asks for no WiFi (set `CONFIRM_DEPLOY=yes` to skip the account
prompt in automation). It rejects `--vpc-id`, `--subnets` and `--wifi-*`.

**What it does**

1. Re-stages everything the full deploy uploads to S3: the broker and
   cert-issuer build contexts, the firmware `source/` prefix (both buildspecs,
   the three sketches, the wolfSSL config, the partition table, with the same
   completeness check), and the Builder-Pi `app.tgz` and assets.
2. Rebuilds the dashboard in CodeBuild, syncs it and invalidates CloudFront.
3. Prints, for each kind of change, what to run next.

**What it skips:** template packaging, the stack update, the broker and
cert-issuer image builds, broker cert provisioning, the broker scale-up, and
the SSM config and WiFi publish.

**Which change needs which follow-up**

| You changed | After `--stage-only` |
|---|---|
| Dashboard (`raw_device_builder/dashboard-v2/frontend/`) | Nothing — it is live. Hard-refresh the browser. |
| Firmware sketches, `buildspec.yml`, wolfSSL config, partition table | Takes effect on the **next firmware build**. *Register & Flash* always builds. A dashboard migration **reuses** an existing `<serial>/<version>/firmware.bin`, so Deregister + Register the device (or delete that S3 prefix) to force a rebuild. |
| Firmware-builder image (`Dockerfile`, `buildspec-image.yml`) | `aws codebuild start-build --project-name pqc-workshop-build-image` |
| Builder-Pi app (`raw_device_builder/builder-pi/`) | `./raw_device_builder/setup-builder-pi-remote.sh --region us-east-1 --only deploy-app` |
| CloudFormation, Lambda code, broker or cert-issuer image, SSM config, WiFi | Not covered — run the full `./deploy-workshop.sh us-east-1`. |

> **`--stage-only` is for iterating, not for acceptance.** It updates a running
> environment in place. A change is only done when it survives a destroy + a
> from-scratch deploy that follows the workshop instructions (see the project
> steering).

## VPC: create one, or bring your own

The master takes a `CreateVpc` parameter (default `true`):

- **`CreateVpc=true` (default):** the stack provisions its own VPC via
  `NetworkStack` — two public subnets across two AZs, an internet gateway, and a
  public route table. `VpcId`/`SubnetIds` are ignored. This is the right choice
  for **Workshop Studio / Control Tower accounts**, which often have no usable
  default VPC.
- **`CreateVpc=false`:** bring your own — `VpcId` + `SubnetIds` are then
  required (≥2 subnets across ≥2 AZs).

Either way the broker gets **VPC endpoints** (ECR api/dkr, Secrets Manager,
CloudWatch Logs interface + an S3 gateway) so its Fargate task pulls the image
and secrets over private AWS networking rather than depending on public egress.

## Why the broker image build is a separate step

CloudFormation cannot upload a Docker build context, and the broker's image-build
CodeBuild reads its source from a bucket the broker stack itself creates —
a chicken-and-egg (circular dependency). So the master ships `BrokerTriggerBuild=false`: the stack
creates all infrastructure (including the empty broker source bucket) with the
broker ECS service at `DesiredCount: 0`, and `deploy-workshop.sh` step 3 then
uploads the build context, builds the image, and scales the service to 1. This
is why the image build lives in the deploy script, not in the stack.

## Inputs

| Parameter | Required? | Why |
|---|---|---|
| `TemplateBucketName` | Always | Where the packaged nested templates + Lambda zips live |
| `CreateVpc` | Optional (default `true`) | `true` = stack builds its own VPC; `false` = bring your own |
| `VpcId` | Only if `CreateVpc=false` | Existing VPC for the PQC broker (ECS Fargate + NLB) |
| `SubnetIds` | Only if `CreateVpc=false` | ≥2 subnets across ≥2 AZs for the NLB + ECS tasks |
| `DeviceCount` | Optional (default 100) | Dashboard scaling hint |

## Workshop Studio

Workshop Studio provisions only the operator **Code Editor** IDE
(`static/infrastructure/code-editor.yaml` in the workshop content repository)
and loads `pqc-workshop-src.zip` from the event assets bucket into it (it is the
only template in the workshop's `contentspec.yaml`). The operator then runs this
whole sequence — `deploy-workshop.sh` and steps 4–8 — from that IDE.

The zip ships with the workshop content as `assets/pqc-workshop-src.zip`, which
Workshop Studio publishes to the assets bucket. Rebuild it from the committed
source whenever the shipped source changes, and commit it to the workshop
content repository:

```bash
./package-workshop-src.sh            # writes pqc-workshop-src.zip from HEAD
```

`package.sh` (packaging the nested templates into a provided bucket for a
platform-driven stack deploy) is not used by the current Workshop Studio flow.
