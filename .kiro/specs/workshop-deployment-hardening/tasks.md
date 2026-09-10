# Implementation Plan — Workshop Deployment Hardening

Tasks are sequenced so the demo stays working throughout: tests-first where
practical, new resources added before old ones are retired, and destructive
cleanup last and confirmation-gated. Each task references the requirements it
satisfies. Per constraint **C1**, no task deploys against the live demo without
explicit operator approval — tasks produce templates/code validated locally and
target a fresh-account deploy.

Branch: `refactor-deployment`.

---

## Phase 0 — Test harness foundation

- [ ] 0.1 Set up the Python test harness
  - Add `pytest` + `moto` + `boto3` stubber dev dependencies and a top-level
    `run-tests.sh` (or `make test`) that runs only the fast, mocked unit suite.
  - Add a `pytest.ini`/`pyproject` marking `integration` tests as deselected by
    default.
  - _Requirements: 6.5, 6.6_

---

## Phase 1 — Async PQC migration (Step Functions)

- [ ] 1.1 Write failing unit tests for the split migration Lambdas
  - `prepare-build`: registry lookup, cert issuance invoked, `start_build` env
    overrides correct, returns `buildNeeded`/`buildId`/`s3Key`.
  - `publish-ota`: regional presign params (`s3v4`, virtual addressing), publish
    to `cmd/<thing>/ota` with url/version/size/sha256, registry `ota-pending`.
  - `start-migration`: calls `states:StartExecution` with `{thingName}` and
    returns `{success:true}` synchronously.
  - _Requirements: 6.1_

- [ ] 1.2 Implement `prepare-build` and `publish-ota` Lambdas
  - Split `raw_device_builder/lambda/pqc-upgrader/index.py` into
    `raw_device_builder/lambda/prepare-build/index.py` and
    `raw_device_builder/lambda/publish-ota/index.py`.
  - Delete the `wait_for_build` polling loop (the wait moves to Step Functions).
  - Keep the regional-S3 presign fix and the WiFi-password env fallback.
  - Make 1.1 tests pass.
  - _Requirements: 1.2, 1.7, 6.1_

- [ ] 1.3 Implement `start-migration` Lambda
  - Calls `StartExecution` on the migration state machine, returns
    `{success, message, thingName}` immediately (well under 30s).
  - _Requirements: 1.1_

- [ ] 1.4 Author the Step Functions state machine definition (ASL)
  - States: SetStatusBuilding → PrepareBuild → Choice(buildNeeded) →
    WaitForBuild (`codebuild:startBuild.sync`) → SetStatusPushing → PublishOta →
    SetStatusRebooting → Succeed; Catch on all → SetStatusFailed → Fail.
  - `SetStatus*` states are native DynamoDB `UpdateItem` on `pqc-workshop-devices`
    (key `thingName`) writing `migrationStatus` (+ `migrationError` on failure).
  - _Requirements: 1.2, 1.3, 1.4, 1.7_

- [ ] 1.5 Write state-machine tests (definition + paths)
  - Validate ASL structure; assert happy path writes building→pushing-ota→
    rebooting and the catch path writes `failed`+`migrationError` (Step Functions
    Local or definition-level assertions).
  - _Requirements: 6.3_

- [ ] 1.6 Add migration resources to CloudFormation (device-builder stack)
  - `AWS::StepFunctions::StateMachine` (+ least-privilege role: codebuild
    start/batchget on the project, `dynamodb:UpdateItem` on `pqc-workshop-devices`,
    invoke the two task Lambdas), `PrepareBuildFunction`, `PublishOtaFunction`,
    `StartMigrationFunction` (+ roles), all with packaged code (no stubs).
  - Remove the previously-captured `PqcUpgraderFunction`/`PqcUpgraderAppSyncPermission`.
  - _Requirements: 1.2, 1.8, 1.9, 4.2_

- [ ] 1.7 Repoint AppSync `migrateFirmware` at StartMigration
  - Replace `PqcUpgraderDataSource` with a `StartMigration` data source; update
    the resolver. Remove the fixed-ARN pqc-upgrader reference and its invoke
    permission from the dashboard stack's AppSync role.
  - _Requirements: 1.1, 1.9_

