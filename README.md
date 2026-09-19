# Programmable Protocol Emulator ASIC

[![GDS](https://github.com/Vedant817/ProtocolGremlin/actions/workflows/gds.yaml/badge.svg)](https://github.com/Vedant817/ProtocolGremlin/actions/workflows/gds.yaml)
[![Test](https://github.com/Vedant817/ProtocolGremlin/actions/workflows/test.yaml/badge.svg)](https://github.com/Vedant817/ProtocolGremlin/actions/workflows/test.yaml)

An open-source, general-purpose, programmable protocol-emulator ASIC built
for the [Jane Street Protocol Emulator ASIC Competition](https://blog.janestreet.com/can-you-design-a-chip/),
targeting **Tiny Tapeout / IHP 130nm CMOS5L** (deadline: January 18, 2027).

A small deterministic CPU whose 24-opcode instruction set is designed for
reading/writing pins, counting cycles, and hitting timing precisely enough
to implement real protocols in firmware — not a fixed UART+SPI+I2C
peripheral block. **90 protocol firmware programs** have been written and
verified, covering everything from UART to 10GBASE-R Ethernet.

## Architecture at a Glance

```
              ┌──────────────────────────────┐
              │  256-byte Program RAM        │
              │  (on-chip serial bootloader) │
              └──────────────┬───────────────┘
                             │
                    ┌────────┴────────┐
                    │  24-opcode ISA  │
                    │  4 registers    │
                    │  WAITEDGE       │
                    │  SHIFTOUT/IN    │
                    │  Open-Drain     │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
                    │  8-bit GPIO Bus │
                    │  uio[7:0]       │
                    └────────┬────────┘
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
     UART firmware     SPI firmware      I2C firmware
     CAN firmware     JTAG firmware     90+ protocols...
```

## Key Features

- **Reprogrammable after fabrication** — new protocols via serial bootloader
- **24 opcodes** with single-cycle deterministic execution
- **Hardware edge-detect** (`WAITEDGE`) with cycle-capture for autobaud/timing
- **Open-drain bus primitives** (`GODRI`/`GODR`) for I2C, 1-Wire, CAN
- **On-chip CRC-8 bootloader** with fail-safe halt on corruption
- **PVFI formal trace port** (zero silicon overhead)
- **19,291 cells / 37,832 GE** — fits in 8×4 Tiny Tapeout tiles

## Verification

| Metric | Result |
|--------|--------|
| RTL regression tests | **509/509 PASS** across 88 modules |
| Formal verification | **20-step Z3 BMC, 0 violations** |
| Mutation testing | **93/93 killed (100.0% kill rate)** |
| Gate-level simulation | **8/8 PASS** with real cell timing |
| Constrained-random fuzzing | 10 iterations per run, delta-debugging |
| Differential testing | Python ISA model vs RTL, cycle-accurate |

## Protocols Implemented (90 Iterations)

<details>
<summary>Click to expand full protocol list</summary>

### Core (Required)
- UART TX/RX (multiple baud rates)
- SPI Master (all 4 modes: CPOL×CPHA)
- I2C Master (clock stretching, arbitration, open-drain)

### Stretch Goals
- USB 1.1 Low-Speed, USB 2.0 Full-Speed, USB 3.0 SuperSpeed
- 10BASE-T, 100BASE-TX, 1000BASE-T, 10GBASE-R Ethernet

### Additional Protocols
Dallas 1-Wire, PS/2, JTAG IEEE 1149.1, ARM SWD, Manchester Biphase-L,
CAN 2.0A, CAN FD, DMX512, Autobaud, HDLC/SDLC, SpaceWire, ARINC 429,
MIL-STD-1553B, Wiegand, MIDI 2.0, I2S/TDM, SENT, LIN, SSI/BiSS-C,
Quadrature Encoder, MIPI I3C, MIPI I3C HDR-DDR, EtherCAT, Profibus DP,
Ethernet AVB/TSN, Modbus RTU/ASCII, FlexRay, CANopen/J1939, IEEE 1588 PTP,
SATA 3.0, PCIe Gen 1, MIPI D-PHY, MIPI C-PHY, DisplayPort 2.0, SAS-4,
RapidIO, InfiniBand, Fibre Channel, CXL/OpenCAPI, HyperTransport, UCIe,
NVLink, AXI4/AXI5, AXI4-Stream/TileLink, AHB-Lite/APB4,
Wishbone/Avalon-MM, AMBA CHI/ACE, HBM3, DDR5/LPDDR5, GDDR6, LPDDR4,
DDR4/DDR3, eMMC/SD, UFS, HMC, QDR-IV, RLDRAM 3

### Novel Capabilities
- Autonomous protocol sniffer & dynamic pattern classifier
- Deterministic fault injection & protocol stress engine
- Multi-lane dual-core architecture with mailbox IPC
- End-to-end sniff → classify → ingress → replay pipeline
- Cross-protocol translation bridge (UART↔SPI, I2C↔CAN, etc.)
- Automated constrained-random protocol fuzzer
- Hardware watchdog timer & brownout recovery
- Memory protection unit & multi-tenant partitioning
- Hardware CRC-16/CRC-32 coprocessor study
- Cryptographic accelerator feasibility (ChaCha8/Poly1305/SHA-256)

</details>

## Quick Start

```bash
# One-time toolchain setup (conda, no sudo required)
bash scripts/setup_env.sh

# Run the full regression suite (509 tests)
bash scripts/regress.sh

# Run synthesis (Yosys)
bash scripts/synth.sh

# Run gate-level simulation
bash scripts/test_gl.sh
```

## Repository Layout

```
src/            Synthesizable RTL (TT wrapper + core + ALU + GPIO + program RAM)
firmware/       Assembly programs for the core
tools/          assembler.py, isa_model.py (Python reference model), 90+ protocol models
test/           cocotb testbench (509 tests across 88 modules)
formal/         SymbiYosys formal verification harness (Z3 BMC)
scripts/        regress.sh, synth.sh, test_gl.sh, mutate.py
docs/           Architecture, ISA, verification, 90+ protocol study documents
orchestrator/   Durable project state for the continuous engineering loop
.github/        GitHub Actions CI (GDS build, test, docs, precheck)
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — System architecture
- [`docs/isa.md`](docs/isa.md) — Instruction set reference (24 opcodes)
- [`docs/verification.md`](docs/verification.md) — Verification methodology
- [`docs/info.md`](docs/info.md) — Tiny Tapeout datasheet
- [`PROJECT_MASTER_PLAN.md`](PROJECT_MASTER_PLAN.md) — Full competition brief & roadmap
- [`CONTEXT.md`](CONTEXT.md) — Current project status & orientation

## PPA Summary

| Metric | Value |
|--------|-------|
| Total cells | 19,291 |
| Gate equivalents | 37,832 GE |
| Core logic | ~1,580 cells (~2.2 kGE) |
| Program RAM | 91.8% of area (synthesized flip-flop matrix) |
| Target clock | 10 MHz |
| Tile allocation | 6×4 (24 tiles, ~0.7 mm²) |
| Density | <65% |

## AI-Assisted Development

This project was developed using an AI-assisted continuous engineering loop
with Google Antigravity, demonstrating:

- Automated iteration cycles with objective verification gates
- AI-generated protocol firmware validated against independent reference models
- Mutation testing to prove test effectiveness (93/93 architectural faults detected)
- Formal verification property generation and bounded model checking
- Constrained-random test generation with delta-debugging minimization

## License

Apache-2.0 (matching the Tiny Tapeout template), see [`LICENSE`](LICENSE).
