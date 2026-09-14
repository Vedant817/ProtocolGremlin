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

## ~~No UART/SPI/I2C firmware yet~~ (UART TX implemented and verified)

UART TX bit-banged firmware via parameterized assembly (`tools/uart_model.py`)
is fully implemented and verified in `test/test_uart.py` across edge cases
(0x00, 0xFF, 0x55, 0xAA) and pseudorandom bytes at multiple bit periods (4, 8,
16 cycles/bit) against an independent software UART receiver model. SPI and
I2C remain queued.

### UART RX status & quantization jitter finding
Pure software bit-banged UART RX requires polling the line for a start-bit
falling edge. On ISA v1, the tightest polling loop (`poll: GRD R1; ANDI R1, 1;
JNZ poll`) takes 3 cycles per iteration. This creates up to 3 cycles of
unavoidable quantization jitter between the actual edge and software detection.
At fast bit periods ($P \le 8$ cycles), 3 cycles of jitter represents
37.5%–75% of a bit period, severely degrading sampling margin. This concrete
empirical finding directly motivated Iteration 3's `WAITEDGE` hardware
primitive (single-cycle hardware edge wait + timestamp capture).

## `ui_in` / `uo_out` are unused

Only `uio[7:0]` is wired to the programmable GPIO bus. `ui_in` and
`uo_out` are tied off/reserved. Candidate future uses (not yet decided):
auxiliary trigger/event inputs, debug/trace output, or pins for a second
lane in a multi-lane architecture.

## No synthesis / PPA data yet

No Yosys synthesis run has happened yet. Area, cell count, and timing are
all unknown. `clock_hz: 10000000` in `info.yaml` is a placeholder, not a
result of static timing analysis. `docs/ppa.md` will be updated in Iteration 4
once real synthesis numbers exist.

## ~~No formal verification yet~~ (SymbiYosys formal harness proven)

Core invariants are now formally proven with SymbiYosys (`formal/core.sby`) using
Z3: reset convergence, PC range safety, WAIT deterministic countdown and
termination, halt permanence, cycle counter monotonicity, bootloader FSM
absorbency, and PVFI retirement integrity.

## Reserved/illegal opcodes are unspecified-but-not-asserted

Opcodes 22-31 currently behave as `NOP` in both the RTL and the Python
model (by falling through to a `default` case). A fuzzed instruction stream
could execute a reserved opcode without noticing divergence from intent.

## ~~Single test, single program~~ (Expanded 9-test regression suite)

The regression suite (`scripts/regress.sh`) now executes 9 distinct test suites covering:
1. Full cycle-by-cycle differential verification against Python ISA model (`test/test.py`).
2. Real UART TX edge cases (0x00, 0xFF, 0x55, 0xAA) across multiple baud rates (`test/test_uart.py`).
3. Real UART TX randomized data with fixed seeds (`test/test_uart.py`).
4. ALU and register isolated unit tests (`test/test_opcodes.py`).
5. Branching, loops, and condition code unit tests (`test/test_opcodes.py`).
6. GPIO and bit-serial shift isolated unit tests (`test/test_opcodes.py`).
7. WAITEDGE pulse measurement across random unknown pulse widths (`test/test_waitedge.py`).
8. WAITEDGE free-running timestamp capture (`test/test_waitedge.py`).
9. WAITEDGE cycle-by-cycle differential verification with Python model (`test/test_waitedge.py`).

## Top module name is a placeholder

`tt_um_change_me_protocol_emulator` in `src/project.v` / `info.yaml` /
`test/tb.v` must be renamed to include the real GitHub username before
Tiny Tapeout submission (their uniqueness requirement).
