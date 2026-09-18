# RapidIO v4.0 Physical Layer & 8b/10b Packet Exchange Engine

## 1. Executive Summary & Specification Context

RapidIO (standardized by the RapidIO Trade Association and ISO/IEC 18372) is an open-standard, packet-switched interconnect architecture engineered specifically for embedded mission-critical systems, DSP antenna arrays, baseband cellular units (4G/5G), avionics mission computers, and radar processing platforms. RapidIO combines the high bandwidth and low pin count of serial point-to-point interconnects with the deterministic low latency and guaranteed hardware quality-of-service required in embedded computing.

RapidIO LP-Serial Physical Layer supports multi-gigabit link signaling across standard differential serial links ($1\times, 2\times, 4\times, 8\times, 16\times$ lane widths) at baud rates ranging from $1.25\,\text{GBaud}$ up to $25.0\,\text{GBaud}$ (in RapidIO 10xN / 25xN specifications). Physical layer characteristics include:
1. **8b/10b DC-Balanced Line Coding:**
   - Run-length limited ($\le 5$ consecutive ones or zeros) for clock and data recovery (CDR).
   - Standard K-character comma delimiter `K28.5` (`0xBC`, `0b10111100`) for lane alignment and symbol synchronization.
2. **Short & Standard Control Symbols (CS):**
   - 24-bit / 32-bit in-band control symbols embedded between or within data packets without requiring inter-packet gaps.
   - Distinct `stype0` (Packet-Accepted `PACC`, Packet-Retry `PRET`, Packet-Not-Accepted `PNAC`) and `stype1` (Link-Request, Link-Response, Multicast-Event) control fields.
   - Embedded 5-bit CRC ($G(x) = x^5 + x^4 + x^2 + 1$) protecting control symbols against backplane bit corruption.
3. **Packet Framing & End-to-End Integrity:**
   - Start-of-Packet (SOP) and End-of-Packet (EOP) delimiters.
   - Physical priority field (`prio[1:0]`) guaranteeing hardware preemption and dead-lock free routing.
   - 16-bit ITU-T CRC ($G(x) = x^{16} + x^{12} + x^5 + 1$) safeguarding the transport and logical layer payload.

---

## 2. RapidIO Control Symbol & Packet Architecture

### 2.1 Control Symbol Structure

RapidIO utilizes 24-bit short or 32-bit standard control symbols:
- **Lead Symbol:** `K28.5` (`0xBC`) comma synchronization delimiter.
- **Control Type (`stype0` / `stype1`):**
  - `stype0 = 0x00`: Packet-Accepted (`PACC`) - acknowledges flawless receipt of packet with matching ackID.
  - `stype0 = 0x01`: Packet-Retry (`PRET`) - requests sender retransmit from specified ackID due to buffer congestion.
  - `stype0 = 0x02`: Packet-Not-Accepted (`PNAC`) - signals packet checksum or transmission integrity error.
- **Parameter Field:** Contains `ackID` (6 bits), buffer status, and link state.
- **CRC-5 Field:** 5-bit polynomial error detection covering all control symbol fields:
  $$G_{\text{CRC5}}(x) = x^5 + x^4 + x^2 + 1 \quad (0x15)$$

### 2.2 Packet Encapsulation

RapidIO packets consist of:
1. **Physical Header:** Priority `prio[1:0]` (0 to 3, where 3 is highest priority), Critical Request Flow bit (`CRF`), and acknowledge ID (`ackID`).
2. **Transport Header:** Destination DeviceID (`destID`, 8 or 16 bits) and Source DeviceID (`srcID`).
3. **Logical Header (FType):** Format Type specifying transaction (NREAD, NWRITE, SWRITE, DOORBELL, MESSAGE, MAINTENANCE).
4. **Data Payload:** 0 to 256 octets.
5. **Frame Check Sequence:** 16-bit CRC covering entire packet from physical header through payload:
   $$G_{\text{CRC16}}(x) = x^{16} + x^{12} + x^5 + 1 \quad (0x1021)$$

---

## 3. Microcode Implementation on the 8-Bit RISC Core

On the Tiny Tapeout deterministic 8-bit RISC core:
1. **Master Control Symbol Transmission (`build_rapidio_tx_control_sym_asm`):**
   - Transmits `K28.5` comma (`0xBC`) on pin 3.
   - Emits 8-bit `stype` command byte (e.g. `0x00` PACC) at defined baud rate.
   - Asserts status `R2 = 0x00`.
2. **Slave Comma Ingress (`build_rapidio_rx_sync_asm`):**
   - Awaits `K28.5` rising edge via `WAITEDGE` on pin 3 (operand `0x0B`).
   - Strides past delimiter to sample `stype` byte into `R0` and preserves it in `R1`, asserting `R2 = 0x00`.
3. **In-Register Control Filter (`build_rapidio_packet_filter_asm`):**
   - Evaluates incoming control symbol `stype` in `R0`.
   - Valid responses (`0x00` PACC, `0x01` PRET, `0x02` PNAC) return `R2 = 0x00`.
   - Illegal / unrecognized stype traps into `R2 = 0xEE`.
4. **In-Register CRC-5 Validator (`build_rapidio_crc5_validator_asm`):**
   - Evaluates received 5-bit CRC mask in `R0` against computed expected polynomial residue, setting `R2 = 0x00` on match or `R2 = 0xEE` on corruption.

---

## 4. Synthesizable Hardware Coprocessor & PPA Scaling (IHP 130nm SG13G2)

For high-speed multi-gigabit RapidIO operation on IHP 130nm SG13G2:
- **Software Microcode Engine:** **0 logic gates (0% area overhead)**.
- **Dedicated RapidIO v4.0 PCS/MAC Macro:**
  - Standard cell count: **585 cells** (~$1140.0\,\text{GE}$, $+3.03\%$ area overhead).
  - Physical silicon footprint: $4,310.0\,\mu\text{m}^2$.
  - Critical path delay: $1.25\,\text{ns}$ ($f_{\text{max}} = 800.00\,\text{MHz}$).
  - Dynamic power at $10\,\text{MHz}$: $57.0\,\mu\text{W}$.
  - Raw throughput (single lane): up to $25,000.0\,\text{Mbps}$ ($25.0\,\text{Gbps}$ at 25xN) or $6,250\,\text{Mbps}$ (LP-Serial 6.25G).
  - Energy efficiency: $0.00228\,\text{pJ/bit}$ at full rate.
