# Project Steering

## OVERRIDING DESIGN PARADIGM (highest priority — governs every decision)

The entire stack, plus a small number of clearly-documented manual steps, MUST be
deployable in ONE attempt, with ALL features working: ESP32 device registration,
firmware builds, and the Mosquitto broker, along with every supporting AWS
resource. This project is intended for **AWS Workshop Studio**, so excessive or
fragile manual steps are unacceptable — minimise them and document the few that
remain.

The ONLY test that truly matters is the **end-to-end test**: a from-scratch
deployment followed by setting up the first ESP32 device successfully. A command
exiting cleanly is NOT success — success is the full path working end to end.

There MUST be no need to modify workshop code or the environment the day before
(or during) the re:Invent workshop. Any fix that only works via in-place patching
of a running environment is NOT done; it must survive a clean deploy.

**Definition of done (final acceptance):** a destroy + from-scratch rebuild in
`pqc-b`, then a destroy + from-scratch rebuild in `pqc-a`, with BOTH environments
fully working after their clean deployment. Until both pass a from-scratch
deploy, the work is not complete. Note `pqc-b` has VPC Block Public Access
(block-ingress) enabled and `pqc-a` does not — the deployment must work in both,
so it cannot rely on public-IP + IGW egress for the broker task.

Practical implications for any change:
- Prefer fixes in the deployable source (CloudFormation, Dockerfile, deploy
  scripts) over manual console/CLI actions on a live environment.
- If a manual step is unavoidable, it must be minimal, documented, and part of
  the deploy runbook.
- Validate changes by reasoning about (and ultimately performing) a clean
  destroy + redeploy, not just an in-place update.

## VPC Block Public Access (BPA) — CRITICAL for device connectivity

`pqc-b` has account-level VPC Block Public Access = `block-ingress` (set by
"Customer Action"). This breaks the workshop in TWO independent ways, both of
which must be handled or devices cannot connect:

1. **Broker -> IoT Core bridge (OUTBOUND).** A public-IP Fargate task egressing
   via IGW has its return traffic dropped by block-ingress. FIXED in the
   templates by running the broker task in private subnets behind a NAT gateway
   (network-stack.yaml). NAT egress is not blocked by block-ingress.
2. **Device -> Broker NLB (INBOUND).** The ESP32 connects into the
   internet-facing NLB from the public internet = IGW *ingress*, which
   block-ingress DROPS. Symptom: device serial shows WiFi connected + IP
   assigned but "TCP connection failed to <nlb>:8883" (timeout), and even a
   public openssl s_client to the NLB:8883 times out (errno 60). The NLB shows
   healthy targets + correct SG (0.0.0.0/0 on 8883) + public subnets — all fine;
   BPA is what blocks it. This CANNOT be fixed with NAT (NAT is egress-only).

**Resolution — now PERMANENT in the deploy (create-VPC path):** network-stack.yaml
creates an `AWS::EC2::VPCBlockPublicAccessExclusion` (VpcBpaExclusion,
InternetGatewayExclusionMode: allow-bidirectional) on the workshop VPC,
unconditionally. AWS allows creating exclusions even when BPA is off, so it is a
harmless no-op on non-BPA accounts (e.g. pqc-a) and protects BPA accounts (e.g.
pqc-b) — one code path, no manual step, works whether or not the account has BPA
and even if BPA is enabled later. So a clean rebuild NO LONGER needs the manual
`create-vpc-block-public-access-exclusion` CLI step.
(Historical: pqc-b had a manual exclusion vpcbpa-exclude-08f67396a2b74befc created
by hand during debugging; it is tied to the VPC and is removed on teardown — the
CFN-managed one replaces it on rebuild.)

**BYOVPC caveat:** the exclusion lives in the network stack, which only runs on
the create-VPC path (CreateVpc=true, the Workshop Studio default). If someone
brings their own VPC (CreateVpc=false) into a BPA account, they must add the
exclusion themselves. Acceptable since Workshop Studio uses create-VPC.

## Clean teardown / rebuild notes (avoid re-discovering these)

Deleting the master stack `pqc-workshop` cascades to all nested stacks, but a
few resources are NOT cleaned up by CloudFormation and must be handled around
the teardown/rebuild. Handle these to keep a from-scratch deploy clean:

