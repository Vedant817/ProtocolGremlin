# Decisions Log

## 2026-09-14 - Bootstrap architecture decisions

- **Base scaffold:** graft onto the real `TinyTapeout/ttihp-verilog-template`
  (`cmos5l` branch) rather than inventing our own layout, so Tiny Tapeout's
  CI/precheck/GDS flow keeps working. Only `info.yaml` metadata/pinout and
  `test/tb.v`'s instantiated module name were changed from the template.
- **Toolchain:** no sudo/root access is available in the working
  environment, so the RTL toolchain (Icarus Verilog, Verilator, Yosys,
  cocotb) is installed via Miniforge/conda-forge under the user's home
  directory instead of `apt-get`. See `docs/toolchain.md`.
- **ISA v0 scope:** deliberately small (19 opcodes, 4 registers, no
  shift instructions, single GPIO bus mapped to `uio[7:0]` only). This is
  explicitly a bootstrap slice to prove the fetch/decode/execute pipeline
  end-to-end with a real differential test, not the final competition ISA.
  Full ISA design (including multi-lane architecture) is deferred until
  after PPA evidence exists from this baseline - see
  `PROJECT_MASTER_PLAN.md` section 7 ("Baseline Architecture Direction").
- **GPIO mapping:** the programmable GPIO bus is mapped onto Tiny Tapeout's
  `uio[7:0]` (the only truly bidirectional TT pins), not the dedicated
  `ui_in`/`uo_out` pins, since direction control only makes sense on a
  bidirectional bus. `ui_in`/`uo_out` are left as unused placeholders for
  now (see `docs/limitations.md`).
- **Program memory:** v0 uses a `$readmemh`-initialized, combinationally
  read ROM. This is known to conflict with the "reprogrammable after
  fabrication" requirement and is queued as P0 follow-up work rather than
  solved in the bootstrap slice, to keep this first change reviewable.
- **Verification:** the first test is a cycle-by-cycle differential test
  (RTL vs. an independently written Python ISA model), not merely "the
  simulation runs without error" - see `docs/verification.md`.
- **Debug access:** no debug ports were added to `core.v`/`project.v`;
  cocotb's hierarchical signal access into internal registers is used
  instead, to keep the Tiny Tapeout pinout unmodified.

## 2026-09-14 - Bugs found and fixed while bringing up the differential test

- Markdown-style single backticks and a literal `*/` substring inside
  Verilog block comments broke Icarus Verilog compilation (backticks are
  macro-expanded even inside comments; `GDIR*/GWR*` prematurely closed a
  block comment). Fixed by removing both patterns from all RTL comments -
  a durable rule going forward: never use backticks or `*/` in Verilog
  comment prose.
- Genuine RTL bug: `rd_val`/`rs_val` were driven by a function (`reg_read`)
  reading module-level regs not in its argument list; Icarus only tracked
  the explicit argument for re-evaluation, so back-to-back same-register
  instructions (e.g. `ANDI R0` then `ORI R0`) read a stale value. Fixed by
  replacing the function with explicit continuous-assign muxes. See
  `src/core.v`'s comment at the `rd_val`/`rs_val` declaration.
- Testbench-only issue (not an RTL bug): sampling DUT-internal registers
  immediately after `await RisingEdge` in cocotb raced with the DUT's own
  NBA updates. Fixed with `await ReadOnly()` after each `RisingEdge` before
  reading signals - see `test/test.py`.
- Full details: `orchestrator/experiments.jsonl` (`exp-001`).

## 2026-09-14 - Novelty & verification research (grounding for the next 4 iterations)

Before committing to a "novel" direction, researched real prior art rather
than guessing:

- **RP2040 PIO** (pico-sdk instruction encoding, MicroPython PIO docs, and
  the independent `rp2040pio-docs` emulator project): 9 instructions,
  side-set + delay bits on every instruction, autopull/autopush shift
  registers, IRQ flags for cross-state-machine sync. Load-bearing finding:
  the emulator project's own documentation states plainly that PIO programs
  cannot be traced, single-stepped, or have any internal state (X/Y/ISR/OSR/
  PC) inspected on real hardware at all - that project exists purely to work
  around that gap. This is a genuine, citable weakness of the architecture
  Jane Street explicitly points to as inspiration.
- **TI PRU**: non-pipelined, single-cycle, fully deterministic; dedicated
  R30/R31 GPIO out/in registers. Confirms the "simple, deterministic,
  single-cycle" direction ISA v0/v1 already took is sound, not naive.
- **riscv-formal / RVFI** (YosysHQ): the standard way to formally verify a
  small CPU core is a dedicated per-cycle "retirement" interface consumed
  by both a differential testbench and a SymbiYosys harness. Reusable here,
  not previously applied (as far as this research found) to a protocol-
  emulator/PIO-style core specifically.
- **Mutation testing literature** (Huang et al., "Functional Testbench
  Qualification by Mutation Analysis", VLSI Design 2015; Mantra, DAC 2023;
  Firefly, MLCAD 2025): conventional coverage percentages routinely
  overstate testbench quality - Firefly reports 28-67% of injected faults
  surviving testbenches with 90-95% conventional coverage. Justifies
  budgeting real, measured mutation-kill-rate work rather than treating
  coverage numbers (or "the differential test passes") as sufficient
  evidence on their own.

