# Verification strategy

Status: v0 bootstrap. This documents what verification exists today and
what's queued next; see `PROJECT_MASTER_PLAN.md` section 8 for the full
long-term strategy (unit tests per block, firmware-level protocol tests,
randomized/constrained tests, formal, gate-level, differential, mutation
testing).

## What exists now: differential testing

`test/test.py` is a cocotb testbench that:

1. Assembles `firmware/loop_demo.asm` with `tools/assembler.py`.
2. Runs the resulting program through `tools/isa_model.py`
   (`CoreModel`), an independently written, cycle-accurate Python
   interpreter of ISA v0.
3. Runs the *same* program on the RTL (`src/core.v` via `src/project.v`
   through the Tiny Tapeout `tb.v` wrapper), stepping one clock cycle at a
   time.
4. After every single clock cycle, asserts that the RTL and the Python
   model agree on: `pc`, `r0`-`r3`, the `z` flag, `halted`, and the
   externally observable `uio_oe`/`uio_out` pins.

This is deliberately a whitebox test: it reaches into `core`'s internal
registers via cocotb's hierarchical signal access
(`dut.user_project.u_core.<signal>`) rather than adding debug ports to the
fixed Tiny Tapeout pinout. See `src/core.v`'s header comment.

Two independently written implementations of the same spec (`docs/isa.md`)
agreeing on every single cycle of a program that exercises 15 of the 19
opcodes is meaningfully stronger evidence than either "the RTL simulation
doesn't crash" or "the Python model looks right" alone. If the RTL and
model ever disagree, that's a genuine bug in one of them (or in the shared
spec understanding) - not a flaky test.

## Running it

```bash
bash scripts/setup_env.sh   # one-time toolchain install (see docs/toolchain.md)
bash scripts/regress.sh     # assembles firmware, runs the cocotb suite
```

## Known verification debt (see `orchestrator/queue.md` for priority order)

- Only one hand-written program is exercised. No randomized/constrained
  instruction stream fuzzing yet, and no per-opcode isolated unit tests.
- No coverage measurement - we do not yet know which opcodes/paths are
  actually exercised versus merely believed to be exercised.
- No formal verification (SymbiYosys) yet - no PC-in-range, reset,
  wait-terminates, or GPIO-invariant proofs exist yet.
- No mutation testing yet - we have not yet demonstrated that this test
  would actually catch an injected bug (e.g. an inverted branch condition
  or an off-by-one in `WAIT`).
- No gate-level simulation yet (no synthesis has run).
- UART/SPI/I2C-specific protocol verification does not exist yet (no
  firmware for those protocols exists yet either).

Do not describe any of the above as "verified" until the corresponding test
exists and passes; `docs/limitations.md` tracks this explicitly.
