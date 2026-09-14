# ISA v1

Status: **actively evolving**, not yet the final competition ISA. v0 proved
the fetch/decode/execute/GPIO/wait pipeline end-to-end with a real
differential test. v1 adds the two things v0 was most conspicuously missing:
a genuinely reprogrammable program store (a serial bootloader, replacing the
`$readmemh`-fixed ROM) and bit-serial `SHIFTOUT`/`SHIFTIN` instructions,
which real UART/SPI-style firmware needs. See `orchestrator/queue.md` for
what's next (event/sync instructions, multi-lane coordination).

The authoritative implementations are `src/core.v` (hardware),
`tools/isa_model.py` (independent Python reference model) and
`tools/assembler.py` (assembler). All three must agree; `test/test.py`
differentially tests the RTL against the Python model specifically to catch
drift between them.

## Registers

Four 8-bit general-purpose registers: `R0`-`R3`.

One 1-bit flag: `Z` (zero flag), set by every instruction that produces a
register result (`LDI`, `MOV`, `ADDI`, `SUBI`, `ANDI`, `ORI`, `XORI`, `GRD`,
`DECJNZ`).

Program counter `PC`: 8 bits (256-word address space in this encoding; the
physical ROM in v0 is also sized 256 x 16 bits - see
`docs/limitations.md` for why this will change before synthesis/PPA work).

## Instruction word format

Fixed 16-bit instruction width:

```text
 15         11 10  9 8              1 0
+-------------+-----+----------------+---+
|   opcode    |  rd |    operand     | - |
|   (5 bits)  |(2b) |    (8 bits)    |(1)|
+-------------+-----+----------------+---+
```

- `opcode` (bits 15:11): selects the instruction, see table below.
- `rd` (bits 10:9): destination/source register index (0-3). Always present
  in the encoding; ignored by opcodes that don't need it.
- `operand` (bits 8:1): reinterpreted per-opcode as an 8-bit immediate, an
  8-bit jump address, or (for `MOV`) a source register index in its low 2
  bits.
- bit 0: reserved, must be 0 in v0.

## Opcode table

| Opcode | Mnemonic | Operands   | Effect                                                              |
|-------:|----------|------------|----------------------------------------------------------------------|
|      0 | `NOP`    | -          | No state change.                                                    |
|      1 | `LDI`    | rd, imm8   | `rd = imm8`; `Z = (imm8 == 0)`.                                     |
|      2 | `MOV`    | rd, rs     | `rd = rs`; `Z = (rs == 0)`. `rs` is `operand[1:0]`.                 |
|      3 | `ADDI`   | rd, imm8   | `rd = rd + imm8` (mod 256); `Z` = result is zero.                   |
|      4 | `SUBI`   | rd, imm8   | `rd = rd - imm8` (mod 256); `Z` = result is zero.                   |
|      5 | `ANDI`   | rd, imm8   | `rd = rd & imm8`; `Z` = result is zero.                             |
|      6 | `ORI`    | rd, imm8   | `rd = rd \| imm8`; `Z` = result is zero.                            |
|      7 | `XORI`   | rd, imm8   | `rd = rd ^ imm8`; `Z` = result is zero.                             |
|      8 | `GDIRI`  | imm8       | GPIO direction register (`uio_oe`) = imm8 (1 = output).             |
|      9 | `GDIR`   | rd         | GPIO direction register = `rd`.                                    |
|     10 | `GWRI`   | imm8       | GPIO output register (`uio_out`) = imm8.                            |
|     11 | `GWR`    | rd         | GPIO output register = `rd`.                                       |
|     12 | `GRD`    | rd         | `rd` = synchronized GPIO input (`uio_in`, 2-cycle delay); `Z` set.  |
|     13 | `WAIT`   | imm8       | Stall exactly `imm8 + 1` cycles before the next instruction issues. |
|     14 | `JMP`    | addr8      | `PC = addr8`.                                                       |
|     15 | `JZ`     | addr8      | `PC = addr8` if `Z`, else `PC = PC + 1`.                            |
|     16 | `JNZ`    | addr8      | `PC = addr8` if `!Z`, else `PC = PC + 1`.                           |
|     17 | `DECJNZ` | rd, addr8  | `rd = rd - 1`; `Z` set; `PC = addr8` if `rd != 0`, else `PC + 1`.   |
|     18 | `HALT`   | -          | Core stops fetching/executing until the next reset.                |
|     19 | `SHIFTOUT` | rd, pin  | Drive `rd[0]` onto GPIO bit `pin` (operand[2:0]); `rd = rd >> 1` (zero-fill). `Z` set. |
|     20 | `SHIFTIN`  | rd, pin  | `rd = {sampled bit, rd[7:1]}` (sampled bit from synchronized GPIO bit `pin`). `Z` set. |
|     21 | `WAITEDGE` | rd, imm8 | Stall until GPIO edge; `rd = elapsed_cycles` (autobaud/timing discovery). Mode 3: timestamp (`rd = cycle_cnt[7:0]`). `Z` set. |

