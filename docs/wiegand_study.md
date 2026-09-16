# Wiegand Access Control Protocol & Pulse Width Discovery Study

## 1. Executive Summary & Physical Principles

The **Wiegand protocol** is the predominant, ubiquitous physical-layer standard for electronic physical access control systems (PACS), RFID card readers (125 kHz HID Proximity, 13.56 MHz iCLASS/MIFARE), biometric fingerprint scanners, keypad credential units, and security portal controllers.

Originally derived from the magnetic polarization switching discovered by John R. Wiegand in 1974 using specially treated Vicalloy (cobalt-iron-vanadium) magnetic wires, the electrical standard has evolved into a two-wire asynchronous pulsed communication interface:
- **DATA0 (Green wire):** Active-low pulse indicates a logical bit **0**.
- **DATA1 (White wire):** Active-low pulse indicates a logical bit **1**.
- **GND (Black wire):** Shared electrical reference potential.
- **VCC (Red wire):** Auxiliary supply (+5V to +12V DC).

```text
  IDLE HIGH (+5V) ---------\                /------------------------
                            \              /
  DATA0 (Bit 0)              \____________/  <-- Pulse Width (T_pw ~ 50 us)
                             Falling Edge

  DATA1 (Bit 1)   --------------------------------------------------- (Remains HIGH)

                            |<-- T_pi ~ 1000 us -->|
  IDLE HIGH (+5V) -------------------------\                /--------
                                            \              /
  DATA1 (Bit 1)                              \____________/
                                             Falling Edge
```

### Physical Timing Characteristics

| Parameter | Description | Min | Typical | Max | ASIC Test Scale |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **$T_{pw}$** | Pulse Width (Active LOW duration) | $20\,\mu\text{s}$ | $50\,\mu\text{s}$ | $100\,\mu\text{s}$ | 8–16 cycles |
| **$T_{pi}$** | Pulse Interval (Leading edge to leading edge) | $200\,\mu\text{s}$ | $1000\,\mu\text{s}$ | $2000\,\mu\text{s}$ | 24–48 cycles |
| **$T_{frame}$** | Inter-Frame Gap (Transaction delimiter) | $25\,\text{ms}$ | $50\,\text{ms}$ | $\infty$ | 100+ cycles |
| **$V_{IL}$** | Input Low Voltage | $-0.3\,\text{V}$ | $0.0\,\text{V}$ | $0.8\,\text{V}$ | CMOS Ground |
| **$V_{IH}$** | Input High Voltage (external pullup) | $2.4\,\text{V}$ | $5.0\,\text{V}$ | $5.5\,\text{V}$ | CMOS $V_{DD}$ (3.3V) |

---

## 2. Standard 26-bit Wiegand Frame Structure (H10301)

The open, unencrypted 26-bit Wiegand format (SIA AC-01 / HID H10301) is the universal baseline credential format:

```text
  Bit Index:   25  24 23 22 21 20 19 18 17  16 15 14 13 12 11 10  9  8  7  6  5  4  3  2  1   0
  Field:      [EP] [---- Facility Code ----] [------------------- Card ID -------------------] [OP]
  Width:      1-bit          8-bits                                16-bits                    1-bit
```

1. **Leading Even Parity Bit ($EP = \text{Bit } 25$):**
   Calculated over the first 12 data bits (Bits 24 through 13):
   $$EP = \bigoplus_{i=13}^{24} B_i$$
   Ensures that the total number of 1s in bits $[25:13]$ is an **even** number.

2. **Facility Code (Bits 24–17):**
   8-bit integer representing the installation/site credential identifier ($0 \le FC \le 255$).

3. **Card ID (Bits 16–1):**
   16-bit integer representing the user's specific access card number ($0 \le \text{ID} \le 65,535$).

4. **Trailing Odd Parity Bit ($OP = \text{Bit } 0$):**
   Calculated over the last 12 data bits (Bits 12 through 1):
   $$OP = 1 \oplus \bigoplus_{i=1}^{12} B_i$$
   Ensures that the total number of 1s in bits $[12:0]$ is an **odd** number.

