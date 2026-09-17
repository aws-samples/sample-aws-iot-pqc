#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# package-workshop-src.sh — build a clean source zip for the Workshop Studio
# assets bucket. The Code Editor CloudFormation template (provisioned by
# Workshop Studio into the operator/central account) pulls this zip into the
# operator IDE home folder so the operator opens the IDE with the repo already
# present and can run cloudformation/deploy-workshop.sh from there.
#
# Uses `git archive` so the zip contains ONLY git-tracked files — .git, .venv,
# node_modules, secrets.h, build output and caches are excluded automatically
# (they are all in .gitignore), with no hand-maintained exclude list to drift.
#
# Usage:
#   ./package-workshop-src.sh                 # archive committed HEAD (default)
#   ./package-workshop-src.sh --working-tree  # include uncommitted tracked changes
#   ./package-workshop-src.sh --out /path/to/pqc-workshop-src.zip
#
# After building, upload the zip to the Workshop Studio assets bucket (via the
# WS admin UI) as: pqc-workshop-src.zip
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PREFIX="Sample-aws-iot-pqc/"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO_ROOT/pqc-workshop-src.zip"
MODE="head"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --working-tree) MODE="working"; shift ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[ERROR] Unknown arg: $1"; echo "Run $0 --help for usage."; exit 1 ;;
  esac
done

cd "$REPO_ROOT"
command -v git >/dev/null || { echo "[ERROR] git not found"; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "[ERROR] not a git repo"; exit 1; }

rm -f "$OUT"

if [[ "$MODE" == "working" ]]; then
  # Archive the working tree (tracked files incl. uncommitted edits) by
  # creating a temporary tree object without touching your working copy.
  # `git stash create` returns empty when there is nothing to stash, so fall
  # back to HEAD in that case.
  echo "[*] Archiving working tree (tracked files, incl. uncommitted)..."
  TREE=$(git stash create || true)
  TREE=${TREE:-HEAD}
  git archive --format=zip --prefix="$PREFIX" -o "$OUT" "$TREE"
else
  echo "[*] Archiving committed HEAD ($(git rev-parse --short HEAD))..."
  # Warn if there are uncommitted changes that WON'T be in the zip.
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "[WARN] Uncommitted changes exist and will NOT be included in the zip."
    echo "       Commit them first, or re-run with --working-tree to include them."
  fi
  git archive --format=zip --prefix="$PREFIX" -o "$OUT" HEAD
fi

# Belt-and-braces: drop Python bytecode caches from the zip. These SHOULD be
# excluded by .gitignore, but if any __pycache__/*.pyc files were committed
# before the ignore rule existed, git still tracks them and `git archive`
# includes them. Strip them post-archive so the workshop zip is always clean
# regardless of the repo's tracking state. (`zip -d` no-ops harmlessly when
# there is nothing to delete.)
if command -v zip >/dev/null 2>&1; then
  zip -q -d "$OUT" '*/__pycache__/*' '*.pyc' >/dev/null 2>&1 || true
fi

SIZE=$(du -h "$OUT" | cut -f1)
COUNT=$(unzip -l "$OUT" 2>/dev/null | tail -1 | awk '{print $2}')
echo "[✓] Wrote $OUT  (${SIZE}, ${COUNT} files)"
echo "    Upload this to the Workshop Studio assets bucket as: pqc-workshop-src.zip"
