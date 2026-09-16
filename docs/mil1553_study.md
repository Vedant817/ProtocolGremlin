# MIL-STD-1553B Avionic Multiplex Data Bus Dual-Redundant Protocol Engine

## 1. Executive Summary & Protocol Architecture

MIL-STD-1553B (Notice 2) is the premier military and aerospace command/response multiplex data bus standard governing avionics integration aboard fighter aircraft (F-16, F/A-18, Eurofighter), transport aircraft (C-17, A400M), military helicopters, and long-duration space exploration platforms (International Space Station, Hubble Space Telescope, James Webb Space Telescope).

The standard establishes a deterministic, dual-redundant, time-division multiplexed (TDM) digital communications architecture designed for mission-critical flight environments requiring zero undetected errors and continuous fault tolerance.

### 1.1 Physical Layer & Signaling Characteristics
- **Signaling Mode**: Balanced differential transmission over 78-ohm shielded twisted-pair (STP) cable with transformer or direct coupling.
- **Data Bit Rate**: $1.0\,\text{Mbps} \pm 0.1\%$ ($1.0\,\mu\text{s}$ nominal bit time).
- **Line Code**: Manchester II Biphase-L:
  - Logic '1': High-to-Low transition at mid-bit ($+V \to -V$).
  - Logic '0': Low-to-High transition at mid-bit ($-V \to +V$).
- **Word Length**: Exactly 20 bit times ($20.0\,\mu\text{s}$ per word):
  - **3 bit times ($3.0\,\mu\text{s}$)**: Synchronization pulse (non-Manchester violation).
  - **16 bit times ($16.0\,\mu\text{s}$)**: Information payload field.
  - **1 bit time ($1.0\,\mu\text{s}$)**: Odd parity bit.

---

## 2. Word Formats & Synchronization Physics

MIL-STD-1553B defines three distinct 20-bit word formats:

### 2.1 Synchronization Field (The Non-Manchester Sync)
A fundamental innovation of MIL-STD-1553B is the non-Manchester sync waveform spanning 3 complete bit times ($3.0\,\mu\text{s}$). Normal Manchester bits guarantee a zero-crossing transition every $0.5\,\mu\text{s}$ or $1.0\,\mu\text{s}$. The 1553 sync pulse violates this rule by holding a continuous level for $1.5\,\mu\text{s}$ before transitioning to the opposite level for $1.5\,\mu\text{s}$. This guarantees that no legitimate sequence of data bits can ever mimic a sync pulse.

1. **Command / Status Sync**:
   - $1.5\,\mu\text{s}$ Positive phase ($+V$), followed by $1.5\,\mu\text{s}$ Negative phase ($-V$).
2. **Data Sync**:
   - $1.5\,\mu\text{s}$ Negative phase ($-V$), followed by $1.5\,\mu\text{s}$ Positive phase ($+V$).

In the clocked digital domain (with 8 clock cycles per bit time):
- Command Sync: 12 cycles HIGH, followed by 12 cycles LOW.
- Data Sync: 12 cycles LOW, followed by 12 cycles HIGH.

### 2.2 Word Definitions

```text
 1  2  3 |  4  5  6  7  8 | 9 | 10 11 12 13 14 | 15 16 17 18 19 | 20
+--------+----------------+---+----------------+----------------+---+
|  Sync  |   RT Address   |T/R|   Subaddress   |   Word Count   | P |  Command Word
| (Cmd)  |    (5 bits)    |1b |    (5 bits)    |    (5 bits)    |odd|
+--------+----------------+---+----------------+----------------+---+
|  Sync  |   RT Address   |    Status Flags    |   Reserved     | P |  Status Word
| (Stat) |    (5 bits)    |      (9 bits)      |    (2 bits)    |odd|
+--------+----------------+--------------------+----------------+---+
|  Sync  |               Data Word (16 bits)                    | P |  Data Word
| (Data) |             High Byte / Low Byte                     |odd|
+--------+------------------------------------------------------+---+
```

