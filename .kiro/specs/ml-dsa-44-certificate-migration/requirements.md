# Requirements — ML-DSA-44 Certificate Migration (Step 2)

## Overview

This feature implements **Step 2** of the PQC migration path described in
NIST IR 8547: migrating device and broker **certificates** from classical
signatures (ECDSA P-256) to a post-quantum signature scheme, **ML-DSA-44**
(FIPS 204, Level 2). Step 1 (already shipped) migrated the TLS **key exchange**
to hybrid ML-KEM, closing the "harvest now, decrypt later" confidentiality risk.
Step 2 closes the remaining **certificate-signature-forgery** risk: today a
device's certificate is signed with a classical algorithm that a future quantum
computer could forge, allowing device/CA impersonation and defeating mutual TLS.

**Feasibility note (resolved):** AWS Private CA (ACM PCA) supports **ML-DSA-44**
for the root certificate, so the second CA is a managed ACM PCA — we do NOT need
a self-managed OpenSSL/oqs-provider CA. This supersedes the earlier feasibility
caveat in the steering backlog.

## Design intent (governs all requirements)

The classical and ML-DSA-44 worlds run **in parallel** so a fleet migrates
gradually over the existing OTA process — **no flag-day cutover**. Both CAs, both
broker device-facing listeners, and all firmware variants coexist. A device that
has done Step 1 (classical cert + ML-KEM) can be OTA'd to Step 2 (ML-DSA-44 cert
+ ML-KEM) individually and reversibly.

The **bridge from the broker to AWS IoT Core stays classical** — IoT Core is a
classical-only TLS endpoint, so the broker continues to present its classical,
IoT-issued bridge certificate on the outbound bridge. Only the **device-facing**
side gains a post-quantum certificate path.

Port scheme: the classical device listener stays on **8883**; the new ML-DSA-44
device listener runs on **8884** (8883 + 1), under the same broker name / NLB
DNS endpoint.

## Requirements

### 1. Second certificate authority (ML-DSA-44)

**User story:** As the workshop operator, I want a second Private CA that signs
certificates with ML-DSA-44, running alongside the existing classical CA, so that
post-quantum-signed certificates can be issued without disturbing the classical
trust chain.

Acceptance criteria:
1. WHEN the stack is deployed THEN it SHALL create a second ACM Private CA whose
   signing algorithm is **ML-DSA-44**, in addition to the existing classical
   (ECDSA P-256) root CA.
2. The system SHALL keep the existing classical CA unchanged; both CAs SHALL
   coexist permanently.
3. The ML-DSA-44 CA SHALL be created as a ROOT CA (per ACM PCA ML-DSA support).
4. WHERE a from-scratch deploy runs, the system SHALL provision both CAs with no
   manual step, and teardown SHALL clean up both.

### 2. Dual-certificate generation & registration

**User story:** As a device (and as the broker), I want a certificate issued from
BOTH CAs, so I can present either a classical or an ML-DSA-44 identity depending
on which listener I connect to.

Acceptance criteria:
1. WHEN a device is registered THEN the registration process SHALL generate TWO
   device certificates — one signed by the classical CA and one by the ML-DSA-44
   CA — for the same device identity.
2. WHEN the broker is created THEN the system SHALL generate TWO broker server
   certificates — one from each CA.
3. The system SHALL store/track both certificates per device and per broker so
   the correct cert is available to the correct listener/firmware.
4. The classical certificate path SHALL remain fully functional for devices that
   have not migrated to Step 2.

### 3. Firmware pipeline & device code (third variant)

**User story:** As the workshop, I want a firmware variant that does hybrid
ML-KEM key exchange AND presents/trusts the ML-DSA-44 certificate, buildable and
OTA-deliverable per device, so a fielded device can complete Step 2 over the air.

Acceptance criteria:
1. The system SHALL provide a THIRD firmware variant (a new/updated `.ino`
   sketch) that performs hybrid ML-KEM key exchange AND presents an ML-DSA-44
   device certificate and trusts the ML-DSA-44 CA. (Step 1's PQC firmware does
   ML-KEM with a *classical* cert.)
2. The per-device firmware build SHALL accept this new variant (extending the
   `FIRMWARE_VARIANT` selection beyond `classical`/`pqc`) and build it with the
   device's ML-DSA-44 certificate injected.
3. WHEN a Step-2 migration is triggered THEN the firmware SHALL be delivered over
   the EXISTING OTA process (the migration Step Function / publish-ota path),
   targeting the device's second (ML-DSA-44) listener endpoint.
4. The wolfSSL configuration SHALL have ML-DSA sign/verify enabled (it already
   has ML-KEM), and the resulting firmware SHALL fit the ESP32-S3 memory budget.
5. The system SHALL verify the S3 presigned-URL OTA path works for REPEATED OTAs
   from the same firmware bucket, so a device that already did Step 1 can be
   OTA'd again for Step 2 (and again later).

### 4. Broker: second listener + NLB + bridge

**User story:** As a migrated device, I want to connect to the broker on a
post-quantum port that presents an ML-DSA-44 server certificate, while classical
devices keep using the original port and the broker keeps bridging to AWS IoT
Core classically.

Acceptance criteria:
1. The broker (Mosquitto) SHALL run TWO device-facing listeners in parallel: the
   existing classical-cert listener on **8883** and a new listener on **8884**
   presenting the **ML-DSA-44** server certificate.
2. Both listeners SHALL operate under the SAME broker name and NLB DNS endpoint.
3. The Network Load Balancer SHALL gain an ADDITIONAL listener and target group
   for **8884**, forwarding TCP (passthrough, no TLS termination) to the broker
   task, alongside the existing 8883 listener/target group.
4. The broker's OUTBOUND bridge to AWS IoT Core SHALL continue to use its
   CLASSICAL IoT-issued certificate (IoT Core is classical-only) — the bridge
   SHALL NOT be changed to ML-DSA-44.
5. The broker security group SHALL allow inbound TCP on both 8883 and 8884.
6. A classical device on 8883 and an ML-DSA-44 device on 8884 SHALL both connect
   and flow telemetry/commands simultaneously, with no disruption to either.

### 5. End-to-end & migration path

Acceptance criteria:
1. WHEN a device completes Step 2 THEN its key exchange AND its certificate SHALL
   both be post-quantum (hybrid ML-KEM + ML-DSA-44) — the terminal state of the
   Classical TLS → PQC algorithms → PQC certificates path.
2. The migration SHALL be per-device and observable (consistent with Step 1),
   with the dashboard able to reflect a device's certificate state.
3. The workshop deploy/teardown, Builder-Pi provisioning, and validation
   (`check_setup_status.sh`) SHALL continue to pass from a from-scratch build
   with both CAs and both listeners present.

## Out of scope (this iteration)

- Attendee click-by-click challenge steps for Section 6 (documented separately
  once the implementation lands).
- Deprecating/retiring the classical path (parallel coexistence is the goal).
- Any change to the ML-KEM key-exchange behaviour delivered in Step 1.

## Open questions (to resolve in design)

- ACM PCA ML-DSA-44 exact API/parameters (signing algorithm enum, template) and
  region availability in the workshop region (us-east-1).
- How the device's two certs are stored/keyed (registry schema) and how the
  build selects the ML-DSA-44 cert for the third variant.
- Whether the ML-DSA-44 broker cert is issued the same way as the classical one
  (Lambda custom resource + Secrets Manager) and how the entrypoint mounts both.
- ESP32-S3 memory headroom for ML-KEM + ML-DSA together; wolfSSL settings deltas.
- Dashboard representation of certificate state (Step 1 vs Step 2).
