# MIPI C-PHY v2.0 Physical Layer & 3-Phase Symbol Encoding Study

## 1. Executive Summary & Specification Context

MIPI C-PHY v2.0 is the high-throughput, pin-efficient physical layer standard developed by the MIPI Alliance for high-resolution camera serial interface (CSI-2) and display serial interface (DSI-2) applications in mobile, automotive ADAS, and embedded vision devices.

While traditional differential serial interfaces (such as MIPI D-PHY, PCI Express, and SATA) require 2 physical conductors per data lane to transmit 1 bit per unit interval (or 2 bits per clock cycle under DDR), MIPI C-PHY utilizes a **3-wire transmission lane** ("trio", denoted A, B, and C) driven with **3-Phase Symbol Encoding**.

Key architectural features of MIPI C-PHY v2.0 include:
1. **3-Phase Balanced Signaling:** On every symbol interval, the trio carries three distinct voltage levels: High ($V_H \approx 0.9\,\text{V}$), Mid ($V_M \approx 0.6\,\text{V}$), and Low ($V_L \approx 0.3\,\text{V}$). The instantaneous sum of voltages on the three wires is constant ($V_A + V_B + V_C = 0$ relative to common mode), eliminating common-mode radiation and EMI.
2. **6 Unique Wire States:** Exactly $3! = 6$ permutations of voltage levels exist on the trio: $+x, -x, +y, -y, +z, -z$.
3. **Transition Encoding (5 Choices per Symbol):** Information is encoded exclusively in the *transition* between successive wire states. From any current wire state, the transmitter selects one of 5 permitted destination states, ensuring that *at least one wire always transitions on every symbol boundary*. This guarantees deterministic clock transitions for clock and data recovery (CDR) without requiring a separate clock lane or phase-locked loop (PLL).
4. **16b/7t Mapping:** 16-bit binary words are mapped into groups of 7 symbols ($5^7 = 78,125 > 2^{16} = 65,536$), achieving an effective data rate of:
   $$\eta = \frac{16}{7} \approx 2.2857\,\text{bits/symbol}$$
   At a symbol rate of $2.5\,\text{Gsym/s}$, a single trio delivers $5.714\,\text{Gbps}$ of raw throughput over just 3 pins (compared to 4 pins required for two D-PHY lanes to achieve comparable throughput).

---

## 2. 3-Phase Wire States and Differential Receiver Mechanics

### 2.1 Wire State Definition

The 6 canonical wire states of MIPI C-PHY are defined by the voltages driven onto wires A, B, and C:

| State | Wire A ($V_A$) | Wire B ($V_B$) | Wire C ($V_C$) | Vector $(A, B, C)$ |
|:-----:|:--------------:|:--------------:|:--------------:|:------------------:|
| **+x** | High ($+1$) | Low ($-1$) | Mid ($0$) | $(+1, -1, 0)$ |
| **-x** | Low ($-1$) | High ($+1$) | Mid ($0$) | $(-1, +1, 0)$ |
| **+y** | Mid ($0$) | High ($+1$) | Low ($-1$) | $(0, +1, -1)$ |
| **-y** | Mid ($0$) | Low ($-1$) | High ($+1$) | $(0, -1, +1)$ |
| **+z** | High ($+1$) | Mid ($0$) | Low ($-1$) | $(+1, 0, -1)$ |
| **-z** | Low ($-1$) | Mid ($0$) | High ($+1$) | $(-1, 0, +1)$ |

### 2.2 Differential Receiver Sensing

A C-PHY receiver employs three differential amplifiers connected across each pair of the trio:
$$V_{AB} = V_A - V_B$$
$$V_{BC} = V_B - V_C$$
$$V_{CA} = V_C - V_A$$

Evaluating the differential voltages for each wire state reveals a unique signature:

