# Work Queue

Priority order within each tier is not strict; use judgement per
`orchestrator/WARP_CYCLE_PROMPT.md` step 4 ("identify the highest-value
bounded problem").

- ~~Implement UART TX firmware (bit-banged via `SHIFTOUT`+`WAIT`) and verify
  against an independent Python UART receiver model~~ - done: `tools/uart_model.py`
  (UartReceiver + build_uart_tx_asm) + `test/test_uart.py` (verified across
  edge cases, pseudorandom frames, and periods 4, 8, 16).
- ~~Add per-opcode isolated unit tests~~ - done: `test/test_opcodes.py` directly
  exercises all ALU, branch, loop, GPIO, and shift instructions.

## P0

- Rename `tt_um_change_me_protocol_emulator` to include the real GitHub
  username, once known, in `src/project.v`, `info.yaml`, and `test/tb.v`.

## P1

- Add a bootloader checksum/CRC and a way to signal a failed/short load
  back to the host (see `docs/limitations.md`).
- ~~Add an RVFI-style per-cycle verification interface ("PVFI", debug-only,
  gated by a `SIM`/`PVFI` define) and a first SymbiYosys formal harness
  under `formal/`: "PC stays in valid range", "reset reaches a known
  state", "WAIT/bootloader-load sequences terminate"~~ - done: `src/core.v`
  PVFI ports gated under `ifdef PVFI, formal harness `formal/core.sby` +
  `formal/core_formal.v` verified 20 steps with Z3 (0 violations).
- ~~Add `WAITEDGE rd, imm8` (stall for an edge on the GPIO bus, capture
  elapsed cycles into `rd`) plus a free-running cycle counter - a genuine
  autobaud/timing-discovery primitive for reverse-engineering unknown
  protocols, and something RP2040 PIO's documented total lack of runtime
  observability cannot do~~ - done: `src/core.v` opcode 21, 32-bit cycle counter,
  `test/test_waitedge.py` pulse-width measurement to single-cycle precision.
- Run first Yosys synthesis pass; record mapped cell count/area breakdown
  in `docs/ppa.md` and `orchestrator/metrics.json`.
- Add a randomized instruction-stream fuzzer that runs against both the
  RTL and `tools/isa_model.py`, with automatic shrink-on-failure.

## P2

- Mutation testing harness: deliberately invert a branch condition /
  off-by-one a `WAIT` counter / wrong ALU op / dropped reset, confirm the
  test suite catches it, and report a measured kill rate (not just
  coverage - see `docs/verification.md` for why coverage alone is
  insufficient evidence, citing Huang et al. 2015 and the Firefly 2025
  mutation-testing results).
- SPI and I2C firmware once UART is solid.
- Explicit test coverage for reserved/illegal opcodes 21-31 (currently
  silently behave as NOP, untested - see `docs/limitations.md`).
- Convert `program_ram.v` to a synchronous-read design once real synthesis
  data shows it matters for PPA (`docs/limitations.md`).

## P3 (research / novelty, once core protocols are solid)

- Multi-lane architecture investigation (event fabric, protocol bridging).
- JTAG / SWD / PS/2 / CAN-related protocol experiments.
- Low-speed USB / 10 Mbit Ethernet feasibility studies.
- Protocol sniff -> classify -> replay demo.
- Deterministic fault injection demo.