- **Versioned S3 buckets block stack deletion.** Empty ALL object versions +
  delete-markers first for: `pqc-workshop-broker-source-<acct>`,
  `pqc-workshop-firmware-<acct>`, `pqc-workshop-firmware-dashboard-<acct>`.
- **ECR images block repo deletion.** Batch-delete images in
  `pqc-workshop-mosquitto-broker` (and `pqc-workshop-firmware-builder`) before
  deleting the stack.
- **IoT Thing Type `esp32-pqc-device` deletion** needs no associated Things and
  a ~5-min deprecation wait. Detach principals + delete any `esp32-*` device
  Things first. (Leave `pqc-workshop-broker-bridge` to the broker stack's
  BridgeProvisioner custom resource.)
- **Stale SSM managed instances (builder-Pi).** Each `bundle-for-builder-pi.sh`
  run mints a NEW SSM managed-instance id; the physical Pi re-registers under a
  new `mi-...` id and the old ones linger as `ConnectionLost`. After registering
  the Pi, deregister stale entries:
  `aws ssm describe-instance-information` -> for each `ConnectionLost`
  `pqc-workshop-builder-pi`, `aws ssm deregister-managed-instance --instance-id mi-...`.
  Keep only the `Online` one. (Backlog: have the bundler/setup reuse a stable
  registration or auto-prune stale ones.)
