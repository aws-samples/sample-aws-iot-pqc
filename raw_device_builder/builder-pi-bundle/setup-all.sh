#!/bin/bash
# ─── Builder-Pi Complete Setup ───────────────────────────────────────────────
# Runs all setup steps in order. Execute with sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "═══════════════════════════════════════════════════════════════"
echo "  Builder-Pi — Complete Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""

echo "Step 1: SSM Agent..."
"$SCRIPT_DIR/setup-ssm.sh"
echo ""

echo "Step 2: Builder Application..."
"$SCRIPT_DIR/setup-builder-pi.sh"
echo ""

echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Setup complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  GUI: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
