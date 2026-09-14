#!/usr/bin/env bash
# Sets up a no-sudo-required RTL/verification toolchain inside WSL (or any Linux host)
# using Miniforge (conda-forge) so it works without root/apt access.
#
# Installs: python, icarus verilog, verilator, yosys, cocotb, pytest.
#
# Usage:
#   bash scripts/setup_env.sh
#
# Re-running is safe/idempotent.
set -euo pipefail

MINIFORGE_DIR="${MINIFORGE_DIR:-$HOME/miniforge3}"
ENV_NAME="${ENV_NAME:-asic}"
INSTALLER_URL="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
INSTALLER_PATH="/tmp/miniforge_installer.sh"

if [ ! -d "$MINIFORGE_DIR" ]; then
  echo "==> Downloading Miniforge installer..."
  curl -fsSL "$INSTALLER_URL" -o "$INSTALLER_PATH"
  echo "==> Installing Miniforge to $MINIFORGE_DIR (no sudo required)..."
  bash "$INSTALLER_PATH" -b -p "$MINIFORGE_DIR"
  rm -f "$INSTALLER_PATH"
else
  echo "==> Miniforge already present at $MINIFORGE_DIR"
fi

source "$MINIFORGE_DIR/etc/profile.d/conda.sh"

if ! conda env list | grep -qE "^\s*${ENV_NAME}\s"; then
  echo "==> Creating conda environment '$ENV_NAME' with iverilog/verilator/yosys..."
  conda create -y -n "$ENV_NAME" -c conda-forge python=3.11 iverilog verilator yosys
else
  echo "==> Conda environment '$ENV_NAME' already exists"
fi

conda activate "$ENV_NAME"

echo "==> Installing Python test dependencies (cocotb, pytest)..."
pip install --quiet -r "$(dirname "$0")/../test/requirements.txt"

echo "==> Toolchain versions:"
python --version
iverilog -V | head -1
verilator --version
yosys -V
python -c "import cocotb; print('cocotb', cocotb.__version__)"

echo "==> Done. Activate with:"
echo "    source $MINIFORGE_DIR/etc/profile.d/conda.sh && conda activate $ENV_NAME"