- [ ] 1.8 Add `migrationStatus`/`migrationError` to schema + v2 dashboard
  - Extend the `Device` GraphQL type; add the two fields to `LIST_DEVICES`.
  - Render migration progress on the "Upgrade to PQC" button
    (building → pushing-ota → rebooting → done/failed), replacing the
    fire-and-forget error; keep `pqcAlgorithm` as the green/PQC signal.
  - _Requirements: 1.3, 1.5, 1.6_

---

## Phase 2 — Lambda packaging + duplicate consolidation

- [ ] 2.1 Choose authoritative Lambda source dirs and remove duplicates
  - Keep `dashboard/lambda/*` for dashboard functions and
    `raw_device_builder/lambda/*` for builder/OTA functions; delete the
    superseded top-level `lambda/*` duplicates (telemetry-processor,
    command-publisher, etc.) after confirming they are byte-for-byte or
    inferior copies.
  - _Requirements: 4.3_

- [ ] 2.2 Convert templates to packaged Lambda code (no stubs)
  - Replace inline `ZipFile`/placeholder stubs with `Code:`/`CodeUri:` local
    paths in device-builder and dashboard stacks (registrar, deregistrar,
    firmware-pusher, telemetry-processor, command-publisher, message-logger,
    broker-log-reader, migration Lambdas).
  - _Requirements: 4.2_

- [ ] 2.3 Add deregistrar + telemetry-processor + message-logger unit tests
  - Deregistrar: **returns well-formed JSON** (regression for the stub bug) and
    performs documented IoT/S3/DynamoDB deletes.
  - Telemetry-processor: telemetry vs lifecycle events write correct items + TTL.
  - Message-logger: thingName/topic/direction extraction, TTL, put_item.
  - _Requirements: 6.1, 6.2_

- [ ] 2.4 Bring orphaned backend resources into IaC
  - Ensure `message-logger` Lambda+role, `broker-log-reader` Lambda+role, and
    the `message-log` table are defined in the dashboard stack with packaged code
    (they exist live but unmanaged).
  - _Requirements: 4.1_

---

## Phase 3 — Builder-Pi CLI

- [ ] 3.1 Extract shared `builder_core` module
  - Move device operations out of `app.py`/`device_manager.py` into
    `builder-pi/builder_core/` (`operations.py`, `device_manager.py`, `aws.py`
    SigV4 helper, `logging.py` structured logger). Refactor Flask `app.py` to
    call `builder_core.operations` (no behavior duplication).
  - _Requirements: 2.6, 2.7_

- [ ] 3.2 Implement the `workshop-builder` CLI
  - `cli.py` argparse entry point with subcommands: `scan`, `register`,
    `reflash`, `flash --bin`, `deregister`, `serial`, `service {start|stop|status}`.
  - Structured timestamped logging to stdout + `/var/log/workshop-builder/`,
    non-zero exit on failure, fully non-interactive.
  - Add `pyproject`/`setup.py` `console_scripts` entry point.
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [ ] 3.3 Write CLI unit tests
  - argparse routes each subcommand to the right operation with parsed args
    (AWS/serial mocked); non-zero exit on failure.
  - _Requirements: 6.4_

- [ ] 3.4 Install CLI via Builder-Pi provisioning
  - Update `setup-builder-pi.sh` to `pip install .`/`pipx` the package and
    `bundle-for-builder-pi.sh` to ship `builder_core/` + `cli.py`, so a rebuilt
    Pi has `workshop-builder` on PATH automatically.
  - _Requirements: 2.5_

---

## Phase 4 — Broker observability

- [ ] 4.1 Make broker log streams self-identifying
  - Extend `entrypoint.sh` startup banner to log broker identity (task ARN / NLB
    target); confirm `awslogs-stream-prefix` yields per-task stream names.
  - _Requirements: 3.1, 3.3_

- [ ] 4.2 Establish the device-publish signal
  - Document/ensure the message-logger IoT rules (`pqc_workshop_message_log_dt`/
    `_cmd`) are the authoritative "what did the device publish" source (payloads
    to DynamoDB, surfaced by the dashboard log view), since mosquitto cannot log
    payloads. Optionally raise mosquitto publish-event verbosity.
  - _Requirements: 3.3, 3.4_