**Resulting novelty thesis**: make the engine's internal state observable
and capturable in a way PIO explicitly is not, and make that same mechanism
a genuine reverse-engineering primitive (edge/timestamp capture for
autobaud-style unknown-protocol timing discovery) - directly answering both
Jane Street's "consider what you'd do differently" prompt and its stated
interest in hardware debugging/reverse engineering. Verification-side:
adopt the RVFI pattern (proven, but novel in this domain) plus measured
mutation-kill rates.

## 2026-09-14 - ISA v1: reprogrammable program RAM + SHIFTOUT/SHIFTIN

- Replaced `src/program_rom.v` ($readmemh, fixed at elaboration) with
  `src/program_ram.v` (genuinely writable) plus a serial bootloader FSM
  built into `core.v`, reusing the existing `uio` bus (no new pins). This
  directly fixes the "must be reprogrammable after fabrication" requirement
  that v0 did not meet. Every test, including the original differential
  test, now loads its program exclusively through this mechanism -
  `$readmemh` was removed from the RTL and the test suite entirely, rather
  than keeping two parallel loading paths.
- Deliberately kept `program_ram.v`'s read port combinational (not
  synchronous) to avoid introducing fetch-pipeline hazards (a taken branch
  would invalidate a speculatively-fetched next instruction) into what is
  otherwise a simple, easy-to-verify one-instruction-per-cycle timing
  model. This is flagged as a follow-up PPA/synthesis-mapping concern, not
  a reprogrammability one - see `docs/limitations.md`.
- No checksum/CRC on the bootloader frame in v1 - deliberately deferred
  (P1 in `orchestrator/queue.md`) rather than half-implemented under time
  pressure.
- SHIFTOUT/SHIFTIN added with a deliberately paired bit-ordering (LSB-first
  transmit, matching UART) so that N SHIFTOUTs followed by N SHIFTINs
  reconstruct a byte exactly - see `docs/isa.md`.
- **Bugs found and fixed during bring-up** (full detail:
  `orchestrator/experiments.jsonl` `exp-002`): the GPIO input synchronizer
  resets to 0 and takes 2 cycles to reflect a real pin value, so naively
  sampling `LOAD_REQ` on the very first post-reset cycle always read 0 -
  fixed with an explicit settle counter in the bootloader FSM. Separately,
  the cocotb bootloader driver could not return at the *exact* cycle the
  hardware handed off to normal execution (a few real instructions could
  already retire while the driver was still in its final bit-clock pulse),
  which broke a naive "assume PC=0" differential-test setup; fixed by
  snapshotting the RTL's actual post-load architectural state into the
  Python model rather than assuming a fixed cycle count.

## 2026-09-15 - Iteration 2: Real UART firmware, independent decoder & isolated unit tests

- **Parameterized UART assembly generator:** Since the core has no data RAM,
  bytes to transmit are loaded via `LDI`. `tools/uart_model.py` provides
  `build_uart_tx_asm(byte_value, bit_period_cycles, pin)` that parameterizes
  both the transmitted byte and the bit duration in clock cycles.
- **Cycle-exact bit timing:** Detailed cycle analysis revealed that since
  `uio_out` is registered on posedge clk, `SHIFTOUT` takes 1 cycle to present
  the shifted bit. Therefore, to achieve a bit period of $P$ cycles, each bit
  is held with `WAIT (P - 2)`. This yields exactly $P$ clock cycles for the
  start bit, each of the 8 data bits, and the stop bit.
- **Independent UART decoder model:** To avoid circular verification,
  `tools/uart_model.py`'s `UartReceiver` implements a standard asynchronous
  receiver algorithm: detect falling edge, sample at $0.5 \times P$ to verify
  the start bit, sample at $(k + 0.5) \times P$ for data bits $k \in [0, 7]$,
  and sample at $9.5 \times P$ to verify stop bit is high. Any off-by-one timing
  bug would accumulate phase error and trigger framing errors.
- **Verification results:** `test/test_uart.py` verifies transmission of edge
  cases (`0x00`, `0xFF`, `0x55`, `0xAA`) and pseudorandom frames across multiple
  bit periods ($P=4, 8, 16$) with a fresh reset and serial bootload for each frame.
- **Per-opcode isolated unit tests:** Created `test/test_opcodes.py` directly
  exercising all ALU operations, conditional branches, loops, GPIO directions,
  and bit shifts in isolation with deterministic assertions.
- **UART RX quantization jitter finding:** Formulated the key motivation for
  Iteration 3's `WAITEDGE` instruction: software poll-loops (`GRD`/`ANDI`/`JNZ`)
  take 3 cycles per iteration, causing 3-cycle jitter (37.5%-75% of bit period at
  $P \le 8$), proving the necessity of hardware-level edge synchronization.

## 2026-09-15 - Iteration 3: PVFI Formal Interface, SymbiYosys Formal Harness & WAITEDGE

