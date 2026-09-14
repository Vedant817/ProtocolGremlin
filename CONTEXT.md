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

- **Phase:** ISA v0 bootstrap, verified.
- **What exists:** a minimal core (19 opcodes, 4 registers, GPIO bus on
  Tiny Tapeout's `uio[7:0]`) in `src/`, an assembler and independent Python
  reference model in `tools/`, and one differential cocotb test
  (`test/test.py`).
- **What's verified:** `test/test.py` passes - the RTL and the Python model
  agree on every architectural signal for all 50 cycles of
  `firmware/loop_demo.asm`. That is currently the *only* test.
- **What's NOT done yet:** UART/SPI/I2C firmware, formal verification,
  synthesis/PPA data, a reprogrammable (non-$readmemh) program memory. Full,
  honest list: `docs/limitations.md`.
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
- Program memory is a `$readmemh`-initialized ROM in v0 - known to conflict
  with "reprogrammable after fabrication" and queued as P0 follow-up.
- Verification is differential-first: RTL vs. an independently written
  Python ISA model, cycle by cycle, not just "the simulation runs".

## What to work on next

Full prioritized backlog: `orchestrator/queue.md`. Current P0s:

1. Replace `src/program_rom.v` with a serially loaded, synchronous-read
   program RAM.
2. Rename the placeholder top module (`tt_um_change_me_protocol_emulator`)
   once the real GitHub username is known.
3. Add SHIFT/SHIFTOUT/SHIFTIN instructions (needed before generic
   UART/SPI firmware can be written).

## Keeping this file current

After completing ANY feature, task, or nontrivial change, before you
consider the work done:

1. Update "Current status" above to reflect what changed.
2. Update "What to work on next" if priorities shifted.
3. Keep it short - this is a summary. Full detail belongs in
   `orchestrator/decisions.md`, `orchestrator/experiments.jsonl`, and `docs/`.

A stale `CONTEXT.md` defeats its entire purpose. This requirement is also
recorded in `AGENTS.md`.
