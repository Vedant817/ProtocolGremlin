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

- **Phase:** ISA v1, iteration 1 of a 4-iteration novelty & verification
  push complete (see `orchestrator/decisions.md` "Novelty & verification
  research" for the plan and its research grounding).
- **What exists:** a core (21 opcodes, 4 registers, GPIO bus on Tiny
  Tapeout's `uio[7:0]`) in `src/`, with a **genuinely reprogrammable
  program RAM loaded by an on-chip serial bootloader** (no `$readmemh`
  anywhere - see `docs/isa.md` "Bootloader protocol") and `SHIFTOUT`/
  `SHIFTIN` bit-serial instructions for byte-oriented protocol firmware.
  An assembler and independent Python reference model live in `tools/`.
- **What's verified:** `test/test.py` passes - the program is loaded
  entirely via the real bootloader protocol (`test/bootload.py`), then the
  RTL and the Python model agree on every architectural signal for all 63
  cycles of `firmware/loop_demo.asm` (now exercising `SHIFTOUT`/`SHIFTIN`
  too). That is currently the *only* test file.
- **What's NOT done yet (iterations 2-4, not started):** UART/SPI/I2C
  firmware, a hardware trace/edge-capture novelty feature, formal
  verification, mutation testing, synthesis/PPA data. Full, honest list:
  `docs/limitations.md`.
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
(iteration 2 of the 4-iteration plan in `orchestrator/decisions.md`):

1. UART TX (and RX if time allows) firmware using `SHIFTOUT`/`WAIT`,
   verified against an independent Python UART model.
2. Per-opcode isolated unit tests (only indirect coverage exists today).
3. Rename the placeholder top module (`tt_um_change_me_protocol_emulator`)
   once the real GitHub username is known.

Iteration 3 (novel differentiator) and iteration 4 (mutation testing, real
PPA) are planned but not started - see `orchestrator/decisions.md` and
`orchestrator/queue.md` P1/P2.

## Keeping this file current

After completing ANY feature, task, or nontrivial change, before you
consider the work done:

1. Update "Current status" above to reflect what changed.
2. Update "What to work on next" if priorities shifted.
3. Keep it short - this is a summary. Full detail belongs in
   `orchestrator/decisions.md`, `orchestrator/experiments.jsonl`, and `docs/`.

A stale `CONTEXT.md` defeats its entire purpose. This requirement is also
recorded in `AGENTS.md`.