- **IAM roles created by the bundler survive teardown.** `bundle-for-builder-pi.sh`
  creates `pqc-workshop-builder-pi-ssm` outside CloudFormation; it persists and
  is reused across rebuilds (fine, just don't expect the stack to own it).

Backlog candidates surfaced during rebuilds (not yet fixed):
- **[BUG — real, reproducible] PQC migration race: publish-ota checks S3 before
  the build artifact lands.** Observed on pqc-a device esp32-FC22E0 (Sep 16): the
  "Migrate to PQC" migration FAILED with
  `RuntimeError: PQC firmware not found in S3: FC22E0/1.0.0-pqc/firmware.bin`
  — even though the PQC build SUCCEEDED and that exact object IS in S3. Root
  cause is a timing race: the state machine's WaitForBuild uses the CodeBuild
  `.sync` integration, which returns when the build's BUILD phase completes, but
  the firmware artifacts are uploaded to S3 during POST_BUILD. publish-ota ran at
  12:49:50 and did a plain head/get for firmware.bin; the object wasn't written
  until 12:49:53 — a ~3s gap — so it raised RuntimeError and the migration went
  to SetStatusFailed. NOT destructive: the firmware is built and in S3, so simply
  re-running the migration succeeds (publish-ota finds it the second time, no
  rebuild). Fix options: (a) add a Step Functions Retry on the RuntimeError from
  the PublishOta task (short backoff, few attempts) — cheapest; (b) make the
  publish-ota Lambda poll/head-object with backoff for firmware.bin before giving
  up (index.py ~line 62); (c) have the build write a final sentinel/marker object
  LAST and gate on that. Recommend (a)+(b). This is exactly the "assumed
  synchronous but actually eventually-consistent" class of bug seen elsewhere in
  this project; intermittent — will bite workshop attendees randomly.
- **[GUI bug] Builder-Pi dashboard "Migrate to PQC" button appears to revert /
  not trigger a fresh migration.** Observed pqc-a Sep 16: pressing the migrate
  button on the device dashboard did NOT start a new pqc-migration Step Functions
  execution (it seems to do a revert or a no-op refresh instead). Had to trigger
  the migration by invoking the `pqc-workshop-start-migration` Lambda directly
  (payload `{"thingName":"esp32-<serial>"}`), which is exactly what the AppSync
  `migrateFirmware` mutation should call — and that worked (execution SUCCEEDED,
  OTA published). So the backend path is fine; the GUI wiring for the migrate
  button is wrong. Fix: check the dashboard-v2 frontend's migrate action —
  confirm it calls the `migrateFirmware` GraphQL mutation (not delete/revert),
  and that the resolver → StartMigration path is hooked up. Relates to the
  earlier "dashboard LED button not working" item — the command/mutation wiring
  in the v2 frontend needs an audit pass.
- **[TOP — do next week alongside pqc-a rebuild] Remove the duplicate WiFi
  password (and SSID) from the DynamoDB registry.** SSM
  (`/pqc-workshop/wifi/password`, a SecureString) is the single source of truth
  since Model B (commit 76fd497); `prepare-build` resolves WiFi as SSM -> registry
  -> env, so SSM wins and the registry copy is never used in normal operation.
  But the registrar still writes `wifiPassword` (PLAINTEXT) into
  `pqc-workshop-device-registry` (leftover from the earlier "persist password"
  fix, commit 20b3b77) — a redundant AND less-secure second copy of a secret.
  Fix: (1) drop `'wifiPassword': wifi_password` from the registrar's put_item
  (`raw_device_builder/lambda/device-registrar/index.py`); (2) drop the
  `device.get("wifiPassword")` middle tier in
  `raw_device_builder/lambda/prepare-build/index.py` so WiFi resolves SSM -> env
  (matches how broker already works — broker was never persisted to the registry).
  Optionally also drop `wifiSsid` from the registry for consistency (not a secret,
  but same "one place" logic) — keep the registry to identity/status/cert metadata
  only. Deferred from the Fri session (didn't want to touch the migration path
  right after the acceptance test); the pqc-a clean rebuild will re-verify the
  migration path for free, so do it then. Low risk: SSM is already the primary
  path. Also check for any other reader of registry wifiPassword before removing.
- **[Workshop Studio delivery] Decide how the repo gets from the WS assets bucket
  into the Cloud IDE so `deploy-workshop.sh` can run.** WS stages content in an
  assets bucket; the operator/attendee runs the deploy from the Cloud IDE, which
  is NOT on the Amazon internal network (so `git clone` from internal GitLab is
  out). Open question: ship the whole repo or just the deploy-relevant subset
  (`cloudformation/`, the Lambda sources, `raw_device_builder/`, firmware, the
  dashboard build inputs)? Agent opinion / recommendation:
  - Prefer **`git archive --format=zip HEAD -o repo.zip`** over a plain `zip -r`:
    it snapshots only tracked files, so `.git`, `.venv`, `node_modules`,
    `__pycache__`, `.pytest_cache`, and build artifacts are excluded for free —
    avoids a bloated, slow artifact. (A raw recursive zip of this tree would be
    huge because of the dashboard `node_modules` and the venv.)
  - Add a **sanitization pass** before zipping to strip internal-only `.kiro`
    config (ties directly to the Kiro/MCP blocker item below) — the artifact
    attendees unzip must not carry `builder-mcp` or the `/Users/swaym` PATH.
  - Then: upload the zip to the WS assets bucket and have the Cloud IDE
    download + unzip it (either a WS setup step or a documented one-liner). Verify
    how WS actually surfaces the assets bucket to the IDE — whether it auto-syncs
    or the attendee pulls it manually.
  - `git clone` is only viable if the repo is reachable from the workshop account
    (e.g. public GitHub); given the internal-network constraint, the zip path is
    the safe default.
  - Decide whole-repo vs curated-subset: a curated subset is leaner and avoids
    shipping test/dev cruft, but whole-repo (via git archive) is simpler and less
    error-prone to keep in sync. Lean: git archive of the whole tracked tree,
    minus sanitized `.kiro`, unless artifact size becomes a problem.
- **[Workshop Studio blocker] Strip internal-only Kiro/MCP config from the repo
  so the Workshop Studio Cloud IDE agent works for non-Amazon attendees.** The
  WS deployment is expected to serve the repo from the assets bucket and run the
  deploy from the Cloud IDE with Kiro support. But the Kiro agent in a *workshop
  account* is NOT on the Amazon internal network, so anything the repo's
  `.kiro/settings/mcp.json` points at that requires internal access will fail for
  attendees. Concretely: the committed `builder-mcp` server (and its
  `env.PATH: /Users/swaym/.toolbox/bin` — an internal toolbox path that only
  exists on an Amazon employee laptop) is unreachable/meaningless in a workshop
  account, and any internal-only MCP tools would error on connect. Fix: audit
  everything under `.kiro/` that ships in the repo (MCP servers, autoApprove
  lists, steering that assumes internal tools) and remove or gate anything an
  external attendee can't use — in particular drop the `builder-mcp` entry (and
  its hardcoded `/Users/swaym` PATH) from the *repo* copy, keeping it only in the
  operator's personal `~/.kiro` config. Keep only MCP servers that work from a
  vanilla workshop account (e.g. the public `drawio` server via `npx`). Net: a
  fresh clone in a Workshop Studio Cloud IDE should give attendees a working Kiro
  agent with no internal dependencies and no employee-specific paths.