- [ ] 4.3 Confirm broker log group is single + in CFN
  - Verify `/pqc-workshop/broker-container` log group + retention remain defined
    in `broker-container-stack.yaml`; ensure the dead `/pqc-workshop/mosquitto-broker`
    group is not created by any template (removed with the Pi path in Phase 5).
  - _Requirements: 3.2, 3.4, 3.5_

---

## Phase 5 — Cleanup and de-duplication (destructive; confirmation-gated)

- [ ] 5.1 Bring v2 dashboard hosting into IaC + build/deploy automation
  - Define v2 hosting (S3 + CloudFront + OAC + bucket policy) in CFN; inject
    `config.js` values (AppSync endpoint, identity pool, REST API) from stack
    outputs at deploy time; add a build+sync+invalidate step
    (`npm ci && npm run build` → `s3 sync dist/` → CloudFront invalidation) to
    the deploy tooling. Do this BEFORE removing v1 so hosting is never absent.
  - _Requirements: 5.2, 4.1_

- [ ] 5.2 Remove the redundant v1 frontend hosting
  - Delete `DashboardBucket`/`DashboardDistribution` (+ related outputs) from
    `dashboard-stack.yaml` and delete the `dashboard/frontend/` source. Keep the
    shared backend intact.
  - _Requirements: 5.1, 5.6, 5.8_

- [ ] 5.3 Remove Greengrass, preserving the SSM-activation naming
  - Rename the `GreengrassThingName` parameter to a neutral
    `WorkshopInstanceName` (default `pqc-workshop`) across the parent, SSM
    activation, and broker (env `GREENGRASS_THING` → `BROKER_NAME`, bridge client
    id `${BROKER_NAME}-bridge`).
  - Remove `greengrass-core-stack.yaml`, the `GreengrassStack` nested stack + its
    outputs, `GreengrassBridgePolicy` (+ output), the greengrass cert-provisioner
    Lambda, and the `greengrass/` scripts. Verify no telemetry/command path
    breaks.
  - _Requirements: 5.3, 5.4, 5.6_

- [ ] 5.4 Refactor `check_setup_status.sh`
  - Validate the current architecture (v2 hosting, shared AppSync + Cognito, the
    three device tables, actual Lambda names incl. migration Lambdas, the
    broker-container/NLB/Private CA stack, real IoT rule names, the state
    machine). Remove all Greengrass/stale checks. Delete
    `greengrass/check_rpi_setup_status.sh`.
  - _Requirements: 5.5, 5.6_

- [ ] 5.5 Fix deploy tooling completeness + single deploy command
  - Update `deploy-workshop.sh`/`package.sh` to `aws cloudformation package` +
    `deploy` every referenced child template (close the ssm-activation gap),
    honor order build-image → broker-container → device-builder → main/dashboard
    → v2 build/sync, and export/import the Private CA ARN explicitly. Document the
    single deploy sequence + prerequisites.
  - _Requirements: 4.5, 4.6, 4.7, 4.4_

- [ ] 5.6 Orphaned live-resource removal (per-item confirmation)
  - Present the `orphan-inventory.md` removal list with impact statements; delete
    live orphans (v1 hosting resources, `pqc-workshop-pqc-upgrader`, dead broker
    log group) ONLY on explicit per-item operator approval. Primary deliverable
    is that a fresh deploy does not recreate them.
  - _Requirements: 5.7, 5.8, C1, C3_

---

## Phase 6 — Validation

- [ ] 6.1 Template validation + change-set dry run
  - `validate-template` on all templates; `create-change-set` (no execute)
    against a scratch/fresh context to confirm clean creation and correct deploy
    order. Do not execute against the live demo without approval.
  - _Requirements: 4.7, C1_

- [ ] 6.2 Full unit-test pass
  - Run `run-tests.sh`; all Lambda, state-machine, and CLI unit tests green with
    mocks only (no live AWS).
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [ ] 6.3 Fresh-account deploy rehearsal (optional, operator-approved)
  - If a scratch account is available, run the documented deploy end-to-end and
    `check_setup_status.sh` to prove from-scratch reproducibility.
  - _Requirements: 4.8, C2_

---

## Phase 7 — Single master template for Workshop Studio

