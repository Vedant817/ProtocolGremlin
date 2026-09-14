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

- **Phase:** ISA v1, iteration 3 of a 4-iteration novelty & verification
  push complete (see `orchestrator/decisions.md` "Iteration 3" for details).
- **What exists:** a core (22 opcodes, 4 registers, GPIO bus on Tiny
  Tapeout's `uio[7:0]`) in `src/`, with a **genuinely reprogrammable
  program RAM loaded by an on-chip serial bootloader** (no `$readmemh`
  anywhere - see `docs/isa.md` "Bootloader protocol"), `SHIFTOUT`/
  `SHIFTIN` bit-serial instructions, and a dedicated `WAITEDGE` edge-capture
  timing discovery instruction backed by a 32-bit cycle counter. Debug
  builds include a PVFI trace port gated under `ifdef PVFI (zero tapeout
  pin overhead). SymbiYosys formal harness (`formal/core.sby`) with Z3
  proves safety invariants over 20 cycles. Assembler, independent Python
  reference model, and asynchronous software UART decoder model live in `tools/`.
- **What's verified:** 9/9 test suites pass cleanly via `scripts/regress.sh`:
  (1) cycle-by-cycle differential test (`test/test.py`), (2) UART TX edge-case
  verification (`0x00`, `0xFF`, `0x55`, `0xAA` at 4, 8, 16 cycles/bit),
  (3) UART TX pseudorandom frames, (4) isolated ALU/register unit tests,
  (5) isolated branch/loop unit tests, (6) isolated GPIO/shift unit tests,
  (7) WAITEDGE single-cycle pulse width measurement (5, 11, 23, 47 cycles),
  (8) cycle counter timestamp capture, (9) WAITEDGE differential test vs Python model.
  SymbiYosys 20-step bounded model check passes with 0 violations.
- **What's NOT done yet (iteration 4):** Mutation testing harness (`scripts/mutate.py`),
  constrained-random fuzzer with automated shrinking, and real Yosys synthesis/PPA data.
- **Git:** history is being built as a sequence of small, reviewable
  commits (see `git log`) rather than one large commit.

## Repository map

```text
src/            RTL: project.v (TT wrapper), core.v, alu.v, gpio.v, program_rom.v
firmware/       Assembly programs (loop_demo.asm)
tools/          assembler.py, isa_model.py (Python reference model)
test/           cocotb differential test + Tiny Tapeout template files
formal/         Formal verification harness (not yet populated)
scripts/        setup_env.sh (toolchain), regress.sh (run tests)
docs/           architecture, ISA, verification, toolchain, PPA, limitations
orchestrator/   Durable state for the continuous engineering loop (see below)
```

## Get productive in 2 minutes

```bash
bash scripts/setup_env.sh   # one-time, no sudo required (see docs/toolchain.md)
bash scripts/regress.sh     # assembles firmware, runs the cocotb test suite
```

## Key decisions (full log: `orchestrator/decisions.md`)

- Built on the real `TinyTapeout/ttihp-verilog-template` (`cmos5l` branch),
  not a custom layout, so Tiny Tapeout's own CI/precheck/GDS flow keeps
  working.
- No sudo/root available in the working environment, so the RTL toolchain
  is installed via Miniforge/conda-forge, not `apt-get`.
- The programmable GPIO bus is mapped to `uio[7:0]` only (TT's only true
  bidirectional pins); `ui_in`/`uo_out` are unused placeholders for now.
- Program memory is a genuinely writable RAM (`src/program_ram.v`), loaded
  by an on-chip serial bootloader over the same `uio` bus - fixed in ISA v1
  after v0 shipped a `$readmemh`-fixed ROM that could not actually be
  reprogrammed after fabrication.
- Verification is differential-first: RTL vs. an independently written
  Python ISA model, cycle by cycle, not just "the simulation runs".

## What to work on next
 
 Full prioritized backlog: `orchestrator/queue.md`. Immediate next
 (iteration 4 of the 4-iteration plan in `orchestrator/decisions.md`):
 
 1. Seeded mutation testing harness (`scripts/mutate.py`) targeting `src/core.v`
    and measuring test suite kill rate (citing Huang et al. 2015 & Firefly 2025).
 2. Constrained-random instruction fuzzer (`tools/fuzzer.py` / `test/test_fuzz.py`)
    with automated shrink-on-failure comparing RTL against `tools/isa_model.py`.
 3. Real Yosys synthesis pass for IHP 130nm / standard cell target, capturing
    actual cell count, gate count, and mapped area in `docs/ppa.md` and `info.yaml`.

## Keeping this file current

After completing ANY feature, task, or nontrivial change, before you
consider the work done:

1. Update "Current status" above to reflect what changed.
2. Update "What to work on next" if priorities shifted.
3. Keep it short - this is a summary. Full detail belongs in
   `orchestrator/decisions.md`, `orchestrator/experiments.jsonl`, and `docs/`.

A stale `CONTEXT.md` defeats its entire purpose. This requirement is also
recorded in `AGENTS.md`.
