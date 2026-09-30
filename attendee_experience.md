This is being built for a workshop at re:Invent.  The time limit is 60 minutes, the attendees work on tables of 8 people with 1 AWS staff member as the facilitator.

The user will walk into the room and the single AWS account will already be created by the facilitator.  The AWS account will have all the AWS cloud side configuration created to facilitate the ESP-32 IoT devices connecting to AWS IOT Core.  The AWS Account will also have a dashboard app that shows the state of the IoT devices connected to it, so the attendees can see the state of their device without needing to login.  

The single account will have a builder-pi, that is a raspberry pi, that will have the tool chain for deploying the fireware v0.1.0, and it will be running the gui app for detecting and flashing devices.  The single account will have the mosquitto broker container running, that is ready for PQC and classical coinnections from the IoT devices.  The certificates will be provisioned by the Private CA, and devices and the broker will have the CA in common.

Each attendee will receive an esp-32 device.  I think I will provide the device with fireware 0.1.0 already configured, rather than trying to run the deploy using the builder-pi on the day.  The case for the device will have the name of the device labled on it so the attendee knows which device is theirs.

The AWS facilitator will do a presentation for 5 minutes on powerpoint.  the presentation will contain information about PQC, the threat and the mitigation. The talk will also talk about different techniques to accomplish the firmware update to PqC firmware at scale.

The attendee will now plug their ESP-32 device into a usb port in their laptop.  The device will use the firmware to conenct to the broker container using TLS classical.  The device will already be in the dashboard, and the attendee will be able to confirm that their devixce is is sending its data packets, including CPU temperature, memory usage, firmware version, TLS encryption algortym, and other stats for the ESP-32 devcice.  They will also view the logs, and issue sample commands to the device such as stopping the blink and restarting it.  

The AWS facilitator will now do a code walkthrough of the firmware updates.  They will explain how to implement the PQC on the device.  the builders session attendees can load Arduino on their laptop and view the code for themselves.

The attendees will no trigger a pipeline build for their specific device.  This should use a button on the dashboard.  The build will complete and be stored in S3.  The build will then trigger the device to pull the new fireware using the OTA channel and by giving it the S3 signed url to the fireware.

The ESP-32 device will download the new image to the OTA partition, reboot using the new firmware, and then reconnect to the broker with PQC.  The device will continue to send its data, and this will show the device is using PQC (hybrid ML-KEM key exchange) now.

The attendee will leave with the ESP-32 device and the source code for the project so they can do this themselves at home.

---

## Section 6 — Migrate the certificate to ML-DSA-44 (challenge / Step 2)

> This is the optional **challenge** section. Steps 1–5 above migrated the key
> *exchange* to hybrid ML-KEM (closing the "harvest now, decrypt later" risk).
> The device's **certificate** is still classical ECDSA P-256 — its signature
> could be forged by a future quantum computer. Section 6 closes that gap by
> moving the certificate to a post-quantum signature, **ML-DSA-44** (FIPS 204,
> NIST security Level 2). This is the terminal state of the migration path in
> NIST IR 8547: classical TLS → PQC key exchange → PQC certificate.

**What's already in the account.** The facilitator's account runs the broker
with **two** listeners on the same endpoint: 8883 (classical/PQC-key-exchange
cert) and **8884** (ML-DSA-44 cert), backed by **two** Private CAs. Nothing new
needs deploying — you just flash a third firmware variant.

**The attendee steps (click-by-click):**

1. Confirm your device is currently on the `pqc` firmware from Section 5 — the
   card shows a hybrid ML-KEM key exchange (e.g. `SECP256R1_MLKEM768`) and
   firmware `2.0.0`, with **Running: PQC key exchange (ML-KEM)**.
2. In your device's card, open the **firmware picker** and choose
   **`PQC + ML-DSA-44 cert`**.
3. Click **🚀 Flash**. Behind the scenes this builds a `pqc-mldsa` firmware for
   *your* device (with an ML-DSA-44 certificate issued from the ML-DSA CA) and
   pushes it over OTA — the same OTA channel you used in Section 5. Watch the
   card: **Building… → Pushing OTA… → Rebooting…**.
4. Your device reboots into the new firmware and reconnects — now on **port
   8884**, still using hybrid ML-KEM for the key exchange, and now presenting an
   **ML-DSA-44** certificate. The card updates to firmware `3.0.0` and
   **Running: PQC + ML-DSA-44 cert**.
5. (Optional) Open the device **logs** and the message view to confirm telemetry
   is flowing over the new post-quantum-authenticated connection. You can also
   roll back at any time by picking an earlier variant and clicking Flash again.

At this point both halves of your device's TLS are post-quantum: the key
exchange (ML-KEM) **and** the certificate signature (ML-DSA-44). That is a fully
quantum-resistant mutual-TLS connection to the broker.

> **How it maps to the code.** The `pqc-mldsa` variant is the
> `aws_iot_pqc_esp32_v3_mldsa` sketch; wolfSSL is built with ML-DSA verify/sign;
> the device cert is minted from the second ACM Private CA (ML-DSA-44); and the
> broker's 8884 listener presents its own ML-DSA-44 server cert. Attendees can
> open all three sketches (classical / pqc / pqc-mldsa) in Arduino to compare
> them — the `pqc` and `pqc-mldsa` sketch bodies are identical; only the cert and
> port (baked in at build time) differ.
