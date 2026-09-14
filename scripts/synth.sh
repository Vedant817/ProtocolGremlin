#!/usr/bin/env bash
# scripts/synth.sh - Run Yosys synthesis and report area/cell metrics
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_FILE="orchestrator/synth.log"
mkdir -p "$(dirname "$LOG_FILE")"

echo "==> Running Yosys synthesis..."
yosys -s scripts/synth.ys 2>&1 | tee "$LOG_FILE"
echo "==> Synthesis completed. Full log saved to $LOG_FILE"
