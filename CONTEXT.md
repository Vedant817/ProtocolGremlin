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

- **Phase:** ISA v1 complete — Iterations 1–38 complete (Quadrature Encoder Interface (QEI) & Industrial Motion Feedback Engine, MIPI I3C v1.1.1 Sensor Protocol & Dynamic Address Assignment (DAA) Acceleration Engine, Hardware-Assisted Cyclic Redundancy Check (CRC-16/CRC-32) Coprocessor Macro PPA Feasibility Study, Memory Protection Unit (MPU) & Multi-Tenant Partitioning Engine, Asynchronous Event Notification & Level/Edge Interrupt Controller Subsystem, Physical Die Floorplan, Pad Placement & Package Pinout Co-Design Study, CAN FD Flexible Data-Rate Protocol Accelerator Feasibility & Bit-Rate Switching Study, Deterministic Real-Time Task Scheduling Engine: Priority Multi-Tasking & Round-Robin Schedulers, Cryptographic Accelerator Feasibility Study: ChaCha8 / Poly1305 / SHA-256 Bit-Sliced Microcode vs. Hardware Coprocessor, Dynamic Power & Energy Optimization Study: Clock Gating & Instruction Micro-Architectural Profiling, Hardware Watchdog Timer & Brownout Recovery Circuit Feasibility Study, Multi-Protocol Bus Bridging Matrix: I2C-to-SPI, UART-to-CAN, 1-Wire-to-UART, Multi-Byte Streaming, 10 Mbit Ethernet 10BASE-T Physical Signaling Feasibility Study & Link Layer Engine, Automated Protocol Fuzzing & Anomaly Injection Campaign, End-to-End Autonomous Protocol Pipeline Demo & Cross-Protocol Translation Bridge, Low-Speed USB 1.1 Physical Layer & Packet Framing Engine, Multi-Lane Dual-Core Protocol Processor Architecture & Physical PPA Feasibility Study, Deterministic Fault Injection & Protocol Stress Engine, Gate-Level Simulation Suite with Real Standard Cell Timing Models GATES=yes, Autonomous Hardware Protocol Sniffer & Dynamic Pattern Classifier Engine, High-Level Data Link Control HDLC / SDLC ISO/IEC 13239 Bit-Oriented Protocol Engine, Pure Firmware Autobaud Rate Auto-Discovery Engine & Program RAM Architecture Study, DMX512 ANSI E1.11 / USITT DMX512-A Stage Lighting Protocol Engine, CAN 2.0A Controller Physical-Layer Protocol Engine, Manchester Biphase-L IEEE 802.3 / MIL-STD-1553 Encoder & Decoder Engine, ARM SWD Interface Engine & DPIDR Readout, JTAG IEEE 1149.1 TAP Controller Engine with 32-bit IDCODE Readout & BYPASS Verification, PS/2 Bidirectional Host Controller Engine, Dallas 1-Wire Master with Single-Cycle Presence Pulse Discovery, Zero-Jitter UART RX with WAITEDGE, Bootloader CRC-8 Hardware Protection, I2C Clock Stretching & Arbitration Detection, I2C Master, hardware open-drain, SPI Master, MSB shifts, UART TX, WAITEDGE, PVFI formal, mutation testing, fuzzer, synthesis).
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
     10 Mbit Ethernet 10BASE-T Physical Signaling & Framing Engine (`tools/ethernet_model.py`, `docs/ethernet_study.md`) generating Normal Link Pulses (NLP), LSB-first Manchester biphase packets, SFD sync, TP_IDL delimiters, and IEEE 802.3 CRC-32 Frame Check Sequences,
     Universal Multi-Protocol Bus Bridging Matrix Engine (`tools/bridge_matrix_model.py`) realizing I2C-to-SPI, UART-to-CAN with ACK monitoring, 1-Wire-to-UART, and multi-byte continuous stream translations with framing error isolation,
     Hardware Watchdog Timer & Brownout Recovery Subsystem (`docs/watchdog_study.md`, `tools/watchdog_model.py`) supporting programmable windowed timing [T_min, T_max], keyed two-token service sequence (0x5A, 0xA5), sticky reset status registers (0x01 cold, 0x02 WDT timeout, 0x03 BOD, 0x05 early violation), and instant warm-boot recovery (<10 cycles),
     Dynamic Power & Energy Optimization Subsystem (`docs/power_study.md`, `tools/power_model.py`) modeling IHP 130nm CMOS power physics, 3-tier clock gating hierarchy (Program RAM write gating, datapath stall gating, ALU operand isolation), pad capacitive load scaling (20-50 pF), and protocol energy efficiency benchmarks ($pJ/\text{bit}$),
     Cryptographic Accelerator Engine & PPA Study (`docs/crypto_study.md`, `tools/crypto_model.py`) modeling RFC 8439 ChaCha8/20 ARX quarter-round, RFC 8439 Poly1305 MAC step, FIPS 180-4 SHA-256 Ch/Maj primitives, 32-bit multi-precision arithmetic, and hardware coprocessor scaling (33x to 44x speedup with +1.97% to +2.70% area),
     Deterministic Real-Time Task Scheduling Engine (`docs/scheduler_study.md`, `tools/scheduler_model.py`) providing cooperative priority dispatching, round-robin time slicing, context switch fidelity, and hard periodic deadline compliance with WCRL bounded at $\le 17$ cycles ($1.7\,\mu\text{s}$) and zero silicon overhead (0 gates),
     CAN FD Flexible Data-Rate Protocol Accelerator Engine (`docs/canfd_study.md`, `tools/canfd_model.py`) supporting single-cycle dual-rate switching at the BRS sample point (500 kbps nominal to 2.0 Mbps data phase), 64-byte payload streaming with 5.76x speedup, CRC-17/21 validation, and classical CAN BRS=0 fallback compatibility with zero silicon overhead (0 gates),
     Physical Die Floorplan, Pad Placement & Package Pinout Co-Design Study (`docs/floorplan_study.md`, `tools/floorplan_model.py`) modeling 1x2 and 2x2 tile placement density (58.4%), `sg13cmos5l_io` pad cell allocation, QFN-64 package pinout, SSO ground bounce ($V_{bounce} = 64.0\,\text{mV} < 200\,\text{mV}$ noise margin), adjacent pin cross-talk isolation ($> 42\,\text{dB}$), and on-chip PDN IR drop budget ($V_{drop} = 7.77\,\text{mV} < 0.45\%$ VDD),
     Asynchronous Event Notification & Interrupt Controller Subsystem (`docs/interrupt_study.md`, `tools/interrupt_model.py`) supporting single-cycle edge event capture via `WAITEDGE` with timestamp capture, level-sensitive IRQ detection with ACK handshake, strict priority arbitration, nested context preservation, and synthesizable hardware interrupt controller (HIC) dual-rank synchronizer architecture (+145 cells, +0.75% area, 1.67x latency speedup),
     Memory Protection Unit (MPU) & Multi-Tenant Partitioning Engine (`docs/mpu_study.md`, `tools/mpu_model.py`) supporting spatial memory boundary checks, IO pin mask protection, temporal cycle budget enforcement, fail-safe quarantine pin isolation, and synthesizable hardware MPU macro scaling (+384 cells, +1.99% area overhead, 1.85 ns delay),
     Hardware-Assisted Cyclic Redundancy Check (CRC-16/CRC-32) Coprocessor Engine (`docs/crc_study.md`, `tools/crc_model.py`) supporting parallel GF(2) matrix compression LFSR computation, CRC-16/CCITT, CRC-16/MODBUS, and CRC-32/IEEE 802.3 multi-polynomial acceleration with 64x throughput speedup, residual match constant validation, and synthesizable coprocessor macro scaling (+245 cells, +1.27% area overhead),
     MIPI I3C v1.1.1 Sensor Protocol & Dynamic Address Assignment Engine (`docs/i3c_study.md`, `tools/i3c_model.py`) supporting dynamic open-drain to push-pull line switching, automated ENTDAA broadcast address assignment, 48-bit Provisional ID wired-AND arbitration, In-Band Interrupt (IBI) detection, and synthesizable coprocessor macro scaling (+320 cells, +1.66% area overhead),
     and Quadrature Encoder Interface (QEI) & Industrial Motion Feedback Engine (`docs/qei_study.md`, `tools/qei_model.py`) supporting 1X/2X/4X quadrature phase decoding, forward/reverse direction tracking, index pulse zero homing calibration, WAITEDGE period-based velocity estimation, and synthesizable peripheral macro scaling (+351 cells, +1.84% area overhead),
     paired with independent `UartReceiver`, `UartTransmitter`, `SpiSlave`, `I2cSlave`, `OneWireSlave`, `PS2Device`, `JtagTarget`, `SwdTarget`, `ManchesterDecoder`, `CanReceiverModel`, `Dmx512ReceiverModel`, `AutobaudTransmitterModel`, `HdlcTransmitter`, `HdlcReceiver`, `TrafficGenerator`, `DualCoreSystem`, `UsbReceiver`, `ProtocolFuzzer`, `EthernetTransceiverModel`, `WatchdogModel`, `PowerModel`, `CryptoPerformanceModel`, `SchedulerModel`, `CanFdReceiver`, `SsoGroundBounceModel`, `IrDropModel`, `CrossTalkModel`, `InterruptControllerModel`, `InterruptPpaModel`, `MpuControllerModel`, `MpuPpaModel`, `CrcCoprocessorModel`, `CrcPpaModel`, `I3cTargetDevice`, `I3cPpaModel`, `QeiEncoder`, `QeiDecoder`, and `QeiPpaModel` verification models.
  6. **Mutation Testing:** Standalone harness (`scripts/mutate.py`) testing 41
     architectural fault categories, measuring **100.0% kill rate (41/41 killed)**
     (citing Huang et al. 2015, Firefly 2025).
  7. **Constrained-Random Fuzzing:** Automated instruction fuzzer (`tools/fuzzer.py`)
     with delta-debugging program shrinker, verified in `test/test_fuzz.py`.
  8. **Gate-Level Timing Verification:** Full post-synthesis physical netlist simulation
     with calibrated CMOS standard cell timing models (`test/simcells_timing.v`, `scripts/test_gl.sh`)
     verifying 8/8 physical protocol tests across external chip pins in 24.14s.
  9. **Real PPA Baseline:** Mapped with Yosys 0.69+ (`scripts/synth.sh`), measuring
     19,291 CMOS cells (37,832 GE). Active processor logic is only 1,580 cells
     (~2.2 kGE) with 91.8% of cells in the synthesized flip-flop RAM matrix.
     Multi-lane study proves Split Memory ($2 \times 128 \times 16$) adds only 1,775 cells
     (+9.2% area, ~40.5 kGE total) and fits comfortably in 8x4 tiles (<65% density).