Opcodes 22-31 are reserved/illegal in v1 and currently behave as `NOP`
(documented, not asserted-against - see `docs/limitations.md`).

### WAITEDGE Encoding & Operands
`imm8` layout for `WAITEDGE rd, imm8`:
- `operand[2:0]` (`pin`): GPIO pin index 0–7.
- `operand[4:3]` (`mode`):
  - `00` (`0x00 | pin`): Falling edge wait (1 -> 0). Measures elapsed cycles until edge.
  - `01` (`0x08 | pin`): Rising edge wait (0 -> 1). Measures elapsed cycles until edge.
  - `10` (`0x10 | pin`): Any edge wait (0 -> 1 or 1 -> 0).
  - `11` (`0x18`): Free-running timestamp capture. Immediately writes `cycle_cnt[7:0]` into `rd` without stalling.

`SHIFTOUT`/`SHIFTIN` are deliberately paired so that N back-to-back
`SHIFTOUT`s (LSB of `rd` first) followed by N `SHIFTIN`s on the receiving
side reconstruct the original byte exactly - see `src/core.v`'s comment at
the `OP_SHIFTOUT`/`OP_SHIFTIN` cases for the bit-ordering proof. This is the
same LSB-first convention UART uses, and is what makes these two
instructions sufficient (with `WAIT` for timing) to bit-bang real byte
oriented protocols in firmware.

## Timing model

One instruction issues per clock cycle, with the sole exception of `WAIT`,
which holds the core for `imm8 + 1` total cycles before the following
instruction issues. This determinism (no caches, no variable-latency
memory, no pipelining) is intentional: protocol bit-banging needs exactly
predictable cycle counts.

## GPIO model

v0 maps the programmable protocol GPIO bus onto Tiny Tapeout's bidirectional
`uio[7:0]` pins only:

- `GDIR`/`GDIRI` drive `uio_oe` (1 = output).
- `GWR`/`GWRI` drive `uio_out`.
- `GRD` reads `uio_in` through a 2-flop synchronizer (`src/gpio.v`), so the
  value seen by firmware lags the physical pin by two clock cycles. This is
  a deliberate, real-silicon-appropriate design choice, not a bug - see
  `docs/verification.md` for how the reference model reproduces it exactly.

`ui_in` (dedicated inputs) and `uo_out` (dedicated outputs) are unused
placeholders in v0 - see `docs/limitations.md`.

## Bootloader protocol

Program memory (`src/program_ram.v`) is a real, writable RAM. Immediately
after reset, `src/core.v` runs a small bootloader FSM, entirely over the
same `uio[7:0]` bus the protocol engine uses for everything else - no
dedicated pins, no `$readmemh`. This is what makes the chip reprogrammable
after fabrication (the actual competition requirement v0 did not meet - see
`orchestrator/decisions.md`).

Wire assignment during an active load (host-driven):

- `uio[0]` - `LOAD_REQ` (level; the host must hold this at 1 for the entire
  load, and can hold it at 0 to skip loading and run whatever program is
  already in RAM - e.g. after a warm reset during iterative testing).
- `uio[1]` - `LOAD_CLK` (host-driven bit clock; one rising edge per bit).
- `uio[2]` - `LOAD_DATA` (serial data, valid while `LOAD_CLK` is asserted).

Frame format: an 8-bit word count (MSB first), then that many 16-bit
instruction words (each MSB first, matching the instruction encoding above).
There is no checksum/integrity check in v1 - see `docs/limitations.md`.

Timing: the GPIO input synchronizer (`src/gpio.v`) is a 2-flop
synchronizer, and the bootloader FSM additionally waits for it to settle
before sampling `LOAD_REQ` for the first time. A host must therefore:

1. Assert `LOAD_REQ` (and hold `LOAD_CLK`/`LOAD_DATA` at 0) at least 3 clock
   cycles before sending the first bit.
2. Hold each bit's value stable, then hold `LOAD_CLK` high, then low, for at
   least 2 clock cycles each phase, before changing to the next bit.

See `test/bootload.py` for a reference implementation (used by the cocotb
test suite itself to load every test program - there is no `$readmemh` path
left anywhere in the test suite either).

If `LOAD_REQ` drops before the full frame is received, the bootloader
aborts (best-effort: whatever words were already written stay in RAM) and
hands off to execution immediately - documented, not a soft/graceful error
recovery path.

## Known gaps (tracked in `orchestrator/queue.md`)

- No event/synchronization instructions yet - needed for any future
  multi-lane architecture.
- `JZ`/`JNZ` only test the global `Z` flag (set by the most recent
  flag-setting instruction), not an arbitrary register directly; this keeps
  the encoding simple but firmware must plan around it (e.g. via `ANDI`
  before a conditional branch).
- The bootloader frame has no checksum/integrity check, and a checksum
  failure/short frame is not signaled back to the host in any way.
- `program_ram.v`'s read port is combinational, not synchronous - a
  synthesis/PPA-mapping consideration (a real SRAM macro is typically
  synchronous-read), not a reprogrammability one. Tracked in
  `orchestrator/queue.md`.
