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
             (fetch / decode / execute, ISA v0)
                    |            |          |
              src/alu.v   src/gpio.v   src/program_rom.v
             (combinational   (uio bus,     (v0: readmemh-
              ADD/SUB/AND/     2-flop        initialized,
              OR/XOR)          input sync)   combinational read)
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

## Verification-driven development

Per `PROJECT_MASTER_PLAN.md` section 8, verification is treated as at least
as important as the RTL itself. The first concrete instance of that is the
differential test in `test/test.py`: the same program
(`firmware/loop_demo.asm`) is assembled once and run through both the RTL
(via cocotb) and an independently written Python model
(`tools/isa_model.py`), with an assertion after every clock cycle. See
`docs/verification.md`.

## What's deliberately deferred (see `orchestrator/queue.md`)

- Loadable (not $readmemh-fixed) program memory.
- UART/SPI/I2C firmware and the ISA extensions (shift/shift-out) they need.
- Formal verification (SymbiYosys).
- Synthesis/PPA measurement and place-and-route.
- Multi-lane architecture exploration.