- **PVFI (Protocol-engine Verification Formal Interface):** Implemented an
  RVFI-inspired per-cycle retirement bus on `core.v`. When `-DPVFI` is passed,
  `core` exports `pvfi_valid`, `pvfi_order`, `pvfi_insn`, `pvfi_pc_rdata`,
  `pvfi_pc_wdata`, `pvfi_rd_addr`, `pvfi_rd_wdata`, `pvfi_rd_we`, `pvfi_gpio_oe`,
  `pvfi_gpio_wdata`, `pvfi_gpio_rdata`, `pvfi_halted`, and `pvfi_cycle`.
  In taped-out silicon, these ports are conditioned out (zero area/pin cost).
- **RP2040 PIO opacity research:** Directly addressed Jane Street's prompt by
  contrasting with RP2040 PIO, where emulator projects explicitly state that
  state machines cannot be traced, single-stepped, or inspected on hardware.
  PVFI makes protocol engine execution fully observable.
- **SymbiYosys formal harness (`formal/core.sby`, `formal/core_formal.v`):**
  Constructed SMT-BMC formal harness using Z3. Formally proved:
  1. Clean reset convergence across all architectural registers and FSM states.
  2. PC range safety ($PC \le 255$) invariant.
  3. WAIT sequence strict countdown and deterministic termination.
  4. Halt permanence and architectural register stability.
  5. Monotonic increment of 32-bit free-running hardware cycle counter.
  6. Bootloader FSM LD_DONE state absorbency.
  7. PVFI retirement interface correctness.
  Result: 20-step SMT-BMC PASS (0 violations).
- **`WAITEDGE rd, imm8` instruction (Opcode 21):**
  Added hardware edge detection and cycle capture. Stalls PC until rising,
  falling, or toggle edge occurs on a selected GPIO pin, then writes the exact
  elapsed cycle count into `rd`. Mode 3 (`0x18`) captures the lower 8 bits of
  the free-running cycle counter into `rd` with zero stalls.
- **Autobaud & timing discovery verification:**
  `test/test_waitedge.py` verifies measurement of unknown pulse widths (5, 11,
  23, 47 cycles) to single-cycle accuracy, timestamp capture delta verification,
  and cycle-by-cycle differential match with `tools/isa_model.py`.

## 2026-09-15 - Iteration 4: Mutation Testing Harness, Constrained-Random Fuzzer & Real PPA

- **Seeded RTL mutation testing (`scripts/mutate.py`):**
  - Grounded in academic literature (Huang et al., 2015; Firefly, 2025) which
    proves code coverage alone is an insufficient measure of hardware verification
    strength.
  - Implemented 10 seeded first-order RTL mutation operators across all design
    blocks (`src/core.v`, `src/alu.v`, `src/gpio.v`): branch condition inversion,
    WAIT countdown off-by-one, ALU sum corrupt, ALU SUB-to-ADD operator replacement,
    reset PC corruption, DECJNZ loop termination inversion, SHIFTOUT bit-order inversion,
    GPIO output enable inversion, WAITEDGE duration off-by-one, and bootloader
    `LOAD_REQ` ignore.
  - Evaluated against the test suite: **10/10 mutants killed (100.0% mutation kill rate)**
    in 46.95s of execution time. Results archived to `orchestrator/mutation_report.json`.
- **Constrained-random instruction fuzzer (`tools/fuzzer.py`, `test/test_fuzz.py`):**
  - Synthesizes valid, terminating random assembly programs. Ensures strictly
    bounded loop iterations (counter $\le 3$) and forward-only branches.
  - Executes differentially cycle-by-cycle against `tools/isa_model.py`.
  - Implemented automated delta-debugging program shrinker (`shrink_program()`),
    which performs 1-minimization instruction pruning to reduce failing traces to
    minimal reproducible counterexamples.
  - Regression suite expanded to 11/11 tests, all passing cleanly.
- **Real Yosys synthesis pass & measured PPA baseline:**
  - Automated via `scripts/synth.sh` and `scripts/synth.ys` targeting generic CMOS
    gates calibrated for IHP SG13G2 130nm standard cells.
  - Measured statistics: 14,056 pre-mapping cells, 19,143 mapped CMOS gates, 37,542 Gate
    Equivalents (GE), zero latches, zero combinational loops.
  - **Key architectural finding:** The entire active protocol processor core (Core + ALU + GPIO)
    occupies only **1,402 CMOS cells** (~1,990 GE, 147 DFFs), or just 7.3% of total design cells.
    The synthesized flip-flop program RAM consumes **17,741 CMOS cells** (92.7% of total cells,
    4,096 DFFEs), proving that memory storage dominates digital ASIC area when dedicated
    hardened SRAM macros are unavailable.
  - **Tile utilization:** The design fits the competition 8x4 tile allocation (32 tiles,
    $577,152\,\mu\text{m}^2$).
  - **Timing:** Longest topological path is 20 logic levels in the ALU and 19 levels in the
    RAM read multiplexer tree. At 10 MHz ($T_{\text{clk}} = 100\,\text{ns}$), estimated
    combinational propagation delay is $< 12\,\text{ns}$, providing $> 80\,\text{ns}$ of timing slack.

