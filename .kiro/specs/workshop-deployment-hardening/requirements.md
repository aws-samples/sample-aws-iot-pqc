# Requirements — Workshop Deployment Hardening

## Introduction

The ESP32 post-quantum-cryptography (PQC) AWS IoT workshop demo currently works
end-to-end, but the path to get there exposed structural weaknesses that make a
clean, repeatable deployment fragile. Because the workshop is destroyed and
recreated per delivery (typically in a fresh AWS account via Workshop Studio),
the deployment must be fully automated, reproducible from source, and free of
manual steps and stale/duplicate resources.

This spec hardens the deployment across six workstreams:

1. **Async PQC migration** — replace the synchronous, timeout-prone migration
   Lambda with an observable AWS Step Functions workflow that reports live
   status to the dashboard.
2. **Builder-Pi CLI** — replace ad-hoc, on-the-fly scripts with a single
   installed CLI so operations are reviewable, logged, and reproducible.
3. **Broker observability** — ensure the PQC broker container logs device
   telemetry/connections with clear identity, shipped to CloudWatch.
4. **Infrastructure as single source of truth** — every resource defined in
   version-controlled templates, Lambda code packaged as part of deployment,
   firmware/config values de-duplicated to one authoritative source.
5. **Cleanup / de-duplication** — remove the redundant v1 dashboard frontend,
   remove unused Greengrass components, refactor the readiness checker, and
   delete orphaned resources — without touching the shared, load-bearing
   backend.
6. **Automated tests** — unit tests for Lambda handlers, the state machine
   definition, and the Builder-Pi CLI, tied to acceptance criteria.

### Established facts (verified against the live account 087195661091 / us-east-1)

These facts are load-bearing for the requirements below and were confirmed by
inspecting live AWS resources and deployed artifacts:

- **The live dashboard the workshop uses is the v2 "PQC Firmware Manager"**
  (`raw_device_builder/dashboard-v2/`, a Vite/React app), served from CloudFront
  distribution `E2C5AHMIRUKFJE` (`d21c2wyzdgiex7.cloudfront.net`), S3 bucket
  `pqc-workshop-firmware-dashboard-087195661091`. It is currently deployed **by
  hand with no CloudFormation** (build + `s3 sync`).
- **The v2 frontend shares the v1 backend.** It calls AppSync
  `pqc-workshop-device-api`
  (`https://mpalbkcb6za2hnwibxygktd7ya.appsync-api.us-east-1.amazonaws.com/graphql`),
  the device-builder REST API
  (`https://683vdu3tii.execute-api.us-east-1.amazonaws.com/prod`), and Cognito
  identity pool `us-east-1:589e1d0a-9a61-4021-a7e9-643cd1899375`. This backend is
  created by `dashboard/infrastructure/dashboard-stack.yaml` and is
  **load-bearing — it must not be deleted.**
- **The v1 dashboard frontend is redundant.** The `dashboard/frontend/` "PQC
  Workshop — Device Dashboard" app, served from CloudFront `E35PBBUBSD7O3C` /
  bucket `pqc-workshop-dashboard-087195661091`, is superseded by v2 and is safe
  to remove (frontend hosting only).
- **The v2 frontend polls; it does not subscribe.** It refreshes device state
  every 5s via the `listDevices` query (`pollInterval: 5000`); there are no
  GraphQL subscriptions. Live migration status therefore only needs to be
  written into the device record that `listDevices` returns.
- **Greengrass is not on the working data path.** Telemetry flows
  ESP32 → ECS broker-container (Mosquitto+oqs) → MQTT bridge → IoT Core → IoT
  Rules → telemetry-processor Lambda → DynamoDB → AppSync → dashboard. Greengrass
  is legacy and not load-bearing for telemetry or commands.
- **Lambda templates use placeholder `ZipFile` stubs**; real code lives in
  `*/index.py` and is deployed out-of-band via `update-function-code`. This
  caused a shipped bug (deregistrar stub returned invalid JSON).
- **Deploy-script gap:** `cloudformation/deploy-workshop.sh` does not upload
  `ssm-hybrid-activation-stack.yaml`, which the parent stack references.

### Global constraints

- **C1 — Do not disrupt the live demo.** No requirement in this spec authorizes
  deploying changes against the currently-running live stacks without explicit
  operator approval. Templates target a clean fresh-account deployment.
