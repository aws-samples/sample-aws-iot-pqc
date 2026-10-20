#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# teardown-workshop.sh — single-command, idempotent teardown of the PQC
# workshop environment. The counterpart to deploy-workshop.sh.
#
# Deleting the `pqc-workshop` master stack cascades to all nested stacks, but a
# handful of resources are NOT cleaned up by CloudFormation and will BLOCK the
# stack deletion (or linger and break the next from-scratch deploy). This script
# removes those first, in the right order, then deletes the master stack:
#
#   1. IoT: detach principals/policies + delete every `esp32-*` device Thing,
#      then deprecate the `esp32-pqc-device` Thing Type (a Thing Type cannot be
#      deleted while Things reference it; deletion also needs a ~5-min
#      deprecation wait, so by default we deprecate and let the stack/next run
#      finish the delete).
#   2. ECS: scale the broker service to 0 so the task releases its ENI.
#   3. ECR: batch-delete all images in the workshop repos (a repo with images
#      cannot be deleted by CloudFormation).
#   4. S3: empty ALL object versions + delete-markers from the versioned
#      workshop buckets (a non-empty / versioned bucket blocks stack deletion).
#   5. CloudFormation: delete the master stack and (optionally) wait.
#   6. S3: finally empty + delete the CFN artifacts bucket (held back until
#      after the stack delete because the nested-stack templates live in it).
#
# It is SAFE TO RE-RUN: every step tolerates already-absent resources, so if a
# previous teardown half-finished (or the stack delete failed on one resource),
# just run it again.
#
# Usage:
#   ./teardown-workshop.sh <aws-region> [--profile NAME] [--wait] [--delete-thing-type] [--prune-ssm] [--yes]
#
#   <aws-region>          AWS region (default: us-east-1). Positional, like deploy.
#   --profile NAME        AWS CLI profile to target (ada-managed). Omit for ambient creds.
#   --wait                Block until the master stack reaches DELETE_COMPLETE.
#   --delete-thing-type   Also DELETE the deprecated Thing Type (requires a ~5-min
#                         wait after deprecation; off by default — deprecation
#                         alone unblocks the stack delete and the next deploy
#                         re-creates/re-uses the type).
#   --prune-ssm           Deregister stale builder-Pi SSM managed instances
#                         (ConnectionLost). The Online one is left alone.
#   --yes                 Skip the confirmation prompt (or set CONFIRM_TEARDOWN=yes).
#
# Examples:
#   ./teardown-workshop.sh us-east-1 --profile pqc-b --wait
#   CONFIRM_TEARDOWN=yes ./teardown-workshop.sh us-east-1 --profile pqc-a --wait --prune-ssm
#
# ⚠️  DESTRUCTIVE: this permanently deletes the workshop stack, its S3 bucket
#     contents, ECR images, and IoT devices in the TARGET ACCOUNT. It prints the
#     resolved account id and (unless --yes) prompts before deleting anything.
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

AWS_REGION="${1:-us-east-1}"; shift || true
PROFILE=""
DO_WAIT="false"
DELETE_THING_TYPE="false"
PRUNE_SSM="false"
ASSUME_YES="${CONFIRM_TEARDOWN:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="$2"; shift 2 ;;
        --wait) DO_WAIT="true"; shift ;;
        --delete-thing-type) DELETE_THING_TYPE="true"; shift ;;
        --prune-ssm) PRUNE_SSM="true"; shift ;;
        --yes) ASSUME_YES="yes"; shift ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done

# Target the chosen profile for every aws call (exported so nested tools inherit).
if [[ -n "$PROFILE" ]]; then
    export AWS_PROFILE="$PROFILE"
fi
export AWS_DEFAULT_REGION="$AWS_REGION"

STACK_NAME="pqc-workshop"
THING_TYPE="esp32-pqc-device"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
ARTIFACTS_BUCKET="pqc-workshop-cfn-${ACCOUNT_ID}-${AWS_REGION}"

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Environment Teardown"
echo "═══════════════════════════════════════════════════════════════"
echo "  Profile:          ${PROFILE:-<ambient/default credentials>}"
echo "  Target account:   $ACCOUNT_ID"
echo "  Caller identity:  $CALLER_ARN"
echo "  Region:           $AWS_REGION"
echo "  Stack:            $STACK_NAME"
echo "  Delete ThingType: $DELETE_THING_TYPE   Prune SSM: $PRUNE_SSM   Wait: $DO_WAIT"
echo ""
echo "  ⚠️  This DESTROYS the workshop stack + S3 contents + ECR images + IoT"
echo "      devices in account $ACCOUNT_ID. This cannot be undone."
echo ""
if [[ "$ASSUME_YES" != "yes" && -t 0 ]]; then
    read -r -p "  Tear down account $ACCOUNT_ID? [y/N] " reply
    [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "Aborted."; exit 1; }
    echo ""
fi

