# PQC Workshop — Deployment Guide

The workshop deploys from a **single master CloudFormation template**,
`cloudformation/pqc-workshop-main.yaml`, which nests all five stacks:

```
pqc-workshop-main.yaml (master)
├── BrokerStack          — Private CA + ECS Fargate PQC broker + NLB
├── DeviceBuilderStack   — builder API, CodeBuild, PQC migration (Step Functions)
│                          (consumes BrokerStack's Private CA + NLB endpoint via
│                           native cross-stack references)
├── DeviceStack          — device auto-registration (IoT rule + Lambda)
├── SSMActivationStack   — Builder-Pi SSM hybrid activation
└── DashboardStack       — AppSync/DynamoDB/Cognito backend + v2 dashboard hosting
```

CloudFormation owns the ordering: `DeviceBuilderStack` references
`BrokerStack.Outputs.PrivateCAArn` and `BrokerStack.Outputs.BrokerEndpoint`, so
the broker is fully created before the device-builder stack runs.

### Broker container image: a two-pass build

CloudFormation cannot upload a Docker build context, and the broker's image-build
CodeBuild reads its source from a bucket the broker stack itself creates. To avoid
that chicken-and-egg, the master template takes a `BrokerTriggerBuild` parameter
(default `false`). `deploy-workshop.sh` therefore:

1. deploys the stack with `BrokerTriggerBuild=false` — this creates all infra
   (including the empty broker source bucket) but skips the in-stack image build;
   the broker ECS service ships at `DesiredCount: 0`, so nothing needs the image yet;
2. uploads the Docker build contexts to their now-existing source buckets;
3. starts the broker image-build CodeBuild, waits for it, then scales the broker
   ECS service to 1 task.

## One-command deploy (self-hosted)

```bash
cd cloudformation
./deploy-workshop.sh <region> [--profile NAME] --vpc-id <vpc-xxxx> --subnets <subnet-a,subnet-b> [--device-count 100]
```

The script runs five steps:

1. package each child template's Lambda code (`aws cloudformation package`) and
   upload the packaged children to the templates bucket;
2. deploy the single master stack (with `BrokerTriggerBuild=false`);
3. stage the broker + firmware Docker build contexts to S3, build the broker
   container image, and scale the broker ECS service up;
4. build + sync the v2 dashboard (a build step CloudFormation cannot do);
5. print the remaining manual steps.

`--profile NAME` targets a named AWS CLI profile for every call (useful for
multi-account testing); it prints the resolved account + caller ARN and asks for
confirmation before any writes (bypass with `CONFIRM_DEPLOY=yes`).

**Approximate duration: ~10-11 minutes.** The CloudFormation create is ~6 min
(BrokerStack ~3.5 min, then DeviceBuilderStack ~2.5 min serialized after it), and
the post-deploy broker Docker image build adds ~3-4 min.

## Workshop Studio

1. Stage the assets (packages all five children + uploads them):
   ```bash
   ./package.sh <region> <ws-assets-bucket> pqc-workshop
   ```
2. Point Workshop Studio content provisioning at:
   `cloudformation/pqc-workshop-main.yaml`
3. Supply parameters:
   - `TemplateBucketName` = `<ws-assets-bucket>`
   - `TemplateBucketPrefix` = `pqc-workshop`
   - `VpcId` = the workshop VPC
   - `SubnetIds` = at least two subnets across two AZs
   - `DeviceCount` (optional, default 100)

> **Note:** deploying the master directly (Workshop Studio) leaves
> `BrokerTriggerBuild=false`, so the broker container image is **not** built and
> the v2 dashboard is **not** built/synced automatically — those are the local
> steps `deploy-workshop.sh` performs. After a direct master deploy you must
> stage the broker Docker context, run the broker image build, scale the broker
> service to 1, and build+sync the dashboard yourself (or run the relevant part
> of `deploy-workshop.sh`).

## Required inputs

| Parameter | Why |
|---|---|
| `VpcId` | The PQC broker (ECS Fargate + NLB) is deployed into this VPC |
| `SubnetIds` | The NLB + ECS tasks need ≥2 subnets across ≥2 AZs |
| `TemplateBucketName` | Where the packaged nested templates + Lambda zips live |

## Remaining manual steps (cannot be done by CloudFormation)

These are inherent limitations — CloudFormation cannot build container images,
build a frontend, or register software on physical hardware.

1. **One-time firmware Docker image build.** Before any device firmware can be
   built, the firmware build image must exist in ECR. After the stack deploys,
   run the image-builder CodeBuild project once (its name is the
   `FirmwareImageBuilderProject` stack output):
   ```bash
   aws codebuild start-build --project-name <FirmwareImageBuilderProject>
   ```
   (The **broker** container image is built automatically by the broker stack on
   create; only the **firmware** build image needs this one-time run.)

2. **v2 dashboard build + sync.** `deploy-workshop.sh` does this for you (builds
   the Vite app, injects the AppSync/Cognito/API config from stack outputs,
   syncs to S3, invalidates CloudFront). If you deploy the master template
   directly (e.g. via Workshop Studio) rather than through `deploy-workshop.sh`,
   run that build+sync step afterward.

3. **Builder-Pi SSM registration.** Installing and registering the SSM agent on
   the physical Raspberry Pi is on-device. Use
   `raw_device_builder/bundle-for-builder-pi.sh` to generate the bundle, then run
   its `setup-all.sh` on the Pi. This also installs the `workshop-builder` CLI.

## First-deploy note (broker image)

On a cold account the broker ECS service may start before its first container
image finishes building (the broker stack triggers that build on create). The
task typically self-heals within one restart cycle once the image is pushed.

## Validate

```bash
./check_setup_status.sh --region <region>
```