- **C2 — Fresh-account reproducibility.** Every requirement must be satisfiable
  by a from-scratch deploy in an empty AWS account with no manual post-deploy
  steps.
- **C3 — Least privilege & production safety.** New IAM roles grant only the
  permissions needed. Destructive cleanup actions require explicit operator
  confirmation and clear impact statements.
- **C4 — Inclusive language** in all code, comments, and documentation.

---

## Requirement 1 — Asynchronous, observable PQC migration

**User story:** As a workshop attendee, I want to click "Migrate to PQC" and see
live progress on the dashboard, so that I understand what is happening and am not
shown a spurious error while the migration is still working.

#### Acceptance criteria

1.1. WHEN the dashboard invokes the `migrateFirmware` mutation THEN the system
SHALL start an asynchronous workflow and return a success response to the
dashboard within the AppSync resolver timeout (30s), without waiting for the
firmware build to complete.

1.2. WHEN a migration workflow runs THEN the system SHALL be implemented as an
AWS Step Functions state machine that orchestrates: mark building → start
firmware build → wait for build completion → presign firmware URL → publish the
OTA command → mark awaiting-reboot.

1.3. WHEN the workflow transitions between stages THEN the system SHALL write a
`migrationStatus` value (one of: `building`, `pushing-ota`, `rebooting`, `done`,
`failed`) to the device's record in the device state table read by
`listDevices`.

1.4. WHEN any workflow stage fails THEN the system SHALL set
`migrationStatus=failed` and record a human-readable `migrationError`, and SHALL
NOT leave the device stuck in an intermediate status.

1.5. WHEN the device subsequently reports telemetry with a PQC algorithm THEN the
dashboard SHALL reflect the device as PQC-enabled based on that real telemetry
(the telemetry `pqcAlgorithm` remains the source of truth for "actually PQC", not
`migrationStatus`).

1.6. WHEN `migrationStatus` changes THEN the dashboard SHALL surface it to the
user via its existing 5-second `listDevices` poll (no GraphQL subscription is
required).

1.7. WHERE the migration workflow needs to wait for the firmware build THEN the
system SHALL wait using a Step Functions mechanism (not by holding a Lambda open
for the full build duration).

1.8. WHEN the migration workflow and its resources are deployed THEN they SHALL
be defined entirely in CloudFormation (state machine, its IAM role, and any
supporting Lambda), with no manually-created resources.

1.9. WHEN the migration workflow replaces the current synchronous
`pqc-workshop-pqc-upgrader` Lambda THEN the system SHALL retire the synchronous
invocation path so there is exactly one migration mechanism.

---

## Requirement 2 — Builder-Pi CLI

**User story:** As a workshop operator, I want a single installed CLI on the
Builder-Pi for all device operations, so that SSM commands are short, reviewable,
logged, and identical every time, and so the Builder-Pi can be recreated
reliably in a new account.

#### Acceptance criteria

2.1. WHEN an operator performs a Builder-Pi operation THEN the system SHALL
provide a single installed command-line entry point (e.g. `workshop-builder`)
exposing subcommands for the operations previously done via ad-hoc scripts:
register a device, build/reflash firmware, flash a device, start/stop the builder
service, and read serial output.

2.2. WHEN a CLI subcommand runs THEN it SHALL emit structured, timestamped log
output to stdout/stderr and to a persistent log file, so that an SSM
`send-command` invocation captures a reviewable record.

2.3. WHEN an operation fails THEN the CLI SHALL exit with a non-zero status code
and a clear error message, so that automation and operators can detect failure.

2.4. WHEN the CLI is invoked over SSM THEN each operation SHALL be expressible as
a single non-interactive command line with explicit arguments (no interactive
prompts).

2.5. WHEN the Builder-Pi is provisioned THEN the CLI SHALL be installed
automatically by the Builder-Pi setup/bootstrap process, so a rebuilt Pi has the
CLI without manual copying.

2.6. WHERE the existing Flask GUI service and the CLI overlap in functionality
THEN they SHALL share the same underlying operation implementation (single
source of truth for the device operations), so behavior cannot drift between GUI
and CLI.

2.7. WHEN the CLI performs a cloud-authenticated call (e.g. register/deregister)
THEN it SHALL use the Builder-Pi's own instance credentials (SSM role) with
SigV4, matching the existing GUI behavior, and SHALL NOT embed long-lived
secrets.

---

## Requirement 3 — Broker container observability

