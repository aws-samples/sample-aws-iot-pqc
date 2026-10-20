<!--
Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Security Scan Findings — Accepted Exceptions

This file documents security-scanner findings that are **intentionally accepted**
("won't fix — by design") for this repository, with the reasoning for each. It is
the human-readable companion to the scanner-specific suppression files:

- `.checkov.yaml` — repo-wide Checkov skip-checks (text-level scanners).
- `.gitleaksignore` — accepted gitleaks fingerprints.
- Inline `# nosemgrep: <rule>` / resource `Metadata` suppressions — per-line /
  per-resource exceptions for scanners that honor them.

Some hosted scanners (e.g. the internal **Probe** pipeline) run Semgrep with
inline suppressions disabled, so a `# nosemgrep:` comment does **not** clear the
finding there. Those findings must be accepted in the scanner's own
baseline/UI. This document records why, so the acceptance is auditable and the
next triager does not re-investigate a known, deliberate decision.

---

## Accepted: `missing-user` — broker container runs as root (by design)

- **Scanner / rule:** Semgrep `missing-user` (and the equivalent Checkov
  `CKV_DOCKER_3`).
- **File:** `broker-container/docker/Dockerfile` (final stage; no trailing
  `USER` directive).
- **Severity reported:** ERROR.
- **Status:** Accepted — will not fix. Accept/baseline in the scanner UI.

### Why the container runs as root

The Mosquitto broker container **must** run as `root` for its one-shot Workshop
Studio deployment to work:

1. `entrypoint.sh` fetches the broker's TLS certificate and private key from
   **AWS Secrets Manager** at container start and writes them into
   root-owned `/mosquitto/certs`.
2. It writes the generated IoT Core bridge configuration into root-owned
   `/mosquitto/config/conf.d` before exec'ing `mosquitto`.

Both directories are created root-owned in the image build. Introducing a
non-root `USER` would require re-architecting the entrypoint's privileged cert
and bridge-config writes, which risks breaking a **verified-working**
from-scratch deploy — the project's overriding requirement is that a clean
destroy + rebuild of both workshop accounts succeeds end-to-end.

### Why it is low risk here

- This is a dedicated infrastructure container (the MQTT broker), not a
  multi-tenant or user-content-serving workload.
- It runs as a Fargate task in **private subnets** (no public IP; device
  traffic arrives via an internet-facing NLB, broker→IoT egress via NAT).
- It holds no interactive shell surface; the entrypoint exec's `mosquitto`
  directly.

The in-code `# nosemgrep: missing-user` comment on the Dockerfile documents the
same rationale for human readers, even though Probe ignores it for suppression.

---

## Resolved in code (no longer findings)

The following earlier findings were **fixed in source** (not suppressed) during
the Probe remediation passes, so they should not reappear:

- `insecure-document-method` (ERROR) — `raw_device_builder/builder-pi/templates/index.html`:
  the device-grid and serial-output renderers now build DOM nodes with
  `document.createElement` + `textContent` instead of assigning HTML strings via
  `innerHTML`, so device-supplied values are never parsed as HTML.
- `insecure-object-assign` (WARNING) — `raw_device_builder/dashboard-v2/frontend/src/config.js`:
  the runtime config merge now copies an explicit allow-list of known keys
  instead of a blanket `Object.assign(config, loaded)`.
- `remove-package-cache` (WARNING) — `broker-container/docker/Dockerfile`:
  every `apt-get install` RUN now ends with `apt-get clean && rm -rf
  /var/lib/apt/lists/*`.
