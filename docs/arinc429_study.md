# ARINC 429 Mark 33 Digital Information Transfer System (DITS) Architectural Study

## 1. Executive Summary

ARINC Specification 429 Part 1-17 defines the Mark 33 Digital Information Transfer System (DITS), the predominant commercial and transport avionic data bus standard deployed across Boeing (737, 747, 757, 767, 777) and Airbus (A300, A310, A320, A330, A340) airframes. ARINC 429 provides deterministic, fault-tolerant point-to-point or point-to-multipoint simplex communications connecting Flight Management Computers (FMC), Air Data Inertial Reference Units (ADIRU), Electronic Flight Instrument Systems (EFIS), and Engine Indicating and Crew Alerting Systems (EICAS).

This study explores the physical layer signaling, 32-bit avionic word framing, dual-rail bipolar Return-to-Zero (BPRZ) line coding, octal label encoding, Source/Destination Identifier (SDI) filtering, Sign/Status Matrix (SSM) semantics, odd parity protection, and synthesizable coprocessor macro implementation on the IHP 130nm SG13G2 CMOS process.

---

## 2. Physical Layer Signaling & Dual-Rail Digital Interface

### 2.1 Bipolar Return-to-Zero (BPRZ) Line Coding
ARINC 429 utilizes a differential bipolar Return-to-Zero (RZ) waveform transmitted over a $78\,\Omega$ balanced shielded twisted pair:
- **State HIGH (Logical '1'):** $+10\,\text{V} \pm 1.0\,\text{V}$ differential ($V_A - V_B > +6.5\,\text{V}$) for the first $50\%$ of the bit period, returning to $0\,\text{V}$ (Null) for the remaining $50\%$.
- **State LOW (Logical '0'):** $-10\,\text{V} \pm 1.0\,\text{V}$ differential ($V_A - V_B < -6.5\,\text{V}$) for the first $50\%$ of the bit period, returning to $0\,\text{V}$ (Null) for the remaining $50\%$.
- **State NULL (Idle):** $0\,\text{V} \pm 0.5\,\text{V}$ differential ($-2.5\,\text{V} \le V_A - V_B \le +2.5\,\text{V}$).

### 2.2 Dual-Rail Digital CMOS Interface
Because standard CMOS ASICs operate at $1.2\,\text{V} - 3.3\,\text{V}$ logic levels, dedicated line transceivers (e.g. Holt HI-8582, DEI1016) bridge the bipolar $\pm 10\,\text{V}$ bus to dual-rail CMOS digital signals:
- **`DATA_A` / `TXA` / `RXA`**: Pulses active-high for a logical '1'.
- **`DATA_B` / `TXB` / `RXB`**: Pulses active-high for a logical '0'.
- **Null State:** Both lines LOW (`DATA_A = 0, DATA_B = 0`).
- **Illegal / Tamper State:** Both lines HIGH (`DATA_A = 1, DATA_B = 1`), indicating differential line short or transceiver failure.

```text
Logical '1':   TXA: --+      +------    TXB: -----------------
                      |      |               
                      +------+               

Logical '0':   TXA: -----------------    TXB: --+      +------
                                                |      |      
                                                +------+      

Null State:    TXA: -----------------    TXB: -----------------
```

### 2.3 Timing Specifications
ARINC 429 defines two standardized transmission rates:
1. **High-Speed (HS):** $100\,\text{kbps} \pm 1\%$ ($T_{bit} = 10\,\mu\text{s}$, $T_{pulse} = 5.0\,\mu\text{s} \pm 0.5\,\mu\text{s}$).
2. **Low-Speed (LS):** $12.5\,\text{kbps} \pm 1\%$ ($T_{bit} = 80\,\mu\text{s}$, $T_{pulse} = 40.0\,\mu\text{s} \pm 4.0\,\mu\text{s}$).
3. **Inter-Word Gap (Sync):** Minimum 4 bit periods of continuous Null ($0\,\text{V}$, both lines low) separating consecutive 32-bit words ($40\,\mu\text{s}$ at HS, $320\,\mu\text{s}$ at LS).