# ─── Helper: empty ALL object versions + delete-markers from a bucket ────────
empty_versioned_bucket() {
    local bucket="$1"
    if ! aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
        echo "    (bucket $bucket absent — skip)"
        return 0
    fi
    echo "    emptying s3://$bucket ..."
    while : ; do
        local vers payload n
        vers=$(aws s3api list-object-versions --bucket "$bucket" --max-items 500 \
            --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}, DeleteMarkers: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
            --output json 2>/dev/null || echo '{}')
        payload=$(printf '%s' "$vers" | python3 -c "
import sys, json
d = json.load(sys.stdin)
objs = (d.get('Objects') or []) + (d.get('DeleteMarkers') or [])
objs = [o for o in objs if o and o.get('Key')]
print(json.dumps({'Objects':[{'Key':o['Key'],'VersionId':o['VersionId']} for o in objs], 'Quiet':True}))
" 2>/dev/null || echo '{"Objects":[]}')
        n=$(printf '%s' "$payload" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['Objects']))" 2>/dev/null || echo 0)
        if [[ -z "$n" || "$n" == "0" ]]; then break; fi
        printf '%s' "$payload" > /tmp/_pqc_del_payload.json
        aws s3api delete-objects --bucket "$bucket" --delete file:///tmp/_pqc_del_payload.json >/dev/null 2>&1 || true
        echo "      deleted $n versions/markers"
    done
}

# ─── 1. IoT: delete esp32-* Things (detach principals + policies first) ──────
echo "[1/6] IoT devices + thing type"
THINGS=$(aws iot list-things --query "things[?starts_with(thingName,'esp32-')].thingName" --output text 2>/dev/null || true)
for t in $THINGS; do
    echo "  thing $t"
    PRINCIPALS=$(aws iot list-thing-principals --thing-name "$t" --query 'principals' --output text 2>/dev/null || true)
    for p in $PRINCIPALS; do
        certid="${p##*/}"
        for pol in $(aws iot list-attached-policies --target "$p" --query 'policies[].policyName' --output text 2>/dev/null || true); do
            aws iot detach-policy --policy-name "$pol" --target "$p" >/dev/null 2>&1 || true
        done
        aws iot detach-thing-principal --thing-name "$t" --principal "$p" >/dev/null 2>&1 || true
        aws iot update-certificate --certificate-id "$certid" --new-status INACTIVE >/dev/null 2>&1 || true
        aws iot delete-certificate --certificate-id "$certid" --force-delete >/dev/null 2>&1 || true
    done
    aws iot delete-thing --thing-name "$t" >/dev/null 2>&1 || true
done
# Deprecate the thing type (lets the stack delete it; it cannot be deleted while
# Things reference it, and a hard delete needs a ~5-min post-deprecation wait).
if aws iot describe-thing-type --thing-type-name "$THING_TYPE" >/dev/null 2>&1; then
    aws iot deprecate-thing-type --thing-type-name "$THING_TYPE" >/dev/null 2>&1 || true
    echo "  deprecated thing type $THING_TYPE"
    if [[ "$DELETE_THING_TYPE" == "true" ]]; then
        echo "  waiting 5 min for deprecation before deleting thing type..."
        sleep 300
        aws iot delete-thing-type --thing-type-name "$THING_TYPE" >/dev/null 2>&1 \
            && echo "  deleted thing type $THING_TYPE" \
            || echo "  (thing type delete deferred — stack/next run will finish it)"
    fi
fi

# ─── 2. ECS: scale the broker service to 0 ───────────────────────────────────
echo "[2/6] Broker ECS service -> desired 0"
if aws ecs describe-clusters --clusters pqc-workshop-broker --query 'clusters[0].status' --output text >/dev/null 2>&1; then
    SVC=$(aws ecs list-services --cluster pqc-workshop-broker --query 'serviceArns[0]' --output text 2>/dev/null || echo None)
    if [[ -n "$SVC" && "$SVC" != "None" ]]; then
        aws ecs update-service --cluster pqc-workshop-broker --service "$SVC" --desired-count 0 >/dev/null 2>&1 || true
        echo "  scaled $(basename "$SVC") to 0"
    else
        echo "  (no broker service — skip)"
    fi
else
    echo "  (no broker cluster — skip)"
fi

