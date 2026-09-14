# Known limitations (v0 bootstrap)

This is an honest list of what is *not yet true* about this project, kept
up to date so nothing here is ever claimed as done without evidence
(`PROJECT_MASTER_PLAN.md` section 18, "quality rules").

## Program memory is not reprogrammable after fabrication

`src/program_rom.v` is a `$readmemh`-initialized array with a combinational
read. This is adequate for RTL/gate-level simulation and an early Yosys
mapping, but:

- It cannot actually be loaded with new firmware on fabricated silicon,
  which directly conflicts with the competition's core requirement
  ("reprogrammable after fabrication").
- Combinational-read memory arrays typically do not map cleanly onto a
  dense SRAM macro during place-and-route.

**Follow-up (P0 in `orchestrator/queue.md`):** replace with a serially
loaded, synchronous-read program RAM before any milestone is treated as
representative of the final PPA/tapeout-readiness story.

## No UART/SPI/I2C firmware yet

The required initial protocols (UART, SPI, I2C) have not been implemented
as firmware. ISA v0 also lacks shift/shift-out instructions that make
generic byte shifting practical; see `docs/isa.md` "Known gaps".

## `ui_in` / `uo_out` are unused

Only `uio[7:0]` is wired to the programmable GPIO bus in v0. `ui_in` and
`uo_out` are tied off/reserved. Candidate future uses (not yet decided):
auxiliary trigger/event inputs, debug/trace output, or pins for a second
lane in a multi-lane architecture.

## No synthesis / PPA data yet

No Yosys synthesis run has happened yet. Area, cell count, and timing are
all unknown. `clock_hz: 10000000` in `info.yaml` is a placeholder, not a
result of static timing analysis. `docs/ppa.md` will be updated once real
numbers exist.

## No formal verification yet

None of the properties listed in `PROJECT_MASTER_PLAN.md` section 8.4 have
been proven yet (PC-in-range, reset convergence, wait-terminates, GPIO
output-enable safety, etc).

## Reserved/illegal opcodes are unspecified-but-not-asserted

Opcodes 19-31 currently behave as `NOP` in both the RTL and the Python
model (by falling through to a `default` case), but this has not been
turned into an explicit formal or test assertion. A random/fuzzed
instruction stream could currently execute a reserved opcode without any
test noticing whether RTL and model still agree by coincidence or by
matching intent.

## Single test, single program

`test/test.py` currently exercises exactly one hand-written program. See
`docs/verification.md` "Known verification debt" for the full list of
missing test categories.

## Top module name is a placeholder

`tt_um_change_me_protocol_emulator` in `src/project.v` / `info.yaml` /
`test/tb.v` must be renamed to include the real GitHub username before
Tiny Tapeout submission (their uniqueness requirement).
