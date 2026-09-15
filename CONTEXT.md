# CONTEXT.md — Start Here

This is the single-page onboarding summary for this repository. Read this
first, before `PROJECT_MASTER_PLAN.md` or anything else in `AGENTS.md`'s
reading list - it should get any new agent or contributor oriented in under
a minute. Full detail always lives in the linked files; this file is a
pointer/summary, not a duplicate.

**This file must be updated after every completed feature or task.** See
"Keeping this file current" at the bottom - that's not optional.

## What this is

An open-source, general-purpose, programmable protocol-emulator ASIC built
for the Jane Street / Tiny Tapeout IHP 130nm CMOS5L competition (deadline
January 18, 2027). Full brief: `PROJECT_MASTER_PLAN.md`.

## Current status

- **Phase:** ISA v1 complete — Iterations 1–26 complete (10 Mbit Ethernet 10BASE-T Physical Signaling Feasibility Study & Link Layer Engine, Automated Protocol Fuzzing & Anomaly Injection Campaign, End-to-End Autonomous Protocol Pipeline Demo & Cross-Protocol Translation Bridge, Low-Speed USB 1.1 Physical Layer & Packet Framing Engine, Multi-Lane Dual-Core Protocol Processor Architecture & Physical PPA Feasibility Study, Deterministic Fault Injection & Protocol Stress Engine, Gate-Level Simulation Suite with Real Standard Cell Timing Models GATES=yes, Autonomous Hardware Protocol Sniffer & Dynamic Pattern Classifier Engine, High-Level Data Link Control HDLC / SDLC ISO/IEC 13239 Bit-Oriented Protocol Engine, Pure Firmware Autobaud Rate Auto-Discovery Engine & Program RAM Architecture Study, DMX512 ANSI E1.11 / USITT DMX512-A Stage Lighting Protocol Engine, CAN 2.0A Controller Physical-Layer Protocol Engine, Manchester Biphase-L IEEE 802.3 / MIL-STD-1553 Encoder & Decoder Engine, ARM SWD Interface Engine & DPIDR Readout, JTAG IEEE 1149.1 TAP Controller Engine with 32-bit IDCODE Readout & BYPASS Verification, PS/2 Bidirectional Host Controller Engine, Dallas 1-Wire Master with Single-Cycle Presence Pulse Discovery, Zero-Jitter UART RX with WAITEDGE, Bootloader CRC-8 Hardware Protection, I2C Clock Stretching & Arbitration Detection, I2C Master, hardware open-drain, SPI Master, MSB shifts, UART TX, WAITEDGE, PVFI formal, mutation testing, fuzzer, synthesis).
- **What exists:**
  1. **Core:** 24 opcodes, 4 registers, bidirectional GPIO bus on `uio[7:0]`,
     `SHIFTOUT`/`SHIFTIN` with MSB/LSB direction select (`imm8[3]`), `WAITEDGE`
     hardware edge-detect and cycle-capture timing discovery backed by a 32-bit cycle counter,
     and `GODRI`/`GODR` hardware open-drain primitives for contention-free open-collector buses.
  2. **Reprogrammability:** On-chip serial bootloader FSM writing to a true
     writable program RAM (`src/program_ram.v`) over `uio[0:2]`, protected by
     hardware CRC-8 frame integrity verification (poly 0x07, init 0x00) with
     dedicated hardware status pins (`uo_out[0]` boot_done, `uo_out[1]` boot_err)
     and fail-safe permanent halt locking on corruption. Zero `$readmemh` in design.
  3. **Formal Trace Port:** PVFI (Protocol-engine Verification Formal Interface)
     RVFI-style per-cycle retirement bus gated under `ifdef PVFI (zero silicon overhead).
  4. **Formal Harness:** SymbiYosys (`formal/core.sby`) with Z3 SMT solver proving
     reset convergence, PC bounds, WAIT termination, halt permanence, counter
     monotonicity, PVFI interface correctness, open-drain electrical isolation,
     and bootloader error locking invariants (20 steps, 0 violations).
  5. **Firmware & Decoders:** Parameterized bit-banged UART TX and zero-jitter UART RX with
     framing error and glitch rejection (`tools/uart_model.py`),
     full-duplex SPI Master supporting all 4 modes ($CPOL \in \{0,1\}, CPHA \in \{0,1\}$) (`tools/spi_model.py`),
     I2C Master write/read transactions with compact `DECJNZ` loops (`tools/i2c_model.py`),
     Dallas 1-Wire Master protocol engine with single-cycle presence pulse width measurement and read/write timeslots (`tools/onewire_model.py`),
     PS/2 Bidirectional Host Controller engine with dynamic odd-parity accumulation and RTS transmission (`tools/ps2_model.py`),
     JTAG IEEE 1149.1 TAP Controller engine with 32-bit IDCODE readout and BYPASS verification (`tools/jtag_model.py`),
     ARM SWD Interface engine with JTAG-to-SWD switching and DPIDR readout (`tools/swd_model.py`),
     Manchester Biphase-L encoder/decoder engine with jitter-free half-bit timing (`tools/manchester_model.py`),
     CAN 2.0A controller engine with ISO 11898 open-drain bit stuffing, in-cell arbitration, and dominant ACK assertion (`tools/can_model.py`),
     DMX512 engine with Break pulse duration measurement in `R3` and per-slot `WAITEDGE` edge synchronization (`tools/dmx512_model.py`),
     Autobaud Rate Auto-Discovery Engine with pulse symmetry noise rejection and multi-rate classification (`tools/autobaud_model.py`),
     HDLC / SDLC ISO/IEC 13239 bit-oriented framing engine with NRZI line coding, dynamic zero-bit insertion (stuffing) and deletion (destuffing), flag delimiters (`0x7E`), and abort detection (`tools/hdlc_model.py`),
     Autonomous Hardware Protocol Sniffer & Dynamic Pattern Classifier Engine (`tools/classifier_model.py`) fingerprinting UART, Manchester, 1-Wire, DMX512, HDLC, and noise via `WAITEDGE` pulse measurement and two's-complement bounds checking,
     Deterministic Fault Injection & Protocol Stress Engine (`tools/fault_injector_model.py`) generating intentional protocol violations across CAN, HDLC, UART, and Manchester standards,
     Multi-Lane Dual-Core Architecture Simulator & Full-Duplex Bridge Engine (`tools/multilane_model.py`, `docs/multilane_study.md`) supporting concurrent execution, 1-cycle event strobe wakeup, and lock-free mailbox byte transfers,
     USB 1.1 Low-Speed (1.5 Mbps) Physical Layer & Packet Framing Engine (`tools/usb_model.py`) supporting differential J/K states, NRZI modulation, dynamic bit stuffing, Token CRC-5, Data CRC-16, and SE0 EOP delimiter,
     End-to-End Autonomous Protocol Sniff -> Classify -> Ingress -> Replay Pipeline & Cross-Protocol Bridge Engine (`tools/pipeline_model.py`),
     Automated Protocol Fuzzing & Anomaly Injection Engine (`tools/protocol_fuzzer.py`) generating constrained-random phase-bounded edge jitter, false-start glitches, framing errors, and recovery sequences,
     and 10 Mbit Ethernet 10BASE-T Physical Signaling & Framing Engine (`tools/ethernet_model.py`, `docs/ethernet_study.md`) generating Normal Link Pulses (NLP), LSB-first Manchester biphase packets, SFD sync, TP_IDL delimiters, and IEEE 802.3 CRC-32 Frame Check Sequences,
     paired with independent `UartReceiver`, `UartTransmitter`, `SpiSlave`, `I2cSlave`, `OneWireSlave`, `PS2Device`, `JtagTarget`, `SwdTarget`, `ManchesterDecoder`, `CanReceiverModel`, `Dmx512ReceiverModel`, `AutobaudTransmitterModel`, `HdlcTransmitter`, `HdlcReceiver`, `TrafficGenerator`, `DualCoreSystem`, `UsbReceiver`, `ProtocolFuzzer`, and `EthernetTransceiverModel` verification models.
  6. **Mutation Testing:** Standalone harness (`scripts/mutate.py`) testing 29
     architectural fault categories, measuring **100.0% kill rate (29/29 killed)**
     (citing Huang et al. 2015, Firefly 2025).
  7. **Constrained-Random Fuzzing:** Automated instruction fuzzer (`tools/fuzzer.py`)
     with delta-debugging program shrinker, verified in `test/test_fuzz.py`.
  8. **Gate-Level Timing Verification:** Full post-synthesis physical netlist simulation
     with calibrated CMOS standard cell timing models (`test/simcells_timing.v`, `scripts/test_gl.sh`)
     verifying 8/8 physical protocol tests across external chip pins in 30.24s.
  9. **Real PPA Baseline:** Mapped with Yosys 0.69+ (`scripts/synth.sh`), measuring
     19,291 CMOS cells (37,832 GE). Active processor logic is only 1,580 cells
     (~2.2 kGE) with 91.8% of cells in the synthesized flip-flop RAM matrix.
     Multi-lane study proves Split Memory ($2 \times 128 \times 16$) adds only 1,775 cells
     (+9.2% area, ~40.5 kGE total) and fits comfortably in 8x4 tiles (<65% density).
- **What's verified:** 126/126 RTL tests pass via `scripts/regress.sh` and 8/8 gate-level timing tests pass via `scripts/test_gl.sh`:
  (1) cycle-by-cycle differential test (`test/test.py`),
  (2) UART TX edge-case verification (`0x00`, `0xFF`, `0x55`, `0xAA` at 4, 8, 16 cycles/bit),
  (3) UART TX pseudorandom frames,
  (4) UART RX edge-case verification (`0x00`, `0xFF`, `0x55`, `0xAA` at 8, 16 cycles/bit),
  (5) UART RX pseudorandom frames,
  (6) UART RX framing error detection (`R2 = 0xFE`),
  (7) UART RX false-start noise glitch rejection (`R2 = 0xFF`),
  (8) isolated ALU/register unit tests,
  (9) isolated branch/loop unit tests,
  (10) isolated GPIO/shift unit tests,
  (11) WAITEDGE single-cycle pulse width measurement (5, 11, 23, 47 cycles),
  (12) cycle counter timestamp capture,
  (13) WAITEDGE differential test vs Python model,
  (14) constrained-random fuzzing (10 iterations differential vs Python model),
  (15) delta-debugging shrinker unit test,
  (16) SPI Mode sweep (Modes 0, 1, 2, 3),
  (17) SPI full-duplex simultaneous bidirectional transfer (0x7E tx / 0x42 rx),
  (18) SPI edge-case and random frame verification,
  (19) I2C Master single-byte write with slave ACK and STOP detection,
  (20) I2C Master multi-byte EEPROM write with ordered data latching,
  (21) I2C Master single-byte read with Master NACK and bus release,
  (22) I2C unresponsive slave NACK detection,
  (23) I2C electrical open-drain contention prevention proof,
  (24) I2C slave clock stretching absorption via WAITEDGE with latency capture,
  (25) I2C multi-master arbitration loss detection and atomic bus release,
  (26) I2C clean arbitration win path,
  (27) clean bootload with valid CRC-8,
  (28) bootloader corrupted CRC rejection and halt lock,
  (29) single-bit payload bitflip corruption rejection,
  (30) premature LOAD_REQ deassertion abort detection,
  (31) warm-boot skip without recalculating CRC,
  (32) 1-Wire Master reset pulse and presence pulse discovery across duration sweep (15, 20, 25, 30 cycles),
  (33) 1-Wire Master read timeslot scratchpad byte capture into R0,
  (34) 1-Wire Master write timeslot command transmission to OneWireSlave,
  (35) 1-Wire physical open-drain electrical safety (zero bus contention under forced external pull-down),
  (36) PS/2 Host scan code reception across standard codes (0x1C, 0x32, 0xF0, 0xAA, 0x00, 0xFF) with odd-parity verification into R0,
  (37) PS/2 Host parity error detection (R2 = 0xFD),
  (38) PS/2 Host framing error detection on missing stop bit (R2 = 0xFE),
  (39) PS/2 Host-to-Device RTS transmission of commands (0xED, 0xF4, 0xFF) with device ACK verification,
  (40) PS/2 Host detection of unresponsive device NACK (R2 = 0xFC),
  (41) PS/2 physical open-drain electrical safety (zero bus contention under forced external pull-down),
  (42) JTAG IEEE 1149.1 standard IDCODE 0x149511C3 readout into R0..R3,
  (43) JTAG IDCODE multi-pattern value sweep (0x00000001, 0xDEADBEEF, 0x12345679, 0xCAFEBABF),
  (44) JTAG BYPASS register 1-cycle pipeline shift verification,
  (45) JTAG 5-cycle TMS reset recovery from arbitrary TAP states (PAUSE-DR, SHIFT-IR),
  (46) JTAG physical electrical pin isolation (TDO high-Z),
  (47) ARM SWD Line Reset (>= 50 clocks) and JTAG-to-SWD switching sequence (0x79E7) activation,
  (48) ARM SWD standard DPIDR 0x0BA01477 readout into R0..R3 with ACK OK,
  (49) ARM SWD multi-target Cortex identity sweep (Cortex-M7, M33, M4+ETM, M23),
  (50) ARM SWD target non-OK ACK handling (WAIT and FAULT),
  (51) ARM SWD physical pin direction and dynamic tri-state electrical contention safety,
  (52) Manchester Biphase-L TX waveform fidelity (exact 4-cycle half-bits, 50% duty cycle, zero biphase violations),
  (53) Manchester Biphase-L TX pattern sweep (0x00, 0xFF, 0x55, 0xAA, 0x3C),
  (54) Manchester Biphase-L RX standard byte reception (0x55, 0xAA, 0xA5, 0x00, 0xFF) into R0,
  (55) Manchester Biphase-L RX pseudorandom pattern sweep across 8 test vectors,
  (56) Manchester Biphase-L independent reference decoder biphase violation detection,
  (57) Manchester Biphase-L physical pin direction electrical safety,
  (58) CAN 2.0A standard frame transmission (ID 0x123, payload 0xA5) with bit stuffing and CRC-15,
  (59) CAN 2.0A arbitration loss collision detection and clean bus release (ID 0x123 vs 0x120, R2 = 0xAA),
  (60) CAN 2.0A missing ACK error detection (R2 = 0xAE),
  (61) CAN 2.0A standard frame reception with WAITEDGE SOF sync, payload into R0, and dominant ACK assertion,
  (62) CAN 2.0A physical open-drain electrical safety (zero bus contention under forced external pull-down),
  (63) DMX512 TX packet waveform verification (Break 96 cycles, MAB 16 cycles, Start Code 0x00, 3 channel slots),
  (64) DMX512 TX dynamic channel intensity sweep across 3 color vectors,
  (65) DMX512 RX channel 1 extraction into R0 with Break duration measurement (96 cycles in R3),
  (66) DMX512 RX channel 2 extraction skipping channel 1 with zero cumulative drift,
  (67) DMX512 physical pin direction electrical safety (strictly input in RX, single output in TX),
  (68) Autobaud Rate 8 discovery (T = 8 cycles/bit, Profile 1) with payload 0xA5 recovery,
  (69) Autobaud Rate 16 discovery (T = 16 cycles/bit, Profile 2) with payload 0x3C recovery,
  (70) Autobaud Rate 32 discovery (T = 32 cycles/bit, Profile 3) with payload 0x7E recovery,
  (71) Autobaud dynamic payload sweep across all three calibrated rate profiles,
  (72) Autobaud asymmetric pulse noise glitch detection and rejection (R2 = 0xEE),
  (73) Autobaud out-of-range unsupported baud rate detection and rejection (R2 = 0xBF),
  (74) Autobaud missing stop bit framing error detection (R2 = 0xFE),
  (75) Autobaud physical pin direction electrical safety (strictly input in RX),
  (76) HDLC TX waveform fidelity (exact 0x7E opening flag, payload 0xA5, 0x7E closing flag, NRZI transitions),
  (77) HDLC TX dynamic zero-bit insertion (bit stuffing after 5 ones on 0xFF, 0x7E, 0x3F),
  (78) HDLC TX payload sweep across diverse patterns (0x00, 0x55, 0xAA, 0x3C),
  (79) HDLC RX standard frame reception into R0 with closing flag validation (R1 = 0x00),
  (80) HDLC RX zero-bit destuffing with payload recovery on 0xFF, 0x7E, 0x3F,
  (81) HDLC RX abort sequence detection (R1 = 0xAB),
  (82) HDLC physical pin direction electrical safety (strictly input in RX, single output in TX),
  (83) Protocol Sniffer UART start-bit classification (R0 = 0x01),
  (84) Protocol Sniffer Manchester symmetric half-bit classification (R0 = 0x02),
  (85) Protocol Sniffer DMX512 Break + MAB classification (R0 = 0x04),
  (86) Protocol Sniffer Dallas 1-Wire Reset + Recovery classification (R0 = 0x03),
  (87) Protocol Sniffer HDLC flag hold classification (R0 = 0x05),
  (88) Protocol Sniffer unrecognized noise pulse rejection (R0 = 0xFF),
  (89) Protocol Sniffer physical pin direction electrical safety (strictly input, uio_oe = 0x00),
  (90) CAN bit stuff error injection (omitted stuff bit detected by ISO model),
  (91) CAN CRC-15 checksum corruption injection,
  (92) CAN EOF dominant glitch injection,
  (93) HDLC abort sequence injection (7 consecutive 1s detected by receiver),
  (94) HDLC stuff bit omission injection,
  (95) UART framing error injection (forced low stop bit raises UartFramingError),
  (96) UART sub-baud noise glitch rejection (1-cycle runt pulse ignored),
  (97) Manchester biphase violation injection,
  (98) Fault injection pin electrical safety,
  (99) Dual-core concurrent execution and independent PC advancement,
  (100) 1-cycle inter-core event strobe synchronization and WAITEDGE wakeup,
  (101) Lock-free mailbox atomic byte transfer with FULL/EMPTY status flags,
  (102) Mailbox overflow and underflow fault protection with state preservation,
  (103) End-to-end full-duplex protocol bridge (Manchester Ingress -> Mailbox -> SPI Master Mode 0 Egress) verified against independent SpiSlave,
  (104) Strict physical pin isolation and electrical safety between Lane 0 (uio[3:0]) and Lane 1 (uio[7:4]),
  (105) USB Handshake Packets (ACK 0xD2, NAK 0x5A, STALL 0x1E) with valid SYNC and EOP,
  (106) USB Token Packet: SETUP (0x2D) with 11-bit address/endpoint and CRC-5,
  (107) USB dynamic bit stuffing on 8 consecutive 1s (0xFF) with clean destuffing,
  (108) USB multi-byte DATA0 packets across diverse payloads ([0x12, 0x34], [0xAA, 0x55], [0x00, 0x7E]),
  (109) USB continuous SE0 bus reset detection (>= 30 bit periods),
  (110) USB physical electrical safety (zero SE1 states, clean High-Z bus release),
  (111) Autonomous UART sniff -> classify (0x01) -> ingress (0xA5) -> echo replay (0xA6) onto uio[1] verified against independent UartReceiver,
  (112) Autonomous cross-protocol translation bridge: UART ingress on Lane 0 (uio[0]) -> SPI Master Mode 0 egress on Lane 1 (uio[4..6]) verified against independent SpiSlave,
  (113) Autonomous sub-baud noise rejection: runt pulse classifies as Class 0xFF (Noise) with zero spurious egress replay,
  (114) Autonomous pipeline dynamic electrical safety: passive sniff operates at high-Z (uio_oe=0x00), asserting outputs strictly during active replay,
  (115) UART RX timing jitter tolerance under +-1 cycle phase-bounded edge jitter across pseudorandom payloads (0x55, 0xAA, 0x3C, 0xA5) with R2=0x00,
  (116) UART false-start 1-cycle runt glitch rejection (trapped with R2=0xFF without hanging),
  (117) UART framing error detection under corrupted stop bits (trapped with R2=0xFE),
  (118) Manchester Biphase-L timing jitter tolerance under +-1 cycle half-bit distortions (payload 0x96 recovered cleanly),
  (119) Multi-frame anomaly recovery (Valid 0xA5 -> Corrupt Framing Error 0x55 -> Valid Recovery 0x3C) with zero lockup,
  (120) Physical electrical safety under fuzzed input waveforms (zero bus contention, uio_oe strictly 0x00),
  (121) 10BASE-T Ethernet Normal Link Pulse (NLP) periodic heartbeat pulse generation (2-cycle pulse width and clean High-Z return),
  (122) 10BASE-T TX packet framing with Preamble (0x55), SFD (0xD5), Payload (0xA5), and TP_IDL delimiter decoded with 100% fidelity,
  (123) 10BASE-T RX core edge synchronization and payload byte reception (0x7E) into R0,
  (124) IEEE 802.3 Frame Check Sequence (CRC-32) verification against standard vectors,
  (125) Ethernet receiver link loss detection upon cessation of incoming NLP heartbeats,
  (126) Ethernet physical pin direction and electrical safety (uio_oe strictly 0x00 during RX/idle).
- **Git:** Sequence of small, reviewable commits (`git log`).

## Repository map

```text
src/            RTL: project.v (TT wrapper), core.v, alu.v, gpio.v, program_ram.v
firmware/       Assembly programs (loop_demo.asm)
tools/          assembler.py, isa_model.py, uart_model.py, spi_model.py, i2c_model.py, onewire_model.py, ps2_model.py, jtag_model.py, swd_model.py, manchester_model.py, can_model.py, dmx512_model.py, autobaud_model.py, hdlc_model.py, classifier_model.py, fault_injector_model.py, multilane_model.py, usb_model.py, pipeline_model.py, protocol_fuzzer.py, ethernet_model.py, fuzzer.py
test/           cocotb test suite (test, test_uart, test_opcodes, test_waitedge, test_fuzz, test_spi, test_i2c, test_bootload, test_onewire, test_ps2, test_jtag, test_swd, test_manchester, test_can, test_dmx512, test_autobaud, test_hdlc, test_classifier, test_fault_injection, test_multilane, test_usb, test_pipeline, test_protocol_fuzz, test_ethernet, test_gate_level)
formal/         SymbiYosys formal harness (core.sby, core_formal.v)
scripts/        setup_env.sh, regress.sh, test_gl.sh, mutate.py, synth.sh, synth.ys
docs/           architecture, ISA, verification, toolchain, PPA, limitations, multilane_study, ethernet_study
orchestrator/   Durable state (decisions.md, queue.md, metrics.json, experiments.jsonl)
```

## Get productive in 2 minutes

```bash
bash scripts/setup_env.sh   # one-time toolchain install (see docs/toolchain.md)
bash scripts/regress.sh     # runs all 126 cocotb regression tests (~60s)
bash scripts/test_gl.sh     # runs gate-level timing simulation (8/8 tests pass, ~30s)
sby -f formal/core.sby      # runs SymbiYosys formal verification with Z3 (20 steps pass)
python3 scripts/mutate.py   # runs RTL mutation testing campaign (29/29 killed)
bash scripts/synth.sh       # runs Yosys synthesis and outputs cell/area metrics
```

## Key decisions (full log: `orchestrator/decisions.md`)

- Built on the real `TinyTapeout/ttihp-verilog-template` (`cmos5l` branch).
- No sudo/root required; toolchain runs from Miniforge/conda-forge.
- Programmable protocol GPIO mapped to bidirectional `uio[7:0]`.
- Genuinely reprogrammable program RAM loaded via serial bootloader with CRC-8 frame integrity protection.
- Multi-tiered verification: differential cycle-by-cycle testing, SymbiYosys formal
  safety proofs, independent protocol decoders, 100% mutation kill rate,
  constrained-random fuzzing with automated shrinking, and gate-level timing simulation.
- Synthesized PPA measured: active core is 1,580 cells (~2.2 kGE), RAM is 17,757 cells.
- Multi-lane architecture verified feasible inside 8x4 tiles (<65% density, Split Memory 2x128x16, +9.2% area overhead).
- USB 1.1 Low-Speed physical signaling implemented with zero specialized macros (differential J/K, NRZI, dynamic bit stuffing, SE0 EOP).
- End-to-end autonomous protocol pipeline: sniff -> classify -> transform -> replay executed purely in core assembly with zero bus contention.
- Protocol robustness proven under constrained-random fuzzing and anomaly injection: phase-bounded edge jitter tolerance, false-start glitch trapping, framing error detection, and multi-frame recovery without reset.
- 10 Mbit Ethernet 10BASE-T physical signaling feasibility proven with cycle-accurate Normal Link Pulses (NLP), LSB-first Manchester framing, SFD delimiter synchronization, TP_IDL delimiters, and IEEE 802.3 CRC-32 Frame Check Sequence.

## What to work on next

Full prioritized backlog: `orchestrator/queue.md`. Entering continuous loop:

1. Iteration 27: Multi-Protocol Bus Bridging Matrix (I2C-to-SPI, UART-to-CAN, 1-Wire-to-UART).
2. Iteration 28: Hardware Watchdog Timer & Brownout Recovery Circuit Feasibility Study.
3. Iteration 29: Dynamic Power & Energy Optimization Study (Clock Gating & Instruction Micro-Architectural Profiling).




## Keeping this file current

After completing ANY feature, task, or nontrivial change, before you
consider the work done:

1. Update "Current status" above to reflect what changed.
2. Update "What to work on next" if priorities shifted.
3. Keep it short - this is a summary. Full detail belongs in
   `orchestrator/decisions.md`, `orchestrator/experiments.jsonl`, and `docs/`.

A stale `CONTEXT.md` defeats its entire purpose. This requirement is also
recorded in `AGENTS.md`.
