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

- **Phase:** ISA v1 complete — Iterations 1–16 complete (DMX512 ANSI E1.11 / USITT DMX512-A Stage Lighting Protocol Engine, CAN 2.0A Controller Physical-Layer Protocol Engine, Manchester Biphase-L IEEE 802.3 / MIL-STD-1553 Encoder & Decoder Engine, ARM SWD Interface Engine & DPIDR Readout, JTAG IEEE 1149.1 TAP Controller Engine with 32-bit IDCODE Readout & BYPASS Verification, PS/2 Bidirectional Host Controller Engine, Dallas 1-Wire Master with Single-Cycle Presence Pulse Discovery, Zero-Jitter UART RX with WAITEDGE, Bootloader CRC-8 Hardware Protection, I2C Clock Stretching & Arbitration Detection, I2C Master, hardware open-drain, SPI Master, MSB shifts, UART TX, WAITEDGE, PVFI formal, mutation testing, fuzzer, synthesis).
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
     and DMX512 engine with Break pulse duration measurement in `R3` and per-slot `WAITEDGE` edge synchronization (`tools/dmx512_model.py`),
     paired with independent `UartReceiver`, `UartTransmitter`, `SpiSlave`, `I2cSlave`, `OneWireSlave`, `PS2Device`, `JtagTarget`, `SwdTarget`, `ManchesterDecoder`, `CanReceiverModel`, and `Dmx512ReceiverModel` verification models.
  6. **Mutation Testing:** Standalone harness (`scripts/mutate.py`) testing 20
     architectural fault categories, measuring **100.0% kill rate (20/20 killed)**
     (citing Huang et al. 2015, Firefly 2025).
  7. **Constrained-Random Fuzzing:** Automated instruction fuzzer (`tools/fuzzer.py`)
     with delta-debugging program shrinker, verified in `test/test_fuzz.py`.
  8. **Real PPA Baseline:** Mapped with Yosys 0.69+ (`scripts/synth.sh`), measuring
     19,291 CMOS cells (37,832 GE). Active processor logic is only 1,580 cells
     (~2.2 kGE) with 91.8% of cells in the synthesized flip-flop RAM matrix.
     Fits the 8x4 competition tile footprint with >80 ns timing slack at 10 MHz.
- **What's verified:** 67/67 test suites pass cleanly via `scripts/regress.sh`:
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
  (67) DMX512 physical pin direction electrical safety (strictly input in RX, single output in TX).
- **Git:** Sequence of small, reviewable commits (`git log`).

## Repository map

```text
src/            RTL: project.v (TT wrapper), core.v, alu.v, gpio.v, program_ram.v
firmware/       Assembly programs (loop_demo.asm)
tools/          assembler.py, isa_model.py, uart_model.py, spi_model.py, i2c_model.py, onewire_model.py, ps2_model.py, jtag_model.py, swd_model.py, manchester_model.py, can_model.py, dmx512_model.py, fuzzer.py
test/           cocotb test suite (test, test_uart, test_opcodes, test_waitedge, test_fuzz, test_spi, test_i2c, test_bootload, test_onewire, test_ps2, test_jtag, test_swd, test_manchester, test_can, test_dmx512)
formal/         SymbiYosys formal harness (core.sby, core_formal.v)
scripts/        setup_env.sh, regress.sh, mutate.py, synth.sh, synth.ys
docs/           architecture, ISA, verification, toolchain, PPA, limitations
orchestrator/   Durable state (decisions.md, queue.md, metrics.json, experiments.jsonl)
```

## Get productive in 2 minutes

```bash
bash scripts/setup_env.sh   # one-time toolchain install (see docs/toolchain.md)
bash scripts/regress.sh     # runs all 67 cocotb regression tests (~48s)
sby -f formal/core.sby      # runs SymbiYosys formal verification with Z3
python3 scripts/mutate.py   # runs RTL mutation testing campaign (20/20 killed)
bash scripts/synth.sh       # runs Yosys synthesis and outputs cell/area metrics
```

## Key decisions (full log: `orchestrator/decisions.md`)

- Built on the real `TinyTapeout/ttihp-verilog-template` (`cmos5l` branch).
- No sudo/root required; toolchain runs from Miniforge/conda-forge.
- Programmable protocol GPIO mapped to bidirectional `uio[7:0]`.
- Genuinely reprogrammable program RAM loaded via serial bootloader with CRC-8 frame integrity protection.
- Multi-tiered verification: differential cycle-by-cycle testing, SymbiYosys formal
  safety proofs, independent protocol decoders, 100% mutation kill rate, and
  constrained-random fuzzing with automated shrinking.
- Synthesized PPA measured: active core is 1,580 cells (~2.2 kGE), RAM is 17,757 cells.

## What to work on next

Full prioritized backlog: `orchestrator/queue.md`. Entering continuous loop:

1. Iteration 17: Synchronous Program RAM Upgrade (`program_ram.v`) / PPA Timing Analysis.
2. Iteration 18: Pure Firmware Autobaud Rate Auto-Discovery Engine.
3. Iterations 19+: Dual-lane architecture (`core_dual.v`).

## Keeping this file current

After completing ANY feature, task, or nontrivial change, before you
consider the work done:

1. Update "Current status" above to reflect what changed.
2. Update "What to work on next" if priorities shifted.
3. Keep it short - this is a summary. Full detail belongs in
   `orchestrator/decisions.md`, `orchestrator/experiments.jsonl`, and `docs/`.

A stale `CONTEXT.md` defeats its entire purpose. This requirement is also
recorded in `AGENTS.md`.
