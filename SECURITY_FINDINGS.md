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
