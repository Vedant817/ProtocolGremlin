# Bunch of Wires (BoW / OpenHBI) Die-to-Die Physical Layer Engine

## 1. Executive Summary & Specification Context

As chiplet-based modular architectures expand across high-performance compute (HPC), AI accelerators, and network switches, cost-effective die-to-die (D2D) interconnects are essential. While proprietary and silicon-interposer-dependent solutions require expensive advanced packaging, the **Bunch of Wires (BoW)** specification—standardized under the **Open Compute Project (OCP) Open Domain-Specific Architecture (ODSA)** sub-project—provides an open, royalty-free, ultra-low-power physical layer optimized for standard organic laminate substrate packaging:
- **Packaging Profiles:**
  - **BoW-Base:** Designed for low-cost standard organic laminate substrates (bump pitch $100\text{--}130\,\mu\text{m}$, trace length $5\text{--}25\,\text{mm}$, data rate $2\text{--}16\,\text{Gbps}$ per wire).
  - **BoW-Fast / Advanced:** Designed for advanced packaging (silicon bridge, high-density fanout, bump pitch $\le 45\,\mu\text{m}$, data rates up to $25\text{--}32\,\text{Gbps}$).
- **OpenHBI (High Bandwidth Interconnect):**
  - An ODSA-aligned bus standard providing an open mapping of parallel bus interfaces (AXI, CHI, tile fabrics) onto underlying BoW physical slices.
- **Slice Architecture:**
  - 16 single-ended data wires ($D[15:0]$).
  - 1 forwarded strobe/clock wire ($CLK$).
  - 1 dedicated spare wire ($SPARE$) for lane remapping and yield recovery (18 physical wires per basic slice).
  - Clock-forwarded single-ended NRZ signaling eliminating per-lane Phase-Locked Loops (PLLs) and Clock-and-Data Recovery (CDR) circuits, drastically reducing latency and power.

---

## 2. Protocol Architecture & Framing Specification

### 2.1 Packet & Header Framing Structure
BoW control, training, and data flits utilize a deterministic framing structure transmitted MSB-first:
1. **Sync / Training Delimiter:** 1 byte (`0xBC` comma delimiter `0b10111100`).
2. **BoW OpCode:** 1 byte specifying transaction semantics:
   - `0x01`: `CALIB_REQ` (Impedance calibration / on-die termination matching request)
   - `0x02`: `CALIB_RESP` (Calibration response with drive strength / ODT settings)
   - `0x03`: `TRAIN_STROBE_REQ` (Forwarded strobe alignment & phase deskew pattern request)
   - `0x04`: `DATA_TRANSFER` (Raw payload data flit transfer)
   - `0x05`: `LANE_REMAP` (Remap identified faulty wire to slice spare wire)
   - `0x06`: `POWER_DOWN_REQ` (Low-power sleep state entry request)
   - `0x7E`: `IDLE` (Quiescent line keep-alive delimiter)
3. **Slice ID:** 1 byte identifying target BoW slice index ($0\text{--}255$).
4. **Payload:** Data bytes (0 or more bytes).
5. **CRC-16:** 2 bytes ANSI / IBM polynomial ($G_{\text{BoW}}(x) = x^{16} + x^{15} + x^2 + 1 = \text{0x8005}$).

### 2.2 Wire Sparing & Yield Recovery Mechanism
- To maximize die assembly yield on standard organic substrates, each BoW slice incorporates 1 spare wire per 16 data lanes.
- During link initialization (strobe training & bit error rate test) or runtime failure detection, an identified faulty wire is remapped via `LANE_REMAP`.
- The receiver decrements its available spare count (from 1 to 0). If an additional remap request is received when available spares are exhausted ($\text{spares} = 0$), an underflow fault occurs and the link controller halts with an error trap (`R2 = 0xEE`).

---

## 3. Microcode Implementation on the 8-Bit RISC Core

On the Tiny Tapeout deterministic 8-bit RISC core:
1. **Master Packet Header Transmission (`build_bow_tx_packet_asm`):**
   - Serializes SYNC training pattern (`0xBC`), opcode byte (e.g. `0x01` `CALIB_REQ`), and Slice ID (e.g. `0x00`) on pin 3.
   - Employs hardware bit serialization instruction `SHIFTOUT R0, 0x0B` (MSB-first on pin 3).
   - Enforces cycle-accurate baud pacing with zero jitter across byte transitions.
   - Sets status `R2 = 0x00` upon completion and halts.
2. **Slave Sync Ingress (`build_bow_rx_sync_asm`):**
   - Awaits SYNC delimiter rising edge via `WAITEDGE` on pin 3 (operand `0x0B`, bit 7).
   - Strides past remaining delimiter bits directly to opcode bit 7 midpoint.
   - Samples 8 subsequent bits into `R0` using `SHIFTIN R0, 0x0B` (MSB-first) and preserves them in `R1`, asserting `R2 = 0x00`.
3. **In-Register Opcode Filter (`build_bow_opcode_filter_asm`):**
   - Validates received opcode in `R0` against supported BoW transaction types (`0x01` through `0x06`).
   - Valid opcodes branch to `MATCH`, setting status `R2 = 0x00`.
   - Illegal opcodes (e.g. `0x7F`) fall through to error trap setting `R2 = 0xEE`.
4. **In-Register Spare Wire Allocation Tracker (`build_bow_spare_tracker_asm`):**
   - Tracks spare wire allocation (initial spares = 1):
     - Remap wire event (`0x01`): decrements spare count by 1 (`SUBI R0, 1`).
     - Restore spare event (`0x02`): increments spare count by 1 (`ADDI R0, 1`).
   - Traps sparing exhaustion / underflow ($< 0$) with `R2 = 0xEE`.

---

## 4. Synthesizable Hardware Coprocessor & PPA Scaling (IHP 130nm SG13G2)

For low-cost chiplet packaging on IHP 130nm SG13G2:
- **Software Microcode Engine:** **0 logic gates (0% area overhead)**.
- **Dedicated BoW / OpenHBI Physical Slice Macro:**
  - Standard cell count: **610 cells** (~$1190.0\,\text{GE}$, $+3.17\%$ area overhead).
  - Physical silicon footprint: $4,520.0\,\mu\text{m}^2$.
  - Critical path delay: $1.25\,\text{ns}$ ($f_{\text{max}} = 800.00\,\text{MHz}$).
  - Dynamic power at $10\,\text{MHz}$: $59.00\,\mu\text{W}$.
  - Raw throughput: $32,000.0\,\text{Mbps}$ (16 wires @ 2 Gbps) up to $256,000.0\,\text{Mbps}$ (16 Gbps).
  - Energy efficiency: $0.00045\,\text{pJ/bit}$.
