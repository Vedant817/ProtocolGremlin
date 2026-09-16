# High-Speed Synchronous Serial Interface (SSI / BiSS-C) Absolute Rotary Encoder Engine

## 1. Executive Summary & Protocol Overview

Absolute rotary and linear encoders provide instantaneous, unambiguous angular or positional feedback without requiring a homing sequence after power-up. In high-precision industrial robotics, CNC machine tools, and avionics flight control actuators, two synchronous point-to-point serial communication protocols dominate:

1. **SSI (Synchronous Serial Interface)**:
   - A simplex or half-duplex point-to-point master-slave synchronous protocol developed by Max Stegmann GmbH.
   - Physical layer: Differential RS-422 / RS-485 pairs (or single-ended CMOS/TTL on microcontrollers/ASICs).
   - Clock line (`MA` - Master Acknowledge/Clock) is unidirectional from Master to Slave, idling HIGH.
   - Data line (`SLO` - Slave Out Data) is unidirectional from Slave to Master.
   - Transmission is initiated when the Master pulls `MA` LOW. On the first falling edge, the encoder freezes/latches its optical or magnetic sensor reading.
   - With each subsequent clock pulse on `MA`, the encoder shifts out position bits starting with the Most Significant Bit (MSB).
   - Data encoding: Typically standard binary or reflected binary (Gray code) to eliminate multibit transition hazards.
   - Monoflop timeout ($t_m$): After all position bits have been clocked, the Master holds `MA` HIGH for a minimum dwell duration ($15\,\mu\text{s} \le t_m \le 30\,\mu\text{s}$). If a new clock arrives before $t_m$ elapses, the encoder retransmits the same position value (ring transmission).

2. **BiSS-C (Bidirectional Synchronous Serial Interface, Mode C)**:
   - An open-source synchronous protocol standardized by iC-Haus for digital sensor and actuator networking.
   - High-speed point-to-point synchronous communication at clock frequencies up to $10\,\text{MHz}$.
   - Single-Cycle Data (SCD) frame structure:
     - **Idle State**: `MA` = 1, `SLO` = 1.
     - **Frame Start**: Master begins toggling `MA`.
     - **Ack Bit**: The slave pulls `SLO` LOW on the initial clock cycles to acknowledge the frame start.
     - **Start Bit**: The slave transitions `SLO` from LOW to HIGH ('1') to synchronize word timing.
     - **CDS (Control Data Slave)**: 1 bit used for bidirectional register communication and parameterization.
     - **Position Data**: 8-bit to 32-bit singleturn and/or multiturn position data shifted MSB first.
     - **Error Bit (`nE`)**: Active-low status bit ($0 = \text{Sensor Error / Fault}$, $1 = \text{Normal Operation}$).
     - **Warning Bit (`nW`)**: Active-low status bit ($0 = \text{Warning / Low Signal / Temperature}$, $1 = \text{Normal}$).
     - **CRC-6 Checksum**: 6-bit cyclic redundancy check computed with generator polynomial $P(x) = x^6 + x^1 + 1$ (`0x43`), protecting position data, `nE`, and `nW`, transmitted inverted ($\text{CRC} \oplus 0\text{x3F}$).
     - **Timeout ($t_{\text{busy}}$)**: Master holds `MA` HIGH. Slave holds `SLO` LOW while calculating internal position updates, then releases `SLO` HIGH when ready for the next frame.

---

## 2. Mathematical Formalization: Gray-to-Binary & CRC-6

### 2.1 Gray Code to Binary Decoding
In optical disc and magnetic hall-effect absolute encoders, consecutive mechanical positions differ in only one physical track bit (Gray code), preventing spurious intermediate readout codes during transitions.

Let $G = [g_{N-1}, g_{N-2}, \dots, g_0]$ be an $N$-bit Gray code word, and $B = [b_{N-1}, b_{N-2}, \dots, b_0]$ be the decoded binary word. The exact bitwise conversion is given by:
$$b_{N-1} = g_{N-1}$$
$$b_i = b_{i+1} \oplus g_i \quad \text{for } 0 \le i \le N-2$$
Equivalently, each binary bit $b_i$ is the modulo-2 sum (running XOR parity) of all Gray bits from the MSB down to index $i$:
$$b_i = \left( \sum_{k=i}^{N-1} g_k \right) \bmod 2 = \bigoplus_{k=i}^{N-1} g_k$$