- [ ] 7.1 Nest broker-container + device-builder under the master
  - Add `BrokerStack` and `DeviceBuilderStack` as `AWS::CloudFormation::Stack`
    children in `pqc-workshop-main.yaml`, sourced from the templates bucket.
  - _Requirements: 7.1_

- [ ] 7.2 Wire native cross-stack references + ordering
  - `DeviceBuilderStack` gets `PrivateCAArn` and `DefaultBrokerEndpoint` from
    `!GetAtt BrokerStack.Outputs.*` with `DependsOn: BrokerStack`; master derives
    and passes `IoTEndpoint` to both. Remove the script-passed CA ARN.
  - _Requirements: 7.2_

- [ ] 7.3 Add master parameters for broker networking
  - Add `VpcId` + `SubnetIds` master parameters, pass to `BrokerStack`; document
    them as required Workshop Studio inputs.
  - _Requirements: 7.3_

- [ ] 7.4 Update deploy tooling to package all 5 children + deploy the master
  - `deploy-workshop.sh`/`package.sh`: `cloudformation package` each of the five
    child templates, upload to `templates/`, then deploy the single master (CFN
    owns ordering via DependsOn). Keep v2 build/sync as the documented post step.
  - _Requirements: 7.1, 7.4, 4.6_

- [ ] 7.5 Document the remaining manual steps
  - README/deploy docs: one-time firmware image build, v2 dashboard build+sync,
    Builder-Pi SSM registration; plus the broker first-deploy image note.
  - _Requirements: 7.4_

- [ ] 7.6 Validate
  - cfn-lint 0 errors on the master + all children; confirm nested `!GetAtt`
    references resolve to real child outputs; confirm each child still deploys
    standalone (params retain defaults). Re-run unit suite.
  - _Requirements: 7.5, 7.6_

---

## Phase 8 — Builder-Pi observability

- [x] 8.1 Persist the full Pi flash/download/serial transcript to disk
  - `device_manager.py`: `DeviceSlot.log()` appends every line (UTC-stamped,
    device-keyed) to `/var/log/workshop-builder/device-<serial|port>.log` + a
    combined `flash.log`, in addition to the in-memory ring buffer. Best-effort
    (never fails a flash). Captures the GUI path, not just the CLI.
  - _Requirements: 8.1_

- [x] 8.2 Keep the full esptool output on flash
  - `flash_firmware` writes complete esptool stdout/stderr to the device log
    file (GUI slot keeps its concise tail for the browser), so a failed flash is
    diagnosable beyond the old 300-char tail.
  - _Requirements: 8.2_

- [x] 8.3 Install + configure the CloudWatch agent on the Pi
  - `setup-builder-pi.sh`: install arch-matched `amazon-cloudwatch-agent`, write
    a config tailing `/var/log/workshop-builder/*.log` to `/pqc-workshop/builder-pi`
    (per-host stream, 14-day retention), start in `onPremise` mode (uses the SSM
    instance role). Region resolved from config.json → SSM registration →
    us-east-1. Idempotent + graceful `[WARN]` on download/install failure.
  - _Requirements: 8.3, 8.5_

- [x] 8.4 Scope the Builder-Pi log IAM to least privilege
  - `BuilderPiSsmRole` in `device-builder-stack.yaml`: `logs:` statement scoped
    from `*` to the `/pqc-workshop/builder-pi` log-group ARN
    (Create/Put/DescribeLogStreams/PutRetentionPolicy).
  - _Requirements: 8.4_

- [ ] 8.5 Validate on hardware (first flash)
  - On the next physical flash, confirm `/var/log/workshop-builder/*` populates,
    the `/pqc-workshop/builder-pi` log group appears in CloudWatch with the
    flash/serial transcript, and a forced failure surfaces the full esptool
    output remotely.
  - _Requirements: 8.1, 8.2, 8.3, 8.6_

---

## Notes

- **Sequencing safety:** new migration resources (Phase 1) and v2 hosting (5.1)
  are added before their old counterparts are retired (1.9, 5.2), so the demo is
  never missing a component mid-refactor.
- **Firmware is out of scope** (deferred). No task edits firmware sources or
  wolfSSL settings; the CodeBuild image and its baked settings are untouched
  except where a non-firmware config parameter is threaded through.
