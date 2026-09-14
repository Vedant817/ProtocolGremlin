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

- ~~Add a bootloader checksum/CRC and a way to signal a failed/short load
  back to the host (see `docs/limitations.md`)~~ - done: hardware CRC-8 (poly 0x07)
  accumulator in `src/core.v`, `uo_out[1:0]` status reporting, permanent halt on mismatch,
  `test/test_bootload.py` (5/5 tests), SymbiYosys formal proof.
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
- ~~Run first Yosys synthesis pass; record mapped cell count/area breakdown
  in `docs/ppa.md` and `orchestrator/metrics.json`~~ - done: `scripts/synth.sh` +
  `scripts/synth.ys`, mapped 19,143 CMOS cells (37,542 GE), 1,402 cells in active processor core.
- ~~Add a randomized instruction-stream fuzzer that runs against both the
  RTL and `tools/isa_model.py`, with automatic shrink-on-failure~~ - done:
  `tools/fuzzer.py` + `test/test_fuzz.py`, automated 1-minimization shrinker.

## P2

- ~~Mutation testing harness: deliberately invert a branch condition /
  off-by-one a `WAIT` counter / wrong ALU op / dropped reset, confirm the
  test suite catches it, and report a measured kill rate~~ - done:
  `scripts/mutate.py`, 10/10 mutants killed (100.0% kill rate) in 46.95s.
- ~~SPI Master firmware supporting all 4 modes (CPOL 0/1, CPHA 0/1) and full-duplex~~ -
  done: `tools/spi_model.py` (build_spi_master_asm + SpiSlave) + `test/test_spi.py` (Mode 0-3, full duplex, random).
- ~~I2C firmware (START, STOP, ACK/NACK, open-drain primitive OP_GODRI/OP_GODR)~~ -
  done: `src/core.v`, `src/gpio.v`, `tools/i2c_model.py` (build_i2c_write_asm + build_i2c_read_asm + I2cSlave) + `test/test_i2c.py` (5/5 tests pass, open-drain contention prevention formally proven).
- ~~I2C Clock Stretching & Multi-Master Arbitration Detection via `WAITEDGE` & `GRD`~~ -
  done: `tools/i2c_model.py` (build_i2c_write_with_stretch_asm, build_i2c_write_with_arbitration_asm) + `test/test_i2c.py` (22/22 regression pass, stretch absorbed, arbitration loss cleanly aborted).
- ~~Bootloader CRC-8 checksum addition to detect corrupted/truncated frames~~ -
  done: on-chip hardware CRC-8 accumulator, `test/test_bootload.py` (5/5 tests), SymbiYosys proven, 12/12 mutants killed.
- ~~UART RX firmware with start-bit edge synchronization via `WAITEDGE`~~ -
  done: `tools/uart_model.py` (build_uart_rx_asm + UartTransmitter) + `test/test_uart.py` (31/31 regression pass, zero jitter, framing error & glitch rejection, 13/13 mutants killed).
- 1-Wire Master Protocol Engine (Dallas DS18B20 Timing) with presence pulse discovery and bit timeslots via open-drain and `WAITEDGE`.
- Convert `program_ram.v` to a synchronous-read design once real synthesis
  data shows it matters for PPA (`docs/limitations.md`).

## P3 (research / novelty, once core protocols are solid)

- Multi-lane architecture investigation (event fabric, protocol bridging).
- JTAG / SWD / PS/2 / CAN-related protocol experiments.
- Low-speed USB / 10 Mbit Ethernet feasibility studies.
- Protocol sniff -> classify -> replay demo.
- Deterministic fault injection demo.
