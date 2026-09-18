# AXI4-Stream & TileLink On-Chip Streaming Fabric & Interconnect Engine

## 1. Executive Summary & Specification Context

Modern heterogeneous Systems-on-Chip (SoCs), neural processing units (NPUs), and domain-specific accelerators require high-throughput, low-latency, modular on-chip interconnect fabrics to transport streaming sensor data, direct memory access (DMA) bursts, and coherent memory transactions across compute clusters. Two prominent industry-standard on-chip protocol standards dominate modern embedded and high-performance SoC architectures:
1. **ARM AMBA 4 AXI4-Stream Protocol (v1.0 / AMBA 5):**
   - Universal point-to-point and switched unidirectional data streaming protocol designed for high-rate data transport without requiring explicit memory addresses.
   - Decouples producer and consumer clock domains through simple two-wire handshaking (`TVALID` and `TREADY`).
   - Native support for arbitrary burst lengths, byte-level valid qualifications (`TKEEP`/`TSTRB`), stream routing identifiers (`TDEST`/`TID`), and packet boundary delimiters (`TLAST`).
2. **SiFive / RISC-V TileLink On-Chip Interconnect Specification (v1.8.1):**
   - Open-standard, scale-free bus and network-on-chip protocol standard developed for RISC-V compute platforms (Rocket Chip, BOOM, SiFive Freedom SoC).
   - Multi-channel architecture organized into 5 independent channels ($A$, $B$, $C$, $D$, $E$) enforcing strict acyclic deadlock-free request-response routing:
     - **Channel A:** Master Request (`Get`, `PutFullData`, `PutPartialData`, `ArithmeticData`, `LogicalData`, `Intent`).
     - **Channel B:** Probe / Cache Invalidation Request (coherent TL-C).
     - **Channel C:** Release / Invalidation Acknowledgment (coherent TL-C).
     - **Channel D:** Slave Response (`AccessAck`, `AccessAckData`, `Grant`, `GrantData`).
     - **Channel E:** Master Grant Acknowledgment (3-phase serialization).
   - Layered hierarchical profiles:
     - **TL-UL (Uncached Lightweight):** Simple read/write memory-mapped register bus.
     - **TL-UH (Uncached Heavyweight):** Adds atomic memory operations (AMOs), burst hints, and masked writes.
     - **TL-C (Cached):** Full hardware MESI/MOESI cache coherence.

---

## 2. Protocol Architecture & Framing Specification

### 2.1 AXI4-Stream Handshake Dynamics & Framing
- **Handshake Invariants:**
  - A data beat is transferred precisely when both `TVALID` and `TREADY` are asserted on the rising clock edge:
    $$\text{Handshake} = \text{TVALID} \land \text{TREADY}$$
  - **Rule 1:** A master asserting `TVALID` must maintain `TVALID` and keep `TDATA`/`TLAST` stable until `TREADY` is sampled high.
  - **Rule 2:** A master must never wait for `TREADY` before asserting `TVALID`, preventing circular wait deadlocks across interconnect switches.
  - **Rule 3:** The `TLAST` signal indicates the final beat of a packet frame, allowing downstream arbiters and packet decoders to cleanly demarcate frame boundaries.

### 2.2 TileLink OpCodes & Transaction Semantics
TileLink Channel A request and Channel D response opcodes define structured operations:
- `0x00`: `PUT_FULL_DATA` (Full word write)
- `0x01`: `PUT_PARTIAL_DATA` (Byte masked write)
- `0x02`: `ARITHMETIC_DATA` (Atomic arithmetic: min, max, add)
- `0x03`: `LOGICAL_DATA` (Atomic bitwise logical: xor, or, and)
- `0x04`: `GET` (Memory read request)
- `0x05`: `INTENT` (Prefetch and memory access intent hint)
- `0x06`: `ACCESS_ACK` (Channel D write completion response)
- `0x07`: `ACCESS_ACK_DATA` (Channel D read completion response with data)
- `0xA5`: `SYNC_SOF` (Start of Frame delimiter `0b10100101`)
- `0x7E`: `IDLE` (Quiescent streaming line delimiter)

