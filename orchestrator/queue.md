# Work Queue

Priority order within each tier is not strict; use judgement per
`orchestrator/WARP_CYCLE_PROMPT.md` step 4 ("identify the highest-value
bounded problem").

## P0

- Replace `src/program_rom.v` with a serially loaded, synchronous-read
  program RAM (the $readmemh ROM cannot be reprogrammed after fabrication -
  see `docs/limitations.md`). Add a corresponding "program load" mechanism
  to the ISA (e.g. a bootstrap sequence over the GPIO bus) and a test that
  loads a program purely through that mechanism, not via `$readmemh`.
- Rename `tt_um_change_me_protocol_emulator` to include the real GitHub
  username, once known, in `src/project.v`, `info.yaml`, and `test/tb.v`.
- Add SHIFT/SHIFTOUT/SHIFTIN instructions to ISA v0 (needed for generic
  UART/SPI byte-level firmware); update `docs/isa.md`,
  `tools/assembler.py`, `tools/isa_model.py`, and `src/core.v` together,
  and extend the differential test to exercise them.

## P1

- Run first Yosys synthesis pass; record mapped cell count/area breakdown
  in `docs/ppa.md` and `orchestrator/metrics.json`.
- Implement UART TX firmware (bit-banged via the GPIO bus) once shift
  instructions exist; verify against a Python UART receiver model
  (`PROJECT_MASTER_PLAN.md` section 8.2).
- Add per-opcode isolated unit tests (currently only exercised indirectly
  via `firmware/loop_demo.asm`).
- Add a randomized instruction-stream fuzzer that runs against both the
  RTL and `tools/isa_model.py` and reports/minimizes any mismatch.

## P2

- Formal verification harness under `formal/` (SymbiYosys): start with
  "PC stays in valid range" and "reset reaches a known state".
- Mutation testing: deliberately invert a branch condition / off-by-one a
  WAIT counter / etc, confirm the differential test (or a strengthened
  version of it) catches it. Measure and record detection rate.
- SPI and I2C firmware once UART is solid.
- Explicit test coverage for reserved/illegal opcodes 19-31 (currently
  silently behave as NOP, untested - see `docs/limitations.md`).

## P3 (research / novelty, once core protocols are solid)

- Multi-lane architecture investigation (event fabric, protocol bridging).
- JTAG / SWD / PS/2 / CAN-related protocol experiments.
- Low-speed USB / 10 Mbit Ethernet feasibility studies.
- Protocol sniff -> classify -> replay demo.
- Deterministic fault injection demo.
