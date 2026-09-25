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
- **[FEATURE — Step 2 of the migration path: migrate the CERTIFICATES to PQC
  signatures (ML-DSA-44)] Build the certificate-migration flow.** The workshop
  today performs Step 1 only — migrating the key EXCHANGE to hybrid ML-KEM
  (closing "harvest now, decrypt later"). The device's certificate stays
  CLASSICAL (ECDSA P-256), so its signature is still forgeable by a future
  quantum computer. Step 2 closes that certificate-signature-forgery risk by
  moving the certificate to a post-quantum signature (ML-DSA, FIPS 204; use
  ML-DSA-44 / Level 2 for the workshop). This is surfaced to attendees as a
  CHALLENGE section (attendee/6-migrate-certificates) whose click-by-click steps
  are NOT yet written — this backlog item is the implementation behind it.

  High-level spec (four parts; the design intent is that the classical and the
  ML-DSA-44 worlds run in PARALLEL so devices migrate over the existing OTA
  process without a flag-day cutover):

  1. **Two Private CAs, running side by side.** Create a SECOND Private CA for
     the ML-DSA-44 certificates, alongside the existing classical CA (keep the
     original for the classical ECDSA certs). BOTH CAs coexist permanently.
     - **Device registration** is modified so that when a device is registered it
       generates device certificates in BOTH CAs — one classical cert and one
       ML-DSA-44 cert per device.
     - **Broker creation** does the same: TWO broker certificates are generated,
       one from each CA.
     NOTE feasibility risk: AWS Private CA does not issue ML-DSA certs today (the
     current broker CA is a ROOT ACM PCA issuing ECDSA P-256 — see
     broker-container-stack.yaml), so the second (ML-DSA-44) CA likely needs a
     self-managed/local CA (e.g. OpenSSL 3.5 + oqs-provider, which the broker
     image ALREADY builds) rather than ACM PCA. Confirm before design.

  2. **Two broker listeners, in parallel.** Change the broker configuration so it
     has TWO listeners for the IoT->Mosquitto connections\: one presenting the
     classical TLS cert, and another presenting the ML-DSA-44-signed cert. They
     run in parallel on DIFFERENT PORTS but under the SAME broker name and
     endpoint (same NLB DNS / hostname; a second listener port + NLB listener/
     target group). A classical device keeps using the classical port; a migrated
     device uses the ML-DSA-44 port. No disruption to devices on either side.

  3. **A third firmware .ino that supports ML-KEM + the ML-DSA cert.** Create a
     new firmware sketch that does BOTH hybrid ML-KEM key exchange AND presents/
     trusts the ML-DSA-44-signed certificate (the Step-1 firmware only does
     ML-KEM with a classical cert). The per-device firmware build process for OTA
     delivery must handle being invoked with this NEW third firmware variant,
     built with the new ML-DSA-44-signed ESP32 device certificate (extend
     FIRMWARE_VARIANT beyond classical/pqc, and have buildspec select the third
     sketch + inject the ML-DSA device cert). Deployment is over the EXISTING OTA
     process — same migration Step Function / publish-ota path. wolfSSL must have
     ML-DSA verify/sign enabled in user_settings.h (it already has ML-KEM;
     confirm ML-DSA support + memory budget on the ESP32-S3).

  4. **Verify the S3 presigned-URL path still works for repeated OTAs.** Confirm
     the presigned S3 GET URL used by publish-ota still works for ADDITIONAL
     firmware changes at future dates from the SAME firmware S3 bucket (i.e. a
     device that already did Step 1 can be OTA'd again for Step 2, and again
     later) — check presign expiry, per-version keys (`<serial>/<version>/`), and
     that nothing about the bucket/versioning blocks a second/third OTA to the
     same device.

  End state: a device whose key exchange AND certificate are both post-quantum —
  the terminal state of the Classical TLS -> PQC algorithms -> PQC certificates
  path (per NIST IR 8547). Both CAs, both broker listeners, and all three
  firmware variants coexist so a fleet migrates gradually over OTA rather than in
  a flag-day cutover. Lower urgency than Step 1 (cert forgery needs a quantum
  computer that does not yet exist), and gated on the CA feasibility question in
  part 1.
