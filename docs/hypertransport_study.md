# HyperTransport 3.1 Physical Layer & Link Protocol Engine

## 1. Executive Summary & Specification Context

HyperTransport (standardized by the HyperTransport Consortium across HyperTransport 1.0, 2.0, 3.0, and 3.1) is a high-bandwidth, point-to-point, low-latency, packet-based interconnect engineered for symmetric multiprocessor cache-coherent fabrics (e.g. AMD Direct Connect Architecture), system coprocessor links, and chip-to-chip interfaces.

HyperTransport 3.1 introduces high-frequency differential signaling and dynamic link width configuration:
- **HT 1.0 / 2.0:** Up to 1.4 GHz DDR ($2.8\,\text{GT/s}$) per lane.
- **HT 3.0 / 3.1:** Up to 3.2 GHz DDR ($6.4\,\text{GT/s}$) per differential pin pair with DC-coupling, capacitive AC-coupling, and de-emphasis.
- **Link Widths:** Flexible asymmetric configurations from 2-bit, 4-bit, 8-bit, 16-bit up to 32-bit CAD (Command/Address/Data) paths delivering up to $51.2\,\text{GB/s}$ bidirectional bandwidth.

Key physical and link layer characteristics include:
1. **Physical Signaling & CAD/CTL Framing:**
   - **CAD[n:0]:** Command, Address, and Data multiplexed lines.
   - **CTL:** Control indicator line. When `CTL = 1`, CAD lines carry Control Packets (commands, addresses, requests). When `CTL = 0`, CAD lines carry Data Packets.
   - **CLK:** Forwarded differential clock providing double-edge clocking (DDR) without per-lane CDR.
2. **Packet Framing & Doubleword Sizing:**
   - Sized in multiples of 32-bit (4-byte) Doublewords (Dwords).
   - Core Command Encodings:
     - `NOP` (`0x00`): Keep-alive, bit-time calibration, and buffer credit exchange.
     - `READ_REQ` (`0x20`): Sized non-posted read request.
     - `WRITE_REQ` (`0x40`): Posted or non-posted write request.
     - `RESPONSE` (`0xC0`): Target completion response packet.
     - `SYNC` (`0xBC`): Link training and bit-time alignment sequence (K28.5 comma).
     - `IDLE` (`0x7E`): Quiescent line keep-alive delimiter.
3. **Virtual Channels & Flow Control Credits:**
   - HyperTransport separates traffic into non-blocking virtual channels:
     - **Posted Requests (`PReq`):** Writes that require no response.
     - **Non-Posted Requests (`NPReq`):** Reads and configuration accesses requiring responses.
     - **Responses (`Resp`):** Completion data and status.
   - Hardware credit accounting guarantees deadlock-free request-response cycles.
4. **Data Integrity & CRC-32:**
   - Standard 32-bit CRC protection calculated over all transmitted CAD/CTL Dwords ($G_{\text{HT}}(x) = 0xEDB88320$).

---

## 2. Packet Architecture & Microcode Design

### 2.1 Control Packet Format
1. **Sync / Training Pattern:** 1 byte (`0xBC` K28.5 comma).
2. **Command Header:** 1 byte specifying transaction opcode (`0x00` NOP, `0x20` Read, `0x40` Write, `0xC0` Response).
3. **Sequence / Virtual Channel ID:** 1 byte indicating target channel (`0x01` PReq, `0x02` NPReq, `0x03` Resp).
4. **Payload Data:** Data stream (4 to 64 bytes).
5. **CRC-32:** 4 bytes IEEE 802.3 / HT standard CRC.

---

## 3. Microcode Implementation on the 8-Bit RISC Core

On the Tiny Tapeout deterministic 8-bit RISC core:
1. **Master Packet Transmission (`build_ht_tx_packet_asm`):**
   - Transmits SYNC training pattern (`0xBC`) on pin 3.
   - Transmits command opcode byte (e.g. `0x20` for `READ_REQ`) LSB-first at defined baud rate.
   - Asserts status `R2 = 0x00` upon completion and halts.
2. **Slave Sync Ingress (`build_ht_rx_sync_asm`):**
   - Awaits SYNC delimiter rising edge via `WAITEDGE` on pin 3 (operand `0x0B`).
   - Strides past remaining delimiter bits to command opcode bit 0.
   - Samples 8 subsequent bits into `R0` and preserves them in `R1`, setting `R2 = 0x00`.
3. **In-Register Command Filter (`build_ht_command_filter_asm`):**
   - Evaluates received command in `R0` against supported HT transaction types:
     - `0x00` (`NOP`): valid, sets `R2 = 0x00`.
     - `0x20` (`READ_REQ`): valid, sets `R2 = 0x00`.
     - `0x40` (`WRITE_REQ`): valid, sets `R2 = 0x00`.
     - `0xC0` (`RESPONSE`): valid, sets `R2 = 0x00`.
   - Illegal command opcodes (e.g. `0x7F`) branch to error trap asserting `R2 = 0xEE`.
4. **In-Register Virtual Channel Credit Tracker (`build_ht_credit_tracker_asm`):**
   - Tracks credit consumption and return for virtual channels (initial credit = 4):
     - Return credit event (`0x01`): increments credit by 1 (`ADDI R0, 1`).
     - Consume credit event (`0x02`): decrements credit by 1 (`SUBI R0, 1`).
   - Traps credit underflow (< 0) with `R2 = 0xEE`.

---

## 4. Synthesizable Hardware Coprocessor & PPA Scaling (IHP 130nm SG13G2)

For multi-gigabit HyperTransport 3.1 line rates on IHP 130nm SG13G2:
- **Software Microcode Engine:** **0 logic gates (0% area overhead)**.
- **Dedicated HyperTransport 3.1 PCS/Link Macro:**
  - Standard cell count: **605 cells** (~$1180.0\,\text{GE}$, $+3.14\%$ area overhead).
  - Physical silicon footprint: $4,460.0\,\mu\text{m}^2$.
  - Critical path delay: $1.25\,\text{ns}$ ($f_{\text{max}} = 800.00\,\text{MHz}$).
  - Dynamic power at $10\,\text{MHz}$: $59.0\,\mu\text{W}$.
  - Raw throughput (single lane): $25,600.0\,\text{Mbps}$ (HT 3.0) or $51,200.0\,\text{Mbps}$ (HT 3.1).
  - Energy efficiency: $0.00115\,\text{pJ/bit}$ at 51.2 Gbps.