**User story:** As a workshop operator debugging connectivity, I want the PQC
broker container to log device connections and telemetry with clear identity and
ship all logs to CloudWatch, so that I can immediately tell which broker a device
is talking to and what it is publishing.

#### Acceptance criteria

3.1. WHEN a device connects, disconnects, subscribes, or publishes to the PQC
broker THEN the broker container SHALL log the event including the device/client
identity and source, in a way that unambiguously identifies the broker instance.

3.2. WHEN the broker container produces logs THEN all logs SHALL be shipped to a
CloudWatch Logs group dedicated to the broker container, defined in
CloudFormation.

3.3. WHEN device telemetry passes through the broker THEN there SHALL be a log
signal sufficient to confirm the device is publishing through that broker
(closing the gap that made it impossible to tell which broker a device used).

3.4. WHEN the broker log group is queried by the dashboard's broker-log feature
THEN the log group name/wiring SHALL match what the dashboard reads, so the
"Broker Log" view shows the correct broker.

3.5. WHEN the broker container is deployed THEN log configuration (log group,
retention, log driver) SHALL be defined in CloudFormation with no manual setup.

---

## Requirement 4 — Infrastructure as single source of truth

**User story:** As a maintainer, I want every resource and every piece of code
defined in version control and deployed automatically, so that a fresh account
deploy reproduces the working demo with no manual patching.

#### Acceptance criteria

4.1. WHEN the workshop is deployed to a fresh account THEN every AWS resource
required for the working demo SHALL be created by CloudFormation, including
resources that are currently created manually (the PQC migration workflow, the
message-logger, the broker-log reader, and the v2 dashboard hosting).

4.2. WHEN a Lambda function is deployed THEN its real implementation SHALL be
packaged and deployed as part of the stack deployment (e.g. via
`aws cloudformation package`/`sam` or an equivalent build step), so that no
Lambda is left as a placeholder stub and no separate `update-function-code` step
is required.

4.3. WHEN the same logical Lambda exists in more than one source location today
(e.g. `lambda/telemetry-processor` and `dashboard/lambda/telemetry-processor`)
THEN the system SHALL consolidate to a single authoritative source per function.

4.4. WHEN a **non-firmware** config value that must match across layers (e.g.
WiFi SSID, broker endpoint, IoT endpoint, table/bucket names passed to Lambdas)
is defined THEN it SHALL have a single authoritative definition (stack
parameter/output) consumed by dependents rather than copied into multiple files.
(Firmware-embedded values such as wolfSSL `MAX_CERTIFICATE_SZ` and OTA buffer
sizes are explicitly deferred — see Out of scope: firmware de-duplication.)

4.5. WHEN the parent workshop stack references a child template THEN the deploy
tooling SHALL upload/package every referenced child template (closing the
`ssm-hybrid-activation-stack.yaml` gap), so the parent stack cannot reference a
missing template.

4.6. WHEN cross-stack references are required THEN they SHALL be expressed
explicitly (export/import or documented fixed-name convention) and SHALL be
satisfiable by a from-scratch deploy in the correct order
(broker-container → device-builder → dashboard/main).

4.7. WHEN the deployment completes THEN a single documented command (or a small
fixed sequence) SHALL deploy the entire workshop from scratch, with deploy order
and prerequisites documented.

---

## Requirement 5 — Cleanup and de-duplication

**User story:** As a maintainer, I want redundant frontends, unused Greengrass
components, stale checks, and orphaned resources removed, so that the workshop
has no unnecessary duplication and a fresh deploy is lean.

#### Acceptance criteria

5.1. WHEN the redundant v1 dashboard frontend is removed THEN the system SHALL
remove only the v1 frontend hosting (CloudFront `E35PBBUBSD7O3C`, S3 bucket
`pqc-workshop-dashboard-*`, and the `dashboard/frontend/` "Device Dashboard"
source) and SHALL NOT remove the shared backend (AppSync `pqc-workshop-device-api`,
DynamoDB tables, Lambdas, IoT rules, Cognito) that the live v2 dashboard depends
on.

5.2. WHEN the v2 dashboard is made reproducible THEN its hosting (S3 + CloudFront)
and its build/deploy SHALL be defined in IaC + automation, replacing the current
manual `s3 sync`, so a fresh account serves the v2 dashboard without manual
steps.

