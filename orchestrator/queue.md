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
- ~~1-Wire Master Protocol Engine (Dallas DS18B20 Timing) with presence pulse discovery and bit timeslots via open-drain and `WAITEDGE`~~ -
  done: `tools/onewire_model.py` (OneWireSlave + build_onewire_reset_presence_asm + build_onewire_read_byte_asm + build_onewire_write_byte_asm) + `test/test_onewire.py` (35/35 regression pass, presence duration sweep, read/write timeslots, 14/14 mutants killed).
- ~~PS/2 Bidirectional Host Controller (device clock edge sync on falling clock, 11-bit odd-parity verified frame reception and host-to-device inhibit/send via open-drain and `WAITEDGE`)~~ -
- ~~JTAG TAP Controller Engine (TMS state machine: Test-Logic-Reset, Run-Test/Idle, Shift-DR, Shift-IR, BYPASS and IDCODE readout)~~ -
- ~~ARM SWD (Serial Wire Debug) Interface Engine (Line Reset sequence 50+ clocks high, JTAG-to-SWD switching, turnaround bits, SWD header and ACK readout)~~ -
- ~~Manchester Biphase-L (IEEE 802.3 / MIL-STD-1553) Encoder & Decoder Engine~~ -
  done: `tools/manchester_model.py` (ManchesterDecoder + ManchesterTransmitter + build_manchester_tx_asm + build_manchester_rx_asm) + `test/test_manchester.py` (57/57 regression pass, 4-cycle symmetric half-bit waveform, standard/pseudorandom RX into R0, biphase violation detection, 18/18 mutants killed).
- ~~CAN Bus Physical-Layer Controller (Dominant/Recessive Bit Timing, Bit Stuffing & Arbitration via `GRD`)~~ -
  done: `tools/can_model.py` (CanReceiverModel + compute_can_crc15 + build_can_tx_asm + build_can_rx_asm) + `test/test_can.py` (62/62 regression pass, bit stuffing, CRC-15, in-cell arbitration loss detection R2=0xAA, missing ACK R2=0xAE, dominant ACK assertion, 19/19 mutants killed).
- ~~DMX512 Stage Lighting Protocol Engine (Break pulse >= 88us, MAB, Start Code 0x00, and slot reception with per-slot edge sync)~~ -
  done: `tools/dmx512_model.py` (Dmx512ReceiverModel + build_dmx512_tx_packet_asm + build_dmx512_rx_slot_asm) + `test/test_dmx512.py` (67/67 regression pass, Break pulse duration measurement in R3, per-slot WAITEDGE edge resynchronization, 20/20 mutants killed).
- ~~Pure Firmware Autobaud Rate Auto-Discovery Engine (pulse duration discovery via WAITEDGE, symmetry validation, multi-profile classification, and adaptive sampling)~~ -
  done: `tools/autobaud_model.py` (AutobaudTransmitterModel + build_autobaud_rx_asm) + `test/test_autobaud.py` (75/75 regression pass, rate 8/16/32 profiles, noise rejection, framing error detection, 21/21 mutants killed).
- ~~Program RAM architecture trade-off study (`program_ram.v` combinational vs synchronous read)~~ -
  done: quantified >80 ns timing slack at 10 MHz (<12 ns combinational path delay across 20 logic levels), justified retaining combinational read to guarantee 1-cycle determinism without pipeline bubbles or branch penalty stalls.
- ~~HDLC / SDLC Bit-Oriented Protocol Engine (ISO/IEC 13239 bit-oriented framing, NRZI line coding, zero-bit stuffing/destuffing, flag framing 0x7E, abort sequences)~~ -
  done: `tools/hdlc_model.py` (HdlcTransmitter + HdlcReceiver + build_hdlc_tx_words + build_hdlc_rx_words) + `test/test_hdlc.py` (82/82 regression pass, zero jitter, bit stuffing on 0xFF/0x7E/0x3F, payload recovery in R0, abort detection R1=0xAB, 22/22 mutants killed).
- ~~Autonomous Hardware Protocol Sniffer & Dynamic Pattern Classifier Engine (passive pulse timing measurement via `WAITEDGE`, two's-complement bounds checking, automatic protocol fingerprinting into UART, Manchester, 1-Wire, DMX512, HDLC, or Noise)~~ -
  done: `tools/classifier_model.py` (TrafficGenerator + build_protocol_sniffer_asm) + `test/test_classifier.py` (89/89 regression pass, multi-protocol fingerprinting, noise rejection R0=0xFF, pin safety, 23/23 mutants killed).

## P3 (research / novelty, advanced validation)

- Gate-level simulation with real standard-cell timing models (`GATES=yes`).
- Deterministic protocol fault injection & protocol stress engine (intentional CAN stuff errors, CRC corruption, I2C collision, UART framing error, HDLC aborts).
- Multi-lane architecture investigation (dual-core protocol bridging, event fabric within 8x4 Tiny Tapeout footprint).
- Low-speed USB / 10 Mbit Ethernet feasibility studies.
- End-to-end Protocol Sniff -> Classify -> Replay pipeline demo.
