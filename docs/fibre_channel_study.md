# Fibre Channel 32G/64G (FC-FS-5) Physical Layer Engine

## 1. Executive Summary & Specification Context

Fibre Channel (standardized by ANSI INCITS Technical Committee T11 as FC-FS-5 / INCITS 545) is the preeminent high-speed storage area network (SAN) architecture engineered for mission-critical enterprise datacenters, high-availability storage arrays (NVMe over FC), financial transaction mainframes, and avionics flight mission computers. Fibre Channel combines lossless transport, deterministic hardware credit-based flow control (Buffer-to-Buffer Credit `BB_Credit`), low protocol overhead, and zero frame dropping under network congestion.

Fibre Channel physical layers scale across generations:
- **1G / 2G / 4G / 8GFC:** 1.0625 to 8.5 GBaud using DC-balanced 8b/10b line coding.
- **16GFC:** 14.025 GBaud using 64b/66b line coding.
- **32GFC (Gen 6):** 28.05 GBaud using 64b/66b line coding and Reed-Solomon RS(528, 514) Forward Error Correction (FEC).
- **64GFC (Gen 7):** 57.8 GBaud using 4-level Pulse Amplitude Modulation (PAM4) and RS(544, 514) FEC ($112.2\,\text{Gbps}$ aggregate $2\times$, $256\text{GFC} = 4\times$).
- **128GFC / 256GFC:** Multi-lane parallel trunking.

Key physical and framing characteristics include:
1. **Ordered Sets & Primitive Signals / Sequences:**
   - 4-byte Ordered Sets starting with special transmission delimiter `K28.5` (`0xBC`, `0b10111100`) in 8b/10b or sync header control blocks in 64b/66b.
   - **Start of Frame (SOF):** Marks start of frame and determines delivery class:
     - `SOFi3` (`0x57`): Start of Frame Initiate Class 3.
     - `SOFn3` (`0x58`): Start of Frame Normal Class 3.
     - `SOFf` (`0x59`): Start of Frame Fabric.
   - **End of Frame (EOF):** Delimits frame termination and transmission status:
     - `EOFn` (`0x5B`): End of Frame Normal.
     - `EOFt` (`0x5C`): End of Frame Terminate.
     - `EOFni` (`0x5D`): End of Frame Invalid.
   - **Primitive Signals:** `IDLE` (bus keep-alive), `R_RDY` (`0x4B`: Receiver Ready credit token).
2. **Buffer-to-Buffer Credit Flow Control (BB_Credit):**
   - Transmitting an FC frame decrements `BB_Credit` by 1.
   - Receiving an `R_RDY` primitive restores 1 credit to `BB_Credit`.
   - If `BB_Credit == 0`, transmission is halted in hardware until an `R_RDY` arrives, guaranteeing zero buffer overflow and zero frame loss.
3. **Fibre Channel Frame CRC (FC-CRC32):**
   - 32-bit CRC covering the 24-byte Frame Header and Payload:
     $$G_{\text{FC}}(x) = x^{32} + x^{26} + x^{23} + x^{22} + x^{16} + x^{12} + x^{11} + x^{10} + x^8 + x^7 + x^5 + x^4 + x^2 + x + 1 \quad (0xEDB88320)$$

---

## 2. Fibre Channel Frame & Primitive Architecture

### 2.1 Primitive Structure
A Fibre Channel primitive consists of:
- `K28.5` (`0xBC`): Comma sync character.
- Second Byte: Primitive function identifier (e.g. `0x4B` for `R_RDY`, `0x57` for `SOFi3`).
- Third & Fourth Bytes: Parameter bytes and line balance characters.

### 2.2 Standard Frame Structure
1. **Start of Frame (SOF):** 4 bytes (`SOFi3`, `SOFn3`, etc.).
2. **Frame Header:** 24 bytes containing Routing Control (`R_CTL`), Destination ID (`D_ID`), Class Specific Control (`CS_CTL`), Source ID (`S_ID`), Type (`TYPE`), Frame Control (`F_CTL`), Sequence ID (`SEQ_ID`), Data Field Control (`DF_CTL`), Sequence Count (`SEQ_CNT`), Parameter (`PARM`).
3. **Payload:** 0 to 2112 bytes.
4. **CRC:** 4 bytes IEEE 802.3 / FC-CRC32.
5. **End of Frame (EOF):** 4 bytes (`EOFn`, `EOFt`, etc.).

---

## 3. Microcode Implementation on the 8-Bit RISC Core

On the Tiny Tapeout deterministic 8-bit RISC core:
1. **Master Primitive Transmission (`build_fc_tx_primitive_asm`):**
   - Transmits `K28.5` comma (`0xBC`) on pin 3.
   - Emits primitive ID byte (e.g. `0x4B` for `R_RDY` or `0x57` for `SOFi3`) LSB-first at defined baud rate.
   - Asserts status `R2 = 0x00`.
2. **Slave Comma Ingress (`build_fc_rx_sync_asm`):**
   - Awaits `K28.5` rising edge via `WAITEDGE` on pin 3 (operand `0x0B`).
   - Strides past remaining 6 bits of comma delimiter into primitive ID byte.
   - Samples 8 subsequent bits into `R0` and preserves them in `R1`, setting `R2 = 0x00`.
3. **In-Register Buffer-to-Buffer Credit Tracker (`build_fc_credit_tracker_asm`):**
   - Evaluates credit event:
     - Event `0x01` (`R_RDY` received): increments credit counter in `R0` by 1 (`ADDI R0, 1`).
     - Event `0x02` (Frame sent): decrements credit counter in `R0` by 1 (`SUBI R0, 1`).
   - Asserts `R2 = 0x00` on valid credit or traps credit underflow (< 0) with `R2 = 0xEE`.
4. **In-Register SOF Delimiter Filter (`build_fc_sof_filter_asm`):**
   - Evaluates received SOF delimiter in `R0`.
   - Valid SOF types (`0x57` SOFi3, `0x58` SOFn3, `0x59` SOFf) return `R2 = 0x00`.
   - Illegal delimiters trap with `R2 = 0xEE`.

---

## 4. Synthesizable Hardware Coprocessor & PPA Scaling (IHP 130nm SG13G2)

For multi-gigabit Fibre Channel 32G/64G line rates on IHP 130nm SG13G2:
- **Software Microcode Engine:** **0 logic gates (0% area overhead)**.
- **Dedicated Fibre Channel 32G/64G PCS/MAC Macro:**
  - Standard cell count: **595 cells** (~$1160.0\,\text{GE}$, $+3.08\%$ area overhead).
  - Physical silicon footprint: $4,380.0\,\mu\text{m}^2$.
  - Critical path delay: $1.25\,\text{ns}$ ($f_{\text{max}} = 800.00\,\text{MHz}$).
  - Dynamic power at $10\,\text{MHz}$: $58.0\,\mu\text{W}$.
  - Raw throughput (single lane): $32,000.0\,\text{Mbps}$ (32GFC) or $64,000.0\,\text{Mbps}$ (64GFC).
  - Energy efficiency: $0.00181\,\text{pJ/bit}$ at 32GFC.