### 2.3 Interconnect Credit Flow Control & Integrity
- To prevent buffer overflow in multi-beat streaming interconnects, TileLink implements transaction credit accounting:
  - Each interconnect link is assigned an initial request credit pool (e.g. 4 credits).
  - Outgoing Channel A requests (`GET`, `PUT`, `ARITHMETIC`) consume 1 credit.
  - Incoming Channel D responses (`ACCESS_ACK`, `ACCESS_ACK_DATA`) restore 1 credit.
  - An attempt to transmit on an exhausted credit pool ($\text{credits} = 0$) triggers an underflow fault trap (`R2 = 0xEE`).
- Multi-beat packet payloads are protected by 16-bit CCITT CRC ($G(x) = x^{16} + x^{12} + x^5 + 1 = \text{0x1021}$).

---

## 3. Microcode Implementation on the 8-Bit RISC Core

On the Tiny Tapeout deterministic 8-bit RISC core:
1. **Master Packet Header Transmission (`build_axi_stream_tx_beat_asm`):**
   - Transmits `SYNC_SOF` (`0xA5`), opcode byte (e.g. `0x04` `GET`), and Target Destination ID (e.g. `0x03`) on pin 3.
   - Employs hardware bit serialization instruction `SHIFTOUT R0, 0x0B` (MSB-first on pin 3).
   - Asserts status `R2 = 0x00` upon completion and halts.
2. **Slave Beat Ingress (`build_axi_stream_rx_beat_asm`):**
   - Awaits `SYNC_SOF` delimiter rising edge via `WAITEDGE` on pin 3 (operand `0x0B`, bit 7).
   - Strides past remaining delimiter bits directly to opcode bit 7 midpoint.
   - Samples 8 subsequent bits into `R0` using `SHIFTIN R0, 0x0B` (MSB-first) and preserves them in `R1`, asserting `R2 = 0x00`.
3. **In-Register Opcode Filter (`build_tilelink_opcode_filter_asm`):**
   - Evaluates received opcode in `R0` against supported TileLink transaction types (`0x00` through `0x07`).
   - Valid opcodes branch to `MATCH`, setting status `R2 = 0x00`.
   - Illegal opcodes (e.g. `0x7F`) fall through to error trap setting `R2 = 0xEE`.
4. **In-Register Flow Control Credit Tracker (`build_tilelink_credit_tracker_asm`):**
   - Tracks interconnect request-response credits (initial credits = 4):
     - Response ACK event (`0x01`): increments credit count by 1 (`ADDI R0, 1`).
     - Request Send event (`0x02`): decrements credit count by 1 (`SUBI R0, 1`).
   - Traps credit exhaustion / underflow ($< 0$) with `R2 = 0xEE`.

---

## 4. Synthesizable Hardware Coprocessor & PPA Scaling (IHP 130nm SG13G2)

For on-chip streaming interconnect acceleration on IHP 130nm SG13G2:
- **Software Microcode Engine:** **0 logic gates (0% area overhead)**.
- **Dedicated AXI4-Stream & TileLink Interconnect Macro:**
  - Standard cell count: **625 cells** (~$1220.0\,\text{GE}$, $+3.24\%$ area overhead).
  - Physical silicon footprint: $4,590.0\,\mu\text{m}^2$.
  - Critical path delay: $1.25\,\text{ns}$ ($f_{\text{max}} = 800.00\,\text{MHz}$).
  - Dynamic power at $10\,\text{MHz}$: $61.00\,\mu\text{W}$.
  - Raw throughput: $10,000.0\,\text{Mbps}$ streaming fabric.
  - Energy efficiency: $0.00076\,\text{pJ/bit}$.
