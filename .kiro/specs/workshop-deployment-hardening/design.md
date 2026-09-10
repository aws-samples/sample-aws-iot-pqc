# Design — Workshop Deployment Hardening

## Overview

This design turns the six approved requirements into a concrete architecture. The
guiding principle is **single source of truth**: every resource and every piece
of code is defined in version control and deployed by automation, so a fresh
Workshop Studio account reproduces the working demo with no manual steps.

The design is organized by workstream. Each section states the current state (as
verified against the live account), the target design, and the specific
resources/files involved. A cross-cutting "Deployment model" section ties the
stacks together and defines deploy order.

> Verified facts this design builds on (see `orphan-inventory.md`):
> - Live dashboard = the **v2** "PQC Firmware Manager" (`raw_device_builder/dashboard-v2/`),
>   orphaned (hand-deployed), sharing the v1 backend from
>   `dashboard/infrastructure/dashboard-stack.yaml`.
> - The dashboard **polls** `listDevices` every 5s; no GraphQL subscriptions.
> - Telemetry path: ESP32 → ECS broker-container → MQTT bridge → IoT Core →
>   IoT Rules → `telemetry-processor` → DynamoDB `pqc-workshop-devices` → AppSync.
> - Broker already logs to CloudWatch `/pqc-workshop/broker-container` via the
>   `awslogs` driver with `log_type all` + `connection_messages true`.
> - `pqc-upgrader` is synchronous: it triggers CodeBuild and **waits up to 5 min**
>   inside the Lambda — the cause of the AppSync 30s-timeout error.

---

## Workstream 1 — Asynchronous PQC migration (Step Functions)

### Current state

`migrateFirmware` (AppSync) → invokes the `pqc-upgrader` Lambda synchronously.
The Lambda does: registry lookup → (if needed) issue cert from Private CA +
`start_build` → **`wait_for_build` polling loop up to 300s** → firmware
size/sha256 → presign URL → publish `cmd/<thing>/ota` → update registry status.
Because the build wait exceeds the AppSync 30s resolver timeout, the dashboard
always shows "Failed to trigger PQC migration" even when the migration succeeds.

### Target design

Replace the synchronous invoke with an **AWS Step Functions Standard state
machine** that the resolver starts and returns from immediately. The long wait
moves out of Lambda into the state machine. Live status is written to the device
record so the dashboard's existing 5s poll surfaces progress.

```
Dashboard "Upgrade to PQC"
        │  migrateFirmware(thingName)   (AppSync mutation)
        ▼
AppSync resolver ──► StartExecution (Step Functions)  ──► returns {success:true} <30s
        │
        ▼  (async, no client waiting)
┌──────────────────────── State machine: pqc-workshop-pqc-migration ───────────────────────┐
│ 1. SetStatusBuilding        DynamoDB UpdateItem migrationStatus=building                   │
│ 2. PrepareBuild (Lambda)    registry lookup, issue cert from Private CA, start CodeBuild,  │
│                             return {buildId, s3Key, thingName, serialNumber}               │
│    └─ if firmware already exists → skip to step 5 (Choice on "buildNeeded")                │
│ 3. WaitForBuild             CodeBuild via .sync integration (SFN waits natively)           │
│ 4. SetStatusPushing         DynamoDB UpdateItem migrationStatus=pushing-ota                │
│ 5. PublishOta (Lambda)      presign regional S3 URL, publish cmd/<thing>/ota, size+sha256  │
│ 6. SetStatusRebooting       DynamoDB UpdateItem migrationStatus=rebooting                  │
│ 7. Succeed                                                                                 │
│                                                                                            │
│ Catch (any state) → SetStatusFailed  migrationStatus=failed, migrationError=<cause> → Fail │
└────────────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼  device downloads firmware, reboots with PQC, reports telemetry pqcAlgorithm=MLKEM
Dashboard tile turns green off real telemetry (unchanged; source of truth for "actually PQC")
```

**Why CodeBuild `.sync`:** Step Functions' `arn:aws:states:::codebuild:startBuild.sync`
integration blocks the state machine until the build reaches a terminal state
without consuming Lambda time. This removes the 300s Lambda wait entirely
(satisfies 1.7).

**Status writes:** each `SetStatus*` state is a native DynamoDB `UpdateItem`
service integration (no Lambda needed) against `pqc-workshop-devices`, keyed by
`thingName`, setting `migrationStatus` (and clearing/settling `migrationError`).
Because `listDevices` scans this table and the dashboard polls every 5s, the
status appears without any subscription (satisfies 1.3, 1.6).

