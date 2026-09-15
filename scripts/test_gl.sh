#!/usr/bin/env bash
# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
#
# Run Gate-Level Simulation with calibrated CMOS standard cell timing models (GATES=yes).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "================================================================================"
echo "Jane Street Protocol Emulator - Gate-Level Simulation Suite (GATES=yes)"
echo "Target: Generic CMOS Standard Cells / IHP 130nm reference with real propagation delays"
echo "================================================================================"

# Step 1: Synthesize updated gate-level netlist
echo "--> Synthesizing up-to-date gate-level netlist (test/gate_level_netlist.v)..."
bash scripts/synth.sh > /dev/null 2>&1

if [ ! -f "test/gate_level_netlist.v" ]; then
    echo "ERROR: test/gate_level_netlist.v was not generated!" >&2
    exit 1
fi

echo "--> Synthesized gate-level netlist ready ($(wc -l < test/gate_level_netlist.v) lines)."

# Step 2: Run gate-level simulation
echo "--> Executing gate-level simulation suite (test/test_gate_level.py)..."
make -C test clean > /dev/null 2>&1
make -C test GATES=yes

echo "================================================================================"
echo "Gate-level simulation PASSED: All 8 physical protocol tests verified on netlist!"
echo "================================================================================"