---

## 3. Micro-Architectural Implementation & WAITEDGE Synergy

### Reader Operation via GRD & Single-Cycle Polling
Wiegand signals are inherently asynchronous. While polling with microcontrollers usually introduces sampling jitter or consumes heavy MCU interrupt overhead, the protocol emulator ASIC executes single-cycle GPIO reads (`GRD Rd`) and branch-on-zero instructions to achieve jitter-free sub-cycle edge detection:
```asm
poll_wiegand_edge:
    GRD R0                  ; Read GPIO pin state
    ANDI R0, 0x18           ; Mask DATA0 (pin 3) and DATA1 (pin 4)
    CMPI R0, 0x18           ; Are both lines idling HIGH?
    BEQ poll_wiegand_edge   ; Yes -> loop with 0 jitter
```

### Pulse Width & Interval Discovery via WAITEDGE
Our core's dedicated `WAITEDGE Rd, pin` primitive allows the ASIC to perform automated physical line inspection:
1. **Pulse Width Discovery:** Measures active-low pulse duration directly into register `Rd` with single-cycle resolution.
2. **Interval Timing Profiling:** Measures the elapsed cycles between successive pulses to determine reader transmission rate ($T_{pi}$).
3. **Glitch & Noise Rejection:** Pulses narrower than a threshold $T_{min}$ are cleanly rejected as induced electrical transients on long building cables.

### Physical Tamper Detection (Simultaneous Low)
Under normal Wiegand operation, `DATA0` and `DATA1` can **never** be driven LOW simultaneously. If `DATA0 == 0` AND `DATA1 == 0`, a physical cable short-circuit, deliberate wire cut, or ground-fault condition has occurred:
```asm
    CMPI R0, 0x00           ; Both DATA0 and DATA1 LOW?
    BEQ wiegand_tamper_trap ; Immediate security alarm & pin tri-state
```

---

## 4. Hardware Coprocessor PPA Scaling on IHP 130nm SG13G2

For high-density multi-door access control units requiring simultaneous monitoring of 4 to 8 Wiegand card readers without software intervention, a dedicated synthesizable peripheral macro is modeled.

### Dedicated Macro Architecture
- **Dual Falling-Edge Synchronizers:** Dual-rank flip-flops for `DATA0_N` and `DATA1_N`.
- **Pulse Width Counter:** 8-bit timer verifying $T_{min} \le T_{pw} \le T_{max}$.
- **Shift Register & Bit Counter:** 32-bit shift register capturing up to 32 bits of credential data.
- **Hardware Parity Checker:** Combinational parity trees for instantaneous even/odd parity verification.
- **Tamper Fault Flag:** Sticky status bit latched upon simultaneous low assertion.

### IHP 130nm SG13G2 Cell Synthesis Estimates

| Component | Standard Cells | Gate Equivalents (GE) | Area ($\mu\text{m}^2$) | Max Frequency |
| :--- | :--- | :--- | :--- | :--- |
| Single-Channel Wiegand Reader/Writer | 285 | 556.8 GE | $2,080.50\,\mu\text{m}^2$ | 820 MHz |
| Dual-Channel Reader (In/Out Doors) | 480 | 936.0 GE | $3,504.00\,\mu\text{m}^2$ | 800 MHz |
| 4-Channel Industrial PACS Controller | 890 | 1,735.5 GE | $6,497.00\,\mu\text{m}^2$ | 780 MHz |

### Microcode vs Dedicated Macro Comparison

| Metric | Software Microcode | Dedicated Hardware Coprocessor |
| :--- | :--- | :--- |
| **Silicon Gate Overhead** | **0 gates (0.0% area)** | +285 cells (+1.48% area) |
| **CPU Core Utilization** | 100% during active frame | 0% (Autonomous background FIFO) |
| **Format Flexibility** | 26, 32, 34, 37, 64-bit arbitrary | Fixed width or register configured |
| **Jitter / Timing Precision** | Single-cycle deterministic | Single-cycle deterministic |