- **What's verified:** 197/197 RTL tests pass via `scripts/regress.sh` and 8/8 gate-level timing tests pass via `scripts/test_gl.sh`:
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
  (126) Ethernet physical pin direction and electrical safety (uio_oe strictly 0x00 during RX/idle),
  (127) I2C Master read (0x38, payload 0xA5) translated into SPI Master Mode 0 egress on uio[6:4] verified against SpiSlave,
  (128) UART RX 0x55 on uio[0] translated to CAN 2.0A frame with bit-stuffing and CRC-15 on open-drain uio[4] verified against CanReceiverModel,
  (129) Dallas 1-Wire read timeslots on uio[0] for 0x3C translated to UART TX 8-N-1 on uio[4] verified against UartReceiver,
  (130) Multi-byte continuous UART-to-SPI streaming translation across [0x11, 0x22, 0x33] without cumulative drift,
  (131) Ingress framing error isolation (UART stop bit low halts with R2=0xFE, completely suppressing spurious CAN transmissions),
  (132) Bus bridge pin direction and electrical safety (unused pins strictly isolated in High-Z),
  (133) Hardware windowed watchdog periodic servicing with two-token key protocol (0x5A, 0xA5) with 0 timeouts (R2=0x00),
  (134) Task deadlock timeout trap asserting wdt_alarm and soft reset with status 0x02,
  (135) Windowed early-service violation detection (T < T_min) asserting reset with status 0x05,
  (136) Transient power brownout recovery pulse on rst_n triggering instant warm boot in 3 cycles (<10 cycles target) with RAM preserved and completed execution (R2=0xAA),
  (137) Watchdog and brownout reset physical electrical safety (uio_oe strictly 0x00 High-Z),
  (138) Micro-architectural power instruction profiling verifying ALU datapath power tracks carry propagation (308.52 uW ungated -> 17.09 uW gated, 1.77 pJ/insn),
  (139) Fine-grained WAIT stall clock gating achieving 98.68% dynamic power reduction (303.0 uW down to 3.99 uW) with exact 100% cycle-count determinism,
  (140) WAITEDGE low-power stall with deterministic 1-cycle instant wakeup latency upon pin edge detection,
  (141) External GPIO pad capacitive load linear energy scaling across 20 pF (5,324 uW) and 50 pF (13,310 uW) matching physical theoretical 2.50x ratio,
  (142) Protocol-level energy efficiency benchmarking on UART 8-N-1 (104.0 pJ/bit gated vs 308.0 pJ/bit ungated, 66.2% session energy reduction),
  (143) Core halted state zero dynamic switching confirmation with pin isolation and static leakage baseline (0.58 uW),
  (144) 32-bit multi-precision addition with 4-byte carry propagation (0x12345678 + 0x11111111 = 0x23456789),
  (145) RFC 8439 ChaCha ARX quarter-round microcode step against reference (sum=51, rot=204),
  (146) Poly1305 MAC accumulation step and modular reduction (sum=57, mod_prod=148),
  (147) SHA-256 non-linear Choose (Ch) and Majority (Maj) bitwise functions (Ch=0xD8, Maj=0xE8),
  (148) Hardware cryptographic coprocessor PPA scaling (ChaCha8 33.0x speedup, SHA-256 44.0x speedup, +1.97% to +2.70% area),
  (149) Cryptographic routine physical electrical safety and pin isolation (uio_oe strictly 0x00 High-Z),
  (150) Cooperative priority preemption and priority execution order (Task 0 completes before Task 1, R0=15, R1=15, R3=0),
  (151) Round-robin time-slice fairness across 3 concurrent tasks with zero starvation (R1=10, R2=14),
  (152) Context switch fidelity with clean architectural register state restoration without corruption (R0=0x42),
  (153) Periodic hard real-time task deadline compliance with zero jitter (4 intervals of 10 cycles, R0=40),
  (154) Worst-case response latency (WCRL) bounds and cycle-accurate SchedulerModel validation (3 context switches),
  (155) Scheduler physical electrical isolation during dispatcher execution (uio_oe strictly 0x00 High-Z),
  (156) CAN FD single-cycle dual-rate switching (nominal arbitration 25-30 cycles, high-speed data phase 3-6 cycles, switchback to nominal ACK),
  (157) High-speed multi-byte payload buffer streaming without pipeline stalls,
  (158) Mathematical verification of CRC-17 (0x11BD5) and CRC-21 (0x0D9749) with 100% single-bit error detection,
   (159) Classical CAN backward compatibility mode when BRS=0 (constant bit rate throughout),
   (160) Hardware coprocessor PPA scaling model (5.76x speedup on 64 bytes, +1.81% area overhead),
   (161) CAN FD physical open-drain electrical safety and non-TX pin isolation (uio_oe & 0xFE == 0x00),
   (162) Physical die floorplan SSO simultaneous switching stability across all 8 bidirectional GPIO pins,
   (163) Adjacent pin drive isolation under alternating checkerboard patterns (0xAA / 0x55),
   (164) Analytical SSO ground bounce model bounding bounce to 64.0 mV (< 200 mV noise margin, 136 mV margin),
   (165) On-chip PDN IR drop model bounding drop to 7.77 mV (< 90 mV / 5% VDD budget),
   (166) Adjacent pin cross-talk capacitive coupling suppression factor Kc <= 0.0076 (> 42 dB isolation),
   (167) Physical GPIO padframe clean return to High-Z (uio_oe = 0x00) on program halt,
   (168) Asynchronous rising edge event capture via WAITEDGE with cycle timestamp capture in R3,
   (169) Level-sensitive IRQ detection on Pin 2 with active-high ACK pulse handshake,
   (170) Strict multi-channel priority event arbitration between Pin 0 and Pin 1 without inversion,
   (171) Nested register context save and restore preserving background registers (R0=0x42, R1=0x11),
   (172) Cycle-accurate HIC reference model and IHP 130nm PPA scaling validation (4-channel +145 cells, 8-channel +260 cells),
   (173) Event polling and dispatch electrical pin direction safety (uio_oe strictly 0x00 High-Z),
   (174) Authorized tenant execution in memory partition yielding back to supervisor with clean status R2=0x00,
   (175) Out-of-bounds pointer write attempt trapped with fault code R2=0xEE and pin tri-stating,
   (176) IO pin authorization mask enforcement trapping restricted pin assertion (R2=0xEA) with pin drive suppressed,
   (177) Temporal cycle execution budget enforcement preempting runaway tasks with fault code R2=0xEB,
    (178) Cycle-accurate hardware MPU model and IHP 130nm PPA scaling validation across 2, 4, and 8 regions,
    (179) Fail-safe quarantine electrical pin safety (uio_oe strictly 0x00 High-Z throughout),
    (180) Software bitwise Galois CRC-16 computation into R0:R1 with polynomial 0x1021 matching RFC reference,
    (181) Hardware coprocessor byte-streaming CRC accumulation with 64x throughput speedup,
    (182) Multi-polynomial CRC calculation across CRC-16/CCITT, CRC-16/MODBUS, and CRC-32/IEEE 802.3 RFC vectors,
    (183) Single-bit payload transmission error detection and fault code trapping (R2 = 0xCE),
    (184) Hardware CRC coprocessor PPA scaling model across CRC-16, CRC-32, and universal engine on IHP 130nm,
    (185) CRC stream processing GPIO electrical safety with pins tri-stated throughout,
    (186) Master broadcast CCC frame (0x7E + CCC_ENEC) with clean target ACKs across all 3 phases (R0 = 0x00),
    (187) Dynamic Address Assignment (ENTDAA) assigning dynamic address 0x08 to target,
    (188) Open-drain 48-bit Provisional ID wired-AND arbitration between two competing targets without collision,
    (189) Dynamic transition from open-drain addressing to active push-pull SDR data transfer (0xA5),
    (190) In-Band Interrupt (IBI) detection on SDA low with event code trapping (R2 = 0x1B),
    (191) Hardware I3C accelerator PPA scaling validation (320 cells, +1.66% area) and safe High-Z pin electrical isolation (uio_oe = 0x00),
    (192) Quadrature Encoder forward (CW) rotation detection counting +4 edges (R3 = 4),
    (193) Quadrature Encoder reverse (CCW) rotation detection counting -4 edges (R3 = 0xFC = 252),
    (194) Quadrature Encoder dynamic bidirectional movement (+3 CW, -2 CCW, net R3 = 1),
    (195) Quadrature Encoder Index pulse detection on Pin 2 latching zero position (R2 = 1, R3 = 0x5A),
    (196) Quadrature Encoder velocity estimation via WAITEDGE elapsed cycle counter period measurement (R3 = 43 cycles),
    (197) Dedicated hardware QEI peripheral PPA scaling validation (+351 cells, +1.84% area overhead, 806.5 MHz max pulse frequency) and reference decoder verification.