- **[Cloud dashboard — Step 2 device-state display: 3 fixes] Surface the real
  firmware version + running variant, and make the picker offer only the OTHER
  variants.** Observed on pqc-a (Sep 25) during the first full migration test,
  on the cloud dashboard (`raw_device_builder/dashboard-v2/frontend`), device
  card for esp32-C69E18 running fw1 classical. Three related defects, all rooted
  in the same gap — the device telemetry / `pqc-workshop-devices` record does not
  carry the firmware version or the variant, so the card can't show them:
  1. **Firmware version shows `-`.** The card's "Firmware" metric renders
     `device.firmwareVersion`, but the record's `firmwareVersion` is null (the
     device telemetry doesn't report it, and `detectVariant` only *reads* it).
     Fix: have the ESP32 firmware publish its `FIRMWARE_VERSION` in telemetry and
     the telemetry-processor Lambda write it to `firmwareVersion`, so the card
     shows e.g. `1.0.0` / `2.0.0` / `3.0.0` instead of `-`.
  2. **"Running:" shows `unknown`.** `detectVariant(device)` (App.jsx) infers the
     variant from `firmwareVersion` (1.x/2.x/3.x) with a `pqcAlgorithm` fallback;
     with `firmwareVersion` null and only `ECDHE-P256` in `pqcAlgorithm` it
     returns null → "unknown". Fixing (1) makes detectVariant resolve correctly;
     alternatively have telemetry carry an explicit `variant`/`firmwareType`
     field and key off that (more robust than string-matching the version).
  3. **Picker lists the CURRENT variant too.** The `<select>` renders all of
     VARIANTS (with the current one tagged "(current)"). It should offer only the
     variants you can switch TO — exclude `currentVariant` from the options (and
     drop the now-unnecessary "(current)" suffix). Keep a sensible default when
     the current variant is unknown (fall back to showing all, or default to pqc).
  Note the fix order: (1) is the enabler — once `firmwareVersion` is real
  telemetry, (2) and (3) mostly fall out (detectVariant works → the picker can
  exclude the known current). User decision (Sep 25): DEFER until the initial
  full migration path (fw1→2→3 + rollback) is proven on pqc-a; then adjust the
  GUI + registry/telemetry together and re-test. Touches: the ESP32 sketches
  (publish version/variant in telemetry), dashboard/lambda/telemetry-processor
  (persist firmwareVersion/variant), and App.jsx (detectVariant + picker option
  filtering).
- **[GUI — double-click causes a duplicate registration/build] Disable the
  "Register & Flash" button once clicked.** Observed on pqc-b (Sep 18): during a
  parallel-flash test a double-click on one device's button fired TWO
  `POST /api/register` calls, producing TWO concurrent CodeBuild firmware builds
  for the same serial (C69E44) — harmless (both write the same
  `<serial>/0.1.0/` S3 path, last-writer-wins) but wasteful and confusing.
  Root cause: the frontend `registerDevice()` in
  `raw_device_builder/builder-pi/templates/index.html` (~line 124) does not
  disable the button on click, and the server-side guard in
  `raw_device_builder/builder-pi/app.py` `register_device()` (~line 144) admits
  a second call because the device status is still `ready` when the rapid second
  click lands (status only flips to `registered` inside the background pipeline).
  Fix: (a) frontend — grey/disable the button immediately on click (set
  `disabled` + a "registering..." label) so it cannot be clicked twice; and/or
  (b) server — flip the slot status to a transient `registering` synchronously
  in the `/api/register` handler BEFORE spawning the thread, and reject re-entry
  while in that state. Recommend both (defence in depth). Same pattern applies to
  the Reflash/Deregister buttons. NOTE: parallel flashing across DIFFERENT devices
  works fine (3 concurrent builds ran cleanly in the same test) — this is purely
  the same-device double-submit.