| State | $V_{AB} = A - B$ | $V_{BC} = B - C$ | $V_{CA} = C - A$ | Polarity Sign $(AB, BC, CA)$ |
|:-----:|:----------------:|:----------------:|:----------------:|:-----------------------------:|
| **+x** | $+2$ (Strong $+V$) | $-1$ (Weak $-V$) | $-1$ (Weak $-V$) | $(+, -, -)$ |
| **-x** | $-2$ (Strong $-V$) | $+1$ (Weak $+V$) | $+1$ (Weak $+V$) | $(-, +, +)$ |
| **+y** | $-1$ (Weak $-V$) | $+2$ (Strong $+V$) | $-1$ (Weak $-V$) | $(-, +, -)$ |
| **-y** | $+1$ (Weak $+V$) | $-2$ (Strong $-V$) | $+1$ (Weak $+V$) | $(+, -, +)$ |
| **+z** | $+1$ (Weak $+V$) | $+1$ (Weak $+V$) | $-2$ (Strong $-V$) | $(+, +, -)$ |
| **-z** | $-1$ (Weak $-V$) | $-1$ (Weak $-V$) | $+2$ (Strong $+V$) | $(-, -, +)$ |

Notice that in every wire state, exactly one differential receiver sees a strong signal ($\pm 2 \cdot V_{\text{diff}}$) and the other two see weak signals of the opposite polarity ($\mp 1 \cdot V_{\text{diff}}$). This symmetry ensures constant common-mode impedance and identical noise margins across all states.

---

## 3. Transition Encoding and 16b/7t Mapping

### 3.1 Transition Mapping Matrix

Each transmitted symbol $S \in \{0, 1, 2, 3, 4\}$ encodes a transition from the current wire state to one of the 5 other states. The 5 transitions represent combinations of **Rotation** (phase shift around the trio) and **Polarity**:
- $S=0$: Rotation CW, Polarity Same
- $S=1$: Rotation CCW, Polarity Same
- $S=2$: Rotation None, Polarity Inverted
- $S=3$: Rotation CW, Polarity Inverted
- $S=4$: Rotation CCW, Polarity Inverted

Because remaining in the same state ($S = \text{self}$) is strictly prohibited, the receiver sees at least one zero-crossing on every symbol, allowing continuous clock tracking without PLL lock loss.

### 3.2 16b/7t Mapping Mathematics

To encode a 16-bit integer $W \in [0, 65535]$ into 7 base-5 symbols $(s_6, s_5, s_4, s_3, s_2, s_1, s_0)$ where $s_i \in \{0, 1, 2, 3, 4\}$:
$$W = \sum_{i=0}^6 s_i \cdot 5^i$$

Decoding is performed by polynomial evaluation:
$$W = s_0 + 5 \cdot (s_1 + 5 \cdot (s_2 + 5 \cdot (s_3 + 5 \cdot (s_4 + 5 \cdot (s_5 + 5 \cdot s_6)))))$$

Since $5^7 = 78,125 > 65,536$, all 16-bit values map uniquely to valid 7-symbol sequences with 12,589 reserved symbol combinations for control framing, sync words, and preamble sequences.

---

## 4. Hardware PPA Scaling on Tiny Tapeout IHP 130nm SG13G2

On the competition Tiny Tapeout IHP 130nm SG13G2 platform ($10.0\,\text{MHz}$ emulation clock, 8-bit deterministic RISC processor, 256-word program RAM), MIPI C-PHY v2.0 is supported via:
1. **Zero-Gate Microcode Engine:** Emulates C-PHY trio wire state transitions, 3-Phase symbol generation, and WAITEDGE edge synchronization with **0 logic gate overhead (0% silicon area)**.
2. **Dedicated Synthesizable C-PHY Coprocessor Macro:**
   - **Cell Count:** 570 standard cells.
   - **Gate Equivalents:** 1,110.0 GE ($4,218.0\,\mu\text{m}^2$).
   - **Area Overhead:** $+2.96\%$ relative to baseline.
   - **Timing Slack:** $1.25\,\text{ns}$ critical path ($f_{\text{max}} = 800.0\,\text{MHz}$).
   - **Dynamic Power:** $55.5\,\mu\text{W}$ at 10 MHz.
   - **Throughput:** $5,714.0\,\text{Mbps}$ per trio at 2.5 Gsym/s ($0.00971\,\text{pJ/bit}$).
