# CONTEXT.md — Start Here

This is the single-page onboarding summary for this repository. Read this
first, before `PROJECT_MASTER_PLAN.md` or anything else in `AGENTS.md`'s
reading list - it should get any new agent or contributor oriented in under
a minute. Full detail always lives in the linked files; this file is a
pointer/summary, not a duplicate.

**This file must be updated after every completed feature or task.** See
"Keeping this file current" at the bottom - that's not optional.

## What this is

An open-source, general-purpose, programmable protocol-emulator ASIC built
for the Jane Street / Tiny Tapeout IHP 130nm CMOS5L competition (deadline
January 18, 2027). Full brief: `PROJECT_MASTER_PLAN.md`.

## Current status

- **Phase:** ISA v1 complete — all 6 iterations complete (I2C Master, hardware open-drain, SPI Master, MSB shifts, UART, WAITEDGE, PVFI formal, mutation testing, fuzzer, synthesis).
- **What exists:**
  1. **Core:** 24 opcodes, 4 registers, bidirectional GPIO bus on `uio[7:0]`,
     `SHIFTOUT`/`SHIFTIN` with MSB/LSB direction select (`imm8[3]`), `WAITEDGE`
     hardware edge-detect and cycle-capture timing discovery backed by a 32-bit cycle counter,
     and `GODRI`/`GODR` hardware open-drain primitives for contention-free open-collector buses.
  2. **Reprogrammability:** On-chip serial bootloader FSM writing to a true
     writable program RAM (`src/program_ram.v`) over `uio[0:2]`. No `$readmemh`
     anywhere in the RTL or testbench.
  3. **Formal Trace Port:** PVFI (Protocol-engine Verification Formal Interface)
     RVFI-style per-cycle retirement bus gated under `ifdef PVFI (zero silicon overhead).
  4. **Formal Harness:** SymbiYosys (`formal/core.sby`) with Z3 SMT solver proving
     reset convergence, PC bounds, WAIT termination, halt permanence, counter
     monotonicity, PVFI interface correctness, and open-drain electrical isolation (20 steps, 0 violations).
  5. **Firmware & Decoders:** Parameterized bit-banged UART TX (`tools/uart_model.py`),
     full-duplex SPI Master supporting all 4 modes ($CPOL \in \{0,1\}, CPHA \in \{0,1\}$) (`tools/spi_model.py`),
     and I2C Master write/read transactions with compact `DECJNZ` loops (`tools/i2c_model.py`)
     paired with independent `UartReceiver`, `SpiSlave`, and `I2cSlave` verification models.
  6. **Mutation Testing:** Standalone harness (`scripts/mutate.py`) testing 11
     architectural fault categories, measuring **100.0% kill rate (11/11 killed)**
     (citing Huang et al. 2015, Firefly 2025).
  7. **Constrained-Random Fuzzing:** Automated instruction fuzzer (`tools/fuzzer.py`)
     with delta-debugging program shrinker, verified in `test/test_fuzz.py`.
  8. **Real PPA Baseline:** Mapped with Yosys 0.69+ (`scripts/synth.sh`), measuring
     19,243 CMOS cells (37,736 GE). Active processor logic is only 1,486 cells
     (~2.1 kGE) with 92.3% of cells in the synthesized flip-flop RAM matrix.
     Fits the 8x4 competition tile footprint with >80 ns timing slack at 10 MHz.
- **What's verified:** 19/19 test suites pass cleanly via `scripts/regress.sh`:
  (1) cycle-by-cycle differential test (`test/test.py`), (2) UART TX edge-case
  verification (`0x00`, `0xFF`, `0x55`, `0xAA` at 4, 8, 16 cycles/bit),
  (3) UART TX pseudorandom frames, (4) isolated ALU/register unit tests,
  (5) isolated branch/loop unit tests, (6) isolated GPIO/shift unit tests,
  (7) WAITEDGE single-cycle pulse width measurement (5, 11, 23, 47 cycles),
  (8) cycle counter timestamp capture, (9) WAITEDGE differential test vs Python model,
  (10) constrained-random fuzzing (10 iterations differential vs Python model),
  (11) delta-debugging shrinker unit test,
  (12) SPI Mode sweep (Modes 0, 1, 2, 3),
  (13) SPI full-duplex simultaneous bidirectional transfer (0x7E tx / 0x42 rx),
  (14) SPI edge-case and random frame verification,
  (15) I2C Master single-byte write with slave ACK and STOP detection,
  (16) I2C Master multi-byte EEPROM write with ordered data latching,
  (17) I2C Master single-byte read with Master NACK and bus release,
  (18) I2C unresponsive slave NACK detection,
  (19) I2C electrical open-drain contention prevention proof.
- **Git:** Sequence of small, reviewable commits (`git log`).

## Repository map

```text
src/            RTL: project.v (TT wrapper), core.v, alu.v, gpio.v, program_ram.v
firmware/       Assembly programs (loop_demo.asm)
tools/          assembler.py, isa_model.py, uart_model.py, spi_model.py, i2c_model.py, fuzzer.py
test/           cocotb test suite (test, test_uart, test_opcodes, test_waitedge, test_fuzz, test_spi, test_i2c)
formal/         SymbiYosys formal harness (core.sby, core_formal.v)
scripts/        setup_env.sh, regress.sh, mutate.py, synth.sh, synth.ys
docs/           architecture, ISA, verification, toolchain, PPA, limitations
orchestrator/   Durable state (decisions.md, queue.md, metrics.json, experiments.jsonl)
```

## Get productive in 2 minutes

```bash
bash scripts/setup_env.sh   # one-time toolchain install (see docs/toolchain.md)
bash scripts/regress.sh     # runs all 19 cocotb regression tests (~13s)
sby -f formal/core.sby      # runs SymbiYosys formal verification with Z3
python3 scripts/mutate.py   # runs RTL mutation testing campaign (11/11 killed)
bash scripts/synth.sh       # runs Yosys synthesis and outputs cell/area metrics
```

## Key decisions (full log: `orchestrator/decisions.md`)

- Built on the real `TinyTapeout/ttihp-verilog-template` (`cmos5l` branch).
- No sudo/root required; toolchain runs from Miniforge/conda-forge.
- Programmable protocol GPIO mapped to bidirectional `uio[7:0]`.
- Genuinely reprogrammable program RAM loaded via serial bootloader.
- Multi-tiered verification: differential cycle-by-cycle testing, SymbiYosys formal
  safety proofs, independent protocol decoders, 100% mutation kill rate, and
  constrained-random fuzzing with automated shrinking.
- Synthesized PPA measured: active core is 1,486 cells (~2.1 kGE), RAM is 17,757 cells.

## What to work on next

Full prioritized backlog: `orchestrator/queue.md`. Entering continuous loop:

1. Iteration 7: I2C clock stretching & arbitration detection with `WAITEDGE`.
2. Iteration 8: Bootloader CRC-8 frame checksum and error reporting.
3. Iteration 9: UART RX firmware with start-bit synchronization using `WAITEDGE`.
4. Iteration 10+: 1-Wire, JTAG TAP, SWD line reset, Dual-lane architecture (`core_dual.v`).

## Keeping this file current

After completing ANY feature, task, or nontrivial change, before you
consider the work done:

1. Update "Current status" above to reflect what changed.
2. Update "What to work on next" if priorities shifted.
3. Keep it short - this is a summary. Full detail belongs in
   `orchestrator/decisions.md`, `orchestrator/experiments.jsonl`, and `docs/`.

A stale `CONTEXT.md` defeats its entire purpose. This requirement is also
recorded in `AGENTS.md`.