- **[Builder-Pi — harden concurrent multi-device flashing] Make parallel
  register+flash robust for the "attendees plug in and flash at the same time"
  moment.** Observed on pqc-b (Sep 18): flashing two DIFFERENT devices in quick
  succession worked (3 concurrent CodeBuild builds ran cleanly, both devices
  registered), so the cloud path is already parallel-safe. The RISK is on the Pi
  side, where five clash areas could bite with more devices or faster clicks:
  1. **Serial I/O is not serialized per port.** `device_manager.py`'s `_lock`
     guards only the in-memory `devices` dict, NOT the actual esptool/serial
     operations. Two flashes targeting the SAME `/dev/tty*` (e.g. a device that
     re-enumerates mid-flash, or a port reused after a reset) would collide.
     Fix: hold a per-PORT lock for the whole flash (open→erase→write→verify),
     not just around dict mutation.
  2. **Background `port_scanner` races the flasher.** The scanner continuously
     enumerates ports and can observe a device mid-flash (bootloader mode /
     re-enumerating), churning device state. Fix: gate the scanner on a
     per-port "busy/flashing" status and skip (or pause) scanning ports that an
     active flash owns; resume when it completes.
  3. **Registration re-entry while `ready`.** `register_device()` (~line 144)
     admits a new pipeline while the device is already `ready`, so a second
     submit re-runs the whole register→build→flash. Fix: reject/queue new
     submissions for a serial that has an in-flight pipeline (complements the
     GUI double-click disable item above — do both).
  4. **Flask dev server + daemon threads.** `app.py` uses `app.run` (Werkzeug
     dev server) and spawns a daemon thread per registration; under many
     simultaneous clicks this is unbounded and fragile. Fix: cap concurrency
     with a small worker pool / bounded queue and surface a "queued" state in
     the GUI, rather than spawning unbounded threads.
  5. **Physical/identity ambiguity.** `FIRMWARE_DIR` and device identity key off
     `<serial>`, but the flasher targets by `--port`; with many boards on one
     hub, port↔serial mapping can drift (unpowered hub brownouts, re-enumeration
     under load). Fix: use a powered USB hub, cap concurrent flashes to the
     hub's safe count, and confirm the chip's own serial (esptool chip id) after
     connect so a build for serial X can never be flashed onto board Y.
  Net recommendation: per-port lock (1) + scanner status-gate (2) + registration
  in-flight guard (3) are the core correctness fixes; bounded worker pool (4) and
  powered-hub + chip-id verification (5) are the robustness/hardware fixes for a
  full room. Low urgency today (DIFFERENT-device parallel flash already works),
  but this is the path to a reliable "everyone flashes at once" attendee moment.
