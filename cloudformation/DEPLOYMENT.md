# PQC Workshop — Deployment Guide

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

CloudFormation owns the ordering: the broker consumes the VPC from
`NetworkStack` (when creating one), and `DeviceBuilderStack` references
`BrokerStack.Outputs.PrivateCAArn` and `BrokerStack.Outputs.BrokerEndpoint`, so
the broker is fully created before the device-builder stack runs.

---

## Deployment sequence

Run these steps in order. Steps 1–3 stand up the cloud infrastructure; steps 4–6
are the pieces CloudFormation cannot do (build a container image, build a
frontend, register software on physical hardware). Replace `<region>` throughout
(e.g. `us-east-1`), and add `--profile <name>` if you use a named AWS CLI profile.

### 1. Prerequisites

- AWS CLI v2, configured with credentials for the target account (Admin or
  equivalent — the stacks create IAM roles, ECS, NLB, Private CA, VPC, etc.).
- Node.js + npm on the deploy host (the dashboard is built locally in step 3).
- Docker is **not** required locally — container images are built in CodeBuild.

### 2. (Self-hosted only) package the templates — SKIP for Workshop Studio

Self-hosted deploys use `deploy-workshop.sh`, which packages the templates for
you, so you can skip straight to step 3. Run `package.sh` only when staging
assets for **Workshop Studio** (see "Workshop Studio" below).

### 3. Deploy the stack + build the broker image + build the dashboard

> ⚠️ **The workshop WiFi credentials are supplied at deploy time** — the device
> firmware (both the initial flash and the classical→PQC migration) is built
> with them, and the builder-Pi hostapd AP broadcasts the same SSID. They are
> **never committed** to the repo (the template defaults are the harmless
> placeholder `changeme`). Provide them by **one of three methods**, resolved in
> this precedence (per value, first match wins):
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
> the script prints a WARN). The resolved values are threaded to the firmware
> build and published to SSM (`/pqc-workshop/wifi/ssid` as String,
> `/pqc-workshop/wifi/password` as SecureString) as the single source of truth
> the registrar/migration Lambdas and the builder-Pi AP setup consume.
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
   0`);
3. stage the broker + firmware Docker build contexts to S3, build the broker
   container image in CodeBuild, then scale the broker ECS service to 1 task;
4. build + sync the v2 dashboard and invalidate CloudFront;
5. print the outputs (dashboard URL, broker endpoint) and the remaining steps.

Approximate duration: ~10–11 minutes (CloudFormation create ~6 min + broker
image build ~3–4 min).

When it finishes, the cloud side is fully up: broker running behind the NLB and
the dashboard served from CloudFront.

### 4. Build the firmware builder image (ONE-TIME, MANDATORY BEFORE ANY FLASH)

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
project name is the `FirmwareImageBuilderProject` stack output —
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

### 5. Provision the Builder-Pi (two steps: register on the Pi, provision from the host)

The Builder-Pi is provisioned in two phases. The **bundle only registers the
SSM agent**; everything else (arduino-cli, the ESP32 core, the GUI app, the
systemd service) is installed *remotely* by SSM documents run from the deploy
host. This is deliberate: running the provisioning via SSM (as root, with a
consistent environment, output captured to SSM + CloudWatch, each step
independently re-runnable) is far more reliable and debuggable than a big
self-contained script run under `sudo` on the Pi.

**Step 5.1 — register the Pi as an SSM managed instance.** From this repo,
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

After ~30s the Pi appears Online in `aws ssm describe-instance-information`.

**Step 5.2 — provision the Pi from the deploy host.** Back on the machine with
AWS credentials, run the orchestrator. It waits for the Pi to be Online, then
runs the three SSM documents in order (install-toolchain → deploy-app →
configure-service):

```bash
./raw_device_builder/setup-builder-pi-remote.sh            # add --region / --profile if needed
```

The GUI **auto-starts** at the end (systemd `enable`d + started), reachable at
`http://<pi-ip>:8080`. The toolchain step is slow (~10-15 min: the ~5 GB ESP32
core). Every step's output is captured by SSM and shipped to the
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
> broker, or firmware-version input. Every device value is resolved server-side
> by the registrar Lambda from the workshop **single source of truth in SSM**:
> WiFi from `/pqc-workshop/wifi/{ssid,password}` and the broker endpoint from
> `/pqc-workshop/broker/endpoint` (published from the broker stack output at
> deploy time). The operator just plugs in an ESP32 and clicks **Register &
> Flash** — the only per-device input is the auto-detected serial number. The
> GUI shows the resolved broker endpoint read-only for reference. To change the
> WiFi or broker later, update the SSM parameter — the next build picks it up,
> no redeploy.

### 5a. (re:Invent only) Turn the Builder-Pi into the workshop WiFi AP + 2.4↔5 bridge

