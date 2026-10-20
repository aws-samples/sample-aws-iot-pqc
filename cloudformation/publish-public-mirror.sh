#!/usr/bin/env bash
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────
# publish-public-mirror.sh — publish a CURATED, scrubbed snapshot of this repo
# to the public aws-samples GitHub mirror.
#
# This repo lives on an INTERNAL GitLab (origin) and is mirrored to a PUBLIC
# GitHub repo (aws-samples). The public copy must NOT contain internal-only
# content: the `.kiro/` working notes, real AWS account IDs, the workshop WiFi
# PSK, or internal GitLab URLs. This script reproduces the exact curation that
# was first done by hand, so refreshing the mirror after internal `main` moves
# is one command instead of a manual, error-prone sequence.
#
# HOW IT WORKS (safe by construction):
#   1. Requires a clean working tree and refuses to run from a detached HEAD.
#   2. Snapshots the CURRENT branch (default: whatever you have checked out,
#      normally `main`) onto a throwaway temp branch — your real branch is never
#      modified.
#   3. On the temp branch: removes `.kiro/`, scrubs account IDs + PSK, and
#      rewrites the internal clone URL to the public GitHub URL.
#   4. Runs a fail-closed safety sweep: if ANY configured secret/internal marker
#      survives the scrub, it ABORTS before pushing.
#   5. Force-pushes the temp branch to the public remote's branch (the public
#      history is intentionally a curated snapshot, not a shared branch, so a
#      forced update is expected and correct).
#   6. Always returns you to your original branch and deletes the temp branch,
#      even on failure.
#
# Your internal `origin` is NEVER pushed to by this script.
#
# Usage:
#   ./cloudformation/publish-public-mirror.sh [options]
#
# Options:
#   --remote NAME        Public git remote name            (default: aws-samples)
#   --remote-url URL     If the remote is missing, add it with this URL
#                        (default: https://github.com/aws-samples/sample-aws-iot-pqc.git)
#   --branch NAME        Public branch to publish to        (default: main)
#   --source REF         Source ref to snapshot             (default: current branch)
#   --account-id ID      Real AWS account id to scrub (repeatable)
#                        (defaults: 087195661091, 569757034460)
#   --placeholder-account ID   Replacement account id       (default: 123456789012)
#   --wifi-psk VALUE     Real WiFi PSK literal to scrub      (default: @reinvent2026)
#   --placeholder-psk V  Replacement PSK text    (default: CHANGE_ME_workshop_wifi_password)
#   --dry-run            Do everything EXCEPT the force-push; leaves the temp
#                        branch in place for inspection, prints the diff stat.
#   --yes                Skip the interactive confirmation (for automation/CI).
#                        Can also be set with CONFIRM_PUBLISH=yes.
#   -h, --help           Show this help.
#
# Exit codes: 0 success; non-zero on any failure (fail-closed before push).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REMOTE="aws-samples"
REMOTE_URL="https://github.com/aws-samples/sample-aws-iot-pqc.git"
PUB_BRANCH="main"
SOURCE_REF=""
PLACEHOLDER_ACCOUNT="123456789012"
WIFI_PSK="@reinvent2026"
PLACEHOLDER_PSK="CHANGE_ME_workshop_wifi_password"
DRY_RUN="no"
ASSUME_YES="${CONFIRM_PUBLISH:-no}"
ACCOUNT_IDS=()
INTERNAL_URL_RE='gitlab\.aws\.dev'
PUBLIC_CLONE_URL="https://github.com/aws-samples/sample-aws-iot-pqc.git"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote) REMOTE="$2"; shift 2 ;;
    --remote-url) REMOTE_URL="$2"; shift 2 ;;
    --branch) PUB_BRANCH="$2"; shift 2 ;;
    --source) SOURCE_REF="$2"; shift 2 ;;
    --account-id) ACCOUNT_IDS+=("$2"); shift 2 ;;
    --placeholder-account) PLACEHOLDER_ACCOUNT="$2"; shift 2 ;;
    --wifi-psk) WIFI_PSK="$2"; shift 2 ;;
    --placeholder-psk) PLACEHOLDER_PSK="$2"; shift 2 ;;
    --dry-run) DRY_RUN="yes"; shift ;;
    --yes) ASSUME_YES="yes"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[ERROR] Unknown arg: $1"; echo "Run $0 --help for usage."; exit 1 ;;
  esac
done