- **[Architecture — stable broker DNS via a permanent hosted zone + broker cert
  SANs] Decouple the ESP32 firmware from the NLB's ephemeral DNS name.** Today
  the broker endpoint is the raw NLB DNS name (`pqc-broker-nlb-*.elb.us-east-1.
  amazonaws.com`), which changes on every broker-stack rebuild and forces a
  firmware rebuild + re-flash. This is a core pain in the rebuild loop and the
  single biggest simplification available to the ESP32 build. Desired design
  (user-approved):
  - A SEPARATE permanent AWS account owns a Route 53 hosted zone. For each
    workshop account, create a **CNAME** in that zone pointing at that account's
    NLB DNS name — e.g. `broker-a.<zone>` -> pqc-a NLB, `broker-b.<zone>` ->
    pqc-b NLB, `broker-ws.<zone>` -> the Workshop Studio account NLB.
  - The ESP32 firmware bakes in the STABLE alias (one of the three above) as
    `MQTT_BROKER_ENDPOINT` instead of the raw NLB name. It already flows into
    SNI automatically (wolfSSL_UseSNI uses the endpoint host).
  - The broker server certificate MUST carry these alias names as **Subject
    Alternative Names (SANs)** — at least 3: pqc-a, pqc-b, pqc-ws aliases (issue
    all three on every broker cert so one build works in any account). CURRENT
    STATE: the broker cert has NO SANs — the CSR is just
    `/CN=pqc-mqtt-broker/O=AWS PQC Workshop/C=US` and `acm_pca.issue_certificate`
    passes no ApiPassthrough extensions (see the BrokerCert custom-resource Lambda
    in `broker-container/cloudformation/broker-container-stack.yaml`, ~line 705-730).
    Fix: add the alias FQDNs as SANs, either by putting a SAN extension in the
    openssl CSR (`-addext "subjectAltName=DNS:broker-a...,DNS:broker-b...,DNS:broker-ws..."`)
    AND/OR passing `ApiPassthrough.Extensions.SubjectAlternativeNames` to
    `issue_certificate` (ACM PCA honors passthrough SANs when the CA template
    allows it — use an `EndEntityCertificate/V1` template with CSRPassthrough).
  - The DNS CNAME re-point after each stack rebuild is a MANUAL step in the
    permanent account — user is happy with that trade (it removes the per-rebuild
    firmware re-bake entirely). Document it in the deploy runbook.
  - Bonus hardening: once SANs exist, tighten the firmware to actually ENFORCE
    hostname/SAN matching (wolfSSL domain-name check / `DOMAIN_NAME_CHECK`) — today
    verification is chain-only (`WOLFSSL_VERIFY_PEER` in aws_iot_pqc_esp32.ino
    ~line 595, no explicit domain-name check), so a hostname mismatch is NOT
    currently caught. For a PQC security workshop, enforcing SAN match is the
    correct end state.
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
- **[BLOCKER for pqc-b push — migrationStatus never reconciled to "migrated"
  after PQC confirmed; it BLOCKS the next migration] Close the migration loop
  when the device reports it is actually on ML-KEM.**
  UPGRADED from cosmetic to BLOCKER on pqc-a (Sep 25) during the full
  fw1->fw2->fw3 migration test: after fw1->fw2 succeeded and the device
  reconnected reporting `SECP256R1_MLKEM768`, the card stayed on the disabled
  "Rebooting into PQC…" spinner and **the firmware picker + Flash button
  disappeared**, so fw2->fw3 could NOT be started at all. Root cause in App.jsx:
  `migrating = MIGRATION_IN_PROGRESS.includes(device.migrationStatus)` (building/
  pushing-ota/rebooting) → when migrating, the card renders the disabled spinner
  INSTEAD of the picker+Flash. Because migrationStatus is stuck at `rebooting`
  forever (never reconciled), a device that finished one migration is
  permanently locked out of the next one. Manual unblock used during the test:
  `aws dynamodb update-item ... REMOVE migrationStatus, migrationError` on the
  device record — attendees obviously can't do that, so this MUST be fixed
  before pqc-b (the workshop's whole point is chaining migrations
  classical->pqc->pqc-mldsa). The reconcile fix below also fixes the earlier
  "Running: unknown" / firmware-version display items (they share the telemetry
  gap). Prior observation retained:
  Observed on pqc-b (Sep 18): both devices (esp32-C69E44, esp32-C69DE0)
  successfully migrated — Step Function SUCCEEDED, PQC firmware built + OTA'd,
  devices rebooted and reconnected to the broker with the hybrid handshake
  (telemetry reports `pqc_algorithm: SECP256R1_MLKEM768`, `tls_version:
  TLSv1.3`). BUT the `pqc-workshop-devices` table `migrationStatus` field stays
  permanently at `rebooting` — the Step Function's terminal write is
  `SetStatusRebooting` -> `Succeed` (see
  `cloudformation/statemachine/pqc-migration.asl.json`), and NOTHING ever moves
  it forward once the device comes back on PQC. So the stored field is
  stale/misleading for anything querying the table directly. NOT user-visible
  today only because the dashboard derives "migrated" from the LIVE telemetry
  `pqc_algorithm` field (via the telemetry-processor Lambda), not from
  `migrationStatus` — which is a lucky workaround, not a fix. Desired behaviour:
  when a device publishes telemetry confirming an ML-KEM group is actually in
  use (i.e. `pqc_algorithm` contains `MLKEM`), reconcile the device record to a
  terminal `migrated` state (and record the confirmed algorithm + version). Fix
  options: (a) have the telemetry-processor Lambda (which already parses
  `pqc_algorithm` for the dashboard) also do a conditional `updateItem` on
  `pqc-workshop-devices` setting `migrationStatus=migrated` +
  `firmwareVersion=1.0.0-pqc` + e.g. `confirmedPqcAlgorithm` the first time it
  sees an MLKEM group for that thing — cheapest, closes the loop at the exact
  moment PQC is confirmed; (b) a dedicated IoT rule/Lambda on
  `dt/+/telemetry` that watches for the MLKEM confirmation and updates the
  record; (c) an SNS/EventBridge "migration confirmed" event the record update
  subscribes to. Recommend (a) — the signal (MLKEM in telemetry) and the writer
  (telemetry-processor) already exist; it just needs the conditional write.
  Note the design intent: confirmation must come from the device ACTUALLY
  negotiating ML-KEM (the telemetry), not merely from the OTA being pushed — so
  "migrated" means verified-on-PQC, which is the correct semantics for a PQC
  workshop.
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
- **[Workshop Studio — FUTURE attendee experience, DESIGN ONLY, not built]
  Let attendees edit the ESP32 `.ino` and OTA-update ONLY their own device via a
  scoped submission to the central pipeline.** The compelling stretch experience:
  an attendee tweaks the firmware source in their own account's Cloud IDE,
  submits it, and the central account builds + pushes it OTA to *their* labelled
  device. Design decisions already reasoned through (do NOT re-litigate without a
  reason):
  - **Invert the trust direction.** Do NOT give the attendee account broad
    cross-account read into the central account. Instead the attendee performs
    ONE tightly-scoped action against a single central entry point; the central
    account holds all privilege + identity (cert/thing/policy, Private CA, IoT).
  - **Entry point = S3 for now** (decided): the attendee drops their `.ino` into a
    central S3 bucket under a key bound to THEIR device serial (a per-attendee
    prefix / naming convention). A central trigger (S3 event → the existing
    firmware pipeline) builds it. CodeCommit-as-source is a STRETCH only —
    explicitly deferred because it means reworking the pipeline's source stage;
    not doing that.
  - **Enforce "only their device" server-side, not by label.** The label just
    tells the human which device is theirs; it does not stop attendee A submitting
    for attendee B's serial. Bind the submission to the caller's identity (e.g.
    assumed-role session tag = their serial) and have the central build REJECT any
    other serial. Required for a real room; a controlled demo can rely on
    convention.
  - **Attendee never sees secrets/keys.** The build already injects the device
    cert + private key + WiFi inside the central pipeline (see buildspec.yml); the
    attendee only supplies the sketch. This is an argument FOR the central-pipeline
    model — the attendee account holds no identity material.
  - Cross-account topology caveat: a persistent "central" account that all
    ephemeral WS attendee accounts trust is NON-standard for Workshop Studio (WS
    vends per-event accounts). Validate how WS supports a shared central account
    before designing around it — likely the biggest feasibility risk.
  - Scope: this is the weight-2 slot intentionally left open in the WS content
    nav (introduction=0, deployment=1, [2 = future attendee-submit], attendee
    plug-in=3, attendee migrate=4). Today's WS content ships the operator track +
    a narrative-only attendee track (plug in → migrate via dashboard button); the
    edit-and-submit flow is future work.
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
- **[DONE] Automate the NetworkManager AP as part of Pi provisioning.** The Pi's
  2.4GHz workshop AP used to be a MANUAL `nmcli` setup (DEPLOYMENT.md §5a), which
  caused a `4WAY_HANDSHAKE_TIMEOUT` during a device test when the operator-typed
  AP PSK didn't match `@reinvent2026`. Resolved: the `configure-wifi-ap` SSM
  document builds the AP from the SSM WiFi params (`/pqc-workshop/wifi/{ssid,
  password}` — the same source the firmware is built from, so PSK typos are
  impossible by construction) and detects the AP radio by its `mt76*` driver
  rather than hardcoding `wlan1`. It is now step 5/5 of the DEFAULT provisioning
  chain in `setup-builder-pi-remote.sh` (commit 82fcab7), so a workshop Pi comes
  up fully networked with no manual AP step. Confirmed by a successful Workshop
  Studio run. (Note: the AP lives in an SSM document run by the remote
  orchestrator, not in an on-Pi `setup-all.sh` — the on-Pi provisioning was moved
  to SSM documents; the outcome is the same: no manual `nmcli`.)