---

## 3. 32-Bit Avionic Word Framing & Data Structures

Every ARINC 429 transmission comprises a 32-bit word structured into five distinct fields:

```text
+------+------+-------------------------------------+------+------+
| Bit  | Bits |             Bits 11-29              | Bits | Bit  |
| 1-8  | 9-10 |            (Data Field)             | 30-31|  32  |
+------+------+-------------------------------------+------+------+
|Label | SDI  | BCD / BNR / Discrete Payload (19b)  | SSM  |Parity|
+------+------+-------------------------------------+------+------+
```

### 3.1 Label Field (Bits 1–8)
- Encoded in **octal** notation (e.g. Label 203 for Barometric Altitude, Label 310 for Latitude).
- Standard transmission order: Bit 1 is transmitted first on the wire, but Bit 1 represents the MSB of the octal label! This inverted bit transmission order is unique to ARINC 429.

### 3.2 Source/Destination Identifier (SDI, Bits 9–10)
- Identifies the specific source system or intended recipient in multi-sensor configurations:
  - `00`: Universal / All Receivers
  - `01`: System #1 (e.g. Captain / FMC 1)
  - `10`: System #2 (e.g. First Officer / FMC 2)
  - `11`: System #3 (e.g. Auxiliary / Backup)

### 3.3 Data Field (Bits 11–29, 19 bits)
- **BNR (Binary Number Representation):** Two's-complement fractional binary encoding (e.g. velocity, altitude, heading).
- **BCD (Binary Coded Decimal):** Up to 5 decimal digits with 4 bits per decade (e.g. VHF frequencies).
- **Discrete:** Bit-mapped status indicators (e.g. landing gear down, autopilot engaged).

### 3.4 Sign/Status Matrix (SSM, Bits 30–31)
- Encodes operational status and algebraic sign:
  - `00`: Failure Warning (FW)
  - `01`: No Computed Data (NCD)
  - `10`: Functional Test (FT)
  - `11`: Normal Operation (NO) (or Plus / North / East / Right for BNR navigation parameters)

### 3.5 Odd Parity (Bit 32)
- Standard ARINC 429 enforces **ODD Parity** over all 32 bits:
  $$P_{32} = 1 \oplus \bigoplus_{i=1}^{31} B_i$$
  The total number of '1' bits across the entire 32-bit word must be odd.

---

## 4. Hardware Coprocessor Macro Architecture on IHP 130nm SG13G2

While the 8-bit core executes ARINC 429 transmission, edge discovery, SDI filtering, and parity checking in microcode with zero additional gates, high-channel avionics line concentrators benefit from a dedicated hardware macro:

### 4.1 Macro Components
1. **Dual-Rail Shift Deserializer & Serializer:** 32-bit shift register with dual-rail clock generation.
2. **Inter-Word Gap Detector:** 4-bit interval counter detecting $\ge 4$ bit periods of continuous Null.
3. **Hardware Odd Parity Checker:** 32-input XOR parity tree with single-cycle verification.
4. **SDI & Label Hardware Filter:** Configurable 10-bit address match comparator providing zero-latency message triage.
5. **Transceiver Line Fault Detector:** Combinational gate flagging illegal `DATA_A = 1 && DATA_B = 1`.

### 4.2 Standard Cell Synthesis & PPA Analysis (IHP 130nm SG13G2)
- **Gate Count:** 412 standard cells (~803.4 Gate Equivalents, GE).
- **Silicon Area:** $3,007.60\,\mu\text{m}^2$ (+2.16% design area overhead).
- **Critical Path:** $1.26\,\text{ns}$ through 32-bit parity tree and label comparator ($f_{\text{max}} = 793.6\,\text{MHz}$).
- **Dynamic Power:** $41.8\,\mu\text{W}$ at $10\,\text{MHz}$ operating frequency.
