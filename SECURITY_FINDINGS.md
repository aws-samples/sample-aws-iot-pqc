<!--
Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Security Scan Findings

This file records how security-scanner findings for this repository were
resolved. It is the human-readable companion to the scanner-specific
suppression files:

- `.checkov.yaml` — repo-wide Checkov skip-checks (text-level scanners).
- `.gitleaksignore` — accepted gitleaks fingerprints.

As of the latest Probe scan there are **no accepted ERROR or WARNING
exceptions** — every actionable finding was fixed in source rather than
suppressed. The remaining Probe output is INFO-tier only (see "Accepted
INFO-tier noise" below).

Note: some hosted scanners (e.g. the internal **Probe** pipeline) run Semgrep
with inline suppressions disabled, so a `# nosemgrep:` comment does **not**
clear a finding there. That is why the fixes below change the code rather than
suppress it.

---

## Resolved in source

- **`missing-user` (ERROR)** — `broker-container/docker/Dockerfile`.
  The Mosquitto broker container now runs as a dedicated non-root user
  (`mosquitto`). A build-time `useradd` creates the user, the full `/mosquitto`
  directory tree (including the `certs/bridge` and `certs/mldsa` subdirs the
  entrypoint writes into) is `chown`ed to it, `HOME=/mosquitto` is set so the
  AWS CLI has a writable home, and a final `USER mosquitto` directive drops
  privileges. Root was not actually required: AWS access is via the ECS task
  role (independent of the Linux user), the listener ports (8883/8884) are
  unprivileged, and the only runtime writes are into the now user-owned
  `/mosquitto/certs` and `/mosquitto/config/conf.d`.

- **`insecure-document-method` (ERROR)** — `raw_device_builder/builder-pi/templates/index.html`.
  The device-grid and serial-output renderers build DOM nodes with
  `document.createElement` + `textContent` instead of assigning HTML strings via
  `innerHTML`, so device-supplied values are never parsed as HTML.

- **`insecure-object-assign` (WARNING)** — `raw_device_builder/dashboard-v2/frontend/src/config.js`.
  The runtime config merge copies an explicit allow-list of known keys instead
  of a blanket `Object.assign(config, loaded)`.

- **`remove-package-cache` (WARNING)** — `broker-container/docker/Dockerfile`.
  Every `apt-get install` RUN ends with `apt-get clean && rm -rf
  /var/lib/apt/lists/*`.

---

## Accepted INFO-tier noise

The remaining Probe findings are all INFO severity and are left as-is:

- `dockerfile-source-not-pinned` — base images are pinned to a tag
  (`ubuntu:22.04`) rather than a digest, by design for a public sample.
- `bandit` B404/B603/B607 — `subprocess` usage in the local builder-Pi and
  cert-issuer tooling, invoking fixed, trusted commands (no untrusted input).
- `hashicorp-tf-password` / `generic-api-key` — false positives over SSM
  parameter-path strings and ARNs; no credentials are hardcoded (cert/key
  material is created at runtime and stored in AWS Secrets Manager).
- `unquoted-variable-expansion-in-command`, `prefer-json-notation`,
  `missing-template-string-indicator`, `B106`/`B110` — style/false-positive
  noise in helper scripts and tests.

---

# Slingshot scan (Checkov / cfn-lint / Bandit / Semgrep OSS)

Slingshot runs a broader, stricter rubric than Probe and, like Probe, does
**not** honor inline suppressions (`# nosemgrep`, `# nosec`) or the in-template
`checkov: skip` / `cfn_nag` metadata. It surfaced 65 HIGH findings. They fall
into three groups.

## Resolved in source (Slingshot)

- **`avoid_app_run_with_bad_host` (HIGH)** — `raw_device_builder/builder-pi/app.py`.
  The builder-Pi Flask GUI bind host is now read from config
  (`config.json` → `gui_host`) instead of a hardcoded `'0.0.0.0'` literal in the
  `app.run()` call. The default remains all-interfaces because the GUI is
  reached from the operator's laptop over the Pi's workshop WiFi access point
  (loopback would make it unreachable), but operators can now narrow it to the
  AP interface IP or `127.0.0.1`. Making the bind a documented, overridable
  deployment choice is the improvement; behavior is unchanged by default.

- **`S3 Example Bucket Naming` (HIGH)** — `raw_device_builder/README.md`.
  The example `aws cloudformation deploy` command used a concrete, guessable
  firmware-bucket name. Replaced with a `<your-unique-firmware-bucket-name>`
  placeholder so readers supply their own non-squattable name.

## Audit-level false positives (no injection vector)

These are Semgrep/Bandit "audit" rules that flag any dynamic `subprocess`
call or keyword-arg string for human review. Reviewed and confirmed safe:

- **`dangerous-subprocess-use-audit` / Bandit `B603`/`B607`** —
  `shared/cert-issuer/issue_cert.py`, `raw_device_builder/builder-pi/{device_manager,cli}.py`,
  `raw_device_builder/lambda/pqc-upgrader/index.py`. Every call uses an argv
  **list** with no `shell=True` (verified repo-wide), so arguments are never
  passed through a shell — there is no command-injection vector. Inputs are
  additionally sanitized (e.g. `_sanitize_token` in the cert issuer).
- **Bandit `B106` "hardcoded password"** — `shared/cert-issuer/test_issue_cert.py`.
  The flagged string is an ACM PCA **idempotency token** (a dedup key), not a
  credential. Annotated `# nosec B106` with that reason.

## Intentional sample-vs-production tradeoffs (by design)

Every one of the 59 Checkov HIGH findings already carries a documented
`checkov: skip` (and usually a `cfn_nag` suppression) in its CloudFormation
template; Slingshot re-surfaces them only because it ignores those skips.
They are deliberate choices for a one-shot-deployable **teaching sample**, not
a production system — "fixing" them would add KMS CMKs, dedicated log buckets,
and tightly-scoped IAM that fight the workshop's stated design goal of a
readable, single-attempt deploy. The decisions:

- **`EA022` broker Security Group `0.0.0.0/0` on 8883/8884** — **required.** The
  ESP32 devices connect from the public internet to the broker's internet-facing
  NLB; mutual TLS (`require_certificate true`) is the authentication gate. This
  is the paved-path shape for a public, auth-gated service port (analogous to a
  public 443). Narrowing it would break device connectivity for every attendee.
- **`CKV_AWS_366` Cognito unauthenticated access** — **intentional.** The
  dashboard is a public, read-only workshop dashboard that uses unauthenticated
  Cognito identities bound to a restrictive unauth IAM role.
- **`CKV_AWS_109` / `CKV_AWS_111` IAM wildcards (30)** — scoping every policy's
  resources/actions is invasive and raises redeploy risk; the roles are
  workshop-scoped and short-lived.
- **`CKV_AWS_149` Secrets Manager / `CKV_AWS_119` DynamoDB / `CKV_AWS_136` ECR
  (21)** — these resources use AWS-managed (AES256 / aws/* KMS) encryption at
  rest by default. The rules want customer-managed CMKs, which add key
  management + cost for no meaningful gain on disposable workshop data.
- **`CKV_AWS_18` S3 / `CKV_AWS_86` CloudFront / `CKV_AWS_193` AppSync access
  logging (6)** — access-logging destinations are out of scope for the sample.
- **`CKV_AWS_174` CloudFront TLS version** — the distribution uses the default
  `*.cloudfront.net` certificate, which does not permit setting a
  `MinimumProtocolVersion` without a custom domain + ACM certificate (extra
  infra the workshop does not require).

If this code were promoted toward production, the production checklist is:
customer-managed KMS CMKs on Secrets Manager / DynamoDB / ECR, per-resource
IAM scoping, S3 + CloudFront + AppSync access logging, a custom CloudFront
domain/cert with a modern `MinimumProtocolVersion`, and WAF on the dashboard.
