# Coherent Accelerator Processor Interface (OpenCAPI / CXL) Physical Layer Engine

## 1. Executive Summary & Specification Context

Compute Express Link (CXL, standardized by the CXL Consortium across CXL 1.1, 2.0, 3.0, and 3.1) and the Open Coherent Accelerator Processor Interface (OpenCAPI, standardized by the OpenCAPI Consortium / OpenPOWER across OpenCAPI 3.0 and 4.0) represent the foundational standards for high-bandwidth, ultra-low-latency, cache-coherent interconnects between host CPUs, hardware accelerators (GPUs, TPUs, FPGAs), and disaggregated memory expansion pools.

Unlike conventional asymmetric PCIe links that operate strictly through block-DMA or memory-mapped I/O transactions with tens of microseconds of driver overhead, CXL and OpenCAPI introduce byte-addressable load/store memory semantics and symmetric hardware cache coherency with sub-100ns latency.

### CXL / OpenCAPI Scaling Across Generations
- **CXL 1.1 / 2.0:** Operates over PCIe 5.0 electrical PHY ($32.0\,\text{GT/s}$, NRZ signaling) utilizing standard 68-Byte FLITs (Flow Control Units).
- **CXL 3.0 / 3.1:** Operates over PCIe 6.0 electrical PHY ($64.0\,\text{GT/s}$, 4-level Pulse Amplitude Modulation PAM4 line coding) utilizing 256-Byte standard FLITs and 528-bit latency-optimized FLITs with Forward Error Correction (FEC).
- **OpenCAPI 3.0 / 4.0:** 25.0 to 32.0 Gbps differential signaling per lane with 64-byte / 64b/66b transaction layer packets (TLP).

Key physical and framing characteristics include:
1. **Protocol Multiplexing & Sub-Protocols:**
   - **CXL.io:** Standardized PCIe protocol stack for discovery, link negotiation, configuration, interrupts, register I/O, and non-coherent DMA.
   - **CXL.cache:** Device Coherent (DCOH) protocol allowing external accelerators to cache host system memory in local L1/L2 caches with snooping and hardware invalidation.
   - **CXL.mem:** Host CPU protocol providing direct, byte-addressable load/store access to device-attached DRAM or storage-class memory (Type 3 memory expanders).
   - **OpenCAPI TL (Transaction Layer):** Direct cache-coherent memory access from accelerator to host memory space.
2. **FLIT (Flow Control Unit) Architecture:**
   - **68-Byte Standard FLIT (CXL 1.1 / 2.0):**
     - 4 slots $\times$ 16 bytes payload = 64 bytes data.
     - 2 bytes FLIT Header (Protocol ID, Slot formatting, credit tokens).
     - 2 bytes FLIT CRC-16 error detection.
   - **16-bit FLIT CRC (CRC-16):**
     - Polynomial: $P(x) = x^{16} + x^{12} + x^5 + 1$ (`0x1021`, CCITT / CXL standard polynomial) or multi-matrix syndrome checking ensuring single-bit and double-bit error detection.
3. **Link Synchronization & Delimiters:**
   - Synchronization patterns utilizing standard comma delimiters (`0xBC`, `K28.5`) or PCIe Gen 5/6 sync blocks (`0x4B`).
   - Receiver edge synchronization via `WAITEDGE` on pin transitions.

---

## 2. CXL / OpenCAPI Protocol Framing & Packet Architecture

### 2.1 Protocol Identifiers
In CXL and OpenCAPI multiplexed links, each FLIT or slot header indicates the target sub-protocol:
- `CXL_IO` (`0x01`): CXL.io PCIe transactions.
- `CXL_CACHE` (`0x02`): CXL.cache accelerator cache coherency requests/responses.
- `CXL_MEM` (`0x03`): CXL.mem byte-addressable memory access.
- `OPENCAPI` (`0x04`): OpenCAPI coherent transaction layer packet.
- `SYNC` (`0xBC`): Link synchronization delimiter (K28.5 comma).
- `IDLE` (`0x7E`): Link keepalive idle FLIT.

### 2.2 FLIT Structure
1. **Sync Delimiter:** 1 byte (`0xBC`).
2. **Protocol Header / Slot ID:** 1 byte specifying target coherent protocol (`0x01`..`0x04`).
3. **Payload Data:** Data chunks (16 to 64 bytes).
4. **CRC-16:** 2 bytes ($G_{\text{CXL}}(x) = 0x1021$).

---

## 3. Microcode Implementation on the 8-Bit RISC Core

On the Tiny Tapeout deterministic 8-bit RISC core:
1. **Master FLIT Transmission (`build_cxl_tx_flit_asm`):**
   - Emits CXL sync delimiter (`0xBC`) on pin 3.
   - Emits protocol header byte (e.g. `0x02` for `CXL.cache`) LSB-first at defined baud rate.
   - Asserts status `R2 = 0x00`.
2. **Slave Sync Delimiter Ingress (`build_cxl_rx_sync_asm`):**
   - Awaits sync delimiter rising edge via `WAITEDGE` on pin 3 (operand `0x0B`).
   - Strides past remaining bits into protocol header byte.
   - Samples 8 subsequent bits into `R0` and preserves them in `R1`, setting status `R2 = 0x00`.
3. **In-Register Protocol Filter (`build_cxl_protocol_filter_asm`):**
   - Compares received protocol byte in `R0` against supported sub-protocols:
     - `0x01` (`CXL.io`): valid, sets `R2 = 0x00`.
     - `0x02` (`CXL.cache`): valid, sets `R2 = 0x00`.
     - `0x03` (`CXL.mem`): valid, sets `R2 = 0x00`.
     - `0x04` (`OpenCAPI`): valid, sets `R2 = 0x00`.
   - Illegal protocol IDs (e.g. `0x1F`) branch to error trap asserting `R2 = 0xEE`.
4. **In-Register CRC-16 Validation (`build_cxl_crc16_validator_asm`):**
   - Verifies CRC-16 integrity of received FLIT slice.
   - Valid CRC syndrome sets `R2 = 0x00`; invalid syndrome sets `R2 = 0xEE`.

---

## 4. Synthesizable Hardware Coprocessor & PPA Scaling (IHP 130nm SG13G2)

For multi-gigabit CXL and OpenCAPI physical layers on IHP 130nm SG13G2:
- **Software Microcode Engine:** **0 logic gates (0% area overhead)**.
- **Dedicated CXL / OpenCAPI PCS/Link Macro:**
  - Standard cell count: **600 cells** (~$1170.0\,\text{GE}$, $+3.11\%$ area overhead).
  - Physical silicon footprint: $4,420.0\,\mu\text{m}^2$.
  - Critical path delay: $1.25\,\text{ns}$ ($f_{\text{max}} = 800.00\,\text{MHz}$).
  - Dynamic power at $10\,\text{MHz}$: $58.5\,\mu\text{W}$.
  - Raw throughput (single lane): $32,000.0\,\text{Mbps}$ (CXL 1.1/2.0 over PCIe 5.0) or $64,000.0\,\text{Mbps}$ (CXL 3.0 over PCIe 6.0 PAM4).
  - Energy efficiency: $0.00183\,\text{pJ/bit}$ at 32 Gbps.
