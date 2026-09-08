# Orphaned / Manually-Created Resource Inventory

Read-only inventory captured from the live account **087195661091 / us-east-1**
to ground Requirement 5 (cleanup) and Requirement 4 (bring-into-IaC). No changes
were made to any resource.

"Orphan" = exists live but is **not** managed by any CloudFormation stack (no
`aws:cloudformation:stack-name`, and not present in any stack's
`describe-stack-resources`). These were created manually during debugging
sessions.

Ownership was determined authoritatively via `describe-stack-resources`
(resource tags alone were unreliable for CloudFront).

## Lambdas

| Function | Managing stack | Disposition |
|---|---|---|
| `pqc-workshop-pqc-upgrader` | **ORPHAN** | Replace with Step Functions migration workflow (Req 1). Reuses the stack-managed `pqc-workshop-device-registrar` role (no orphan role of its own). |
| `pqc-workshop-message-logger` | **ORPHAN** | Bring into IaC (Req 4.1). Defined in `dashboard-stack.yaml` source but the live one is manual. Has orphan role `pqc-workshop-message-logger`. |
| `pqc-workshop-broker-log-reader` | **ORPHAN** | Bring into IaC (Req 4.1). Has orphan role `pqc-workshop-broker-log-reader`. |
| `pqc-workshop-device-registrar` | pqc-workshop-device-builder | Keep (managed). |
| `pqc-workshop-device-deregistrar` | pqc-workshop-device-builder | Keep (managed). |
| `pqc-workshop-firmware-pusher` | pqc-workshop-device-builder | Keep (managed). |
| `pqc-workshop-telemetry-processor` | DashboardStack | Keep (managed). |
| `pqc-workshop-command-publisher` | DashboardStack | Keep (managed). |
| `pqc-workshop-auto-register` | DeviceStack | Review under Greengrass/device-provisioning cleanup. |
| `pqc-workshop-bridge-provisioner` | pqc-workshop-broker-container | Keep (managed). |
| `pqc-workshop-cert-provisioner` | pqc-workshop-broker-container | Keep (managed). |
| `pqc-workshop-broker-build-trigger` | pqc-workshop-broker-container | Keep (managed). |
| `pqc-workshop-greengrass-core-cert-provisioner` | GreengrassStack | Remove with Greengrass (Req 5.3). |
| `pqc-workshop-greengrass-core-ssm-activation` | SSMActivationStack | Preserve activation capability; decouple from Greengrass naming (Req 5.3). |

## Orphan IAM roles

| Role | Disposition |
|---|---|
| `pqc-workshop-message-logger` | Becomes stack-managed when message-logger is brought into IaC. |
| `pqc-workshop-broker-log-reader` | Becomes stack-managed when broker-log-reader is brought into IaC. |

## DynamoDB tables

| Table | Managing stack | Disposition |
|---|---|---|
| `pqc-workshop-devices` | DashboardStack (`DeviceTable`) | Keep (managed, shared backend). |
| `pqc-workshop-device-registry` | pqc-workshop-device-builder (`DeviceRegistryTable`) | Keep (managed). |
| `pqc-workshop-message-log` | **ORPHAN** | Bring into IaC (defined in `dashboard-stack.yaml` source; live one is manual). |

## Dashboard hosting (S3 + CloudFront)

| Resource | Managing stack | Disposition |
|---|---|---|
| Bucket `pqc-workshop-dashboard-087195661091` + CloudFront `E35PBBUBSD7O3C` (`d2yxjskna5y9z3` / also fronted as the v1 site) — **v1 "Device Dashboard"** | DashboardStack (`DashboardBucket` / `DashboardDistribution`) | **Remove (Req 5.1)** — redundant v1 frontend. Removal = deleting these resources from the dashboard-stack template. |
| Bucket `pqc-workshop-firmware-dashboard-087195661091` + CloudFront `E2C5AHMIRUKFJE` (`d21c2wyzdgiex7`) — **v2 "PQC Firmware Manager" (LIVE, in use)** | **ORPHAN** | **Bring into IaC (Req 5.2)** — this is the dashboard actually used; currently deployed by hand. Needs a hosting stack + build/deploy automation. |

Note: the v1 CloudFront comment is empty; the v2 CloudFront comment is
"PQC Workshop Firmware Manager Dashboard".

## AppSync / Step Functions

| Resource | State | Disposition |
|---|---|---|
| AppSync `pqc-workshop-device-api` (`ax6cgi3kdfbani34ewakdiowm4`) | DashboardStack | Keep — shared, load-bearing backend for the live v2 dashboard. |
| Step Functions | none exist | Add migration state machine (Req 1). |

## IoT topic rules (live)

`pqc_workshop_telemetry`, `pqc_workshop_lifecycle`, `pqc_workshop_message_log_dt`,
`pqc_workshop_message_log_cmd`, `pqc_workshop_auto_register`.

- `pqc_workshop_auto_register` → targets `pqc-workshop-auto-register` Lambda
  (DeviceStack). Review under device-provisioning/Greengrass cleanup — the root
  `check_setup_status.sh` expects it, but confirm it is part of the current
  auto-registration path before keeping or removing.
- The message-log rules target the orphan `pqc-workshop-message-logger` — they
  become consistent once that Lambda is in IaC.

## Summary of genuine orphans to reconcile

**Bring into IaC (needed by the live demo):**
1. `pqc-workshop-message-logger` Lambda + its role
2. `pqc-workshop-broker-log-reader` Lambda + its role
3. `pqc-workshop-message-log` DynamoDB table
4. v2 dashboard hosting: bucket `pqc-workshop-firmware-dashboard-*` + CloudFront
   `E2C5AHMIRUKFJE` (+ build/deploy automation)

**Replace (superseded by new design):**
5. `pqc-workshop-pqc-upgrader` Lambda → Step Functions migration workflow

**Remove (redundant):**
6. v1 dashboard frontend hosting: bucket `pqc-workshop-dashboard-*` + CloudFront
   `E35PBBUBSD7O3C` (currently DashboardStack-managed → delete from template)

**Remove with Greengrass cleanup (not on live data path):**
7. GreengrassStack resources incl. `pqc-workshop-greengrass-core-cert-provisioner`,
   `GreengrassBridgePolicy`; decouple SSM activation from `GreengrassThingName`.