5.3. WHEN Greengrass components are removed THEN the system SHALL remove the
Greengrass nucleus stack, the `GreengrassBridgePolicy`, Greengrass-specific
outputs/parameters, and related legacy scripts, WHILE preserving any naming
dependency the Builder-Pi SSM activation relies on (the SSM hybrid activation
currently derives names from `GreengrassThingName`) so removing Greengrass does
not break Builder-Pi remote access.

5.4. WHEN Greengrass removal is proposed THEN the system SHALL confirm (by
inspection) that the current live telemetry/command path does not traverse
Greengrass before removing any Greengrass resource.

5.5. WHEN the readiness checker is refactored THEN `check_setup_status.sh` SHALL
validate the resources of the current architecture (v2 dashboard hosting, shared
AppSync backend, the correct Lambda names, the broker-container/ECS/NLB/Private
CA stack, the actual IoT rule names, and all device tables) and SHALL NOT check
for removed Greengrass resources or stale Lambda/table/rule names.

5.6. WHEN legacy/dead code is identified (e.g. `raw_device_builder/dashboard-v2`
being the live one vs `dashboard/frontend` being dead, duplicate Lambda trees,
`greengrass/` legacy scripts, the second `check_rpi_setup_status.sh`) THEN the
system SHALL remove or consolidate it so there is no unused duplication.

5.7. WHEN orphaned live AWS resources from earlier deployments are identified
THEN the system SHALL produce an inventory and remove them ONLY with explicit
operator confirmation per removal, with a clear impact statement (per constraint
C3), since deletion is destructive.

5.8. WHEN cleanup is complete THEN a fresh-account deploy SHALL NOT recreate any
of the removed redundant resources.

---

## Requirement 6 — Automated tests

**User story:** As a maintainer, I want unit tests for the Lambda handlers, the
migration workflow, and the Builder-Pi CLI, so that regressions like the
deregistrar stub bug are caught automatically before deployment.

#### Acceptance criteria

6.1. WHEN Lambda handler logic is tested THEN the system SHALL provide unit tests
(pytest with mocked AWS via `moto`/stubs) covering, at minimum: presign+publish
OTA (correct regional S3 presign, correct `cmd/<thing>/ota` topic and payload),
telemetry-processor (correct DynamoDB writes for telemetry and lifecycle),
device-deregistrar (returns valid JSON and performs the documented deletions),
and the migration status-writing steps (each status transition writes the
expected `migrationStatus`).

6.2. WHEN the deregistrar is tested THEN there SHALL be a regression test
asserting the handler returns a well-formed JSON response (guarding against the
placeholder-stub bug that returned invalid JSON to the GUI).

6.3. WHEN the Step Functions definition is tested THEN the system SHALL validate
the state machine definition (structure/ASL validity) and SHALL test the
happy-path and a caught-failure path (e.g. via mocked service integrations),
asserting the correct `migrationStatus` writes occur.

6.4. WHEN the Builder-Pi CLI is tested THEN the system SHALL provide unit tests
for the CLI's argument parsing and its operation implementations (with AWS/serial
interactions mocked), including non-zero exit on failure.

6.5. WHEN tests are run THEN they SHALL be executable locally with mocked AWS
services (no live AWS calls required) and SHALL be documented with a single
command to run them.

6.6. WHERE full integration tests against real CodeBuild/IoT are valuable but
slow/fragile THEN they SHALL be kept separate from the fast unit-test suite and
clearly marked as optional/integration, so the default test run stays fast and
deterministic.

---

## Out of scope

- **Firmware de-duplication (deferred to a separate later effort).**
  Consolidating the multiple firmware copies (top-level sketch, bundle copy, S3
  build source) and de-duplicating firmware-embedded values (wolfSSL
  `MAX_CERTIFICATE_SZ`, OTA buffer sizes, embedded credentials) is intentionally
  excluded from this spec. The firmware currently works end-to-end, and touching
  it while restructuring the AWS deployment would risk destabilizing a working
  component. It will be its own spec once the deployment/resources work is done.
- Reconciling the current drifted live stacks via CloudFormation resource
  `IMPORT` (the workshop is destroyed/recreated per delivery, so live
  reconciliation is unnecessary; templates target fresh deploys).
- Changing the PQC/TLS cryptographic approach itself (Kyber/ML-KEM, wolfSSL
  choices) — this spec is about deployment reliability, not the crypto.
- Rewriting the dashboard UI beyond what is needed to display `migrationStatus`.
