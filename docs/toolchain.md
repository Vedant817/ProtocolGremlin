# Toolchain

## Requirement

RTL simulation needs Icarus Verilog (and eventually Verilator), Yosys
(synthesis), and cocotb + pytest (Python-driven testbenches). Formal
verification (SymbiYosys) and the Tiny Tapeout/LibreLane physical flow are
queued for later milestones (`orchestrator/queue.md`).

## Environment

Developed against WSL2 Ubuntu 24.04 on Windows. `sudo apt-get install` was
not available in the working environment (no root/sudo password), so the
toolchain is installed **without root**, via
[Miniforge](https://github.com/conda-forge/miniforge) (conda pinned to
conda-forge). This also happens to make the environment fully reproducible
and independent of the host distro's package versions.

If you *do* have sudo access, `sudo apt-get install -y iverilog verilator
yosys` plus `pip install -r test/requirements.txt` is an equally valid,
simpler alternative - `scripts/setup_env.sh` is not required in that case.

## One-time setup

```bash
bash scripts/setup_env.sh
```

This:

1. Downloads and installs Miniforge to `~/miniforge3` (skipped if already
   present).
2. Creates a conda environment named `asic` with `python=3.11`, `iverilog`,
   `verilator`, and `yosys` from conda-forge (skipped if it already exists).
3. `pip install`s `test/requirements.txt` (`cocotb==2.0.1`,
   `pytest==8.4.2`) into that environment.

Re-running is safe.

## Running the regression

```bash
bash scripts/regress.sh
```

This activates the `asic` conda environment and runs `make` in `test/`
(the Tiny Tapeout cocotb Makefile), which assembles the current firmware,
elaborates the RTL with Icarus Verilog, and runs `test/test.py`.

## Manual activation

```bash
source ~/miniforge3/etc/profile.d/conda.sh
conda activate asic
cd test && make
```

## Verified toolchain versions (recorded at bootstrap time)

See `orchestrator/experiments.jsonl` for the exact versions captured the
first time `scripts/setup_env.sh` succeeded in this environment.