- **Live safety:** all deploy/delete steps are explicit and operator-gated;
  implementation output is templates + code + tests validated locally.

---
## Future / Backlog (out of current scope)
- [ ] Move the v2 dashboard build into AWS (CI/CD pipeline).
  - Today `deploy-workshop.sh` runs `npm ci && npm run build` locally and then
    syncs `build/` to S3, so the deploy host needs Node/npm and produces a
    machine-dependent artifact. Move this into a pipeline (e.g. CodePipeline +
    CodeBuild, or the existing CodeBuild pattern) so the dashboard is built in
    the account and published to S3/CloudFront without a local toolchain.
  - Acceptance: `deploy-workshop.sh` no longer needs local Node/npm; the
    dashboard build + S3 sync + CloudFront invalidation run in AWS on deploy.
  - Deferred by operator decision to keep the current local run moving.
- [ ] ESP32 watchdog timer for stall/lockup detection + recovery (FIRMWARE).
  - Add a hardware/task watchdog to the ESP32 firmware so a locked or stalled
    process on the microcontroller is detected and the device recovers (e.g.
    Task Watchdog Timer feeding from the WiFi/MQTT/main tasks, with a controlled
    reset on timeout). Covers both firmware variants as appropriate.
  - Acceptance: a hung task triggers a watchdog reset and the device reconnects
    (WiFi + MQTT) without manual power-cycling; timeout windows are tunable.
  - NOTE: firmware-domain work — out of scope for the deployment-hardening
    effort; tracked here only so it is not lost. Should be done under a firmware
    effort so as not to destabilise the firmware while AWS deploy work is ongoing.