The ESP32 devices are **2.4 GHz only** and must join `reinvent-pqc-iot-wifi` —
the SSID baked into their firmware. At the venue you control that network by
running it *on the Builder-Pi itself*: the Pi becomes a high-density WiFi access
point for the devices and bridges their traffic up to the venue backbone. This is
optional and only needed when you can't (or don't want to) rely on venue-provided
WiFi. The steps below are sized for a full workshop room (up to ~150 devices
across one or two Pis; the deploy default is 100).

**Hardware:** a Raspberry Pi 5 running Raspberry Pi OS (Bookworm) — the
Builder-Pi, or a dedicated second Pi (see the SPOF note) — plus an **Alfa
AWUS036ACM** USB WiFi adapter (MediaTek MT7612U).

**Radio role assignment (this split is deliberate — do not swap it):**

| Role | Interface | Radio | Band | Mode | Why |
|---|---|---|---|---|---|
| Workshop IoT AP (`reinvent-pqc-iot-wifi`) | `wlan1` | **Alfa MT7612U** | 2.4 GHz | NetworkManager AP (shared) | ESP32s are 2.4 GHz-only; the MT7612U `mt76x2u` driver has strong AP-mode support and an external antenna for a table of devices. |
| Uplink to venue backbone | `wlan0` | Pi onboard (brcmfmac) | 5 GHz (preferred) | station (client) | The onboard radio is a solid *client* but a weak AP; putting the uplink on 5 GHz dodges the brutal 2.4 GHz congestion at re:Invent. |

Putting the demanding AP job on the capable Alfa radio and the easy station job
on the onboard radio — **two physical radios, one job each** — is what makes the
dual-band bridge robust. A single radio doing AP+station tends to force both onto
the same channel/band, which would defeat the 2.4↔5 split.

> **NetworkManager approach.** These steps use **NetworkManager (`nmcli`)**, which
> is the default network stack on current Raspberry Pi OS (Bookworm+). NM manages
> the AP mode, the DHCP server (its own dnsmasq instance), the interface IP, and
> the NAT/masquerade to the uplink — all in a single connection profile
> (`ipv4.method shared`). Do **not** also run standalone `hostapd`/`dnsmasq`/
> `dhcpcd` for this interface; exactly one system must own `wlan1`. (A legacy
> `hostapd`+`dnsmasq` recipe for older, non-NM images is in git history if needed.)

> **Interface naming:** confirm which interface is which with `nmcli device` (or
> `iw dev`). The Alfa usually enumerates as `wlan1` when the onboard is `wlan0`,
> but verify — the steps below assume Alfa=`wlan1`, onboard=`wlan0`. If they
> differ, substitute throughout.

**1. Install the DHCP helper package.** NetworkManager's `shared` mode runs its
own `dnsmasq` instance for DHCP/NAT, but on a minimal image it needs the base
files present:

```bash
sudo apt update
sudo apt install dnsmasq-base -y
```

**2. Pull the workshop WiFi credentials from SSM** (the single source of truth
published by the deploy — `/pqc-workshop/wifi/ssid` + `/pqc-workshop/wifi/password`).
Run this on the Pi (it has the `pqc-workshop-builder-pi-ssm` role, which grants
`ssm:GetParameter` on `/pqc-workshop/*` plus SSM-scoped `kms:Decrypt`).

> **Always pull from SSM — do not hand-type the SSID/PSK.** A mistyped PSK is
> exactly what caused a `4WAY_HANDSHAKE_TIMEOUT` device failure during testing.
> SSM is the single source of truth the firmware is built against, so pulling
> from it guarantees the AP PSK matches what the devices expect.

```bash
REGION=<region>
WIFI_SSID=$(aws ssm get-parameter --name /pqc-workshop/wifi/ssid \
  --region "$REGION" --query 'Parameter.Value' --output text)
WIFI_PSK=$(aws ssm get-parameter --name /pqc-workshop/wifi/password --with-decryption \
  --region "$REGION" --query 'Parameter.Value' --output text)
echo "AP SSID: $WIFI_SSID"   # sanity check; do NOT echo the PSK in shared logs
```

**3. Create the AP connection profile on the Alfa (`wlan1`).** `ipv4.method
shared` is what makes NM run a DHCP server on the AP subnet AND set up NAT to the
active uplink automatically — no separate dnsmasq/iptables needed. `band bg` +
`channel 6` pin it to 2.4 GHz for the ESP32s. `autoconnect yes` means the AP
comes back on its own after a reboot:

```bash
sudo nmcli connection add type wifi ifname wlan1 con-name workshop-ap \
    autoconnect yes ssid "$WIFI_SSID"
sudo nmcli connection modify workshop-ap \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.channel 6 \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$WIFI_PSK"
```