> Note on the two tables: telemetry lives in `pqc-workshop-devices` (keyed by
> `thingName`, read by `listDevices`), while device cert/registration lives in
> `pqc-workshop-device-registry` (keyed by `serialNumber`). `migrationStatus`
> MUST be written to `pqc-workshop-devices` so the dashboard sees it. The
> `PrepareBuild`/`PublishOta` Lambdas continue to use the registry table for
> cert material and the `ota-pending` bookkeeping they already do.

### Lambda decomposition

The monolithic `pqc-upgrader` splits into two small Lambdas (both packaged from
source, no stubs):

- **`prepare-build`** — registry lookup; if firmware for the target version is
  absent, issue a device cert from the Private CA and `start_build`, returning
  `{ buildId, s3Key, thingName, serialNumber, buildNeeded }`. (Lifts steps 1-2
  of today's Lambda; the openssl/acm-pca cert issuance stays here.)
- **`publish-ota`** — `head_object` for size, hash for sha256, generate the
  regional presigned URL (keeping the existing `endpoint_url` + `s3v4` +
  `virtual` addressing fix), publish `cmd/<thing>/ota`, and set the registry's
  `targetFirmwareVersion`/`status=ota-pending`. (Lifts steps 3-4.)

The `wait_for_build` polling loop is **deleted** — replaced by the state
machine's `.sync` wait.

### AppSync wiring

- `migrateFirmware` resolver changes from "invoke Lambda" to "start state
  machine execution." Options:
  - **(preferred)** An HTTP data source calling the Step Functions
    `StartExecution` API, or a thin `start-migration` Lambda that calls
    `StartExecution` and returns `{success, message, thingName}` immediately.
  - A thin Lambda is simplest and keeps the resolver mapping trivial; it is the
    chosen approach (one tiny function, easy to unit test).
- The old `PqcUpgraderDataSource`/`pqc-upgrader` ARN references (added in the
  earlier live-capture edits) are removed; `migrateFirmware` targets the
  `start-migration` Lambda's data source.

### GraphQL schema additions

Add to the `Device` type (additive, non-breaking):

```graphql
type Device {
  # ...existing fields...
  migrationStatus: String   # building | pushing-ota | rebooting | done | failed
  migrationError: String
}
```

`listDevices`/`getDevice` already return the whole item, so no resolver change is
needed beyond the schema. The v2 dashboard's `LIST_DEVICES` query gains the two
fields, and `DeviceCard` renders migration progress on the button
(building → pushing-ota → rebooting), replacing the current fire-and-forget
`showMessage`. `pqcAlgorithm` remains what flips the tile to PQC/green
(satisfies 1.5).

### New resources (all in CloudFormation)

| Resource | Type | Notes |
|---|---|---|
| `PqcMigrationStateMachine` | `AWS::StepFunctions::StateMachine` | Standard workflow; definition via `DefinitionSubstitutions` for ARNs/table names |
| `PqcMigrationStateMachineRole` | `AWS::IAM::Role` | least-privilege: `codebuild:StartBuild`+`BatchGetBuilds` on the build project, `dynamodb:UpdateItem` on `pqc-workshop-devices`, `lambda:InvokeFunction` on the two task Lambdas |
| `PrepareBuildFunction` (+role) | `AWS::Lambda::Function` | packaged code; acm-pca + codebuild + registry perms |
| `PublishOtaFunction` (+role) | `AWS::Lambda::Function` | packaged code; s3 + iot:Publish(`cmd/*/ota`) + registry perms |
| `StartMigrationFunction` (+role) | `AWS::Lambda::Function` | packaged code; `states:StartExecution` on the state machine |
| AppSync data source + updated `migrateFirmware` resolver | `AWS::AppSync::*` | targets StartMigration |

Retire: the manually-created `pqc-workshop-pqc-upgrader` Lambda (satisfies 1.9).

### Which stack owns it

The migration workflow depends on the CodeBuild project + Private CA (in the
device-builder stack) and writes to `pqc-workshop-devices` (dashboard stack).
Placing the state machine + task Lambdas in the **device-builder stack** keeps
them next to the CodeBuild project and registry they use; the dashboard stack
only needs the `StartMigration` Lambda ARN for its resolver (referenced by fixed
name, consistent with the existing convention). Final placement is confirmed in
tasks; the key constraint is no cross-stack circular dependency.

---

## Workstream 2 — Builder-Pi CLI

### Current state

Builder-Pi operations live in a Flask app (`raw_device_builder/builder-pi/app.py`
+ `device_manager.py`) driven by a browser GUI and a systemd service
(`pqc-builder-gui.service`). Operators (and this session) drove it by writing
ad-hoc Python scripts over SSM. Operations: scan/detect USB, read serial number,
register (SigV4 → API GW `/register`), wait for build, download firmware from S3,
flash via arduino-cli/esptool, verify boot, deregister, read serial buffer.

### Target design

Introduce a single installed CLI, `workshop-builder`, that shares the exact
operation implementations with the Flask app by extracting them into a reusable
module. No behavior is duplicated between GUI and CLI (satisfies 2.6).

```
raw_device_builder/builder-pi/
  builder_core/            # NEW: extracted operations (importable, no Flask)
    __init__.py
    operations.py          # register(), build_wait(), flash(), reflash(),
                           # deregister(), read_serial(), service_start/stop()
    device_manager.py      # moved here (USB/serial/flash primitives)
    aws.py                 # SigV4 session helper (shared)
    logging.py             # structured, timestamped logging to stdout + file
  cli.py                   # NEW: argparse entry point -> builder_core.operations
  app.py                   # Flask app, refactored to call builder_core.operations
  setup.py / pyproject     # installs `workshop-builder` console_script
```

**Command surface** (each a single non-interactive line, satisfies 2.4):

| Command | Action |
|---|---|
| `workshop-builder scan` | list USB slots + detected serials |
| `workshop-builder register --port <p> [--serial S]` | register → build → download → flash → verify |
| `workshop-builder reflash --port <p>` | re-flash existing S3 firmware |
| `workshop-builder flash --port <p> --bin <path>` | flash a specific binary (the SSM flash-cycle we did by hand) |
| `workshop-builder deregister --port <p>` | SigV4 deregister |
| `workshop-builder serial --port <p> [--lines N]` | dump recent serial buffer |
| `workshop-builder service {start|stop|status}` | control the builder service |

**Logging (2.2):** all commands log structured, timestamped lines to stdout/stderr
and append to `/var/log/workshop-builder/<command>.log`, so an SSM `send-command`
transcript is self-describing.

**Exit codes (2.3):** non-zero on any failure, with a clear final error line.

**Auth (2.7):** reuses the Pi's SSM instance role via the shared `aws.py` SigV4
helper (same as the Flask app today); no embedded secrets.

**Install (2.5):** `setup-builder-pi.sh` installs the package
(`pip install .` / `pipx`) so `workshop-builder` is on `PATH`, and the bundle
(`bundle-for-builder-pi.sh`) ships `builder_core/` + `cli.py`. A rebuilt Pi gets
the CLI automatically.

---

## Workstream 3 — Broker observability

### Current state (better than assumed)

The broker container **already** logs to CloudWatch: `mosquitto.conf` has
`log_dest stdout`, `log_type all`, `connection_messages true`; the ECS task uses
the `awslogs` driver to group `/pqc-workshop/broker-container` (retention 7 days),
defined in `broker-container-stack.yaml`. So connection/subscription logging and
CloudWatch shipping exist (satisfies most of 3.1, 3.2, 3.5 already).

The actual gaps that cost debugging time were:
1. **Two brokers, two log groups** — a dead Raspberry-Pi mosquitto logged to
   `/pqc-workshop/mosquitto-broker` while the live ECS broker logs to
   `/pqc-workshop/broker-container`. Reading the wrong group wasted hours.
2. **No telemetry-content signal at the broker** — mosquitto with `log_type all`
   logs connect/subscribe/publish *events* but not message *payloads*, so you
   cannot confirm from the broker log what a device published.
3. **Broker instance identity** — logs didn't make it obvious *which* broker
   instance/task produced them.

### Target design

- **3a. Eliminate the dead broker + its log group** (ties into Greengrass/legacy
  cleanup, Workstream 5): remove the Pi-mosquitto path and the
  `/pqc-workshop/mosquitto-broker` group so there is exactly one broker and one
  log group. (Directly addresses gap 1 and 3.4.)
- **3b. Broker identity in logs**: set mosquitto to include a clear instance
  marker. The `awslogs-stream-prefix` is already `mosquitto`; add the ECS task
  ID to the stream (default `awslogs` behavior includes the container/task id in
  the stream name) and log a one-line banner at startup identifying the broker
  (`entrypoint.sh` already prints a startup banner — extend it with the task
  ARN / NLB target so the stream is self-identifying). (Addresses gap 3.)
- **3c. Telemetry-content visibility**: rather than making mosquitto log payloads
  (it can't cleanly), rely on the **existing message-logger path** — the IoT
  rules `pqc_workshop_message_log_dt`/`_cmd` already capture full message
  payloads to DynamoDB, surfaced by the dashboard's device log view. The design
  makes this the authoritative "what did the device publish" signal and ensures
  the message-logger is in IaC (Workstream 4/5). Optionally raise mosquitto
  verbosity so a publish to `dt/<thing>/telemetry` is visible as an event in the
  broker log correlated by client id. (Addresses gap 2, 3.3.)
- **3d. Log group + retention stays in CFN** (already true; keep, ensure single
  group). (3.2, 3.5)

This reframes Req 3 from "add logging" (already present) to "make logging
unambiguous: one broker, one group, self-identifying streams, and a clear
device-publish signal."

---

## Workstream 4 — Infrastructure as single source of truth

### 4a. Lambda packaging (no stubs)

**Current:** templates carry placeholder `ZipFile` stubs; real code in
`*/index.py` is pushed by hand via `update-function-code`. This shipped the
deregistrar-stub bug.

**Target:** adopt `aws cloudformation package` (a.k.a. `aws cloudformation
deploy --template ... ` with `AWS::Serverless`/`Code: <local path>`), which zips
each Lambda's source, uploads to an artifacts bucket, and rewrites the template
with the S3 reference. Every `AWS::Lambda::Function` points at real code via
`Code: <dir>` (or SAM `CodeUri`) instead of an inline stub. The deploy tooling
runs `package` then `deploy`, so no separate `update-function-code` step exists
(satisfies 4.2). Functions affected: all in device-builder, dashboard, broker,
and the new migration Lambdas.

### 4b. Consolidate duplicate Lambda source (4.3)

Today `lambda/telemetry-processor` and `dashboard/lambda/telemetry-processor`
(and both `command-publisher` copies) exist. Choose **one** authoritative
location per function — `dashboard/lambda/*` for dashboard functions,
`raw_device_builder/lambda/*` for builder/OTA functions — and delete the
top-level `lambda/*` duplicates that are not the source of truth, updating
templates to package from the chosen path. The dashboard stack stops inlining
`ZipFile` code and packages from `dashboard/lambda/*` instead.

### 4c. Single authoritative non-firmware config (4.4)

Non-firmware config that must match across layers (WiFi SSID, broker endpoint,
IoT endpoint, table/bucket names) is defined once as stack
parameters/outputs and passed to dependents via `Ref`/`ImportValue`/env vars,
never copied. (Firmware-embedded values are out of scope — deferred.)

### 4d. Deploy tooling completeness (4.5, 4.6, 4.7)

- Fix `deploy-workshop.sh`/`package.sh` to upload **every** child template the
  parent references, including `ssm-hybrid-activation-stack.yaml` (closes the
  gap). After Greengrass removal, the parent's child set shrinks accordingly.
- Express cross-stack references explicitly: broker-container exports the Private
  CA ARN; device-builder imports it. Document the fixed-name convention where
  used (e.g. the `StartMigration` Lambda name the dashboard resolver references).
- Provide one documented deploy command/sequence honoring order:
  **broker-container → device-builder → main/dashboard**, plus the one-time
  Docker build-image step (CodeBuild `pqc-workshop-build-image`) that bakes the
  wolfSSL settings into the firmware build image.

### 4e. Bring orphans into IaC (4.1)

Per `orphan-inventory.md`, add to CloudFormation: `message-logger` Lambda+role,
`broker-log-reader` Lambda+role, `message-log` table (all defined in
dashboard-stack source but live as manual), and the **v2 dashboard hosting**
(see Workstream 5.2).

---

## Workstream 5 — Cleanup and de-duplication

### 5.1 Remove the redundant v1 frontend (keep the shared backend)

The v1 frontend hosting (`DashboardBucket` = `pqc-workshop-dashboard-*`,
`DashboardDistribution` = `E35PBBUBSD7O3C`, and `dashboard/frontend/` "Device
Dashboard" source) is **DashboardStack-managed**, so removal = deleting those
resources + their outputs (`DashboardURL`, `DashboardBucketName`) from
`dashboard-stack.yaml`, and deleting the `dashboard/frontend/` source tree. The
shared backend (AppSync `pqc-workshop-device-api`, `pqc-workshop-devices`,
Lambdas, IoT rules, Cognito) **stays** — the live v2 dashboard depends on it.

### 5.2 Make the v2 dashboard reproducible (bring hosting into IaC)

The v2 hosting (bucket `pqc-workshop-firmware-dashboard-*` + CloudFront
`E2C5AHMIRUKFJE`) is orphaned/hand-deployed. Add a hosting definition (S3 +
CloudFront + OAC + bucket policy, mirroring the pattern currently in
dashboard-stack) — either in the dashboard stack (replacing the removed v1
hosting) or a small dedicated hosting stack. Add a build/deploy step: build the
Vite app (`dashboard-v2/frontend`, `npm ci && npm run build`) and sync `dist/` to
the bucket + invalidate CloudFront, wired into the deploy tooling so a fresh
account serves v2 with no manual `s3 sync` (satisfies 5.2, 4.1). The v2
`config.js` values (AppSync endpoint, identity pool, REST API) are injected at
deploy time from stack outputs rather than hardcoded.

### 5.3 / 5.4 Remove Greengrass (confirmed not load-bearing)

Confirmed (Workstream context + inventory): telemetry does not traverse
Greengrass. Remove:
- `cloudformation/greengrass-core-stack.yaml` + the `GreengrassStack` nested
  stack in `pqc-workshop-main.yaml` and its Greengrass outputs.
- `GreengrassBridgePolicy` + `GreengrassBridgePolicyName` output in
  `dashboard-stack.yaml` (that policy is for the dead Pi bridge; the ECS broker
  uses its own `BRIDGE_CERT_SECRET`).
- The `pqc-workshop-greengrass-core-cert-provisioner` Lambda (GreengrassStack).
- `greengrass/` legacy scripts and `greengrass/check_rpi_setup_status.sh`.

**Preserve the SSM-activation naming dependency (5.3):** the SSM hybrid
activation stack and the broker's bridge client id derive a name from
`GreengrassThingName` (e.g. `pqc-workshop-broker`). Rather than delete that
parameter and break Builder-Pi remote access, **rename/replace** it with a
neutral parameter (e.g. `WorkshopInstanceName` defaulting to `pqc-workshop`) and
repoint the SSM activation + broker bridge client id at it. The vestigial
`GREENGRASS_THING` env var in the broker task becomes `BROKER_NAME` (bridge
client id `${BROKER_NAME}-bridge`). This removes the Greengrass coupling without
changing behavior.

### 5.5 Refactor `check_setup_status.sh`

Rewrite to validate the current architecture:
- v2 dashboard hosting (bucket `pqc-workshop-firmware-dashboard-*` + its
  CloudFront) instead of v1.
- Shared AppSync `pqc-workshop-device-api`, Cognito identity pool, and the
  `pqc-workshop-devices` + `pqc-workshop-message-log` + `pqc-workshop-device-registry`
  tables.
- The **actual** Lambda names (device-registrar, device-deregistrar,
  firmware-pusher, telemetry-processor, command-publisher, message-logger,
  broker-log-reader, and the new migration Lambdas) — drop
  `pqc-workshop-auto-register`/`greengrass-*` unless still present.
- The broker-container/ECS/NLB/Private CA stack (currently unchecked).
- The real IoT rule names (`pqc_workshop_telemetry`, `_lifecycle`,
  `_message_log_dt`, `_message_log_cmd`), dropping Greengrass/`auto_register`
  checks that no longer apply.
- The Step Functions state machine.
Remove all Greengrass checks (nucleus, Java, greengrass-credentials secret).

### 5.6 Remove dead code / duplication

- Delete `dashboard/frontend/` (dead v1 UI) — its backend stays under
  `dashboard/infrastructure/`.
- Delete top-level `lambda/*` duplicates superseded by the chosen authoritative
  copies (4.3).
- Delete `greengrass/` legacy tree and the second status script (5.3/5.5).
- The confusingly-named `raw_device_builder/dashboard-v2/` is the **live** app;
  keep it. (Optionally rename to drop the misleading "v2" once v1 is gone — a
  follow-up nicety, not required.)

### 5.7 Orphaned live-resource removal (confirm-before-delete)

Per constraint C3, produce the removal list (from `orphan-inventory.md`) and
delete live resources **only with explicit per-item operator confirmation** and
an impact statement. Because the workshop is destroyed/recreated per delivery,
the primary deliverable is that a **fresh deploy** does not recreate the
redundant resources (5.8); deleting them from the *current* live account is
optional operator cleanup, gated on confirmation.

---

## Workstream 6 — Automated tests

### Framework & layout

- **Python:** `pytest` + `moto` (mock AWS) for all Lambda handlers; `boto3`
  stubbers where `moto` lacks coverage (e.g. acm-pca issuance). Tests live under
  each Lambda source root, e.g. `raw_device_builder/lambda/<fn>/test_index.py`
  and `dashboard/lambda/<fn>/test_index.py`, plus `builder-pi/tests/` for the CLI.
- **State machine:** validate the ASL definition (JSON/structure) and run
  happy-path + caught-failure using either Step Functions Local (mocked service
  integrations) or a definition-level unit test asserting the state graph and the
  `migrationStatus` writes each path performs.
- **One command to run** (6.5): a top-level `run-tests.sh`/`make test` that runs
  the unit suite with mocks only (no live AWS).

### Coverage (maps to 6.1–6.4)

| Test | Asserts | Req |
|---|---|---|
| `publish-ota` | regional presign params (`s3v4`, virtual), publishes to `cmd/<thing>/ota` with correct payload (url/version/size/sha256), sets registry `ota-pending` | 6.1 |
| `prepare-build` | registry lookup, cert issuance path invoked, `start_build` env overrides correct, `buildNeeded` choice output | 6.1 |
| `telemetry-processor` | telemetry event → correct DynamoDB item (pqcAlgorithm/tlsVersion/etc.); lifecycle event → status/ip; TTL set | 6.1 |
| `device-deregistrar` | returns well-formed JSON; performs documented IoT/S3/DynamoDB deletes | 6.1, **6.2 (regression)** |
| `message-logger` | correct thingName/topic/direction extraction, TTL, put_item | 6.1 |
| `start-migration` | calls `StartExecution` with thingName input; returns `{success,true}` synchronously | 6.1 |
| state machine | ASL valid; happy path writes building→pushing-ota→rebooting; failure path writes `failed`+`migrationError` | 6.3 |
| builder CLI | argparse routes each subcommand; operations invoked with parsed args (AWS/serial mocked); non-zero exit on failure | 6.4 |

### Integration tests (kept separate, 6.6)

Real CodeBuild/IoT end-to-end checks are marked `@pytest.mark.integration` and
excluded from the default fast run, documented as optional.

---

## Workstream 7 — Single master template for Workshop Studio

### Current state

The master `pqc-workshop-main.yaml` nests only 3 stacks (device-provisioning,
ssm-hybrid-activation, dashboard). The **broker-container** and **device-builder**
stacks are deployed standalone by `deploy-workshop.sh`, which passes the
broker's Private CA ARN into device-builder between separate deploys. Pointing
Workshop Studio at the master alone would NOT deploy the broker or the
builder/OTA backend, so the demo wouldn't work.

### Target design

Nest all five stacks under the master so a single template deploys everything
CloudFormation can. The cross-stack dependency is expressed natively.

```
pqc-workshop-main.yaml (master)
├── BrokerStack            → broker-container-stack.yaml   (Private CA, ECS PQC broker, NLB)
├── DeviceBuilderStack     → device-builder-stack.yaml     (builder API, CodeBuild, migration)
│     DependsOn: BrokerStack
│     PrivateCAArn:        !GetAtt BrokerStack.Outputs.PrivateCAArn
│     DefaultBrokerEndpoint: !GetAtt BrokerStack.Outputs.BrokerEndpoint
├── DeviceStack            → device-provisioning-stack.yaml
├── SSMActivationStack     → ssm-hybrid-activation-stack.yaml
└── DashboardStack         → dashboard-stack.yaml
```

**Cross-stack references (7.2):** device-builder's `PrivateCAArn` and
`DefaultBrokerEndpoint` come from `!GetAtt BrokerStack.Outputs.*` with a
`DependsOn: BrokerStack`, replacing the script-passed values. `IoTEndpoint` is
derived in the master (`${AWS::AccountId}-ats.iot.${AWS::Region}.amazonaws.com`)
and passed to both.

**New master parameters (7.3):** `VpcId` (`AWS::EC2::VPC::Id`) and `SubnetIds`
(`List<AWS::EC2::Subnet::Id>`) for the broker's NLB/ECS, passed to `BrokerStack`.
Documented as required inputs (Workshop Studio supplies them, or a default-VPC
convenience can be documented).

**Packaging (7.6):** because the broker and device-builder templates contain
Lambda `Code:` local paths, the deploy tooling still runs `aws cloudformation
package` on each child before uploading to the templates bucket the master
references. `deploy-workshop.sh` becomes a thin wrapper that packages + uploads
all five children then deploys the single master (instead of orchestrating the
ordering itself — CloudFormation now owns the ordering via `DependsOn`).

**Standalone deployability preserved (7.5):** each child template keeps its own
parameters (with defaults), so it can still be deployed on its own for
development. The master simply supplies those parameters via nested-stack
`Parameters:`.

### What stays manual (7.4 — documented, not hidden)

CloudFormation cannot do these; they remain documented post-deploy steps:
1. **One-time firmware Docker image build** — run the `pqc-workshop-build-image`
   CodeBuild project once so the firmware build image exists in ECR. (The broker
   image build is already auto-triggered by the broker stack's `TriggerBuild`.)
2. **v2 dashboard build + sync** — build the Vite app and sync to its bucket +
   invalidate CloudFront (a build tool + S3 sync, not expressible in plain CFN).
   Kept in `deploy-workshop.sh`; could later become a CodeBuild custom resource.
3. **Builder-Pi SSM agent registration** — installing/registering the SSM agent
   on the physical Raspberry Pi is inherently on-device.

### Chicken-and-egg note (broker image)

The broker ECS service references the ECR image that the broker stack's own
CodeBuild builds on create (`TriggerBuild=true`). On a truly cold account the
service may start before the first image is pushed; this is existing behavior
(not introduced by nesting) and self-heals once the image is available. Flagged
so the deploy docs can note "the broker task may take one restart cycle on first
deploy."

---

## Workstream 8 — Builder-Pi observability

### Current state (the gap)

The register→build→flash→verify pipeline splits into an AWS half and a Pi half:

- **AWS half — already observable.** The `device-registrar`/`prepare-build`
  Lambdas log to `/aws/lambda/*` (cert issuance, thing/policy creation, build
  start, full tracebacks on failure); the firmware CodeBuild project logs to
  `/aws/codebuild/pqc-workshop-firmware-build`.
- **Pi half — invisible off-box.** S3 download, esptool flash, and serial/boot
  verification were written only to `DeviceSlot.progress`, an in-memory ring
  buffer (last 50, 20 surfaced to the browser). esptool output was kept only as
  a 300-char tail. The GUI service's stdout goes to journald only; the
  interesting detail uses `slot.log()` (in-memory), not `print`. Everything is
  discarded when the device is unplugged or `pqc-builder-gui` restarts, and the
  `BuilderPiSsmRole` did not (in effect, scoped) ship anything to CloudWatch.

So a flash that fails at the download/flash/boot stage — the most failure-prone,
physical part — left no durable, remotely-readable evidence. This parallels
Workstream 3 (broker observability) but for the Pi.

### Target design

- **8a. Persist the full transcript on the Pi.** `DeviceSlot.log()` becomes the
  single choke point: in addition to the in-memory ring buffer it appends every
  line to `/var/log/workshop-builder/device-<serial|port>.log` and a combined
  `flash.log`, with a UTC timestamp and device key. Because every
  download/flash/verify step already routes through `slot.log()`, this captures
  the whole pipeline for the GUI path too (not just the CLI's `OperationLogger`).
  File writes are best-effort (never break a flash if the dir is read-only).
  (Satisfies 8.1.)
- **8b. Keep the FULL esptool output.** `flash_firmware` writes the complete
  esptool stdout/stderr to the device log file (the GUI slot keeps its concise
  last-5-line tail for the browser). (Satisfies 8.2.)
- **8c. Ship logs via the CloudWatch agent.** `setup-builder-pi.sh` installs the
  `amazon-cloudwatch-agent` (arch-matched .deb) and writes a config that tails
  `/var/log/workshop-builder/*.log` to a dedicated log group
  `/pqc-workshop/builder-pi` (per-host stream, 14-day retention), started in
  `onPremise` mode so it uses the SSM instance role. Region is resolved from the
  deployed `config.json`, else the SSM registration, else `us-east-1`.
  (Satisfies 8.3.)
- **8d. Least-privilege IAM.** `BuilderPiSsmRole`'s `logs:` statement is scoped
  from `*` to the `/pqc-workshop/builder-pi` log-group ARN (Create/PutLogEvents/
  DescribeLogStreams/PutRetentionPolicy). No embedded credentials — the agent
  uses the instance role. (Satisfies 8.4.)
- **8e. Graceful degradation.** If the agent can't be installed (download
  failure) or the log dir isn't writable, setup emits a clear `[WARN]`, logs
  still persist locally where possible, and the flow does not fail.
  (Satisfies 8.5.)

Net: `/pqc-workshop/builder-pi` (Pi flash/serial) + `/aws/lambda/*` (registrar)
+ `/aws/codebuild/pqc-workshop-firmware-build` (compile) together make the whole
register→build→flash→verify path readable in CloudWatch. (Satisfies 8.6.)

### Which stack / files own it

- Code: `raw_device_builder/builder-pi/device_manager.py` (log persistence +
  full esptool output), and it composes with the CLI's existing
  `builder_core/logging.py` (same `/var/log/workshop-builder` dir).
- Provisioning: `raw_device_builder/builder-pi/setup-builder-pi.sh` (agent
  install + config).
- IAM + log group access: `BuilderPiSsmRole` in
  `raw_device_builder/cloudformation/device-builder-stack.yaml`.

---

## Deployment model (cross-cutting)

### Stacks (after this work)

| Stack | Owns | Depends on |
|---|---|---|
| `broker-container` | ECS broker, NLB, Private CA, bridge, broker log group | — (first) |
| `device-builder` | registry table, builder API, CodeBuild, ECR, registrar/deregistrar/firmware-pusher + migration state machine & task Lambdas | Private CA ARN (import from broker-container) |
| `main`/`dashboard` | shared backend (AppSync, `pqc-workshop-devices`, telemetry/command/message-logger/broker-log-reader Lambdas, IoT rules, Cognito), v2 dashboard hosting | `StartMigration` Lambda name (device-builder) |

Greengrass/SSMActivation nested stacks: SSM activation is retained (Builder-Pi
remote access) but decoupled from Greengrass naming; the Greengrass nucleus stack
is removed.

### Deploy order & command

```
1. (one-time) build the firmware Docker image  → CodeBuild pqc-workshop-build-image
2. deploy broker-container      (creates Private CA)
3. deploy device-builder        (imports Private CA; migration workflow)
4. deploy main/dashboard        (shared backend; references StartMigration by name)
5. build + sync v2 dashboard    (npm run build → s3 sync → CloudFront invalidate)
6. run check_setup_status.sh    (validate)
```

A single wrapper script (`deploy-workshop.sh`, fixed) runs `aws cloudformation
package` + `deploy` per stack in this order with the artifacts bucket, then the
dashboard build/sync. All child templates it references are uploaded (4.6).

### Safety

Per C1, none of this is deployed against the live working demo without explicit
approval; templates target a fresh account. Destructive live cleanup (5.7) is
per-item confirmed (C3).

---

## Key design decisions (and rejected alternatives)

- **Step Functions `.sync` CodeBuild wait** (rejected: async Lambda + EventBridge
  on build completion). `.sync` is simpler, gives a single visible execution
  history for teaching/observability, and removes the Lambda wait. EventBridge
  wiring would be more moving parts for the same result.
- **Status via DynamoDB write + existing 5s poll** (rejected: AppSync
  subscriptions). The dashboard already polls; adding subscription infra is
  unnecessary complexity for the same UX.
- **Two task Lambdas + one starter** (rejected: keep one fat Lambda invoked
  async). Splitting matches the state boundaries, keeps each unit-testable, and
  lets the state machine own retries/catch per step — the observability the user
  asked for.
- **`cloudformation package` for Lambda code** (rejected: keep placeholder stubs
  + `update-function-code`). Packaging makes code+infra one deployable unit and
  kills the stub-bug class.
- **Rename `GreengrassThingName` → neutral name** (rejected: delete it). The SSM
  activation + broker bridge derive a name from it; renaming preserves Builder-Pi
  access while removing Greengrass coupling.
- **Keep `dashboard-v2` as the live UI** (rejected: revive `dashboard/frontend`).
  v2 is what works and is in use; v1 frontend is the redundant one.
- **CLI shares impl with Flask GUI** (rejected: standalone CLI scripts). Shared
  `builder_core` prevents GUI/CLI drift.
- **Stay on raw CloudFormation** (rejected: rewrite the deployment in AWS CDK
  (Python)). CDK's Lambda-bundling and typed Step Functions constructs would be
  ergonomic, and the whole stack could be Python. But for this project the costs
  outweigh the benefits: (1) CDK requires `cdk bootstrap` in every fresh Workshop
  Studio account — an extra prerequisite and failure mode that works against the
  "smooth, minimal-prerequisite fresh deploy" goal, whereas raw CFN +
  `cloudformation package` needs only an artifacts bucket; (2) it would be a
  full rewrite of ~10 working, hard-won templates (broker task def, CodeBuild
  image wiring, Private CA, wolfSSL-baked build), re-introducing risk into
  exactly the fragile components we must not destabilize; (3) CDK adds a
  Node/CDK-CLI toolchain dependency to the deploy environment (today only the AWS
  CLI is needed); (4) it does not address the actual root cause of this session's
  rework — manually-created resources drifting from IaC and placeholder Lambda
  stubs — which the single-source-of-truth discipline fixes regardless of
  language. The two real CDK wins (automatic Lambda code packaging, explicit
  cross-stack references) are already captured here via `cloudformation package`
  (4a) and export/import (4d). CDK is worth revisiting later as its own
  deliberate migration (with the working CFN as the reference) if the templates
  begin to hurt from repetition — not bundled into this reliability push.
