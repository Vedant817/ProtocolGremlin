# Hardware Watchdog Timer & Brownout Recovery Circuit Feasibility Study

## 1. Executive Summary

This study evaluates the architectural, circuit-level, and firmware feasibility of integrating a **Hardware Watchdog Timer (WDT)** and **Brownout Detection & Safe State Recovery (BOD/POR)** subsystem into the **Jane Street Protocol Emulator ASIC** for the IHP SG13G2 130nm CMOS5L process under Tiny Tapeout constraints.

### 1.1 Mission-Critical Reliability Challenges
In harsh automotive (ISO 26262 ASIL-B/D), aerospace (DO-254), and industrial IoT deployments, programmable protocol emulators face physical threats:
1. **Unbounded Firmware Hangs**: A corrupted or severed communication wire can cause an unconditional edge-wait instruction (`WAITEDGE`) to stall indefinitely if an expected transition never occurs.
2. **Infinite Loops & Soft Errors**: Single-Event Upsets (SEUs) induced by ionizing radiation, thermal noise, or power supply transients can flip bits in architectural registers (`R0`–`R3`) or program counter (`PC`), trapping execution in unintended branch loops.
3. **Supply Rail Brownouts**: Transient supply voltage dips below minimum CMOS operating margins ($V_{DD} < 1.0\,\text{V}$) can corrupt sequential state while leaving static storage partially powered, leading to unpredictable execution upon supply restoration unless cleanly trapped and recovered.

### 1.2 Key Architectural Conclusions
1. **Windowed Watchdog Timer (WWDT)**: A 16-bit programmable hardware downcounter with configurable lower ($T_{\min}$) and upper ($T_{\max}$) service windows prevents both code lockups ($T > T_{\max}$) and erratic runaway execution ($T < T_{\min}$).
2. **Keyed Service Mechanism**: Requiring a two-token service sequence (`0x5A` followed by `0xA5`) prevents runaway or corrupted firmware from inadvertently kicking the watchdog.
3. **Sticky Reset Status Register (`RESET_STATUS`)**: Distinguishes Cold Power-On Reset (`0x01`), Watchdog Timeout Reset (`0x02`), Brownout / External Reset (`0x03`), and Bootloader CRC Error Reset (`0x04`).
4. **Warm-Boot Recovery (< 10 cycles)**: Leveraging the ASIC's serial bootloader warm-boot feature (`LOAD_REQ = 0`), execution resumes immediately from preserved program RAM without requiring external host reprogramming.
5. **Silicon PPA Impact**: Adding a 16-bit windowed watchdog, reset status latch, and alarm output adds ~180 standard cells (~350 GE, +0.9% area overhead), fitting comfortably within the 8x4 Tiny Tapeout footprint with zero impact on operating frequency ($f_{\max} > 50\,\text{MHz}$).

---

## 2. Watchdog Architecture & Theory of Operation

### 2.1 Standard vs. Windowed Watchdog Timer
A standard watchdog timer asserts reset only when a counter underflows ($T > T_{\max}$). However, firmware corrupted by an SEU may become stuck in a tight loop that contains the watchdog service instruction, perpetually kicking the timer while failing to perform its intended protocol translation tasks.

A **Windowed Watchdog Timer (WWDT)** enforces a closed time window $[T_{\min}, T_{\max}]$:
- **Too Early ($T < T_{\min}$)**: If the core services the watchdog before the minimum window expires, the WWDT treats this as a runaway execution fault and asserts an immediate reset.
- **Valid Window ($T_{\min} \le T \le T_{\max}$)**: Servicing within this window reloads the counter to its initial period $T_{\text{reload}}$ and clears the alarm flag.
- **Too Late ($T > T_{\max}$)**: If the counter reaches zero before service, a timeout fault is triggered.

```text
    0 ---------------- [T_min] ==================== [T_max] ------------> Time
        Too Early               Valid Service Window        Too Late
      (Fault -> Reset)              (Permitted)         (Fault -> Reset)
```

### 2.2 Keyed Two-Token Service Protocol
To guarantee that random code execution or pointer corruptions cannot service the watchdog:
1. The core must write Token A (`0x5A`) to the watchdog service register.
2. The core must write Token B (`0xA5`) to the watchdog service register on the subsequent write.
3. Any other write value or out-of-order write immediately trips an **Illegal Service Fault**, asserting reset.

### 2.3 Hardware Fault Actions
When a watchdog fault occurs:
1. **Dedicated Hardware Alarm (`uo_out[2]`)**: Pin `uo_out[2]` (`wdt_alarm`) pulses HIGH, notifying external system supervisors, power management ICs (PMICs), or LED fault indicators.
2. **Internal Core Soft Reset (`rst_core_n`)**: Asserts internal reset for 4 clock cycles, resetting `PC = 0`, register bank `R0`–`R3 = 0`, and GPIO direction masks to High-Z input mode (`0x00`).
3. **Sticky Register Latch**: Captures the fault reason (`RESET_STATUS = 0x02`), preserved across soft-reset cycles.

---

## 3. Brownout Detection & Safe State Recovery

### 3.1 PDK Realities & Tiny Tapeout Constraints
In the IHP 130nm SG13G2 process, true precision analog voltage bandgaps and low-power comparators require analog macro blocks. In standard digital Tiny Tapeout tiles (digital standard-cell only), dedicated analog supply voltage monitors cannot be fabricated directly on-chip.

