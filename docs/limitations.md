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

## ~~Bootloader has no integrity checking~~ (fixed in Iteration 8)

The serial bootloader frame now computes and verifies an on-chip hardware
CRC-8 checksum ($P(x) = x^8 + x^2 + x + 1$, poly `0x07`, init `0x00`) over
the 8-bit word count and all 16-bit program words. If the checksum mismatches
or if the host deasserts `LOAD_REQ` prematurely, the hardware asserts
`boot_err = 1` on `uo_out[1]`, asserts `boot_done = 1` on `uo_out[0]`
(`uo_out = 0x03`), and permanently halts the core without executing corrupted
code. Formal invariant verified in SymbiYosys; 5/5 unit tests in
`test/test_bootload.py`.

## `program_ram.v` read port is combinational (Architecturally Verified)

This keeps the one-instruction-per-cycle timing model simple and deterministic (no fetch
pipeline hazards or branch misprediction bubbles to reason about). Evaluated in
Iteration 17: In Tiny Tapeout, no hard SRAM macros are present, so memory compiles into
flip-flops (`$_DFFE_PP_`) and multiplexer trees regardless of read registration.
At the 10 MHz operating target ($100\,\text{ns}$ clock period), the worst-case combinational
read path delay is $< 12\,\text{ns}$ across 19-20 logic levels, leaving $> 80\,\text{ns}$
of positive timing slack (`docs/ppa.md`). Retaining the combinational read is therefore
an intentional and proven architectural choice.

## ~~No UART/SPI/I2C firmware yet~~ (UART TX/RX, SPI Master, and I2C Master verified)

UART TX and RX firmware via parameterized assembly (`tools/uart_model.py`)
are fully implemented and verified in `test/test_uart.py` across edge cases
(0x00, 0xFF, 0x55, 0xAA) and pseudorandom bytes at multiple bit periods (4, 8,
16 cycles/bit) against independent software models (`UartReceiver` and `UartTransmitter`).
SPI Master (all 4 modes, full-duplex) and I2C Master (START, STOP, ACK/NACK,
clock-stretching via `WAITEDGE`, and arbitration loss detection via `GRD`) are also fully verified.

### ~~UART RX quantization jitter limitation~~ (fixed in Iteration 9)
Pure software bit-banged UART RX originally required polling the line for a start-bit
falling edge, which created up to 3 cycles of unavoidable quantization jitter.
**Fixed in Iteration 9**: Using `WAITEDGE R3, pin`, the hardware edge detector halts PC
advancement on the exact cycle of the falling edge, achieving **0 cycles of quantization jitter**.
The firmware then positions the mid-bit sampling points at $1.5P, 2.5P, \dots, 8.5P$
with single-cycle precision using `WAIT` and `SHIFTIN`, and samples the stop bit at $9.5P$,
reporting framing errors (`0xFE` in `R2`) and rejecting false-start glitches (`0xFF` in `R2`).

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
