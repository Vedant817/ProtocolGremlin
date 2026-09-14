#!/usr/bin/env bash
# Runs the fast RTL regression suite (cocotb + Icarus Verilog) using the
# conda toolchain environment created by scripts/setup_env.sh.
#
# Usage:
#   bash scripts/regress.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MINIFORGE_DIR="${MINIFORGE_DIR:-$HOME/miniforge3}"
ENV_NAME="${ENV_NAME:-asic}"

if [ ! -d "$MINIFORGE_DIR" ]; then
  echo "Toolchain not found at $MINIFORGE_DIR. Run scripts/setup_env.sh first." >&2
  exit 1
fi

source "$MINIFORGE_DIR/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"

cd "$REPO_ROOT/test"
make clean
make
echo "==> Test results:"
if [ -f results.xml ]; then
  grep -E "testcase|failure" results.xml || true
fi
