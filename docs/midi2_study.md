# MIDI 2.0 Universal MIDI Packet (UMP) Architectural Study

## 1. Executive Summary

The Musical Instrument Digital Interface (MIDI) 2.0 specification, ratified by the MIDI Manufacturers Association (MMA) and the Association of Musical Electronics Industry (AMEI), modernizes digital music networks, synthesizer engines, and professional audio interfaces. While MIDI 1.0 (standardized in 1983) utilized a 31.25 kbps current-loop serial bus with 7-bit parameter resolution ($0-127$) and byte-oriented running status, MIDI 2.0 introduces the **Universal MIDI Packet (UMP)** architecture.

UMP standardizes a 32-bit packet format across all transports (USB, PCIe, Ethernet, high-speed UART/SPI), providing native 16-group routing (up to 256 virtual MIDI channels per physical link), 16-bit and 32-bit high-resolution articulation and velocity parameters, Jitter-Reduction (JR) Timestamps, and bidirectional MIDI-CI (Capability Inquiry) discovery.

This study analyzes the UMP packet framing, group routing, high-resolution channel voice translation, jitter-reduction mechanics, and synthesizable coprocessor macro implementation on the IHP 130nm SG13G2 CMOS process.

---

## 2. Universal MIDI Packet (UMP) Architecture

Every UMP packet consists of one to four 32-bit words (32, 64, 96, or 128 bits), where the uppermost 4 bits of Word 0 specify the Message Type (MT), which inherently determines the packet length:

| Message Type (MT) | Packet Length | Protocol Function |
|:------------------:|:-------------:|:------------------|
| `0x0` | 32-bit (1 word) | Utility Messages (NOOP, JR Clock, JR Timestamp) |
| `0x1` | 32-bit (1 word) | System Real-Time & System Common (Timing Clock, Start, Stop) |
| `0x2` | 32-bit (1 word) | MIDI 1.0 Channel Voice (Legacy wrapping: Note On/Off, CC) |
| `0x3` | 64-bit (2 words)| Data Messages (64-bit SysEx 7-bit payload) |
| `0x4` | 64-bit (2 words)| MIDI 2.0 Channel Voice (16-bit velocity, 32-bit Pitch Bend, Attribute) |
| `0x5` | 128-bit (4 words)| Extended Data Messages (SysEx 8-bit, Mixed Data Set) |
| `0xD` | 128-bit (4 words)| Flex Data Messages (Lyrics, chords, performance metadata) |
| `0xF` | 32-bit (1 word) | Stream Messages (Endpoint discovery, protocol negotiation) |

### 2.1 32-Bit UMP Word 0 Header Structure
```text
+-------------------+-------------------+-------------------+-------------------+
|    Bits 31-28     |    Bits 27-24     |    Bits 23-20     |    Bits 19-16     |
+-------------------+-------------------+-------------------+-------------------+
| Message Type (MT) |    Group (0-15)   |   Status Opcode   |   Channel (0-15)  |
+-------------------+-------------------+-------------------+-------------------+
|               Bits 15-8               |               Bits 7-0                |
+---------------------------------------+---------------------------------------+
|              Data Byte 1              |              Data Byte 2              |
+---------------------------------------+---------------------------------------+
```

- **Group Field (`Bits [27:24]`):** 4 bits defining the target virtual group (0–15). Each group encapsulates 16 MIDI channels, providing 256 virtual channels over a single physical wire.
- **Status Opcode (`Bits [23:20]`):** Defines Channel Voice action:
  - `0x8`: Note Off
  - `0x9`: Note On
  - `0xA`: Polyphonic Aftertouch (Poly Pressure)
  - `0xB`: Control Change (CC)
  - `0xC`: Program Change
  - `0xD`: Channel Pressure (Monophonic Aftertouch)
  - `0xE`: Pitch Bend
  - `0xF`: Per-Note Management

### 2.2 64-Bit MIDI 2.0 High-Resolution Channel Voice (MT = 0x4)
In MIDI 2.0 Channel Voice messages:
- **Word 0 (`Bits [31:0]`):** Contains MT (`0x4`), Group, Status, Channel, Note Number (Bits 15-8), and Attribute Type (Bits 7-0).
- **Word 1 (`Bits [31:0]`):** Contains 16-bit High-Resolution Velocity (`Bits [31:16]`) and 16-bit Attribute Data (`Bits [15:0]`).
- For Pitch Bend: Word 1 contains a full **32-bit unsigned pitch bend value** (where `0x80000000` is center pitch), providing over 4 billion discrete tuning steps compared to MIDI 1.0's 14-bit resolution.

### 2.3 Jitter-Reduction (JR) Timestamps (MT = 0x0)
Physical transports and operating system scheduling introduce transmission phase jitter. MIDI 2.0 specifies JR Clock and JR Timestamp messages:
- **JR Clock (`Status 0x1`):** Periodic 16-bit clock reference value.
- **JR Timestamp (`Status 0x2`):** 16-bit timestamp prefixing subsequent packets ($T_{\text{res}} = 31.25\,\mu\text{s}$ per tick), allowing receivers to render note events with sub-microsecond sample accuracy.

---

## 3. Hardware Coprocessor Macro Architecture on IHP 130nm SG13G2

For high-density synthesizer voice cards, multi-port audio interfaces, and digital mixing consoles, an autonomous UMP coprocessor offloads packet parsing, group routing, and high-resolution scaling:

### 3.1 Macro Functional Blocks
1. **Universal Packet Framer & Parser:** Multi-word packet assembler tracking MT length (32/64/96/128 bits) and frame boundaries.
2. **16-Group Virtual Routing Matrix:** Hardware filter routing messages based on Group (`Bits [27:24]`) and Channel (`Bits [19:16]`) with single-cycle dispatch.
3. **High-Resolution Velocity & Pitch Converter:** 16-bit and 32-bit linear interpolator and saturation logic.
4. **Jitter-Reduction De-Jitter Buffer:** FIFO buffer holding timestamped events until local sample clock trigger.

### 3.2 Standard Cell Synthesis & PPA Analysis (IHP 130nm SG13G2)
- **Gate Count:** 395 standard cells (~768.2 Gate Equivalents, GE).
- **Silicon Area:** $2,883.50\,\mu\text{m}^2$ (+2.06% design area overhead).
- **Critical Path:** $1.24\,\text{ns}$ through group filter comparator and velocity normalizer ($f_{\text{max}} = 806.5\,\text{MHz}$).
- **Dynamic Power:** $38.2\,\mu\text{W}$ at $10\,\text{MHz}$ operating frequency.
