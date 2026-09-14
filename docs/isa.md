# ISA v0

Status: **bootstrap slice**, not the final competition ISA. This exists to
prove the fetch/decode/execute/GPIO/wait pipeline end-to-end with a real
differential test before investing in the full instruction set (shift/
shiftout primitives, event/sync instructions, multi-lane coordination) that
UART/SPI/I2C firmware will need. See `orchestrator/queue.md` for what's next.

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

Opcodes 19-31 are reserved/illegal in v0 and currently behave as `NOP`
(documented, not asserted-against - see `docs/limitations.md`).

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

## Known gaps (tracked in `orchestrator/queue.md`)

- No shift/rotate/shift-in/shift-out instructions yet - needed before real
  UART/SPI/I2C bit-banging firmware can be written generically.
- No event/synchronization instructions - needed for any future multi-lane
  architecture.
- `JZ`/`JNZ` only test the global `Z` flag (set by the most recent
  flag-setting instruction), not an arbitrary register directly; this keeps
  the encoding simple but firmware must plan around it (e.g. via `ANDI`
  before a conditional branch).