- **[BLOCKER for pqc-b push] Dashboard LED on/off button not working.**
  Re-confirmed on pqc-a (Sep 25) during the full migration test: the LED on/off
  control on the device card does not actuate the device. On the device
  dashboard, the LED (command) button does not actuate the device. Likely the
  cmd/<thing>/+ command path (dashboard -> AppSync/API -> IoT -> broker bridge
  inbound topic -> device) or the firmware's command handler. Device DOES
  subscribe to cmd/esp32-<serial>/+ (seen in broker logs), so the downlink wiring
  is the place to check. In the v2 frontend the LED toggle calls
  `onCommand(thingName, 'led-on'|'led-off', 'control')` -> the `sendCommand`
  AppSync mutation -> command-publisher Lambda -> IoT publish; audit that whole
  chain (topic suffix 'control' vs the firmware's expected `cmd/<thing>/+`
  subscription, the command-publisher topic construction, and the firmware
  command handler). User decision (Sep 25): this MUST be fixed before we push to
  pqc-b (no longer "low priority / defer"). Not blocking the current
  connect/telemetry/migration validation, but a required GUI fix for the pqc-b
  gate. Relates to the "dashboard v2 command/mutation wiring audit" note above.
- **Registrar partial-failure cleanup (near-term, low effort).** The
  `device-registrar` Lambda does cert -> create thing -> create policy ->
  DynamoDB -> start CodeBuild sequentially; if a later step fails the earlier
  resources (IoT Thing, cert, per-device policy) are left orphaned (observed:
  `esp32-C69E18` Thing created even though the firmware build later failed on the
  missing builder image). Add best-effort rollback on failure so a failed
  registration doesn't accumulate orphans. Small change to the existing Lambda,
  no Step Function required.
