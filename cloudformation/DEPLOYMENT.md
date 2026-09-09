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

```bash
cd cloudformation

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

### 4. Build the firmware builder image (one-time, required before flashing)

The per-device firmware compile runs in a CodeBuild image that must exist in ECR
first. `deploy-workshop.sh` has already staged its build source, so just start
the build (the project name is the `FirmwareImageBuilderProject` stack output —
`pqc-workshop-build-image`):

```bash
aws codebuild start-build --project-name pqc-workshop-build-image [--profile <name>]
```

Wait for it to reach `SUCCEEDED` before flashing any device. Until this
completes, device registration/flashing cannot compile firmware.

### 5. Register the Builder-Pi (on the physical Raspberry Pi)

This runs on the Pi itself, not the deploy host. From this repo, generate the
bundle and copy it to the Pi:

```bash
raw_device_builder/bundle-for-builder-pi.sh
```

Then on the Pi, run the bundle's `setup-all.sh`. It installs the SSM agent
(registering the Pi as an SSM managed instance using the stack's activation) and
the `workshop-builder` CLI. After this the Pi appears in
`aws ssm describe-instance-information`.

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
4. Then continue with the shared steps 4–6 above (firmware image build,
   Builder-Pi registration, validate).

> The simplest way to get the exact commands for 3.i–3.iii is to read the
> corresponding sections of `deploy-workshop.sh`.