# Default account IDs to scrub if none supplied.
if [[ ${#ACCOUNT_IDS[@]} -eq 0 ]]; then
  ACCOUNT_IDS=(087195661091 569757034460)
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

command -v git >/dev/null || { echo "[ERROR] git not found"; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "[ERROR] not a git repo"; exit 1; }

# ── Preconditions ────────────────────────────────────────────────────────────
ORIG_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$ORIG_BRANCH" == "HEAD" ]]; then
  echo "[ERROR] Detached HEAD. Check out a branch (e.g. main) and retry."
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[ERROR] Working tree has uncommitted changes. Commit or stash first."
  exit 1
fi
[[ -n "$SOURCE_REF" ]] || SOURCE_REF="$ORIG_BRANCH"

# Ensure the public remote exists (add it if missing).
if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "[*] Remote '$REMOTE' not found — adding $REMOTE_URL"
  git remote add "$REMOTE" "$REMOTE_URL"
fi

TMP_BRANCH="public-mirror-build-$$"

cleanup() {
  # Always return to the original branch and drop the temp branch. Because the
  # snapshot is committed onto the temp branch before any sweep, the index is
  # clean here and nothing bleeds across; the checkout is a plain branch switch.
  git checkout -q "$ORIG_BRANCH" 2>/dev/null || true
  git branch -q -D "$TMP_BRANCH" 2>/dev/null || true
}
trap cleanup EXIT

echo "═══════════════════════════════════════════════════════════════"
echo "  Publish curated PUBLIC mirror"
echo "═══════════════════════════════════════════════════════════════"
echo "  Source ref:     $SOURCE_REF"
echo "  Public remote:  $REMOTE ($(git remote get-url "$REMOTE"))"
echo "  Public branch:  $PUB_BRANCH"
echo "  Scrub accounts: ${ACCOUNT_IDS[*]} -> $PLACEHOLDER_ACCOUNT"
echo "  Scrub PSK:      $WIFI_PSK -> $PLACEHOLDER_PSK"
echo "  Dry run:        $DRY_RUN"
echo ""

# ── Build the curated snapshot on a throwaway branch ─────────────────────────
echo "[1/5] Creating temp branch $TMP_BRANCH from $SOURCE_REF..."
git checkout -q -b "$TMP_BRANCH" "$SOURCE_REF"

echo "[2/5] Removing internal-only files (.kiro/ + this publisher script) ..."
if git ls-files --error-unmatch .kiro >/dev/null 2>&1; then
  git rm -q -r .kiro
else
  echo "        (.kiro not tracked — nothing to remove)"
fi
# This publisher script is INTERNAL tooling and carries the real account IDs /
# PSK as scrubbing defaults — it must never appear on the public mirror.
if git ls-files --error-unmatch cloudformation/publish-public-mirror.sh >/dev/null 2>&1; then
  git rm -q cloudformation/publish-public-mirror.sh
fi

echo "[3/5] Scrubbing account IDs, WiFi PSK, and internal URLs ..."
# Account IDs — scrub across every tracked text file that contains one.
for acct in "${ACCOUNT_IDS[@]}"; do
  git grep -lIF "$acct" 2>/dev/null > /tmp/mirror_acct_$$.txt || true
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    sed -i '' -e "s/${acct}/${PLACEHOLDER_ACCOUNT}/g" "$f"
  done < /tmp/mirror_acct_$$.txt
  rm -f /tmp/mirror_acct_$$.txt
done
# WiFi PSK.
git grep -lIF "$WIFI_PSK" 2>/dev/null > /tmp/mirror_psk_$$.txt || true
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  sed -i '' -e "s/${WIFI_PSK}/${PLACEHOLDER_PSK}/g" "$f"
done < /tmp/mirror_psk_$$.txt
rm -f /tmp/mirror_psk_$$.txt
# Internal GitLab clone URL -> public GitHub URL (handles the esp32-flash README
# and any other internal clone references).
git grep -lIE "https://${INTERNAL_URL_RE}[^ )\"']*" 2>/dev/null > /tmp/mirror_url_$$.txt || true
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  # Replace any https://gitlab.aws.dev/....git clone URL with the public one.
  sed -i '' -E "s#https://gitlab\.aws\.dev/[^ )\"']*\.git#${PUBLIC_CLONE_URL}#g" "$f"
done < /tmp/mirror_url_$$.txt
rm -f /tmp/mirror_url_$$.txt

# Stage everything (including the .kiro deletion and all scrubs) and COMMIT it
# onto the temp branch immediately. Committing before the sweep is deliberate:
# it means the index is clean afterward, so an abort can never leave staged
# changes that bleed onto the original branch when the cleanup trap switches
# back. A bad snapshot simply dies with the temp branch.
git add -A
git commit -q -m "chore: curate public snapshot for aws-samples mirror" || {
  echo "[WARN] Nothing to commit — snapshot already matches source (no curation delta?)."
}

echo "[4/5] Safety sweep (fail-closed) ..."
# The committed snapshot must contain NOTHING sensitive. Any hit aborts before
# we ever contact the public remote; the temp branch is discarded on exit.
SWEEP_FAIL=0
sweep() {
  local label="$1" pattern="$2" hits
  hits=$(git grep -nIE "$pattern" 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    echo "  [FAIL] $label still present:"
    echo "$hits" | head -10 | sed 's/^/        /'
    SWEEP_FAIL=1
  else
    echo "  [ok]   $label — clean"
  fi
}
ACCT_ALT=$(IFS='|'; echo "${ACCOUNT_IDS[*]}")  # nosemgrep: ifs-tampering -- scoped/intentional: IFS is set only inside this command-substitution subshell to join the array with '|'; the parent shell's IFS is unaffected
# .kiro path check — ls-files is authoritative for presence (a content regex
# would false-positive on benign .kiro/ references in .gitignore/.gitattributes).
if git ls-files .kiro/ | grep -q .; then
  echo "  [FAIL] .kiro/ files still tracked on snapshot"; SWEEP_FAIL=1
else
  echo "  [ok]   .kiro/ removed"
fi
# The publisher script must be gone from the snapshot (it carries the real
# account IDs/PSK as scrubbing defaults).
if git ls-files cloudformation/publish-public-mirror.sh | grep -q .; then
  echo "  [FAIL] publisher script still on snapshot"; SWEEP_FAIL=1
else
  echo "  [ok]   publisher script removed"
fi
sweep "real account IDs"    "$ACCT_ALT"
sweep "WiFi PSK"            "$(printf '%s' "$WIFI_PSK" | sed 's/[.[\*^$]/\\&/g')"
sweep "internal GitLab URL" "$INTERNAL_URL_RE"

if [[ "$SWEEP_FAIL" -ne 0 ]]; then
  echo ""
  echo "[ABORT] Safety sweep found internal/sensitive content. NOT pushing."
  echo "        The temp branch '$TMP_BRANCH' is discarded on exit; your"
  echo "        original branch '$ORIG_BRANCH' is untouched."
  exit 2
fi

echo ""
echo "  Curation diff vs $SOURCE_REF:"
git -P diff --stat "$SOURCE_REF"..HEAD | tail -20 | sed 's/^/    /'
echo ""

# ── Push ─────────────────────────────────────────────────────────────────────
if [[ "$DRY_RUN" == "yes" ]]; then
  echo "[5/5] DRY RUN — skipping force-push."
  echo "        Temp branch '$TMP_BRANCH' retained for inspection."
  echo "        (Run 'git checkout $ORIG_BRANCH && git branch -D $TMP_BRANCH' to clean up.)"
  # Disarm cleanup so the user can inspect the temp branch.
  trap - EXIT
  git checkout -q "$ORIG_BRANCH"
  echo "        Returned to $ORIG_BRANCH."
  exit 0
fi

if [[ "$ASSUME_YES" != "yes" ]]; then
  echo "[5/5] About to FORCE-PUSH this curated snapshot to:"
  echo "        $REMOTE/$PUB_BRANCH  ($(git remote get-url "$REMOTE"))"
  echo "      This OVERWRITES the public branch history. Your internal 'origin'"
  echo "      is NOT touched."
  printf "      Type 'publish' to continue: "
  read -r reply
  [[ "$reply" == "publish" ]] || { echo "[ABORT] Not confirmed."; exit 1; }
fi

echo "[*] Force-pushing $TMP_BRANCH -> $REMOTE/$PUB_BRANCH ..."
git push --force "$REMOTE" "$TMP_BRANCH:$PUB_BRANCH"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Public mirror updated: $REMOTE/$PUB_BRANCH"
echo "     $(git remote get-url "$REMOTE")"
echo "═══════════════════════════════════════════════════════════════"
# cleanup trap returns us to ORIG_BRANCH and deletes the temp branch.