1. **Command Word** (issued strictly by the Bus Controller):
   - `RT Address` (bits 4–8): 5 bits designating Remote Terminal $0 \dots 31$ (31 = Broadcast).
   - `T/R` (bit 9): $1 = \text{Transmit (RT } \to \text{ BC)}$, $0 = \text{Receive (BC } \to \text{ RT)}$.
   - `Subaddress / Mode` (bits 10–14): 5 bits ($00000_2$ or $11111_2$ selects Mode Codes; $00001_2 \dots 11110_2$ selects internal RT memory buffers 1 to 30).
   - `Data Word Count / Mode Code` (bits 15–19): 5 bits ($00000_2 = 32$ data words; $00001_2 \dots 11111_2 = 1 \dots 31$ words).
   - `Parity` (bit 20): Odd parity calculated over bits 4–19.

2. **Status Word** (returned strictly by addressed Remote Terminal):
   - `RT Address` (bits 4–8): Confirms the responding terminal's address.
   - `Status Flags` (bits 9–19): Includes Message Error (`ME`, bit 9), Instrumentation (bit 10), Service Request (bit 11), Broadcast Received (bit 15), Busy (bit 16), Subsystem Flag (bit 17), Dynamic Bus Control (bit 18), and Terminal Flag (bit 19).
   - `Parity` (bit 20): Odd parity.

3. **Data Word**:
   - `Data Field` (bits 4–19): 16 bits of information payload.
   - `Parity` (bit 20): Odd parity.

---

## 3. Terminal Roles & Dual-Redundant Bus Operation

1. **Bus Controller (BC)**:
   - The sole master terminal assigned to schedule and initiate all message transfers.
   - Transmits Command Words, provides data in BC-to-RT transfers, and samples Status Words.
   - Enforces response timeout ($t_{\text{timeout}} = 14.0\,\mu\text{s}$). If an RT fails to respond on the primary bus (Bus A), the BC automatically fails over to the secondary bus (Bus B).

2. **Remote Terminal (RT)**:
   - Operates as a slave listening to both Bus A and Bus B.
   - Decodes incoming command words; if the 5-bit RT address matches its local configuration, it verifies parity and responds within the strict response time window:
     $$4.0\,\mu\text{s} \le t_r \le 12.0\,\mu\text{s}$$

3. **Bus Monitor (BM)**:
   - Passively records bus traffic without asserting active line signals or acknowledgments.

---

## 4. Physical Layer Pin Mapping on Tiny Tapeout

Isolating from serial bootloader pins (`uio[0:2]`):

| Signal | Channel | Pin | Direction | Description |
|:---|:---:|:---:|:---:|:---|
| **TX_A** | Bus A | `uio[3]` | Output | Bus A Manchester transmit driver |
| **RX_A** | Bus A | `uio[4]` | Input | Bus A Manchester receive receiver |
| **TX_B** | Bus B | `uio[5]` | Output | Bus B Dual-redundant transmit driver |
| **RX_B** | Bus B | `uio[6]` | Input | Bus B Dual-redundant receive receiver |
| Auxiliary | - | `uio[7]` | Input/Output | Bus activity strobe / failover flag |

---

## 5. Hardware Coprocessor vs. Firmware Microcode Engine PPA Analysis

Targeting the **IHP 130nm SG13G2** CMOS process ($V_{\text{DD}} = 1.2\,\text{V}$, 7 metal layers):

### 5.1 Firmware Microcode Engine (Zero-Area Approach)
- Synthesizes 3-bit Command/Data sync pulses and 16 Manchester bits using `GWRI`, `WAIT`, and `SHIFTIN`.
- Decodes command fields, verifies odd parity, and implements BC/RT handshakes in microcode.
- Area overhead: **0 gates, 0.00% silicon area overhead**.

### 5.2 Dedicated MIL-STD-1553B Hardware Coprocessor Macro
- Dual-channel analog front-end interface and glitch filter: 64 cells.
- 3-bit Sync pattern generator and detector: 58 cells.
- Manchester II Biphase-L encoder/decoder: 92 cells.
- 16-bit shift register and hardware odd-parity tree: 110 cells.
- Dual-bus failover controller and RT address comparator: 130 cells.
- Total area: **454 standard cells** ($\approx 885.3\,\text{GE}$, $3,311.0\,\mu\text{m}^2$, $+2.38\%$ area overhead on $139,000\,\mu\text{m}^2$ baseline).
- Critical path: $1.32\,\text{ns}$ ($f_{\text{max}} = 757.5\,\text{MHz}$).