Therefore, robust industrial designs employ a **co-design architecture**:
1. **External Voltage Supervisor**: An external automotive-grade supervisory IC (e.g. Texas Instruments TPS3823, Analog Devices MAX809, or STMicroelectronics STM811) monitors the $1.8\,\text{V}$ core and $3.3\,\text{V}$ I/O rails.
2. **Deglitched Digital Input**: When supply voltage drops below $V_{\text{BOD}} = 1.62\,\text{V}$ (10% drop), the supervisor pulls `rst_n` LOW for a guaranteed minimum reset duration ($200\,\text{ms}$).
3. **On-Chip Schmitt Trigger & Glitch Filter**: A multi-stage digital filter on `rst_n` suppresses nanosecond transients and contact bounce, ensuring metastable-free reset deassertion.

### 3.2 Cold vs. Warm Boot State Recovery Flowchart

```text
               +-----------------------------+
               | Power-On / Reset (rst_n=0)  |
               +-----------------------------+
                              |
                              v
               +-----------------------------+
               |  Check LOAD_REQ on uio[0]   |
               +-----------------------------+
                      /               \
            LOAD_REQ=1                 LOAD_REQ=0
                    /                     \
                   v                       v
      +------------------------+  +-------------------------------+
      | Cold Boot: Serial Load |  | Warm Boot: Skip Serial Load   |
      | - Shift in N words     |  | - Retain Program RAM intact   |
      | - Verify CRC-8 Checksum|  | - Set RESET_STATUS = 0x03     |
      | - Success: PC=0        |  | - PC=0, execute immediately   |
      +------------------------+  +-------------------------------+
                   |                               |
                   +--------------+----------------+
                                  |
                                  v
                  +-------------------------------+
                  | Firmware Warm-Boot Handler:   |
                  | 1. Read RESET_STATUS          |
                  | 2. If 0x02 (WDT) / 0x03 (BOD):|
                  |    - Run RAM Checksum (CRC-8) |
                  |    - Restore protocol state   |
                  |    - Emit diagnostic telemetry|
                  | 3. Enter normal protocol loop |
                  +-------------------------------+
```

---

## 4. Hardware Implementation & PPA Feasibility

### 4.1 Logic Architecture
The hardware watchdog consists of:
- **Prescaler**: 8-bit programmable clock divider ($f_{\text{wdt}} = f_{\text{clk}} / 256$). At $10\,\text{MHz}$, tick period is $25.6\,\mu\text{s}$.
- **Downcounter**: 16-bit downcounter providing timeouts from $25.6\,\mu\text{s}$ up to $1.67\,\text{s}$.
- **Window Comparator**: Validates $T_{\text{count}} \le T_{\min}$ on pet attempts.
- **Service FSM**: 2-state token validator (`IDLE` -> `TOKEN_A_SEEN` -> `RESET_COUNTER`).
- **Reset Generator**: 4-cycle pulse stretcher driving `rst_core_n`.

### 4.2 Standard Cell Area & PPA Synthesis Estimation (IHP 130nm)

| Block | Gate / Flip-Flop Breakdown | Cell Count | Estimated GE |
| :--- | :--- | :--- | :--- |
| 8-bit Prescaler | 8 DFFs + 8-bit incrementer + comparator | ~24 cells | ~48 GE |
| 16-bit Downcounter | 16 DFFs + 16-bit decrementer + zero detector | ~52 cells | ~104 GE |
| Window Comparator | 16-bit magnitude comparator ($A \le B$) | ~28 cells | ~56 GE |
| Token FSM & Glitch Guard | 4 DFFs + token matching logic | ~22 cells | ~44 GE |
| Reset Status Registers | 4 sticky DFFs + write-lock muxes | ~18 cells | ~36 GE |
| Reset Pulse Generator | 3 DFFs + output gating | ~12 cells | ~24 GE |
| **Total Watchdog Subsystem** | **~35 DFFs + combinational logic** | **~156–180 cells** | **~312–360 GE** |

### 4.3 Overhead Relative to ASIC Baseline
- **Current ASIC Gate Count**: 19,291 CMOS cells (37,832 GE).
- **Watchdog Addition**: +180 cells (+360 GE).
- **Percentage Increase**: **+0.93%** area overhead.
- **Critical Path Impact**: Zero. The watchdog runs synchronously on its own counter and asserts purely asynchronous or retimed reset signals.

---

## 5. Firmware Servicing & Diagnostic Integration

### 5.1 Safe Firmware Service Routine
In firmware, the protocol loop embeds the two-token service sequence:
```assembly
; =============================================================
; Robust Watchdog Servicing Pattern
; =============================================================
service_wdt:
    LDI   R1, 0x5A             ; Token A
    ; Write Token A to WDT service port (e.g. via dedicated opcode or GPIO)
    LDI   R1, 0xA5             ; Token B
    ; Write Token B to WDT service port
```

### 5.2 Fault Isolation & Recovery Guarantee
1. If an ingress communication bus hangs during `WAITEDGE`, the watchdog timer decrements and trips.
2. The core re-initializes within 4 clock cycles ($400\,\text{ns}$ at $10\,\text{MHz}$).
3. All GPIO output enables are immediately cleared to High-Z (`uio_oe = 0x00`), preventing bus fight or electrical damage.
4. The warm boot sequence checks RAM validity and resumes monitoring within $< 20$ cycles, achieving deterministic recovery with zero human intervention.