- **[Workshop Studio prerequisite] Slim the front-of-repo README.md down to a
  clean entry point.** The root `README.md` has accumulated a lot of redundant
  content and does NOT point the operator to `cloudformation/DEPLOYMENT.md` as the
  primary source of deployment truth. This matters for building the Workshop
  Studio artifacts — the front README is the first thing an attendee/operator
  sees. Target end state:
  - Keep the front `README.md` **simple**: the architecture overview/diagrams
    (`architecture.drawio` / `.png`), a clear pointer to
    `cloudformation/DEPLOYMENT.md` as the authoritative deploy guide, and a
    reference to `attendee_experience.md` for the workshop attendee flow.
  - Move other still-relevant docs into a `docs/` subfolder (rather than deleting
    outright), so the repo root stays uncluttered but nothing useful is lost.
  - Remove genuinely redundant/stale content (e.g. the old
    `aws cloudformation delete-stack --stack-name esp32-pqc-iot` cleanup snippet
    and any instructions that predate the current `pqc-workshop` master-stack +
    `deploy-workshop.sh` flow) — cross-check against DEPLOYMENT.md so the README
    never contradicts it.
  - Net: root README = architecture + links (DEPLOYMENT.md, attendee_experience.md,
    docs/); DEPLOYMENT.md remains the single deployment source of truth. Do this
    before packaging WS artifacts.
- **[Repeat-deploy hardening] Remove the remaining hardcoded resource names that
  block re-deploys into a shared account.** Root cause of the entire pqc-a
  rebuild saga: nearly every resource in the templates uses a fixed name, and
  CloudFormation's Nov-2025 early validation (AWS::EarlyValidation::
  ResourceExistenceCheck) rejects a create when a same-named resource already
  exists. In Workshop Studio this is a NON-issue — the account is created and
  torn down per event, so names never collide. It only bites when running
  MULTIPLE deploys in one account (our testing loop), where a leftover resource
  from a prior/failed deploy blocks the next deploy. ALREADY FIXED (dashboard
  template): the FirmwareDashboardBucket (auto-named; consumers read the
  DashboardBucketName output) and all 9 dashboard IAM roles (auto-named; only
  referenced by !GetAtt .Arn). STILL HARDCODED (deferred — each has EXTERNAL
  literal consumers, so auto-naming requires a coordinated multi-file change,
  not a one-line template edit):
  - DynamoDB `pqc-workshop-devices` (DeviceTable) — consumed by literal name in
    the device-builder Step Functions state machine (DefinitionSubstitutions +
    an IAM policy ARN), the auto-register Lambda code, tests, and
    check_setup_status.sh. Auto-naming needs the generated name wired as an
    output→parameter into the device-builder stack + code/test updates.
  - DynamoDB `pqc-workshop-message-log` (MessageLogTable) — consumed by tests +
    check_setup_status.sh.
  - The 4 dashboard Lambda function names (telemetry-processor, command-publisher,
    message-logger, broker-log-reader) — consumed by literal in
    check_setup_status.sh.
  - The 5 IoT TopicRule names (pqc_workshop_telemetry/lifecycle/message_log_dt/
    message_log_cmd), AppSync API name pqc-workshop-device-api, Cognito pool
    pqc_workshop_dashboard — all looked up by exact name in check_setup_status.sh.
  Also fold in: rewrite `check_setup_status.sh` to discover resources by tag or
  stack output instead of by hardcoded name (removes the biggest external
  consumer and makes the whole set safe to auto-name). NOTE (observed on the
  pqc-a rebuild): the auto-naming fix ALREADY broke two checks in
  check_setup_status.sh — its section 9 looks for the hardcoded bucket
  `pqc-workshop-firmware-dashboard-<acct>` (now auto-named) and a stale CloudFront
  distribution, so it emits a false "missing index.html" warning even though the
  dashboard is live (verified HTTP 200 at the real DashboardURL output). Fix
  section 9 to read DashboardBucketName + DashboardURL from the DashboardStack
  outputs. This is a real (if cosmetic) false-negative today, so bump slightly. Same hardcoded-name
  pattern exists in the other stacks (broker-container, device-builder) — audit
  those too. Priority: only matters for the shared-account testing workflow, so
  medium — but it's the durable fix for the class of failure that cost most of
  this rebuild.