# ─── 3. ECR: batch-delete all images in the workshop repos ───────────────────
echo "[3/6] ECR images"
for repo in pqc-workshop-firmware-builder pqc-workshop-mosquitto-broker pqc-workshop-cert-issuer; do
    if aws ecr describe-repositories --repository-names "$repo" >/dev/null 2>&1; then
        ids=$(aws ecr list-images --repository-name "$repo" --query 'imageIds[*]' --output json 2>/dev/null || echo '[]')
        n=$(printf '%s' "$ids" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
        if [[ -n "$n" && "$n" != "0" ]]; then
            printf '%s' "$ids" > /tmp/_pqc_ecr_ids.json
            aws ecr batch-delete-image --repository-name "$repo" --image-ids file:///tmp/_pqc_ecr_ids.json >/dev/null 2>&1 || true
            echo "  $repo: deleted $n images"
        else
            echo "  $repo: already empty"
        fi
    else
        echo "  $repo: absent — skip"
    fi
done

# ─── 4. S3: empty the versioned workshop buckets (NOT the CFN bucket yet) ─────
echo "[4/6] Empty workshop S3 buckets"
# Discover every pqc-workshop bucket EXCEPT two classes we must NOT touch:
#   1. The CFN artifacts bucket ($ARTIFACTS_BUCKET) — holds the nested templates
#      the stack delete still needs; emptied LAST, in step 6.
#   2. The Workshop Studio assets bucket (…-assets-…) — this is NOT a deploy
#      artifact. Workshop Studio creates it when provisioning an event and
#      uploads the workshop content/source zip there for the Code Editor IDE to
#      pull. deploy-workshop.sh neither creates nor uses it. Emptying it would
#      destroy the WS-provisioned content, so it is explicitly excluded here.
# Covers both the fixed-name buckets (…-<account>) and the CFN-generated
# dashboard bucket (…-dashboardstac-…) without hardcoding the random suffix.
for b in $(aws s3api list-buckets --query "Buckets[?contains(Name,'pqc-workshop')].Name" --output text 2>/dev/null | tr '\t' '\n'); do
    [[ "$b" == "$ARTIFACTS_BUCKET" ]] && continue
    # Skip the Workshop Studio assets bucket (pqc-workshop-assets-<acct>-<region>).
    case "$b" in
        *-assets-*)
            echo "    (skipping Workshop Studio assets bucket $b — not a deploy artifact)"
            continue
            ;;
    esac
    empty_versioned_bucket "$b"
done

# ─── 5. CloudFormation: delete the master stack ──────────────────────────────
echo "[5/6] Delete master stack $STACK_NAME"
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" >/dev/null 2>&1; then
    aws cloudformation delete-stack --stack-name "$STACK_NAME"
    echo "  delete initiated"
    if [[ "$DO_WAIT" == "true" ]]; then
        echo "  waiting for DELETE_COMPLETE (this can take 10-15 min: NAT gateway + VPC)..."
        if aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" 2>/dev/null; then
            echo "  stack deleted"
        else
            echo "  [WARN] wait returned non-zero — check for DELETE_FAILED resources:"
            aws cloudformation describe-stack-events --stack-name "$STACK_NAME" \
                --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].{r:LogicalResourceId,reason:ResourceStatusReason}" \
                --output table 2>/dev/null | head -40 || true
        fi
    fi
else
    echo "  (stack $STACK_NAME absent — skip)"
fi

# ─── 6. S3: empty + delete the CFN artifacts bucket last ─────────────────────
# Only safe to remove once the stack is gone (or at least deleting): the nested
# templates live here. If --wait was not given and the stack is mid-delete, we
# still empty it (the delete already read the templates it needs at init), but
# we only DELETE the bucket itself when the stack is fully gone to avoid racing
# a retry that re-reads a template.
echo "[6/6] CFN artifacts bucket $ARTIFACTS_BUCKET"
if aws s3api head-bucket --bucket "$ARTIFACTS_BUCKET" >/dev/null 2>&1; then
    STACK_GONE="false"
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" >/dev/null 2>&1 || STACK_GONE="true"
    if [[ "$STACK_GONE" == "true" ]]; then
        empty_versioned_bucket "$ARTIFACTS_BUCKET"
        aws s3 rb "s3://$ARTIFACTS_BUCKET" >/dev/null 2>&1 \
            && echo "  deleted $ARTIFACTS_BUCKET" \
            || echo "  (could not delete $ARTIFACTS_BUCKET — remove manually if needed)"
    else
        echo "  stack still present/deleting — leaving artifacts bucket in place."
        echo "  Re-run this script after DELETE_COMPLETE (or use --wait) to remove it."
    fi
else
    echo "  (artifacts bucket absent — skip)"
fi

# ─── Optional: prune stale builder-Pi SSM managed instances ──────────────────
if [[ "$PRUNE_SSM" == "true" ]]; then
    echo "[+] Pruning stale builder-Pi SSM managed instances (ConnectionLost)"
    for mi in $(aws ssm describe-instance-information \
            --query "InstanceInformationList[?ComputerName=='builder-pi' || starts_with(ComputerName,'builder-pi')][?PingStatus=='ConnectionLost'].InstanceId" \
            --output text 2>/dev/null | tr '\t' '\n'); do
        [[ -z "$mi" ]] && continue
        aws ssm deregister-managed-instance --instance-id "$mi" >/dev/null 2>&1 \
            && echo "  deregistered stale $mi" || true
    done
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Teardown steps issued for account $ACCOUNT_ID."
if [[ "$DO_WAIT" != "true" ]]; then
    echo "  Stack deletion runs asynchronously. Check with:"
    echo "    aws cloudformation describe-stacks --stack-name $STACK_NAME${PROFILE:+ --profile $PROFILE} --region $AWS_REGION"
    echo "  Then re-run this script (or with --wait) to remove the CFN artifacts bucket."
fi
echo "═══════════════════════════════════════════════════════════════"
