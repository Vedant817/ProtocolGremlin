# Known limitations (v1)

This is an honest list of what is *not yet true* about this project, kept
up to date so nothing here is ever claimed as done without evidence
(`PROJECT_MASTER_PLAN.md` section 18, "quality rules").

## ~~Program memory is not reprogrammable after fabrication~~ (fixed in v1)

v0's `src/program_rom.v` was a `$readmemh`-initialized array, fixed at
elaboration time - it could not actually be loaded with new firmware on
fabricated silicon, directly conflicting with the competition's core
requirement. **Fixed in v1**: `src/program_ram.v` is a genuinely writable
RAM, loaded by a serial bootloader FSM over the same `uio` bus the protocol
engine already uses (`docs/isa.md` "Bootloader protocol"). Two things about
that fix remain limitations in their own right (see below): no
checksum/error signaling on the load frame, and the RAM's read port is
still combinational rather than synchronous (a PPA/synthesis-mapping
concern, not a reprogrammability one).

## Bootloader has no integrity checking

The v1 serial bootloader frame (`docs/isa.md` "Bootloader protocol") has no
checksum or CRC. A corrupted or truncated load currently just runs whatever
ended up in RAM, with no error reported back to the host. Deliberately
scoped out of the v1 bootstrap to keep it reviewable - see
`orchestrator/queue.md`.

## `program_ram.v` read port is combinational

This keeps the one-instruction-per-cycle timing model simple (no fetch
pipeline hazards to reason about), but a real SRAM macro is typically
synchronous-read, so this will likely need revisiting once real synthesis
data exists (`orchestrator/queue.md`, `docs/ppa.md`).

## No UART/SPI/I2C firmware yet

The required initial protocols (UART, SPI, I2C) have not been implemented
as firmware yet. ISA v1 added the `SHIFTOUT`/`SHIFTIN` primitives real
bit-banging firmware needs (`docs/isa.md`), but no protocol firmware has
been written against them yet - queued next.

## `ui_in` / `uo_out` are unused

Only `uio[7:0]` is wired to the programmable GPIO bus. `ui_in` and
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

Opcodes 21-31 currently behave as `NOP` in both the RTL and the Python
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
