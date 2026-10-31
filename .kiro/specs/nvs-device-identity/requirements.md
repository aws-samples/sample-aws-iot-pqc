# Requirements — Device identity in a dedicated NVS partition

## Goal

Move each device's identity (keys, certificates, CA roots, WiFi credentials) out
of the compiled firmware image and into a dedicated NVS partition, so that:

- every device on the same firmware variant runs the **same** app image;
- OTA images carry **no secrets**;
- identity is created when a device is plugged into the Builder-Pi and
  registered, with no serial list maintained in advance.

The workshop stays in a single AWS account (decided Sep 29; the earlier
central/attendee account split is abandoned).

## Background

Today `raw_device_builder/codebuild/buildspec.yml` issues the device certificate
with the shared cert-issuer (`shared/cert-issuer/issue_cert.py`) and compiles the
key, certificate, CA root and WiFi password into `secrets.h`. Every image is
therefore per-device and key-bearing, stored in the firmware bucket and read by
the Builder-Pi. The threat model records the consequences as T003, T011 and T013.

## Requirements

### R1 — Identity partition
1.1 The partition table SHALL add an NVS partition labelled `certs` at `0x721000`,
    size `0x10000` (64 KB), in currently unused flash after `coredump`.
1.2 The existing partitions (`nvs`, `otadata`, `ota_0`, `ota_1`, `nvs_key`,
    `coredump`) SHALL keep their offsets and sizes.
1.3 OTA SHALL never write the `certs` partition; identity SHALL survive every OTA.
1.4 Identity SHALL NOT be encrypted in this phase (accepted risk T005).

### R2 — Identity contents and format
2.1 The `certs` partition SHALL hold, as NVS **blobs** in DER form: the classical
    and ML-DSA-44 CA roots, the classical device certificate and private key, and
    the ML-DSA-44 device certificate.
2.2 The ML-DSA-44 private key SHALL be stored as its 32-byte FIPS 204 seed, IF the
    gating spike (tasks 0.2) proves wolfSSL on the device regenerates the
    matching key from it. Otherwise it SHALL be stored as a DER private-key blob.
2.3 The WiFi SSID and password and the device serial SHALL be stored as NVS
    strings. A format version SHALL be stored so firmware can reject an
    unknown layout.
2.4 The OTA signer roots SHALL NOT be stored separately: the signer certificates
    are issued by the same two CAs, so the two CA roots serve both purposes.

### R3 — Issuance at registration
3.1 Identity SHALL be generated when a device is plugged into the Builder-Pi and
    registered, by the shared cert-issuer running in CodeBuild. No list of
    serials SHALL be maintained in advance.
3.2 Registration SHALL issue BOTH device certificates (classical and ML-DSA-44)
    so later migrations need no new identity.
3.3 The issuer SHALL produce a ready-to-flash `certs` partition image.
3.4 The WiFi password SHALL reach the issuer from SSM Parameter Store, not as a
    plaintext CodeBuild environment value.
3.5 The identity image SHALL be removed from S3 once the device is flashed, and
    SHALL expire automatically if flashing never completes.

### R4 — Flashing
4.1 The Builder-Pi SHALL flash the shared app image and the device's `certs`
    image in one USB flash operation.
4.2 Re-flashing a registered device SHALL be possible without re-registering
    (for example by re-running identity issuance).

### R5 — Firmware
5.1 All three sketches (classical, PQC, ML-DSA) SHALL load identity from the
    `certs` partition at boot and SHALL NOT need identity in `secrets.h`.
5.2 If the identity is missing, malformed or an unknown format version, the
    firmware SHALL log a clear error and SHALL NOT attempt to connect.
5.3 The wolfSSL Arduino library version SHALL be pinned in the firmware-builder
    image.

### R6 — Shared images and migration
6.1 Each firmware variant SHALL be built and signed once per deployment, with no
    per-device inputs.
6.2 A dashboard migration SHALL push the prebuilt signed image without starting a
    per-device firmware build.

### R7 — Acceptance
7.1 End to end on pqc-a: plug in and register two devices; both connect and show
    on the dashboard; each migrates classical → PQC → ML-DSA over OTA with its
    identity intact.
7.2 Definition of done (project steering): destroy and rebuild pqc-b, then pqc-a,
    from scratch, with both working.

## Out of scope

Multi-account deployment (abandoned), attendee firmware pipelines, device claiming,
NVS or flash encryption, secure boot.
