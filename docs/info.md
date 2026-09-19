<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

The Programmable Protocol Emulator is a deterministic, reprogrammable protocol engine ASIC — a small CPU with an instruction set specifically designed for reading pins, writing pins, counting cycles, and hitting timing precisely enough that any low-to-medium-speed digital protocol can be implemented in firmware rather than fixed logic.

### Architecture

- **24-opcode ISA** with single-cycle execution and deterministic timing
- **4 general-purpose 8-bit registers** (R0–R3) for data manipulation
- **256-byte writable program RAM** loaded via an on-chip serial bootloader
- **8-bit bidirectional GPIO bus** (`uio[7:0]`) with per-pin direction control
- **WAITEDGE** hardware edge-detect with cycle-capture timing discovery (autobaud, pulse measurement)
- **SHIFTOUT/SHIFTIN** bit-serial shift primitives with MSB/LSB direction select
- **GODRI/GODR** hardware open-drain primitives for contention-free I2C / 1-Wire / CAN buses
- **On-chip serial bootloader** with hardware CRC-8 frame integrity verification (poly 0x07, init 0x00)
- **PVFI formal trace port** (Protocol-engine Verification Formal Interface) with zero silicon overhead

### Key instructions

| Opcode | Mnemonic | Description |
|--------|----------|-------------|
| 0x00 | NOP | No operation (1 cycle) |
| 0x01 | LOAD | Load immediate → register |
| 0x02 | ADD | R0 = R0 + Rsrc |
| 0x03 | SUB | R0 = R0 − Rsrc |
| 0x07 | SHIFTOUT | Bit-serial output (MSB/LSB selectable) |
| 0x08 | SHIFTIN | Bit-serial input (MSB/LSB selectable) |
| 0x09 | GWR | Write register to GPIO output |
| 0x0A | GRD | Read GPIO input pins to register |
| 0x0B | GDIR | Set GPIO direction (per-pin output enable) |
| 0x0D | WAIT | Deterministic cycle delay |
| 0x0E | WAITEDGE | Wait for pin edge, capture pulse width in R3 |
| 0x10 | JMP | Unconditional jump |
| 0x11 | JZ | Jump if register is zero |
| 0x14 | DECJNZ | Decrement register, jump if nonzero (compact loops) |
| 0x16 | GODRI | Open-drain: drive low or release (high-Z) |
| 0x17 | GODR | Open-drain: read bus state |

### Verified protocols (90 firmware programs)

The ISA has been validated by implementing 90+ protocol engines entirely in firmware, including:

**Required:** UART TX/RX, SPI Master (4 modes), I2C Master (with clock stretching and arbitration)

**Stretch goals:** USB 1.1 Low-Speed, USB 2.0 Full-Speed, USB 3.0 SuperSpeed, 10BASE-T Ethernet, 100BASE-TX Fast Ethernet, 1000BASE-T Gigabit Ethernet, 10GBASE-R 10GbE

**Extra protocols:** JTAG, ARM SWD, PS/2, CAN 2.0A, CAN FD, Dallas 1-Wire, Manchester, HDLC, DMX512, SpaceWire, ARINC 429, MIL-STD-1553B, FlexRay, EtherCAT, Profibus DP, Modbus RTU/ASCII, MIDI 2.0, I2S/TDM, SENT, Wiegand, LIN, SSI/BiSS-C, Quadrature Encoder, MIPI D-PHY, MIPI C-PHY, DisplayPort 2.0, SATA 3.0, PCIe Gen 1, SAS-4, RapidIO, InfiniBand, Fibre Channel, CXL/OpenCAPI, HyperTransport, UCIe, NVLink, AXI4/AXI5, AXI4-Stream/TileLink, AHB-Lite/APB4, Wishbone/Avalon, AMBA CHI, HBM3, DDR5/LPDDR5, GDDR6, LPDDR4, DDR4/DDR3, eMMC/SD, UFS, HMC, QDR-IV, RLDRAM 3, and more.

### Verification methodology

- **509/509 RTL regression tests** (cocotb + Icarus Verilog)
- **20-step Z3 bounded model checking** (SymbiYosys) — 0 violations
- **93/93 mutation kills** (100% fault detection rate)
- **8/8 gate-level tests** with real standard cell timing models
- **Constrained-random fuzzing** with delta-debugging shrinker
- **Differential testing** against independent Python ISA reference model

### PPA (Power, Performance, Area)

- **19,291 CMOS cells** (37,832 gate equivalents)
- Core logic: ~1,580 cells (~2.2 kGE), 91.8% is synthesized program RAM
- Fits comfortably in **8×4 Tiny Tapeout tiles** (<65% density)
- **10 MHz** target clock verified against synthesis critical path

## How to test

### Bootloading a program

1. Assert `LOAD_REQ` (ui_in[0] = 1) while holding reset
2. Release reset — the bootloader FSM activates
3. Clock in program bytes via `LOAD_DATA` (ui_in[1]) and `LOAD_CLK` (ui_in[2]):
   - Each frame: 8 data bits (MSB first) followed by an 8-bit CRC-8 checksum
   - CRC polynomial: 0x07, initial value: 0x00
4. After all bytes are loaded, deassert `LOAD_REQ`
5. `BOOT_DONE` (uo_out[0]) goes high — the processor begins executing from address 0
6. If CRC fails, `BOOT_ERR` (uo_out[1]) goes high and the processor halts permanently

### Running a protocol

Once bootloaded, the processor executes the firmware program which controls `uio[7:0]` to implement the target protocol. For example:

- **UART TX:** Firmware bit-bangs start bit, 8 data bits, stop bit on a single GPIO pin
- **SPI Master:** Firmware generates SCLK, drives MOSI, samples MISO with configurable CPOL/CPHA
- **I2C Master:** Firmware uses open-drain primitives (GODRI/GODR) for SDA/SCL with clock stretching support

### Simulation

```bash
# Install toolchain (conda, no sudo required)
bash scripts/setup_env.sh

# Run full 509-test regression suite
bash scripts/regress.sh

# Run synthesis
bash scripts/synth.sh

# Run gate-level simulation
bash scripts/test_gl.sh
```

## External hardware

No external hardware is required for simulation and verification.

For post-fabrication testing on the Tiny Tapeout dev board:
- Protocol-specific external devices (e.g., UART adapter, SPI flash, I2C sensor) connected to `uio[7:0]`
- Logic analyzer or oscilloscope for waveform inspection
- A host microcontroller (e.g., RP2040, STM32) or FTDI adapter for bootloading firmware via `ui_in[0:2]`