- **[Consider — decision-gated] Build the v2 dashboard in a CodeBuild project
  instead of on the deploy host.** Today `deploy-workshop.sh` step 4 runs
  `vite build` LOCALLY (needs Node/npm on the deploy host), syncs dist/ to the
  dashboard bucket, and invalidates CloudFront. Proposal: move that into a
  CodeBuild project inside the stack, mirroring how the broker + firmware-builder
  container images build (stage source to S3 -> CodeBuild -> publish).
  Benefits: (1) BIGGEST — removes the Node/npm dependency from the deploy host,
  which matters for the Workshop Studio Cloud IDE where the toolchain is unknown
  and there's no internal network (ties into the two WS backlog items above);
  (2) consistency — all build+publish steps would follow one CodeBuild model;
  (3) could fold into CFN via a trigger Lambda + custom resource (like the
  broker's BuildTrigger), moving toward the one-attempt deploy goal.
  Tradeoffs: (1) doesn't remove orchestration, just relocates it — CodeBuild
  still needs source staged to S3 first (same chicken-and-egg as the broker
  image, which is why that's a script step not pure CFN); (2) the frontend needs
  deploy-time config.json (AppSync endpoint, Cognito pool id, region) which only
  exist AFTER stack create, so the build must run post-outputs either way — pass
  them as CodeBuild env vars; (3) more infra to maintain (project + IAM role +
  buildspec + maybe trigger Lambda/custom resource) vs ~15 lines of shell today;
  (4) slower + harder to debug — the Vite build is ~241ms; CodeBuild adds
  container spin-up (~1-2 min) and moves failures to CloudWatch logs. For a
  minutes-long Docker image build the CodeBuild overhead pays off; for a
  sub-second Vite build it's arguably overkill.
  Recommendation: DECISION-GATE this on whether the WS Cloud IDE can reliably run
  the local Vite build. If yes, keep the simpler local build. If no/uncertain,
  CodeBuild is the robust answer. Lighter middle option: keep the build local but
  make deploy-workshop.sh degrade gracefully (skip + clear manual-fallback
  message when npm is absent). Do NOT implement without confirming the WS host
  toolchain first.
- **[Reliability] Builder-Pi bundle should ONLY bootstrap SSM; drive all build
  steps via SSM documents.** OBSERVED FAILURE (pqc-a rebuild, Sep 16): running
  `setup-all.sh` interactively under `sudo` on the Pi aborted mid-way with
  `Unable to get user home dir: $HOME is not defined` — after the ESP32 core
  install (early step) but BEFORE the app deploy — leaving `/opt/pqc-workshop/
  builder-app` empty, no systemd service, GUI down, and NO captured error (output
  went to the operator's terminal, gone). Because setup-builder-pi.sh uses
  `set -euo pipefail`, the failing step silently killed the rest. Re-running the
  SAME script via SSM Run Command SUCCEEDED (core was cached so it fast-forwarded,
  and SSM's root env has $HOME defined) — which is the whole point.
  Proposal: shrink the on-Pi bundle to ONE job — register the SSM agent
  (hybrid activation). Move everything else (arduino-cli + ESP32 core, wolfSSL
  PQC config, partition table, Python deps, workshop-builder CLI, GUI app deploy,
  systemd service, CloudWatch agent) into SSM documents run from the deploy host,
  ideally split into separate docs (install-toolchain / deploy-app /
  configure-service) so a failure re-runs in isolation without repeating the slow
  ~5GB core install.
  Benefits: (1) fixes THIS failure — SSM runs as root with a consistent env (no
  operator shell/$HOME/sudo variance), the exact variable that broke it;
  (2) full remote observability — every step's stdout/stderr/exit code captured
  by SSM (+ CloudWatch), no lost terminal output (this is literally why we had to
  re-run to see the error); (3) idempotent per-step recovery without repeating the
  10-12min core install; (4) smaller bundle = less operator-side surface to break;
  (5) consistent with the workshop's remote-execution philosophy (CFN/CodeBuild/
  SSM everywhere else).
  Tradeoffs: (1) inherently two-phase — SSM must be Online before the docs can
  fire, so orchestration waits on describe-instance-information; (2) needs a
  control-side orchestrator (a deploy-host script sequencing send-commands, or a
  Step Function) vs today's self-contained script; (3) SSM Run Command timeout +
  output-size caps matter for the 5GB/10-12min core install — chunk steps, log to
  file + tail, set generous --timeout-seconds (we already hit output truncation);
  (4) requires SSM control plane throughout the build, not just registration
  (reduces air-gapped flexibility — fine for a cloud-connected workshop);
  (5) migration effort — notably, setup-builder-pi.sh steps that COPY files "next
  to this script" (app.py, builder_core, config.json, wolfssl_user_settings_
  arduino.h, partition csv) must instead FETCH those from S3 (firmware/assets
  bucket) in the SSM-doc model. That S3-sourced-files change is the non-obvious
  bit that makes the split clean.
  Recommendation: bundle = SSM bootstrap only; build steps = SSM docs that pull
  their file deps from S3. LIGHTER INTERIM WIN (do first, cheap): always run
  `setup-all.sh` VIA SSM (never interactively), make it log to a file + not abort
  silently (surface which step failed), so we get the reliability + observability
  win without the full re-architecture.
- **Fold the NetworkManager AP into `setup-all.sh` (medium priority).** The Pi's
  hostapd/NM AP is currently a MANUAL setup (DEPLOYMENT.md §5a). During the Fri
  device test the classical device failed WiFi with `4WAY_HANDSHAKE_TIMEOUT`
  because the operator-typed AP PSK didn't match `@reinvent2026`; fixed by hand
  with `nmcli ... wifi-sec.psk`. Scripting the `nmcli` AP creation in
  `setup-all.sh` — pulling SSID/PSK from SSM (`/pqc-workshop/wifi/{ssid,password}`,
  the same source firmware uses) — eliminates this class of typo entirely and
  makes the AP consistent with the firmware by construction. The Pi SSM role
  already has `ssm:GetParameter` on `/pqc-workshop/*` + `kms:Decrypt`.
- **Dashboard LED button not working (low priority, later fix).** On the device
  dashboard, the LED (command) button does not actuate the device. Likely the
  cmd/<thing>/+ command path (dashboard -> AppSync/API -> IoT -> broker bridge
  inbound topic -> device) or the firmware's command handler. Device DOES
  subscribe to cmd/esp32-<serial>/+ (seen in broker logs), so the downlink wiring
  is the place to check. Defer — not blocking device connect/telemetry/migration.
- **Registrar partial-failure cleanup (near-term, low effort).** The
  `device-registrar` Lambda does cert -> create thing -> create policy ->
  DynamoDB -> start CodeBuild sequentially; if a later step fails the earlier
  resources (IoT Thing, cert, per-device policy) are left orphaned (observed:
  `esp32-C69E18` Thing created even though the firmware build later failed on the
  missing builder image). Add best-effort rollback on failure so a failed
  registration doesn't accumulate orphans. Small change to the existing Lambda,
  no Step Function required.
- **Initial register+flash as a Step Function (optional, deferred — decide later).**
  The classical migration already runs on the `pqc-workshop-pqc-migration` Step
  Function; the INITIAL register+build+flash does NOT (it's a synchronous
  registrar Lambda + the Pi polling CodeBuild). Migrating the cloud half
  (cert/thing/policy/ddb/start-build) to a Step Function would give consistent
  pipeline-style observability + per-step retries + rollback, symmetric with the
  migration flow. Caveats: the flash/USB/serial half MUST stay on the Pi
  (hardware), so end-to-end visibility is still split; adds deploy surface; the
  registrar isn't where current failures are. Interesting idea worth exploring:
  make the Pi a WORKER that polls the SFN (task token / SFN activity) for "certs
  + firmware ready, your turn to flash" and reports flash status back — this could
  actually improve the Pi-side reliability/structure too. User's lean: not clearly
  worth the effort, but likes the consistent observability + rollback; PARK and
  revisit. Do NOT implement without an explicit go.
- **GUI state gap: "registered but not flashed" is a dead-end.** When a device is
  registered but its firmware build failed (no firmware in S3), the builder-Pi
  GUI card only offers Reflash/Serial/Deregister — and Reflash only DOWNLOADS
  existing S3 firmware (it does NOT trigger a build), so it fails with nothing to
  fetch, and there is no Register/Rebuild button to recover. Workaround:
  Deregister then Register (re-runs the full build+flash pipeline). Fix options:
  (a) show Register/Rebuild when no firmware exists in S3, or (b) make Reflash
  fall back to triggering a build when S3 has none. This dead-end is also a data
  point FOR the initial-flow Step Function idea (a resumable/observable pipeline
  makes "build failed" a visible retryable state rather than a stuck card) —
  consider bumping that item's priority.
- **Firmware builder image is a manual post-deploy build (should be automatic).**
  The broker image auto-builds via a BuildTrigger Lambda + `Custom::TriggerBuild`
  custom resource in broker-container-stack.yaml; the device-builder stack defines
  `ImageBuilderProject` (`pqc-workshop-build-image`) + its ECR repo but has NO
  trigger, so nothing builds it — a fresh deploy leaves `pqc-workshop-firmware-builder`
  ECR empty and the first device flash fails at PROVISIONING
  (`CannotPullImageManifestError`). NOT a dependency issue — just missing wiring.
  Fix: fold the firmware image build into `deploy-workshop.sh` step 3 (trigger +
  wait, exactly like the broker image build), removing the manual step entirely.
  Interim: DEPLOYMENT.md step 4 documents it loudly as mandatory-before-flash.
- Bundler does not bake `broker_endpoint` into the Pi `config.json` (only api +
  firmware bucket), so it must be entered in the GUI. Consider resolving it from
  the `/pqc-workshop/*` SSM params / broker NLB output.
- Broker image build depends on `apt-get` from the Ubuntu mirror in the builder
  stage; hardened with retries/timeouts + 60-min CodeBuild timeout, but a fully
  offline/pinned base or pre-baked deps would be more robust for Workshop Studio.

## Overview

This is an Arduino sample project demonstrating how to connect an ESP32 microcontroller to AWS IoT Core using post-quantum cryptography (PQC). It uses wolfSSL for TLS 1.3 with hybrid Kyber/ML-KEM key exchange, and manages WiFi and MQTT as independent FreeRTOS tasks with automatic failure recovery.

## Tech Stack

- **Platform:** ESP32 (all WiFi-capable variants) via Arduino framework
- **IDE:** Arduino Cloud IDE (app.arduino.cc) and Arduino Desktop IDE
- **TLS Library:** wolfSSL 5.7.0+ with Kyber/ML-KEM PQC support
- **MQTT Library:** PubSubClient 2.8+
- **RTOS:** FreeRTOS (built into ESP32 Arduino core)
- **Cloud Service:** AWS IoT Core (MQTT over TLS 1.3, port 8883)
- **Authentication:** Mutual TLS with X.509 certificates

## Conventions

- Single `.ino` sketch file for Arduino Cloud IDE compatibility
- Configuration and credentials isolated in `secrets.h` (never commit real values)
- wolfSSL compile-time configuration documented in `wolfssl_user_settings.h` (reference only, not included directly)
- FreeRTOS tasks use `xTaskCreatePinnedToCore` with explicit stack sizes
- Serial output uses structured prefixes: `[WiFi]`, `[MQTT]`, `[wolfSSL]`, `[Setup]`
- All timing constants defined as `#define` at the top of the sketch for easy tuning
- Use inclusive language in all code, comments, and documentation (see inclusivity rules)
- C-style code following Arduino conventions (camelCase for functions, g_ prefix for globals)