- [ ] Standardize environment config: SSM Parameter Store + runtime-fetched
      config (stop committing per-account values).
  - Problem: deployment-specific, non-secret values (dashboard `apiEndpoint`,
    `appSyncEndpoint`, `identityPoolId`, `region`; builder-pi `api_endpoint`,
    `firmware_bucket`) currently get baked into tracked source. The dashboard's
    `src/config.js` is regenerated per deploy by `deploy-workshop.sh` and the
    built `build/` output is tracked, so every account produces a git diff with
    that account's endpoints — the same class of leak we fixed for
    `builder-pi-bundle/`.
  - Decision (chosen service): **SSM Parameter Store** (Standard tier, `String`)
    as the canonical store. These values are non-secret, deployment-derived, and
    few — Parameter Store is free, native to CloudFormation, and purpose-built
    for this. Rejected alternatives: Secrets Manager (for rotating secrets; costs
    per secret/API call; these aren't secrets), AppConfig (feature-flag/config
    rollout with validators — overkill), DynamoDB (operational overhead for a
    handful of static strings), native env vars (only help server/Lambda
    compute; the dashboard is a static browser SPA so it can't read them).
  - Design (standard going forward):
    1. Master stack publishes its relevant outputs to a namespaced parameter
       set, e.g. `/pqc-workshop/dashboard/{apiEndpoint,appSyncEndpoint,identityPoolId,region}`
       and `/pqc-workshop/builder-pi/{apiEndpoint,firmwareBucket,region}`.
    2. The dashboard SPA stops compiling `config.js` into the bundle; instead it
       fetches a small `config.json` at runtime (written to the S3 site bucket by
       the deploy from the parameters/stack outputs). One environment-agnostic
       build artifact; only the tiny `config.json` differs per account and it
       lives in S3, never in git.
    3. The builder-pi bundle/setup reads the same parameter namespace (its SSM
       role already has access) rather than the generator hardcoding names.
  - Repo hygiene (part of this task): gitignore + untrack
    `raw_device_builder/dashboard-v2/frontend/build/` and treat the generated
    `src/config.js` as generated (gitignore or template it), mirroring the
    `builder-pi-bundle/` fix.
  - Acceptance: a clean deploy in a new account produces a working dashboard +
    builder-pi with zero account-specific values committed to git; the frontend
    build is byte-identical across accounts; endpoints resolve at
    deploy/runtime from Parameter Store (or stack outputs) only.
- [ ] Decouple deployment into independently deployable component stacks.
  - Problem: `deploy-workshop.sh` runs every component in one sequential pass —
    `[2/5]` master stack, `[3/5]` broker container image CodeBuild (slow),
    `[4/5]` dashboard build+sync. So a dashboard-only change still forces the
    broker image build and a full master update; there is no way to deploy a
    single discrete component. (The CFN is already nested into child stacks —
    Broker, DeviceBuilder, Dashboard, Device, SSMActivation, Network — but the
    orchestration script couples them.)
  - Goal: deploy each discrete component on its own, with dependencies managed.
    Two parts:
    1. Make the components independently deployable (e.g. standalone child-stack
       deploys, or separate root stacks with cross-stack references / SSM
       parameters for the shared values like BrokerEndpoint, PrivateCAArn,
       ApiEndpoint, AppSync/Cognito). Preserve correct ordering: Broker →
       DeviceBuilder (needs PrivateCAArn + BrokerEndpoint); Dashboard + Device
       independent; SSMActivation independent.
    2. Give `deploy-workshop.sh` a component selector (e.g. `--only dashboard`,
       `--only broker`, `--only device-builder`, default = all) so the broker
       image build and other slow steps are skipped when not targeted. The
       dashboard build+sync must run without triggering the broker CodeBuild.
  - Acceptance: `deploy-workshop.sh --only dashboard` redeploys just the
    dashboard (build + config.json + S3 sync + CloudFront invalidation) in
    well under a minute, with no broker image build; a full deploy still works
    end-to-end and respects inter-component dependencies; each component can be
    deployed/updated in isolation.
- [ ] Eliminate the developer-laptop / local-npm dependency for cloud deploys
      (Workshop Studio friendly).
  - Problem: `deploy-workshop.sh` step 4 runs `npm ci && npm run build` locally,
    then `aws s3 sync build/ …`, so deploying the dashboard REQUIRES Node/npm on
    the machine running the script. In a Workshop Studio (WS) account we do not
    want any dependency on a developer laptop or local toolchain — the workshop
    should stand itself up in-account.
  - Primary investigation: use a pipeline + the WS assets bucket to build in the
    cloud. WS provisions each account with a static assets S3 bucket
    (referenced in content via `{{.AssetsBucketName}}` / `{{.AssetsBucketPrefix}}`)
    that can hold the frontend source. Investigate: stage the dashboard source
    into the assets bucket (or the repo/CFN artifacts bucket), then have a
    CodeBuild project (invoked by the master stack as a Custom Resource, or a
    small CodePipeline) run `npm ci && npm run build`, write config.json, and
    sync to the dashboard S3 bucket + invalidate CloudFront — entirely in the
    account. Confirm whether WS lets us trigger a build during stack
    creation and whether the assets bucket is readable by CodeBuild. This also
    subsumes the earlier "move the v2 dashboard build into AWS" backlog item.
  - Fallback (only if in-cloud build during WS provisioning is not feasible):
    deploy a developer IDE INTO the WS account (e.g. a Cloud9 / Code Server /
    small EC2 or container dev environment with Node preinstalled) as part of
    the workshop stack, so the "local" build runs on in-account compute rather
    than a participant's laptop. Document which approach WS supports.
  - Acceptance: a clean deploy in a fresh WS account produces a working
    dashboard with NO Node/npm required on any external/developer machine — the
    build runs on AWS compute (pipeline/CodeBuild) or on in-account dev compute
    provisioned by the workshop. `deploy-workshop.sh` (or its successor) needs
    only AWS CLI + credentials locally, not a JS toolchain.
- [ ] Install the AWS CLI on the Builder-Pi during setup.
  - Problem: a freshly-imaged Builder-Pi has no `aws` CLI on PATH (only boto3,
    pulled in by the app's Python deps). Operator/agent S3 pulls and other
    AWS-CLI-based recovery steps over SSM fail with `aws: not found` and have to
    fall back to ad-hoc `python3 -c "import boto3 …"` one-liners.
  - Task: install the AWS CLI v2 (arch-matched: aarch64 for arm64 Pis) as part
    of the Pi provisioning. Best placed in the SSM/bootstrap setup so it is
    present before any tool-install or fixup step. Idempotent (skip if already
    present); graceful if the download fails (the Pi still works via boto3).
  - Acceptance: after a clean Builder-Pi setup, `aws --version` works over SSM
    and S3 pulls / diagnostics can use the AWS CLI directly, no boto3 fallback.