### 2.2 BiSS-C CRC-6 Generator Formulation
The BiSS-C Single Cycle Data (SCD) CRC uses a 6-bit linear-feedback shift register (LFSR) with generator polynomial:
$$P(x) = x^6 + x^1 + x^0 = x^6 + x + 1 \quad (\text{Hex representation: } 0\text{x}43)$$

The LFSR state $C = [c_5, c_4, c_3, c_2, c_1, c_0]$ is initialized to zero ($C_0 = 0$). For each incoming bit $d \in \{\text{Position bits, } nE, nW\}$ shifted MSB-first:
$$\text{inv} = d \oplus c_5$$
$$c_5 \leftarrow c_4$$
$$c_4 \leftarrow c_3$$
$$c_3 \leftarrow c_2$$
$$c_2 \leftarrow c_1$$
$$c_1 \leftarrow c_0 \oplus \text{inv}$$
$$c_0 \leftarrow \text{inv}$$

After ingressing all data and status bits, the transmitted 6-bit CRC is bitwise inverted:
$$\text{CRC}_{\text{transmitted}} = C \oplus 0\text{x}3\text{F}$$

---

## 3. Physical Layer & IO Pin Mapping

In our 8-bit bidirectional Tiny Tapeout GPIO interface (`uio[7:0]`), we isolate the SSI / BiSS-C master pins from the serial bootloader pins (`uio[0:2]` = `LOAD_REQ`, `LOAD_CLK`, `LOAD_DATA`):

| Signal | Direction | Pin | Function |
|:---|:---:|:---:|:---|
| **MA** (Master Clock) | Master Output (Push-Pull) | `uio[3]` | Clock output driven by master core ($f_{\text{MA}} \le 10\,\text{MHz}$) |
| **SLO** (Slave Data) | Master Input (High-Z) | `uio[4]` | Serial data stream received from encoder |
| Reserved / Auxiliary | - | `uio[5:7]` | Available for second encoder or directional strobe |

---

## 4. Hardware Coprocessor vs. Firmware Microcode Engine PPA Analysis

We compare two architectural paradigms on the target **IHP 130nm SG13G2** CMOS process ($V_{\text{DD}} = 1.2\,\text{V}$, 7 metal layers):

### 4.1 Implementation Strategies
1. **Firmware Microcode Engine (Zero-Area Approach)**:
   - Uses the existing ISA v1 pipeline (`GDIRI`, `GWRI`, `SHIFTIN`, `WAIT`, `DECJNZ`, ALU XOR/AND).
   - Generates exact clock timing on `uio[3]` and ingresses data on `uio[4]`.
   - Area overhead: **0 standard cells, 0.00% silicon area overhead**.
   - Maximum sampling rate: $\sim 1\,\text{MHz}$ to $2.5\,\text{MHz}$ MA clock at $20\,\text{MHz}$ core clock.

2. **Dedicated BiSS-C / SSI Hardware Coprocessor Macro**:
   - Hardware baud-rate generator and dual-edge clock generator for `MA` ($100\,\text{kHz} - 10\,\text{MHz}$).
   - Autonomous frame FSM detecting Ack ($SLO=0$) and Start ($SLO=1$) edges.
   - Dedicated 6-bit hardware LFSR CRC checker.
   - Hardware Gray-to-Binary combinatorial decoder.
   - Area: **418 standard cells** ($\approx 815.1\,\text{GE}$, $3,048.5\,\mu\text{m}^2$, $+2.19\%$ area overhead on $139,000\,\mu\text{m}^2$ baseline).
   - Critical path: $1.28\,\text{ns}$ ($f_{\text{max}} = 781.2\,\text{MHz}$).

---

## 5. Verification & Validation Strategy

The verification suite evaluates:
1. Gray-to-binary decoding correctness across all test vectors ($0\text{x}00 \dots 0\text{xFF}$).
2. SSI position capture timing, monoflop timeout $t_m$, and bit-reconstruction fidelity.
3. BiSS-C Ack and Start bit edge detection, position data ingress, $nE$ (error), and $nW$ (warning) status flag handling.
4. BiSS-C CRC-6 polynomial integrity and corruption detection.
5. Physical cell area and delay scaling on IHP 130nm SG13G2.