- **Git:** Sequence of small, reviewable commits (`git log`).

## Repository map

```text
src/            RTL: project.v (TT wrapper), core.v, alu.v, gpio.v, program_ram.v
firmware/       Assembly programs (loop_demo.asm)
tools/          assembler.py, isa_model.py, uart_model.py, spi_model.py, i2c_model.py, onewire_model.py, ps2_model.py, jtag_model.py, swd_model.py, manchester_model.py, can_model.py, dmx512_model.py, autobaud_model.py, hdlc_model.py, classifier_model.py, fault_injector_model.py, multilane_model.py, usb_model.py, pipeline_model.py, protocol_fuzzer.py, ethernet_model.py, bridge_matrix_model.py, watchdog_model.py, power_model.py, crypto_model.py, scheduler_model.py, canfd_model.py, floorplan_model.py, interrupt_model.py, mpu_model.py, crc_model.py, i3c_model.py, qei_model.py, fuzzer.py
test/           cocotb test suite (test, test_uart, test_opcodes, test_waitedge, test_fuzz, test_spi, test_i2c, test_bootload, test_onewire, test_ps2, test_jtag, test_swd, test_manchester, test_can, test_dmx512, test_autobaud, test_hdlc, test_classifier, test_fault_injection, test_multilane, test_usb, test_pipeline, test_protocol_fuzz, test_ethernet, test_bridge_matrix, test_watchdog, test_power, test_crypto, test_scheduler, test_canfd, test_floorplan, test_interrupt, test_mpu, test_crc, test_i3c, test_qei, test_gate_level)
formal/         SymbiYosys formal harness (core.sby, core_formal.v)
scripts/        setup_env.sh, regress.sh, test_gl.sh, mutate.py, synth.sh, synth.ys
docs/           architecture, ISA, verification, toolchain, PPA, limitations, multilane_study, ethernet_study, watchdog_study, power_study, crypto_study, scheduler_study, canfd_study, floorplan_study, interrupt_study, mpu_study, crc_study, i3c_study, qei_study
orchestrator/   Durable state (decisions.md, queue.md, metrics.json, experiments.jsonl)
```

