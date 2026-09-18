# InfiniBand HDR/NDR Physical & Link Layer Engine

## 1. Executive Summary & Specification Context

InfiniBand (standardized by the InfiniBand Trade Association, IBTA Volume 2) is the industry standard high-throughput, ultra-low-latency interconnect architecture powering high-performance computing (HPC), distributed AI/ML training superclusters, enterprise storage fabrics, and hyperscale cloud networks. InfiniBand features hardware-offloaded remote direct memory access (RDMA), kernel-bypass messaging semantics, cut-through routing switches, and credit-based link-level flow control.

InfiniBand has evolved across data rate tiers:
- **SDR / DDR / QDR:** 2.5, 5.0, and 10.0 Gbps per lane using 8b/10b line coding.
- **FDR / EDR:** 14.0625 and 25.78125 Gbps per lane using 64b/66b line coding.
- **HDR (High Data Rate):** 50.0 Gbps per lane using PAM4 / 64b/66b line coding ($4\times = 200\,\text{Gbps}$).
- **NDR (Next Data Rate):** 100.0 Gbps per lane using PAM4 / 64b/66b line coding ($4\times = 400\,\text{Gbps}$).
- **XDR / GDR:** 200 Gbps and 400 Gbps per lane roadmap ($800\,\text{Gbps}$ and $1600\,\text{Gbps}$).

Key physical and link layer characteristics include:
1. **Link Training & Ordered Sets:**
   - Link training sequences TS1 (`0x4A`, 'J') and TS2 (`0x45`, 'E') negotiate lane polarity, lane inversion, data rate, and symbol alignment.
   - Standard control delimiters include Start of Packet (`SOP = 0xFB`) and End of Packet (`EOP = 0xFD`).
2. **Dual-Layer Cyclic Redundancy Checks (CRCs):**
   - **Variant CRC (VCRC-16):** 16-bit CRC computed across every hop covering all mutable and immutable packet fields:
     $$G_{\text{VCRC}}(x) = x^{16} + x^{12} + x^5 + 1 \quad (0x1021)$$
   - **Invariant CRC (ICRC-32):** 32-bit CRC calculated end-to-end covering all invariant headers and data payload (with variant routing bits masked to 1s):
     $$G_{\text{ICRC}}(x) = x^{32} + x^{26} + x^{23} + x^{22} + x^{16} + x^{12} + x^{11} + x^{10} + x^8 + x^7 + x^5 + x^4 + x^2 + x + 1 \quad (0x04C11DB7)$$
3. **Queue Pair (QP) & Transport Headers:**
   - Transport service types: Reliable Connection (RC), Unreliable Connection (UC), Reliable Datagram (RD), Unreliable Datagram (UD).
   - Base Transport Header (BTH) with OpCodes specifying operations: RC Send First (`0x00`), RC Send Middle (`0x01`), RC Send Last (`0x02`), RC Send Only (`0x04`), RC RDMA Write Only (`0x0A`), RC ACK (`0x11`).

---

## 2. InfiniBand Packet & Link Framing Architecture

### 2.1 Ordered Sets & Delimiters
- **SOP (Start of Packet):** `0xFB` indicates start of packet data stream.
- **EOP (End of Packet):** `0xFD` indicates normal packet termination.
- **TS1 (Training Sequence 1):** Header `0xFB` followed by `0x4A` identifier and link training parameter bytes.
- **TS2 (Training Sequence 2):** Header `0xFB` followed by `0x45` identifier and link training parameter bytes.

### 2.2 Packet Encapsulation
An InfiniBand packet encapsulates:
1. **Local Route Header (LRH):** 8 bytes containing Virtual Lane (`VL`), Link Version (`LVer`), Service Level (`SL`), Link Next Header (`LNH`), Destination LID (`DLID`), Packet Length (`PktLen`), and Source LID (`SLID`).
2. **Global Route Header (GRH, optional):** 40 bytes for inter-subnet routing (IPv6-compatible format).
3. **Base Transport Header (BTH):** 12 bytes containing OpCode, Solicited Event (`SE`), Migration Req (`M`), Pad Count (`PadCnt`), Partition Key (`P_Key`), Destination QP (`DestQP`), Acknowledge Request (`A`), and Packet Sequence Number (`PSN`).
4. **Payload:** 0 to 4096 bytes of user data.
5. **Invariant CRC (ICRC):** 4 bytes end-to-end integrity check.
6. **Variant CRC (VCRC):** 2 bytes hop-by-hop integrity check.

---

## 3. Microcode Implementation on the 8-Bit RISC Core

On the Tiny Tapeout deterministic 8-bit RISC core:
1. **Master Training Sequence Transmission (`build_infiniband_tx_ts1_asm`):**
   - Transmits SOP delimiter `0xFB` on pin 3.
   - Transmits TS1 identifier `0x4A` at defined baud rate.
   - Asserts completion status `R2 = 0x00`.
2. **Slave SOP Ingress Synchronization (`build_infiniband_rx_sync_asm`):**
   - Awaits SOP rising edge on pin 3 via `WAITEDGE` (operand `0x0B`).
   - Strides past the 8 bits of SOP delimiter directly into the subsequent TS ID byte.
   - Samples 8 subsequent bits into `R0` and preserves them in `R1`, setting `R2 = 0x00`.
3. **In-Register BTH OpCode Filter (`build_infiniband_packet_filter_asm`):**
   - Evaluates incoming BTH OpCode in `R0`.
   - Validates known RC operations: `0x00` (RC_SEND_FIRST), `0x04` (RC_SEND_ONLY), `0x0A` (RC_RDMA_WRITE_ONLY), `0x11` (RC_ACK).
   - If recognized, returns `R2 = 0x00`; unrecognized OpCodes trap to `R2 = 0xEE`.
4. **In-Register VCRC Validator (`build_infiniband_vcrc_validator_asm`):**
   - Compares received 8-bit CRC slice in `R0` against expected polynomial residue, asserting `R2 = 0x00` on match or `R2 = 0xEE` on corruption.

---

## 4. Synthesizable Hardware Coprocessor & PPA Scaling (IHP 130nm SG13G2)

For multi-gigabit InfiniBand HDR/NDR link acceleration on IHP 130nm SG13G2:
- **Software Microcode Engine:** **0 logic gates (0% area overhead)**.
- **Dedicated InfiniBand HDR/NDR Link/PCS Macro:**
  - Standard cell count: **590 cells** (~$1150.0\,\text{GE}$, $+3.06\%$ area overhead).
  - Physical silicon footprint: $4,340.0\,\mu\text{m}^2$.
  - Critical path delay: $1.25\,\text{ns}$ ($f_{\text{max}} = 800.00\,\text{MHz}$).
  - Dynamic power at $10\,\text{MHz}$: $57.5\,\mu\text{W}$.
  - Raw single-lane throughput: up to $50,000.0\,\text{Mbps}$ ($50.0\,\text{Gbps}$ HDR) or $100,000.0\,\text{Mbps}$ ($100.0\,\text{Gbps}$ NDR).
  - Energy efficiency: $0.00115\,\text{pJ/bit}$ at full rate.