> `channel 6` is a safe default (1/6/11 are the non-overlapping 2.4 GHz choices).
> At the venue, scan for the quietest with `sudo nmcli device wifi list ifname
> wlan1` and set `802-11-wireless.channel` accordingly. **If you run two AP Pis,
> put them on different non-overlapping channels** (e.g. Pi #1 on `channel 1`,
> Pi #2 on `channel 11`) so they don't step on each other. NM's `shared` mode
> uses the `10.42.0.0/24` subnet by default (Pi = `10.42.0.1`); that's fine — the
> ESP32s get a lease automatically.

**4. Raise the client cap past NetworkManager's default 50.** NM's built-in
`dnsmasq` caps concurrent DHCP clients at 50 by default — well short of a full
workshop room. Add a drop-in to expand the pool and shorten the lease so inactive
IPs recycle quickly:

```bash
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
sudo tee /etc/NetworkManager/dnsmasq-shared.d/iot-density.conf >/dev/null <<'EOF'
# Raise maximum concurrent clients to 150
dhcp-lease-max=150
# Set a 2-hour DHCP lease window to recycle IPs quickly
dhcp-range=10.42.0.10,10.42.0.250,2h
EOF
```

**5. Disable WiFi power saving on the AP radio.** Stop the MT7612U from dropping
into a low-power sleep state under intermittent load, which wrecks MQTT latency.
Set the value from `3` (enabled) to `2` (disabled):

```bash
sudo tee /etc/NetworkManager/conf.d/default-wifi-powersave-on.conf >/dev/null <<'EOF'
[connection]
wifi.powersave = 2
EOF
```

**6. Push changes live.** Restart NetworkManager to pick up the density and
power-save drop-ins, then bring the AP up:

```bash
sudo systemctl restart NetworkManager
sudo nmcli connection up workshop-ap
nmcli connection show --active            # expect workshop-ap (wifi) + your uplink
nmcli -f GENERAL.STATE device show wlan1  # expect connected (as AP)
```

An ESP32 flashed for the workshop should now associate to `reinvent-pqc-iot-wifi`,
pick up a `10.42.0.x` lease, and reach AWS IoT / the broker through the Pi's
`wlan0` uplink (NM masquerades the AP subnet onto whichever interface has the
default route).

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
# Live count of stations authenticated to the Alfa AP
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

### 6. Validate readiness

```bash
./check_setup_status.sh --region <region> [--profile <name>]
```

Before steps 4 and 5 are complete, expect warnings for the firmware image and the
Builder-Pi — that is normal. Once both are done and at least one device has
connected, the check should pass clean.

---

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
a chicken-and-egg. So the master ships `BrokerTriggerBuild=false`: the stack
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

Workshop Studio deploys the master template directly (it does not run
`deploy-workshop.sh`), so the operator must perform the deploy script's post-stack
steps by hand.

1. Stage the assets (packages the network + workshop children and uploads them):
   ```bash
   ./package.sh <region> <ws-assets-bucket> pqc-workshop
   ```
2. Point Workshop Studio content provisioning at
   `cloudformation/pqc-workshop-main.yaml` with parameters:
   - `TemplateBucketName` = `<ws-assets-bucket>`
   - `TemplateBucketPrefix` = `pqc-workshop`
   - `CreateVpc` = `true` (default; no VPC prep needed in the account)
   - `VpcId` / `SubnetIds` = leave empty unless `CreateVpc=false`
   - `DeviceCount` (optional, default 100)
   - `DefaultWifiSsid` = the workshop WiFi SSID (e.g. `reinvent-pqc-iot-wifi`) —
     WS bypasses `deploy-workshop.sh`, so set the real value **here** (the
     template default is the placeholder `changeme`).
   - `DefaultWifiPassword` = the workshop WiFi password (WPA2 PSK, 8–63 chars) —
     `NoEcho`; likewise set here, do not leave as `changeme`.
3. **After the stack completes**, do the steps `deploy-workshop.sh` would have
   done, in this order:
   1. stage the broker Docker build context to the broker source bucket and start
      the broker image build (CodeBuild `pqc-workshop-broker-image-build`); wait
      for `SUCCEEDED`;
   2. scale the broker service up:
      ```bash
      aws ecs update-service --cluster pqc-workshop-broker \
        --service pqc-workshop-broker --desired-count 1
      ```
   3. build the v2 dashboard, sync it to the dashboard S3 bucket, and invalidate
      CloudFront.
   4. publish the workshop WiFi to SSM as the single source of truth for the
      builder-Pi AP setup (the script does this; WS must do it by hand). Use the
      SAME values as the `DefaultWifiSsid`/`DefaultWifiPassword` params above:
      ```bash
      aws ssm put-parameter --name /pqc-workshop/wifi/ssid \
        --type String --overwrite --value 'reinvent-pqc-iot-wifi'
      aws ssm put-parameter --name /pqc-workshop/wifi/password \
        --type SecureString --overwrite --value '@reinvent2026'
      ```
4. Then continue with the shared steps 4–6 above (firmware image build,
   Builder-Pi registration, validate).

> The simplest way to get the exact commands for 3.i–3.iii is to read the
> corresponding sections of `deploy-workshop.sh`.
