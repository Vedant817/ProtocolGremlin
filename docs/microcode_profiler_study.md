# Protocol Microcode Performance Profiling & Cycle Budget Discovery Study

**Iteration:** 93 (RESEARCH_AND_PROOF Mode)  
**Target:** IHP 130nm SG13CMOS5L / Tiny Tapeout CMOS5L Shuttle  
**Status:** Tooling Complete & Verified (6/6 cocotb tests pass; 100% Mutation Kill; 515/515 Regression)  

---

## 1. Executive Summary

In high-assurance embedded systems and programmable protocol-emulation ASICs, software-defined protocol routines must strictly honor microsecond-level timing budgets, bus turnaround constraints, and memory limits. While dedicated fixed-function ASICs implement hardwired state machines with single-cycle throughput, the Jane Street Protocol Emulator achieves universality by executing programmable microcode on a compact 8-bit Harvard datapath.

To systematically evaluate the execution cost, throughput limits, and program memory overhead of emulating diverse communications standards, Iteration 93 introduces the **Automated Protocol Microcode Profiler** (`tools/profiler.py`) and its co-simulation verification suite (`test/test_profiler.py`).

The profiler performs dual-domain analysis:
1. **Static Program Inspection:** Maps instruction categorization, register utilization, pin allocation, and program RAM footprint.
2. **Dynamic Cycle Simulation:** Evaluates exact retirement counts, Cycles Per Instruction (CPI), active vs. wait stall distributions, wire bitrate, and a normalized **Efficiency Factor ($\eta$)** comparing actual cycles against theoretical physical limits.

---

## 2. Profiling Methodology & Architectural Metrics

### 2.1 Static Categorization Engine
Instructions are parsed directly from assembly source or assembled 16-bit binary words and grouped into six fundamental architectural domains:
- **ALU:** Arithmetic, bitwise logic, and comparisons (`ADDI`, `SUBI`, `ANDI`, `ORI`, `XORI`).
- **REGISTER:** Immediate loading and inter-register transfers (`LDI`, `MOV`).
- **GPIO:** Pin direction, push-pull drive, open-drain configuration, and pin reading (`GDIRI`, `GDIR`, `GWRI`, `GWR`, `GRD`, `GODRI`, `GODR`).
- **SHIFT_IO:** Hardware bit-serial serialization and deserialization (`SHIFTOUT`, `SHIFTIN`).
- **TIMING_WAIT:** Calibrated hardware pause and edge-synchronization timing (`WAIT`, `WAITEDGE`).
- **CONTROL_FLOW:** Conditional and unconditional branches, loop counters, and termination (`JMP`, `JZ`, `JNZ`, `DECJNZ`, `HALT`, `NOP`).

### 2.2 Dynamic Performance Modeling
Running atop the cycle-accurate Python `CoreModel` and mirrored in physical Verilog simulation, dynamic profiling computes:
- **Cycles Per Instruction (CPI):**
  $$\text{CPI} = \frac{\text{Total Elapsed Clock Cycles}}{\text{Retired Instructions}}$$
  For pure compute/shift code without wait states, $\text{CPI} = 1.00$.
- **Effective Bitrate:** Given a 10.0 MHz master clock frequency ($T_{\text{clk}} = 100\,\text{ns}$):
  $$\text{Throughput} = \frac{\text{Bits Transferred}}{\text{Total Cycles} \times 100\,\text{ns}} = \frac{\text{Bits} \times 10^7}{\text{Total Cycles}} \quad [\text{bps}]$$
- **Cycles Per Bit:**
  $$\text{CPB} = \frac{\text{Total Cycles}}{\text{Bits Transferred}}$$
- **Microcode Efficiency Factor ($\eta$):** Ratio of theoretical minimum cycles (Shannon wire limit with ideal single-cycle serialization) to actual cycles consumed:
  $$\eta = \frac{T_{\text{theoretical\_min}}}{T_{\text{dynamic}}} \in (0.0, 1.0]$$

---

## 3. Multi-Protocol Benchmark Scorecard

Evaluating the canonical protocol benchmark suite under a nominal 10.0 MHz ASIC clock yields the following empirical scorecard:

| Protocol Routine | Transferred Bits | Total Cycles | CPI | Effective Bitrate (kbps) | Cycles / Bit | Efficiency Factor ($\eta$) | RAM Usage |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **UART_TX_8N1** | 10 | 36 | 1.44 | 2,777.8 kbps | 3.6 | 55.6% | 25w (9.8%) |
| **SPI_MASTER_TRANSFER** | 16 | 46 | 1.00 | 3,478.3 kbps | 2.9 | 69.6% | 11w (4.3%) |
| **I2C_MASTER_WRITE** | 18 | 83 | 1.00 | 2,168.7 kbps | 4.6 | 65.1% | 27w (10.5%) |
| **MANCHESTER_BIPHASE** | 8 | 101 | 1.19 | 792.1 kbps | 12.6 | 47.5% | 19w (7.4%) |
| **SERDES_HEADER_24B** | 24 | 29 | 1.00 | 8,275.9 kbps | 1.2 | **96.6%** | 29w (11.3%) |

*Table 1: Protocol microcode micro-benchmark scorecard measured at 10.0 MHz on IHP 130nm SG13CMOS5L.*

### Key Insights:
1. **Unrolled SerDes Serialization:** By chaining hardware `SHIFTOUT` instructions without loop overhead, high-speed packet framing achieves **8.28 Mbps** with an efficiency factor of **96.6%** (only 1.21 cycles per bit transferred).
2. **Compact RAM Footprint:** All five canonical routines consume fewer than 30 program words each, occupying $\le 11.3\%$ of the 256-word on-chip program RAM.
3. **Pure Execution CPI:** Routines leveraging hardware `DECJNZ` loop branching (SPI, I2C, SerDes) execute with an ideal $\text{CPI} = 1.00$, confirming zero pipeline stalls or branch delay slots.

---

## 4. Verification & Mutation Qualification

1. **Unit & Co-Simulation Verification (`test/test_profiler.py`):**
   - 6 test cases testing static profiling, cycle-accurate simulation vs. RTL execution, UART performance, SPI transfer performance, SerDes throughput, and multi-protocol scorecard generation passed cleanly (6/6 PASS).
   - Total regression test count expanded from 509 to **515 passed tests** across 89 test modules.
2. **Mutation Testing (`scripts/mutate.py`):**
   - Injected mutant `MUT_96_PROFILER_WAIT_DECREMENT_CORRUPT` modifying `wait_remaining <= wait_remaining - 8'h01` to subtract `8'h02` per cycle.
   - The test suite detected the timing disruption and killed the mutant in 9.32s.
   - **Cumulative Mutation Score:** **96 / 96 mutants killed (100.0% kill rate)**.
3. **Silicon Area Impact:**
   - Zero additional gate overhead for profiling instrumentation; the profiler operates non-intrusively via the existing instruction set architecture and simulation models.