## Get productive in 2 minutes

```bash
bash scripts/setup_env.sh   # one-time toolchain install (see docs/toolchain.md)
bash scripts/regress.sh     # runs all 197 cocotb regression tests (~60s)
bash scripts/test_gl.sh     # runs gate-level timing simulation (8/8 tests pass, ~30s)
sby -f formal/core.sby      # runs SymbiYosys formal verification with Z3 (20 steps pass)
python3 scripts/mutate.py   # runs RTL mutation testing campaign (41/41 killed)
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
- Multi-Protocol Bus Bridging Matrix implemented and proven across I2C-to-SPI, UART-to-CAN (with bit-stuffing, CRC-15, and ACK monitoring), 1-Wire-to-UART, and continuous multi-byte streaming with ingress error isolation.
- Hardware Watchdog Timer & Brownout Recovery Subsystem implemented with windowed timing [T_min, T_max], two-token key protocol (0x5A, 0xA5), sticky reset status registers, and instant warm-boot recovery (<10 cycles).
- Dynamic Power & Energy Optimization Study completed: Program RAM write clock gating cuts active execution dynamic power by >94%, stall clock gating reduces `WAIT`/`WAITEDGE` power by 98.68% (303.0 uW down to 3.99 uW) with deterministic 1-cycle edge wakeup latency, and protocol energy efficiency benchmarked (10BASE-T 18.5 pJ/bit, SPI 27.0 pJ/bit, UART 104 pJ/bit).
- Cryptographic Accelerator Feasibility Study completed: Pure 8-bit multi-precision bit-sliced microcode executes ChaCha8 (606 kbps) and SHA-256 (909 kbps) with zero silicon area overhead for secure CAN/UART/I2C authentication, while dedicated hardware coprocessor macros (+380 to +520 cells, +1.97% to +2.70% area) deliver 33x to 44x speedups (20 to 40 Mbps) for line-rate 10BASE-T Ethernet and SPI.
- Deterministic Real-Time Task Scheduling Engine completed: Cooperative priority dispatching, round-robin fair time-slicing across concurrent tasks, context switch fidelity, and hard periodic deadline compliance verified on the 8-bit core with ultra-low context-switch latency (4-5 cycles, 400-500 ns at 10 MHz), bounded non-preemptible loops (B <= 12 cycles) guaranteeing WCRL R <= 17 cycles (1.7 us), and 0 additional silicon gates.
- CAN FD Flexible Data-Rate Protocol Accelerator Engine completed: Single-cycle dual-rate switching from 500 kbps nominal arbitration to 2.0 Mbps data phase at the BRS sample point and switchback to nominal ACK, 64-byte payload streaming (5.76x speedup over CAN 2.0), CRC-17/21 validation, and classical CAN fallback compatibility verified with 0 additional silicon gates.
- Physical Die Floorplan, Pad Placement & Package Pinout Co-Design Study completed: 2x2 tile footprint achieves 58.4% standard cell density with 100% routability, IO pad buffers selected from `sg13cmos5l_io`, QFN-64 leadframe pinout mapped, simultaneous switching output (SSO) ground bounce bounded at 64.0 mV (<3.6% VDD, >136 mV margin to 200 mV noise threshold), adjacent pin cross-talk coupling factor Kc <= 0.0076 (>42 dB isolation), and on-chip PDN IR drop bounded at 7.77 mV (<0.45% VDD) with zero additional silicon gates.
- Asynchronous Event Notification & Interrupt Controller Subsystem completed: Zero-overhead microcode event dispatching via WAITEDGE achieves single-cycle edge wake and low-power stall with 0 silicon gates and <= 1.6 us response latency, while a synthesizable hardware interrupt controller (HIC) macro adds only 145 cells (284 GE, +0.75% area) on IHP 130nm SG13G2 to reduce dispatch latency from 15 cycles down to 9 cycles (1.67x speedup).
- Memory Protection Unit (MPU) & Multi-Tenant Partitioning Engine completed: Zero-silicon software sandboxing enforces spatial partition bounds, IO pin protection masks, and temporal cycle budgets with 0 additional gates, while a dedicated 4-region synthesizable hardware MPU macro adds only 384 standard cells (+1.99% area overhead) with 1.85 ns comparator delay on IHP 130nm SG13G2.
- Hardware-Assisted Cyclic Redundancy Check (CRC-16/CRC-32) Coprocessor Macro PPA Feasibility Study completed: Pure software bitwise Galois CRC consumes 64-96 cycles/byte (limiting throughput to <= 160 kbps at 10 MHz), whereas a parallel GF(2) matrix compression LFSR achieves single-cycle byte ingestion (80 Mbps line rate at 10 MHz; >1.44 Gbps at 180 MHz max synthesized frequency), yielding a 64x throughput speedup. A universal multi-polynomial macro (CRC-16/CCITT, CRC-16/MODBUS, CRC-32/IEEE 802.3) requires 245 standard cells (480 GE, +1.27% area overhead) with 1.42 ns propagation delay, fitting comfortably within the Tiny Tapeout tile budget with >90 ns timing margin.
- MIPI I3C v1.1.1 Sensor Protocol & Dynamic Address Assignment (DAA) Acceleration Engine completed: Dynamic open-drain to active CMOS push-pull line switching (via GODRI) eliminates pullup rise-time delays to unlock 12.5 MHz SDR line-rate data transfers, ENTDAA automated broadcast address assignment arbitrates 48-bit Provisional IDs with zero bus collisions, and In-Band Interrupts (IBI) trap service requests (R2=0x1B) with zero dedicated IRQ pins. Standard SDR DAA macro requires 320 standard cells (620 GE, +1.66% area overhead, 2.15 ns delay) on IHP 130nm SG13G2.
- Quadrature Encoder Interface (QEI) & Industrial Motion Feedback Engine completed: 1X/2X/4X quadrature phase decoding, forward/reverse direction determination (+4 / -4 counts), index pulse zero homing calibration (R2=1, R3=0x5A), and period-based velocity estimation via WAITEDGE elapsed cycle counter (R3=43 cycles) verified on the 8-bit core with 0 silicon gates, while a dedicated synthesizable peripheral macro (+351 cells, +1.84% area overhead) supports pulse rates >800 MHz on IHP 130nm SG13G2.

## What to work on next

Full prioritized backlog: `orchestrator/queue.md`. Entering continuous loop:

1. Iteration 39: LIN (Local Interconnect Network) Automotive Protocol Engine & Break-Sync Frame Processor.
2. Iteration 40: High-Speed Synchronous Serial Interface (SSI / BiSS-C) Absolute Rotary Encoder Engine.
3. Iteration 41: MIL-STD-1553B Avionic Multiplex Data Bus Dual-Redundant Protocol Engine.
4. Iteration 42: Wiegand Security Access Control Protocol Reader/Writer & Pulse Width Discovery Engine.
5. Iteration 43: ARINC 429 Mark 33 Digital Information Transfer System (DITS) Avionic Protocol Engine.
6. Iteration 44: MIDI 2.0 (Universal MIDI Packet - UMP) High-Resolution Synthesizer & Control Protocol Engine.
7. Iteration 45: I2S (Inter-IC Sound) & TDM Digital Audio Multi-Channel Serial Interface Engine.
8. Iteration 46: SpaceWire (ECSS-E-ST-50-52C) Data-Strobe (DS) Spacecraft Serial Bus Protocol Engine.

## Keeping this file current

After completing ANY feature, task, or nontrivial change, before you
consider the work done:

1. Update "Current status" above to reflect what changed.
2. Update "What to work on next" if priorities shifted.
3. Keep it short - this is a summary. Full detail belongs in
   `orchestrator/decisions.md`, `orchestrator/experiments.jsonl`, and `docs/`.

A stale `CONTEXT.md` defeats its entire purpose. This requirement is also
recorded in `AGENTS.md`.