- **Initial register+flash as a Step Function (SEPARATE post-ML-DSA branch —
  decided, do NOT bundle into the cert-migration feature).**
  The classical migration already runs on the `pqc-workshop-pqc-migration` Step
  Function; the INITIAL register+build+flash does NOT (it's a synchronous
  registrar Lambda + the Pi polling CodeBuild). Migrating the cloud half
  (thing/policy/ddb/start-build) to a Step Function would give consistent
  pipeline-style observability + per-step retries + rollback, symmetric with the
  migration flow.
  - **The ML-DSA WS2 refactor (commit 122e0c0) removed the biggest blocker:**
    cert issuance moved OUT of the registrar Lambda into the shared cert-issuer
    CodeBuild job, so `device-registrar` is now pure orchestration (create Thing
    → create policy → write registry → start build). That decomposes into SFN
    states cleanly, and the shared cert-issuer image already drops into an SFN via
    `codebuild:startBuild.sync`. So most of the strategic groundwork is already
    laid by WS2.
  - **Why it is NOT part of the cert-migration feature branch:** different
    concern (initial-flash operability, not PQC certs); bundling enlarges the
    branch + slows the definition-of-done clean rebuild of both accounts. Also the
    natural forcing function is WS3 — once the migration SFN input + dashboard
    picker are variant-aware and BOTH entry points (dashboard migrate + Pi
    register) flow through variant-aware build logic, that is the right moment to
    look at unifying them. Refactoring registration to SFN before WS3 designs
    against a moving target.
  - **Preferred design when it IS done:** the Pi-as-WORKER (SFN task token /
    activity) shape — the SFN owns cert/thing/policy/build, then hands the Pi a
    task token ("certs + firmware ready, your turn to flash") and the Pi reports
    flash status back. This gains real resumability/observability (not just a
    state machine wrapping the same synchronous flow) and improves Pi-side
    structure. Caveat: the flash/USB/serial half MUST stay on the Pi (hardware),
    so end-to-end visibility is inherently split no matter what.
  - **Fold in the "registered but not flashed" GUI dead-end item** (below) — a
    resumable/observable pipeline makes "build failed" a visible retryable state
    rather than a stuck card, so that item is the proper fix that this refactor
    enables. Do them together.
  - Do NOT implement in the cert-migration branch. New branch, after the ML-DSA
    feature reaches its definition of done.
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
