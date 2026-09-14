# Architecture (v0 bootstrap)

## Goal

An open-source, general-purpose, programmable protocol-emulator ASIC for
the Jane Street / Tiny Tapeout IHP 130nm CMOS5L competition
(`PROJECT_MASTER_PLAN.md`). The chip must remain reprogrammable *after*
fabrication so that UART/SPI/I2C (and later JTAG/SWD/PS2/CAN/USB/Ethernet)
can be implemented as firmware rather than fixed peripherals.

## Current shape

```text
                 Tiny Tapeout wrapper (src/project.v)
                              |
                        src/core.v
     (fetch / decode / execute + serial bootloader FSM, ISA v1)
                    |            |          |
              src/alu.v   src/gpio.v   src/program_ram.v
             (combinational   (uio bus,     (writable RAM,
              ADD/SUB/AND/     2-flop        loaded by core.v's
              OR/XOR)          input sync)   bootloader; see
                                              docs/isa.md)
```

- **Single core, single lane.** The master plan raises multi-lane
  architectures (independent protocol engines synchronized through a shared
  event fabric) as a promising differentiator, but that is an architectural
  decision that should be driven by PPA evidence from this working baseline,
  not decided upfront. Tracked as a Level-5 research item in
  `orchestrator/queue.md`.
- **One instruction per cycle**, no pipelining. Simplicity first; revisit
  only if synthesis/timing data demands it (`docs/ppa.md`).
- **GPIO bus = `uio[7:0]` only** (Tiny Tapeout's true bidirectional pins).
  `ui_in`/`uo_out` are currently unused placeholders - see
  `docs/limitations.md` for why and what they might become (auxiliary
  inputs, debug/trace output, a second lane's pins).

## Why the Tiny Tapeout `cmos5l` template, unmodified where possible

`info.yaml`, `src/config.json`, `test/Makefile`, `.devcontainer/`, and
`.vscode/` are copied verbatim (or nearly so) from
`TinyTapeout/ttihp-verilog-template` (`cmos5l` branch) so that Tiny Tapeout's
own CI/precheck/GDS flow keeps working as the project grows. Only
`info.yaml`'s project metadata, `top_module` name, `tiles`, `source_files`,
and `pinout` sections were edited.

## Data flow for a typical instruction

1. `program_rom` combinationally returns the 16-bit word at `pc`.
2. `core` decodes `opcode`/`rd`/`operand` (`docs/isa.md`).
3. On the same cycle, `alu` computes any arithmetic/bitwise result and
   `gpio`'s synchronizer independently advances by one stage.
4. On the next clock edge, `core` commits register/PC/GPIO register updates.

## Observability & Reverse-Engineering Architecture: PVFI & WAITEDGE

A central differentiator of this architecture directly addresses Jane Street's
"consider what you'd do differently" prompt regarding RP2040 PIO:

### RP2040 PIO Opacity vs. PVFI
The independent RP2040 PIO emulator project documentation explicitly notes that
real RP2040 PIO state machines cannot be traced, single-stepped, or inspected on
hardware at all. In this core, we introduce **PVFI** (Protocol-engine Verification
Formal Interface), an RVFI-inspired per-cycle retirement interface. PVFI exposes:
- `pvfi_valid`: Asserts whenever an instruction legitimately retires.
- `pvfi_order`: Monotonic 32-bit execution sequence counter.
- `pvfi_insn`: 16-bit instruction word executing.
- `pvfi_pc_rdata` / `pvfi_pc_wdata`: Architectural PC before and after update.
- `pvfi_rd_addr`, `pvfi_rd_wdata`, `pvfi_rd_we`: Destination register writeback stream.
- `pvfi_gpio_oe`, `pvfi_gpio_wdata`, `pvfi_gpio_rdata`: Full cycle-by-cycle GPIO bus state.
- `pvfi_cycle`: 32-bit free-running hardware cycle counter.

In taped-out silicon, PVFI ports are conditioned out (zero pin/area overhead); in
simulation and formal verification (`-DPVFI`), they provide complete observability
and serve as the formal harness boundary.

### WAITEDGE: Hardware Edge Synchronization & Pulse Capture
In software bit-banging, detecting external edges requires polling (`GRD`/`ANDI`/`JNZ`),
which incurs 3 cycles of loop quantization jitter. At tight bit periods ($P \le 8$),
3 cycles corresponds to 37.5%–75% phase error.

The `WAITEDGE rd, imm8` instruction provides single-cycle hardware edge wait:
- Detects rising, falling, or toggle edges on any selected GPIO pin.
- While waiting, an internal counter counts elapsed clock cycles.
- Upon edge arrival, the exact elapsed pulse width is captured into register `rd`.
- Mode 3 provides an instantaneous 8-bit timestamp capture from the free-running counter.

This turns the protocol engine into an active protocol reverse-engineering
and autobaud timing discovery engine.

## Verification-driven development

Per `PROJECT_MASTER_PLAN.md` section 8, verification is treated as at least
as important as the RTL itself:
1. Differential verification: RTL vs. pure-Python `tools/isa_model.py` cycle-by-cycle.
2. Formal verification: SymbiYosys (`formal/core.sby`) with SMT-BMC using Z3.
3. Independent protocol verification: Parameterized UART TX verified against asynchronous `UartReceiver`.
4. Dedicated opcode isolated unit tests.
