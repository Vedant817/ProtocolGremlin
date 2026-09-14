# Jane Street Protocol Emulator ASIC — Complete Project Brief, Submission Plan, Verification Strategy, and Autonomous AI-Agent Orchestration

> **Prepared:** September 14, 2026  
> **Competition deadline:** January 18, 2027  
> **Target:** Jane Street open-source general-purpose protocol emulator ASIC challenge, targeting Tiny Tapeout / IHP 130 nm CMOS5L  
> **Goal of this document:** Preserve the complete challenge information supplied in the conversation, all major guidance already discussed, the relevant links, and a concrete plan for using Google Antigravity/managed agents to iterate on the implementation for days.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Original Jane Street Challenge Text Supplied in the Conversation](#2-original-jane-street-challenge-text-supplied-in-the-conversation)
3. [Can This Be Built Without Physical Hardware?](#3-can-this-be-built-without-physical-hardware)
4. [What We Actually Need to Submit](#4-what-we-actually-need-to-submit)
5. [Recommended Repository Structure](#5-recommended-repository-structure)
6. [What Jane Street Is Actually Asking For](#6-what-jane-street-is-actually-asking-for)
7. [Baseline Architecture Direction](#7-baseline-architecture-direction)
8. [Verification Strategy](#8-verification-strategy)
9. [ASIC / Tiny Tapeout Flow](#9-asic--tiny-tapeout-flow)
10. [Development Roadmap](#10-development-roadmap)
11. [How to Make the Entry Novel](#11-how-to-make-the-entry-novel)
12. [AI Coding-Agent Options Discussed](#12-ai-coding-agent-options-discussed)
13. [Recommended Autonomous Engineering System](#13-recommended-autonomous-engineering-system)
14. [Continuous-Improvement State Machine](#14-continuous-improvement-state-machine)
15. [Objective Scoring / Acceptance Criteria](#15-objective-scoring--acceptance-criteria)
16. [Antigravity-Orchestrator Design](#16-antigravity-orchestrator-design)
17. [Idle / No-Input Work Queue](#17-idle--no-input-work-queue)
18. [Safety, Reproducibility, and Change-Control Rules](#18-safety-reproducibility-and-change-control-rules)
19. [Master Prompt for Google Antigravity](#19-master-prompt-for-google-antigravity)
20. [All Relevant Links](#20-all-relevant-links)
21. [Current Verification Notes and Corrections](#21-current-verification-notes-and-corrections)

---

# 1. Executive Summary

The competition asks for an **open-source, general-purpose protocol emulator ASIC**: a small programmable hardware engine/CPU whose instruction set is optimized for deterministic pin reads/writes, waits, timing, branching, and low-level protocol behavior.

The intended design is **not** simply:

```text
UART peripheral
SPI peripheral
I2C peripheral
      +
     MUX
```

The intended design is closer to:

```text
                 ┌──────────────────────────┐
                 │ Programmable protocol CPU │
                 │ / state-machine engine    │
                 └────────────┬─────────────┘
                              │
             ┌────────────────┼─────────────────┐
             │                │                 │
             ▼                ▼                 ▼
        GPIO read/write   cycle timing      branch/events
             │                │                 │
             └────────────────┼─────────────────┘
                              ▼
                     programmable pins
                              │
           ┌──────────────────┼───────────────────┐
           ▼                  ▼                   ▼
       UART firmware      SPI firmware        I2C firmware

After fabrication:
new firmware/programs → JTAG / SWD / PS/2 / CAN-related logic /
custom proprietary protocols / sniffing / replay / bridging / fault injection
```

### Core competition constraints

- Open source.
- Target IHP 130 nm CMOS5L through Tiny Tapeout.
- Start from the Tiny Tapeout CMOS5L Verilog template.
- Configure `info.yaml` for **8x4 tiles**.
- Approximately 32 tiles / about 1 mm² nominal tile area.
- Rough planning estimate: approximately 1K logic cells per tile.
- Required starting protocols: **UART, SPI, I2C**.
- Stretch goals: **low-speed USB, 10 Mbit Ethernet**.
- Other interesting protocols: **JTAG, SWD, PS/2, CAN bus**.
- Deadline: **January 18, 2027**.
- FPGA is **optional**, not required.
- Jane Street intends to pay to fabricate selected designs.
- The final competition submission form is expected closer to the deadline.

### Recommended project strategy

Build a small deterministic protocol processor with:

- compact ISA,
- deterministic cycle semantics,
- GPIO direction/read/write operations,
- event/wait instructions,
- bit/sample/shift primitives,
- loops/branches,
- small program memory,
- small register/scratch memory,
- one or more tiny protocol lanes,
- optional synchronization/event fabric,
- firmware/assembler toolchain,
- extremely strong automated verification.

The project can be completed **without owning hardware** by using:

- RTL simulation,
- cocotb,
- Icarus Verilog and/or Verilator,
- formal verification (e.g. SymbiYosys where practical),
- Yosys synthesis,
- Tiny Tapeout / LibreLane physical flow,
- static timing,
- gate-level simulation,
- CI.

An FPGA can be added later if one becomes available, but it is not a prerequisite.

---

# 2. Original Jane Street Challenge Text Supplied in the Conversation

The following section preserves the challenge description supplied by the user.

---

Last month, we asked you to [reverse engineer a
chip](https://blog.janestreet.com/can-you-reverse-engineer-an-asic/) from nothing but its
layout and teased a bigger challenge. Results and our favorite writeups are coming soon.
In the meantime, here’s our next challenge!

This time, you’re designing the chip, and we’ll pay to fabricate our favorite designs!
We’re particularly interested in projects with unique functionality, as well as those
that demonstrate novel approaches to design and verification methodologies! Winners will
receive a fabricated copy of their chip, mounted on a dev boards, so they can test their
design in real silicon.

## The challenge

Design an **open-source, general-purpose protocol emulator ASIC**.

Hardware protocols like UART, SPI, and I2C are simple enough that people routinely
“bit-bang” them: toggle pins from software with careful timing instead of using a
dedicated peripheral. A protocol emulator is a small chip built to do exactly that: a tiny
CPU with an instruction set designed for reading pins, writing pins, counting cycles, and
hitting timing precisely enough that you can implement a real protocol in firmware rather
than in fixed logic. Something like that is a useful tool for hardware debugging and
reverse engineering, which is a good part of what we do.

The hard part is flexibility. The goal isn’t to put a UART block, an SPI block, and an
I2C block on one die and call it done. Your chip should be reprogrammable enough to support
new protocols *after* fabrication, within its timing and I/O constraints. For inspiration,
look at the PIO state machines on the RP2040 or the PRU cores on TI’s Sitara parts, and
consider what you’d do differently.

- Start with UART, SPI, and I2C.
- Stretch goals include low-speed USB and 10Mbit Ethernet.
- Other interesting protocols to consider: JTAG, SWD, PS/2, CAN bus
- If you have access to an FPGA, consider using it to test your RTL before the ASIC flow.
- Show us anything else your architecture makes possible that we haven’t thought of.

At Jane Street, we use [Hardcaml](https://hardcaml.org/) to generate the RTL for our FPGA
and ASIC designs. We are excited to see the languages and verification techniques you use,
including formal methods, random constrained tests, AI-assisted verification, and more.
As AI-assisted chip design becomes more common, we believe verification will be an
extremely important aspect of the ASIC design flow going forwards.

## The rules

- **Process:** We’re targeting IHP’s 130nm CMOS5L process through our friends at [Tiny Tapeout](https://www.tinytapeout.com/). Start with the [CMOS5L Verilog template](https://github.com/TinyTapeout/ttihp-verilog-template/tree/cmos5l), which takes you from RTL to GDS. Set the tile size in `info.yaml` to 8x4.
- **Area:** Our planned maximum is **8×4 Tiny Tapeout tiles per design**.
- **Open source:** Your submission should be open source so others can use and build on it. Unlike the reverse-engineering puzzle, there’s no need to keep your work hidden until the deadline, so feel free to build in public!
- **Teams:** This is a much bigger project than the puzzle, so we strongly recommend working in teams.
- **Deadline:** Submit your design by **January 18th, 2027**.
- **Prize:** We’ll pay to tape out the most novel designs on a Tiny Tapeout shuttle. We’re targeting the **March 2027 CMOS5L shuttle**, subject to the foundry schedule. Winners will receive chips and dev boards back after fabrication, so you can test your design in silicon.

## How much fits?

An 8x4 allocation is 32 tiles. At approximately 200um × 150um per tile, that’s about 1
mm² of nominal tile area. As a rough estimate, budget for about 1K logic cells per tile.
You may need to get creative to fit the functionality you want.

For instruction memory, SRAM can be more area-efficient than flip-flops. Tiny Tapeout has
[examples of SRAM](https://www.tinytapeout.com/chips/ttihp0p2/tt_um_urish_sram_test)
running on this process node you can reference.

Run synthesis early, check the mapped cell area, and leave room for clock-tree buffers
and routing. Then run the full place-and-route flow and check timing. A design that looks
small enough after synthesis can still be difficult to route or too slow at your chosen
clock frequency.

## Getting started

If you’ve never taped out a chip before, the
[Tiny Tapeout documentation](https://www.tinytapeout.com/) walks through the process end
to end, and the tools are all free and open source. Start by getting a UART transmitter
out of a pin. Then make it programmable.

**Sign-up** If you’re interested, please fill out our [sign-up
form](https://docs.google.com/forms/d/e/1FAIpQLSeF7fq756MegxZRQxotBwUJYZx-cL9MrGjxV0z4uD_J0sADxQ/viewform).
We’ll send updates about the tapeout template, deadlines, as well as providing the final
submission link. Note that filling out the form is not a commitment to participating, it’s
just to receive updates!

We’ll add a final submission form to this page closer to the deadline!

If you have questions along the way, reach out to
[asic-competition@janestreet.com](mailto:asic-competition@janestreet.com).

## Hardware at Jane Street

The hardware team at Jane Street designs FPGAs and ASICs that run some of the fastest
trading systems in the world. If designing a chip for fun sounds like your kind of thing,
take a look at our hardware
[internships](https://www.janestreet.com/join-jane-street/position/8624440002/) and
[full-time roles](https://www.janestreet.com/join-jane-street/position/8646893002/). You can
also explore [Hardcaml](https://hardcaml.org/), our open-source OCaml hardware design
libraries, or [stay in touch](https://bit.ly/4lEqiqb).

Ben is a hardware developer at Jane Street. Originally from New
Zealand, with a Ph.D. in EE from the University of Tokyo, he is
interested in the outdoors, sci-fi, cryptography, and pretty much
anything tech related.

Anish joined Jane Street as an FPGA engineer in 2024 after graduating from
Carnegie Mellon. He particularly enjoys building software tools to make
hardware design more efficient.

---

# 3. Can This Be Built Without Physical Hardware?

**Yes.**

A physical FPGA, oscilloscope, logic analyzer, dev board, or custom electronics setup is not required to produce a valid competition entry.

The software-only flow can cover:

```text
Architecture
   ↓
RTL
   ↓
RTL simulation
   ↓
Protocol behavioral models
   ↓
Randomized verification
   ↓
Formal verification
   ↓
Synthesis
   ↓
Area / cell analysis
   ↓
Place & Route
   ↓
Static timing
   ↓
Gate-level simulation
   ↓
Tiny Tapeout prechecks
   ↓
GDS
```

An FPGA is an **optional additional validation stage**:

```text
RTL
 ↓
FPGA prototype     [optional]
 ↓
ASIC physical flow
```

### Software tools that can replace most early hardware testing

- Icarus Verilog
- Verilator
- cocotb
- Python protocol models
- Yosys
- SymbiYosys/formal tools where appropriate
- Tiny Tapeout CI
- LibreLane/OpenROAD-based flow
- gate-level simulation
- static timing analysis

### What cannot be completely reproduced before silicon

Simulation cannot perfectly reproduce every analog/physical effect that may appear on fabricated silicon, for example:

- analog signal integrity,
- pad electrical behavior,
- exact process/voltage/temperature behavior,
- metastability in real physical conditions,
- external board issues,
- actual protocol transceiver electrical layers.

But for a primarily digital protocol engine, simulation + formal + ASIC signoff checks provide a strong development path before fabrication.

---

# 4. What We Actually Need to Submit

The competition says the project must be **open source**. Jane Street's page says a final submission form will be added closer to the deadline.

Treat the public repository as the primary submission artifact.

Recommended contents:

```text
PUBLIC GITHUB REPOSITORY
│
├── synthesizable RTL
├── programmable ISA specification
├── assembler/compiler/tooling
├── UART firmware
├── SPI firmware
├── I2C firmware
├── additional protocol examples
│
├── cocotb verification
├── randomized/constrained tests
├── formal properties
├── coverage/reporting
│
├── architecture documentation
├── timing reports
├── area reports
├── P&R results
├── generated GDS / build artifacts as appropriate
│
├── Tiny Tapeout info.yaml
├── Tiny Tapeout docs/info.md
├── reproducible CI
└── README / demos / evidence
```

### Important submission distinction

Do **not** assume that ordinary Tiny Tapeout checkout/submission alone is the Jane Street competition submission.

The intended sequence is:

1. Build the project using the Tiny Tapeout CMOS5L template.
2. Keep the repository public/open source.
3. Make sure Tiny Tapeout build/precheck flows pass.
4. Sign up for Jane Street challenge updates.
5. Use Jane Street's final submission form when it becomes available.
6. Selected designs are intended to be fabricated by Jane Street through the planned shuttle.

---

# 5. Recommended Repository Structure

A possible structure:

```text
protocol-emulator/
├── README.md
├── LICENSE
├── info.yaml
├── docs/
│   ├── info.md
│   ├── architecture.md
│   ├── isa.md
│   ├── verification.md
│   ├── ppa.md
│   └── protocols/
│       ├── uart.md
│       ├── spi.md
│       ├── i2c.md
│       ├── jtag.md
│       └── ...
│
├── src/
│   ├── project.v
│   ├── core.v
│   ├── decoder.v
│   ├── alu.v
│   ├── gpio.v
│   ├── timer.v
│   ├── event_unit.v
│   ├── program_memory.v
│   ├── scratch_memory.v
│   ├── lane.v
│   └── sync_fabric.v
│
├── firmware/
│   ├── uart_tx.asm
│   ├── uart_rx.asm
│   ├── spi_master.asm
│   ├── spi_slave.asm
│   ├── i2c_master.asm
│   ├── i2c_slave.asm
│   └── ...
│
├── tools/
│   ├── assembler.py
│   ├── disassembler.py
│   ├── firmware_pack.py
│   └── trace_decode.py
│
├── test/
│   ├── test_core.py
│   ├── test_uart.py
│   ├── test_spi.py
│   ├── test_i2c.py
│   ├── test_random_programs.py
│   ├── protocol_models/
│   └── tb.v
│
├── formal/
│   ├── core.sby
│   ├── gpio.sby
│   ├── properties/
│   └── harness/
│
├── scripts/
│   ├── regress.sh
│   ├── synth.sh
│   ├── pnr.sh
│   ├── report_metrics.py
│   └── compare_baseline.py
│
├── orchestrator/
│   ├── runner.py
│   ├── policy.md
│   ├── queue.md
│   ├── state.json
│   ├── metrics.json
│   ├── experiments.jsonl
│   └── prompts/
│
└── .github/
    └── workflows/
        ├── test.yml
        ├── formal.yml
        └── gds.yml
```

The exact structure can change, but all important knowledge must live in the repository rather than only in chat context.

---

# 6. What Jane Street Is Actually Asking For

The spirit of the challenge is **programmability after fabrication**.

### Weak interpretation

```text
UART fixed hardware
SPI fixed hardware
I2C fixed hardware
      ↓
   selector
```

This supports multiple protocols but is not a general-purpose protocol emulator.

### Strong interpretation

```text
              Program memory
                    │
                    ▼
        ┌─────────────────────┐
        │ deterministic core  │
        │ / protocol engine   │
        └─────────┬───────────┘
                  │
    ┌─────────────┼────────────────┐
    ▼             ▼                ▼
 pin input     pin output       cycle/event
 sampling      + direction        timing
    │             │                │
    └─────────────┼────────────────┘
                  ▼
             physical pins
```

UART, SPI, and I2C should primarily be **programs for the engine**, not hard-coded peripherals.

### Key architectural qualities

- deterministic timing,
- low and predictable instruction latency,
- easy bit manipulation,
- low-cost loops,
- pin sampling,
- pin direction changes,
- wait for pin/event,
- exact waits/delays,
- compact program encoding,
- low gate count,
- efficient program memory,
- predictable branches,
- ability to run protocols written after fabrication.

---

# 7. Baseline Architecture Direction

A promising architecture is a **small deterministic multi-lane protocol processor**.

```text
                 ┌───────────────────────────┐
                 │ Program / instruction RAM │
                 └─────────────┬─────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
           Lane 0           Lane 1           Lane 2
              │                │                │
              ▼                ▼                ▼
           GPIO A           GPIO B           GPIO C
              │                │                │
              └────────── event/sync ──────────┘
                               │
                    cycle-accurate coordination
```

Depending on area results, the final design could use:

- one stronger core,
- two or more tiny lanes,
- shared program memory,
- per-lane program counters,
- a tiny shared event fabric,
- shared scratch registers,
- specialized low-area shift/timing instructions.

### Example instruction families

Potential operations to explore:

```text
SET      mask, value
DIR      mask, direction
READ     dst, mask
WAIT     cycles
WAITPIN  pin, level
JMP      addr
JZ/JNZ   reg, addr
MOV      dst, src/immediate
AND/OR/XOR/SHL/SHR
DECJNZ   reg, addr
SHIFTIN
SHIFTOUT
SAMPLE
SETX     auxiliary timing/loop register
IRQ/EVENT
SYNC
```

This is only a starting point. The ISA must be judged by:

- hardware area,
- code density,
- deterministic timing,
- expressiveness,
- ease of implementing protocols,
- ease of formal verification,
- achievable clock frequency.

### Why multiple lanes may be interesting

Multiple tiny deterministic lanes could make the ASIC capable of:

- protocol bridging,
- simultaneous sniffing and transmission,
- independent clock/data handling,
- clock recovery experiments,
- fault injection,
- trigger-on-protocol-event behavior,
- side-by-side protocol conversion,
- logic-analyzer-like functions,
- active man-in-the-middle debugging,
- custom proprietary protocol emulation.

The lane count must ultimately be driven by synthesis/P&R evidence.

---

# 8. Verification Strategy

Verification should be one of the strongest parts of the submission.

Jane Street explicitly says it is interested in:

- formal methods,
- random constrained tests,
- AI-assisted verification,
- novel verification approaches.

## 8.1 Unit verification

Each block should have independent tests:

- decoder,
- ALU,
- timer,
- GPIO,
- instruction memory,
- branch behavior,
- event logic,
- synchronization,
- reset behavior.

## 8.2 Firmware-level protocol verification

### UART

Test:

- TX,
- RX,
- multiple baud divisors,
- data patterns,
- framing,
- start/stop behavior,
- long runs of 0/1,
- randomized bytes,
- back-to-back frames,
- edge timing.

Example virtual verification path:

```text
UART firmware
    ↓
protocol engine
    ↓
GPIO TX
    ↓
cocotb/Python UART receiver model
    ↓
decoded byte + timing assertions
```

### SPI

Test:

- CPOL=0/1,
- CPHA=0/1,
- master operation,
- slave operation if implemented,
- different word lengths,
- idle periods,
- back-to-back transfers,
- edge timing,
- randomized MOSI/MISO.

### I2C

Test:

- START,
- STOP,
- repeated START,
- ACK,
- NACK,
- address bytes,
- reads,
- writes,
- clock stretching,
- bus release/open-drain behavior,
- arbitration-related behavior where supported,
- edge cases around SDA/SCL ordering.

## 8.3 Randomized testing

Generate:

- random legal instruction streams,
- random timing parameters,
- random protocol transactions,
- randomized stalls/events,
- randomized input transitions.

Maintain deterministic seeds for replay.

## 8.4 Formal verification

Candidate properties:

- PC remains in valid instruction address space.
- No illegal writes to protected state.
- Reset converges to a defined idle state.
- GPIO output-enable semantics are safe.
- Wait counters terminate.
- Defined instructions have deterministic state transitions.
- Branch semantics match ISA specification.
- No unintended combinational loops.
- Event logic cannot deadlock under stated assumptions.
- Multi-lane synchronization satisfies invariants.
- Open-drain behavior never actively drives an illegal high state when configured for I2C mode, if such a hardware primitive exists.

Formal claims must be precise. Do not claim a protocol is "formally verified" unless the proof scope is explicitly stated.

## 8.5 Gate-level verification

After synthesis/P&R:

- run gate-level protocol tests,
- test reset,
- confirm timing assumptions remain valid,
- compare key traces with RTL simulation.

## 8.6 Differential / reference-model verification

Build a small ISA reference simulator in Python.

Then run:

```text
program + input trace
        │
        ├────────► Python ISA model
        │
        └────────► RTL simulation

Compare:
registers
PC
GPIO outputs
GPIO direction
events
cycle timing
```

This gives an excellent target for fuzzing.

## 8.7 Mutation testing

Deliberately inject bugs into RTL or firmware and verify the verification stack catches them.

Examples:

- invert a branch condition,
- shift on wrong SPI edge,
- off-by-one in wait counter,
- wrong UART stop-bit duration,
- drive I2C SDA high rather than release,
- skip event clear.

This demonstrates that the test suite is not merely large—it is effective.

---

# 9. ASIC / Tiny Tapeout Flow

Recommended flow:

```text
RTL
 │
 ├── lint
 ├── unit simulation
 ├── cocotb regressions
 └── formal checks
 │
 ▼
Yosys synthesis
 │
 ├── cell count
 ├── area estimate
 └── critical logic review
 │
 ▼
Tiny Tapeout / LibreLane physical flow
 │
 ├── floorplan
 ├── placement
 ├── CTS
 ├── routing
 ├── timing
 └── physical checks
 │
 ▼
gate-level netlist
 │
 └── gate-level protocol regressions
 │
 ▼
GDS + precheck
```

### Exit checklist

```text
RTL simulation         PASS
protocol regressions   PASS
random regressions     PASS
formal properties      PASS / documented bounded scope
synthesis              PASS
area budget            PASS
place & route          PASS
timing                 PASS
Tiny Tapeout precheck  PASS
gate-level simulation  PASS
documentation          COMPLETE
reproducibility        VERIFIED
```

### Why synthesis must happen early

A design that appears elegant in RTL may be:

- too large,
- too slow,
- routing-congested,
- memory-dominated.

Therefore architecture decisions must be informed by PPA evidence from early prototypes.

---

# 10. Development Roadmap

An initial progression previously proposed:

```text
Week 1
tiny CPU + GPIO + deterministic WAIT instruction
        ↓
Week 2
UART TX/RX in firmware
        ↓
Week 3
SPI + I2C firmware
        ↓
Week 4
assembler + refine ISA
        ↓
Week 5
randomized/cocotb verification
        ↓
Week 6
formal verification
        ↓
Week 7
synthesis + area optimization
        ↓
Week 8+
novel differentiator / stretch protocols / P&R refinement
```

A more complete milestone plan:

### Milestone A — Toolchain and baseline

- fork/use correct CMOS5L template,
- `info.yaml` = 8x4,
- CI working,
- trivial pin toggle,
- basic simulation.

### Milestone B — Minimal programmable core

- PC,
- instruction fetch,
- GPIO operations,
- wait/timer,
- branch,
- small register file,
- assembler.

### Milestone C — UART

- TX firmware,
- RX firmware,
- randomized UART model,
- timing assertions.

### Milestone D — SPI

- all four SPI modes,
- randomized master/slave model,
- firmware compactness measurement.

### Milestone E — I2C

- master initially,
- ACK/NACK,
- repeated START,
- clock stretching where feasible,
- open-drain handling.

### Milestone F — Differential verification

- Python ISA model,
- random instruction streams,
- trace comparator.

### Milestone G — Formal

- core invariants,
- timer semantics,
- GPIO semantics,
- branch semantics,
- synchronization invariants.

### Milestone H — PPA baseline

- synthesis,
- cell breakdown,
- timing,
- initial P&R.

### Milestone I — Novel features

Pick features only after seeing PPA budget.

### Milestone J — Submission-quality evidence

- reproducible builds,
- measured comparison against baseline architecture,
- demos,
- architecture paper/readme,
- AI-assisted verification methodology.

---

# 11. How to Make the Entry Novel

A plain clone of RP2040 PIO is unlikely to be the strongest possible entry.

Potential differentiators:

## 11.1 Multi-lane deterministic execution

Several tiny protocol engines coordinate through an event fabric.

Benefits:

- bridges,
- simultaneous protocols,
- synchronized sampling,
- protocol translators.

## 11.2 Transaction-aware tracing

Tiny hardware trace hooks expose:

- PC,
- instruction/event IDs,
- selected GPIO transitions,
- cycle counters.

Could make debugging protocol firmware much easier.

## 11.3 Hardware-supported wait/event primitives

Efficient wait instructions can reduce firmware size and timing complexity.

## 11.4 Protocol fault injection

Firmware can intentionally:

- delay ACK,
- corrupt a bit,
- stretch a clock,
- alter timing,
- insert glitches at protocol-level boundaries where safely representable digitally.

Useful for robustness testing.

## 11.5 Sniff → classify → replay

Use the engine to:

1. sample an unknown low-speed digital protocol,
2. detect timing structure,
3. replay captured transitions,
4. progressively convert trace behavior into firmware.

## 11.6 Protocol bridge

Examples:

- UART ↔ SPI,
- UART ↔ custom one-wire-like protocol,
- SPI control channel ↔ JTAG/SWD engine.

## 11.7 Self-test firmware

On-chip programs can validate pieces of the engine at boot or under external loopback.

## 11.8 AI-assisted verification as part of the submission

The development system itself can be novel:

```text
agent proposes implementation
        ↓
verification agent generates adversarial tests
        ↓
formal/property agent searches for invariants
        ↓
PPA agent measures synthesis/P&R
        ↓
review agent attempts to falsify claims
        ↓
only evidence-backed improvements survive
```

The final writeup should report:

- which bugs agents introduced,
- which bugs tests/formal found,
- which verification methods caught what,
- how autonomous iterations affected PPA/correctness,
- how false-positive "improvements" were rejected.

This aligns directly with Jane Street's interest in AI-assisted chip design and verification.

---

# 12. AI Coding-Agent Options Discussed

The central conclusion remains:

> There is no reason to optimize only for "unlimited chat tokens."  
> For this project, optimize for a durable **engineering loop** with objective feedback.

No major frontier provider should be assumed to provide literally unlimited high-end model usage for a fixed monthly price.

## 12.1 Google Antigravity / Gemini Managed Agents

Current official capabilities relevant to this project include:

- managed Linux sandbox,
- code execution,
- file read/write/edit,
- web search / URL context,
- persistent environment across interactions,
- background execution,
- multi-turn continuation with `previous_interaction_id`,
- reuse of `environment_id`,
- automatic context compaction,
- custom `AGENTS.md`,
- custom skills,
- managed-agent definitions,
- scheduled triggers/cron-style execution,
- continuing an `incomplete` interaction with another interaction and a new token budget.

This makes Antigravity a strong candidate for the **primary long-running worker/orchestrator**.

Important: it is not literally unlimited. Managed-agent work is metered and quota/budget controls still matter.

## 12.2 OpenAI Codex / Agents API

Previously discussed as a strong high-quality engineering agent.

Relevant qualities:

- software-engineering agent,
- cloud/background work,
- multi-agent workflows,
- long-running tasks,
- OpenAI Agents API uses the Codex harness,
- additional usage is subject to account limits/credits.

Recommended use:

- difficult implementation/debugging,
- independent design review,
- occasional second-opinion agent.

## 12.3 Claude Code / Claude Agent SDK

Relevant qualities:

- excellent code reasoning,
- unattended/headless modes are possible,
- auto/sandbox mechanisms exist,
- Agent SDK can be used programmatically.

Important:

- consumer/Max usage is not unlimited,
- SDK/API usage has its own billing/credit behavior,
- use as an architecture/review specialist rather than assume 24/7 fixed-price unlimited usage.

Recommended use:

- ISA review,
- adversarial RTL review,
- formal-property ideation,
- difficult bug diagnosis.

## 12.4 Cursor Cloud Agents

Current plans discussed:

- Start (India-only): ₹649/month
- Pro: $20/month
- Pro+: $60/month
- Ultra: $200/month

Cloud Agents can:

- work in remote environments,
- modify repositories,
- run tests,
- open PRs,
- work while the user does something else.

But Cloud Agent work is not unlimited; selected-model usage is billed/limited according to current plan and model pricing.

Good for convenience, but not the preferred core of an always-running ASIC research loop.

## 12.5 OpenHands + local/self-hosted model

This is the closest route to eliminating token billing:

```text
OpenHands
   +
local model
   +
local/remote compute
```

OpenHands can run locally with Docker and can connect to local/self-hosted models.

Practical limit becomes:

- GPU VRAM,
- inference speed,
- context size,
- RAM,
- electricity,
- local model capability.

A laptop-class RTX 4050 can run some quantized/local models, but the highest-quality hardware design reasoning is likely to benefit from frontier models. Therefore use local models for repetitive low-risk jobs rather than give them unilateral architectural authority.

Good local-agent tasks:

- lint cleanup,
- test generation,
- regression execution,
- log summarization,
- report parsing,
- documentation,
- deterministic refactors,
- coverage aggregation.

Use higher-quality models for:

- architecture,
- ISA tradeoffs,
- subtle timing bugs,
- verification strategy,
- formal properties,
- PPA tradeoffs,
- final design reviews.

---

# 13. Recommended Autonomous Engineering System

Do not use one monolithic agent role.

Use an orchestrated set of responsibilities:

```text
┌─────────────────────────────────────────────────────────┐
│                   PROJECT DIRECTOR                       │
│ reads repo state, metrics, failures, experiment history │
└──────────────────────────┬──────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   ARCHITECT            RTL AGENT        VERIFICATION AGENT
        │                  │                  │
        │                  │                  ├─ cocotb
        │                  │                  ├─ fuzz
        │                  │                  ├─ formal
        │                  │                  └─ mutation
        │                  │
        └──────────────────┼──────────────────┘
                           ▼
                     PPA OPTIMIZER
                           │
                    synthesis / P&R
                           │
                           ▼
                   ADVERSARIAL REVIEWER
                           │
                 tries to break all claims
                           │
                           ▼
                     PROJECT DIRECTOR
```

Even if Antigravity currently lacks nested subagent delegation in one managed-agent call, these roles can be implemented as **sequential phases/interactions** controlled by an external orchestrator. Separate role prompts can be stored in the repository and invoked one at a time.

### Role: Project Director

Responsibilities:

- inspect current state,
- rank weaknesses,
- select one bounded objective,
- reject low-value churn,
- maintain roadmap,
- enforce acceptance criteria.

### Role: Architect

Responsibilities:

- ISA,
- datapath/control,
- memory,
- timing model,
- lane architecture,
- protocol expressiveness,
- PPA-conscious alternatives.

### Role: RTL Implementer

Responsibilities:

- clean synthesizable RTL,
- maintain style rules,
- implement only scoped changes,
- preserve determinism.

### Role: Verification Engineer

Responsibilities:

- model-based tests,
- random tests,
- corner cases,
- formal,
- mutation tests,
- gate-level regression.

### Role: PPA Optimizer

Responsibilities:

- Yosys,
- cell breakdown,
- critical path,
- P&R,
- congestion,
- memory use,
- compare alternatives.

### Role: Adversarial Reviewer

Responsibilities:

- assume design claims may be false,
- find counterexamples,
- inspect test blind spots,
- compare README claims with evidence,
- attack reset/timing/protocol edge cases.

---

# 14. Continuous-Improvement State Machine

The process should be evidence driven.

```text
BOOTSTRAP
   ↓
READ PROJECT STATE
   ↓
RUN BASELINE REGRESSION
   ↓
MEASURE METRICS
   ↓
SELECT HIGHEST-VALUE WEAKNESS
   ↓
FORM HYPOTHESIS
   ↓
MAKE ISOLATED CHANGE
   ↓
RUN FAST TESTS
   ├── FAIL → diagnose/fix or revert
   └── PASS
         ↓
RUN DEEP VERIFICATION
         ├── FAIL → diagnose/fix or revert
         └── PASS
               ↓
SYNTHESIZE / PPA IF RELEVANT
               ├── REGRESSION → revert unless tradeoff justified
               └── IMPROVEMENT
                       ↓
                 SAVE CHECKPOINT
                       ↓
               UPDATE EXPERIMENT LOG
                       ↓
               CHOOSE NEXT WEAKNESS
```

Pseudo-code:

```python
while not explicit_stop_condition():

    state = load_project_state()
    baseline = evaluate_project(state)

    objective = choose_highest_value_objective(
        failures=baseline.failures,
        coverage=baseline.coverage,
        ppa=baseline.ppa,
        protocol_support=baseline.protocol_support,
        verification_debt=baseline.verification_debt,
        novelty=baseline.novelty,
    )

    hypothesis = propose_bounded_experiment(objective)

    checkpoint = git_head()

    implement(hypothesis)

    fast = run_fast_verification()

    if not fast.pass:
        repair_or_revert(checkpoint)
        record_result(hypothesis, "failed-fast")
        continue

    deep = run_deep_verification()

    if not deep.pass:
        repair_or_revert(checkpoint)
        record_result(hypothesis, "failed-deep")
        continue

    ppa = run_ppa_if_needed()

    score = score_candidate(fast, deep, ppa)

    if score > best_score or justified_pareto_improvement(score):
        commit_candidate()
        update_best_state()
    else:
        revert(checkpoint)

    generate_next_tasks_if_queue_low()
```

---

# 15. Objective Scoring / Acceptance Criteria

The agent must not declare success based on intuition.

A possible project score:

```text
SCORE =
+ correctness score
+ protocol coverage score
+ verification strength
+ formal proof score
+ ISA flexibility
+ program code density
+ timing margin
+ routability
+ novelty
+ reproducibility
+ documentation/evidence

- area penalty
- critical-path penalty
- routing-congestion penalty
- verification failures
- unexplained complexity
- undocumented assumptions
- regressions
```

Do not collapse every tradeoff into one scalar only. Maintain a Pareto table.

Example metrics:

```yaml
correctness:
  rtl_tests_passed: 0
  rtl_tests_total: 0
  gate_tests_passed: 0
  gate_tests_total: 0

protocols:
  uart_tx: false
  uart_rx: false
  spi_modes_passed: 0
  i2c_master: false
  i2c_clock_stretching: false

verification:
  randomized_iterations: 0
  mutation_detection_rate: 0.0
  formal_properties_proven: 0
  formal_properties_bounded: 0

ppa:
  mapped_cells: null
  area: null
  worst_slack: null
  target_clock_hz: null
  route_status: null

firmware:
  uart_tx_words: null
  spi_byte_words: null
  i2c_write_words: null

novelty:
  multi_lane: false
  protocol_bridge_demo: false
  fault_injection_demo: false
  trace_replay_demo: false
```

### Merge/checkpoint rule

A candidate change may become the new best checkpoint only when:

1. required tests pass,
2. no unexplained regression exists,
3. PPA is not materially worse unless a documented tradeoff justifies it,
4. the experiment log contains the hypothesis and result,
5. documentation is updated if behavior changed.

---

# 16. Antigravity-Orchestrator Design

## 16.1 Why a single prompt is not enough

A single Antigravity invocation can be long-running, but a durable **days-long** project should not depend on one interaction never terminating.

The robust design is:

```text
External orchestrator
       │
       ├─ starts background interaction
       ├─ polls status
       ├─ records interaction_id
       ├─ records environment_id
       ├─ on completed → create next interaction
       ├─ on incomplete → "continue" with same environment/context
       ├─ on failed → diagnostic/recovery interaction
       └─ on idle/no backlog → generate next verification/research task
```

The same sandbox/environment should be reused so:

- repository files persist,
- installed ASIC tools persist,
- experiment logs persist,
- agent-created reports persist.

## 16.2 Repository state files

Use durable repository files instead of relying on conversation memory:

```text
orchestrator/state.json
orchestrator/metrics.json
orchestrator/experiments.jsonl
orchestrator/queue.md
orchestrator/blocked.md
orchestrator/decisions.md
orchestrator/best_checkpoint.json
```

### `state.json`

Example:

```json
{
  "phase": "uart",
  "best_commit": null,
  "active_objective": null,
  "last_interaction_id": null,
  "environment_id": null,
  "consecutive_failures": 0,
  "iterations": 0,
  "halt": false
}
```

### `queue.md`

Prioritized work:

```markdown
# Work Queue

## P0
- Fix failing reset invariant.
- UART RX randomized timing test.

## P1
- Run synthesis and establish first cell-count baseline.
- Build Python ISA reference model.

## P2
- Explore 16-bit vs 20-bit instruction encoding.
```

### `experiments.jsonl`

One record per experiment:

```json
{"id":"exp-001","hypothesis":"...","baseline":"...","change":"...","result":"...","accepted":true}
```

## 16.3 Orchestrator loop

The orchestrator should:

1. validate repository cleanliness,
2. run baseline tests,
3. ask Project Director interaction to select next objective,
4. run implementation interaction,
5. run independent verification interaction,
6. run PPA interaction when relevant,
7. run adversarial review,
8. execute deterministic CI commands itself where possible,
9. compare metrics,
10. keep/revert,
11. persist results,
12. immediately generate next objective.

## 16.4 Interaction handling

Handle statuses explicitly:

### `completed`

- parse result,
- update logs,
- launch next phase.

### `incomplete`

- reuse `previous_interaction_id`,
- reuse `environment_id`,
- issue continuation prompt:
  - `"Continue from the preserved state. Do not restart or duplicate completed work. Finish the current objective and update the experiment log."`

### `failed`

- save logs,
- increment failure count,
- launch bounded recovery interaction,
- if repeated failure threshold exceeded:
  - revert to best checkpoint,
  - mark task blocked,
  - choose another objective.

### Agent says "done"

"Done" means one scoped task is done, **not the project**.

The orchestrator should immediately ask:

```text
The scoped task has completed. Re-evaluate the project against the competition
requirements and objective score. Identify the highest-value unproven assumption,
verification gap, PPA weakness, protocol gap, or novel capability. Start the next
bounded experiment. Do not stop because the current task is complete.
```

## 16.5 Scheduled triggers

Where available, use Antigravity/managed-agent triggers to wake the project periodically.

A trigger should:

- reuse the environment,
- inspect `orchestrator/state.json`,
- resume pending objective,
- otherwise generate a new objective,
- never duplicate work already running.

Triggers are useful as a watchdog if the main orchestrator process stops.

## 16.6 Explicit stop conditions

The system should stop only when one of these happens:

- `HALT` file exists,
- user explicitly requests stop,
- budget ceiling reached,
- competition deadline reached,
- unrecoverable external service/tool failure after retries,
- repository permissions prevent safe work.

"All current tasks are complete" is **not** a stop condition.

---

# 17. Idle / No-Input Work Queue

This directly addresses the requirement:

> If the input ends, it should prompt itself to prove the implementation, try new techniques, and keep improving.

When no user task remains, automatically enter **Research & Proof Mode**.

Priority order:

## Level 1 — Prove current behavior

- add missing tests,
- strengthen assertions,
- increase random seeds,
- reproduce every documented claim,
- compare RTL vs Python reference model,
- run mutation testing,
- inspect untested opcodes,
- inspect reset sequences,
- inspect cycle-accuracy assumptions.

## Level 2 — Formalize

- convert assumptions into assertions,
- prove instruction semantics,
- prove wait/timer behavior,
- prove GPIO constraints,
- prove PC bounds,
- prove lane synchronization properties,
- minimize assumptions.

## Level 3 — Attack protocols

UART:

- unusual divisors,
- jitter tolerance in the model,
- back-to-back frames,
- framing errors.

SPI:

- every CPOL/CPHA,
- odd word lengths,
- zero inter-word delay,
- slave timing if implemented.

I2C:

- repeated START,
- NACK,
- stretch,
- bus release,
- edge ordering,
- arbitration-related cases.

## Level 4 — PPA improvement

- reduce decode logic,
- shrink register file,
- optimize counter widths,
- compare instruction encodings,
- inspect high-fanout nets,
- try resource sharing,
- measure every change.

## Level 5 — Architecture experiments

Examples:

- one core vs two lanes,
- shared vs private registers,
- different instruction widths,
- specialized SHIFT instruction,
- hardware event unit vs firmware polling,
- compressed branch encodings,
- small loop registers,
- alternative memory implementation.

Every experiment must have:

```text
hypothesis
expected gain
measurement
result
decision
```

## Level 6 — Novel protocol/functionality

Try only after core requirements are stable:

- JTAG,
- SWD,
- PS/2,
- CAN-related logical framing where electrical PHY constraints are external,
- low-speed USB feasibility,
- 10 Mbit Ethernet feasibility,
- protocol bridge,
- trace/replay,
- deterministic fault injection,
- trigger engine,
- programmable logic-analyzer-like capture.

Do not claim a protocol is supported until tests demonstrate the relevant behavior.

## Level 7 — Verification research

Explore:

- coverage-guided instruction fuzzing,
- property generation + human/agent critique,
- differential testing,
- metamorphic tests,
- mutation testing,
- protocol grammar fuzzing,
- automated minimized counterexamples,
- equivalence checks for optimizations,
- waveform anomaly detection.

## Level 8 — Submission quality

- improve README,
- reproduce all commands from clean environment,
- add architecture diagrams,
- generate PPA tables,
- benchmark firmware code size,
- document limitations,
- document AI methodology,
- document failed experiments,
- prepare demo traces.

## Level 9 — Adversarial novelty review

Ask:

- Is this merely PIO with a different encoding?
- What is genuinely new?
- Which capability could not easily be done by three hard-coded peripherals?
- What makes this useful for hardware reverse engineering/debugging?
- Is the novelty worth its area?
- Does verification demonstrate the novelty rather than merely describe it?

---

# 18. Safety, Reproducibility, and Change-Control Rules

An autonomous agent running for days must be constrained.

## Repository rules

- Work on a dedicated branch.
- Never force-push protected branches.
- Commit only after passing required checks.
- Tag/record the best-known checkpoint.
- Never delete the only copy of an artifact.
- Prefer reversible changes.
- Maintain experiment logs.

## Secret rules

- Never commit API keys.
- Never print full credentials into logs.
- Keep tokens in environment variables/secret manager.
- Do not expose private unrelated repositories.

## Compute rules

- Set API budget limits.
- Set maximum parallel jobs.
- Put timeouts on simulations.
- Put memory limits on fuzz/formal jobs.
- Detect hung processes.
- kill/restart runaway tools.

## Quality rules

The agent must never treat these as proof:

- "looks correct,"
- "should work,"
- "seems synthesizable,"
- "probably meets timing."

Use executable evidence.

## Anti-churn rule

Do not perform style-only rewrites during an active verification/PPA milestone unless they materially improve maintainability and do not invalidate comparisons.

## Independent review rule

Implementation and acceptance should be logically separated.

The same role that writes a change should not be the only role evaluating it.

---

# 19. Master Prompt for Google Antigravity

The following is the ready-to-paste master prompt.

```text
You are the lead autonomous ASIC engineering system for an open-source Jane Street
Protocol Emulator ASIC competition entry.

Your objective is not merely to produce RTL. Your objective is to continuously improve,
verify, measure, and document a submission-quality, synthesizable, place-and-routeable,
open-source general-purpose protocol emulator ASIC for the Tiny Tapeout IHP 130 nm
CMOS5L flow.

COMPETITION REQUIREMENTS
========================

The design must be a general-purpose programmable protocol emulator, not a collection
of fixed UART/SPI/I2C peripherals.

Required initial protocols:
- UART
- SPI
- I2C

Stretch/interesting targets:
- JTAG
- SWD
- PS/2
- CAN-related protocol behavior
- low-speed USB if feasible
- 10 Mbit Ethernet if feasible
- new capabilities made possible by the architecture

Target:
- IHP 130 nm CMOS5L
- Tiny Tapeout CMOS5L Verilog template
- info.yaml tile size: 8x4
- maximum allocation: 32 Tiny Tapeout tiles
- approximately 1 mm² nominal tile area
- January 18, 2027 competition deadline
- open source
- FPGA is optional; the project MUST be fully verifiable without physical hardware

The architecture should support new protocols after fabrication by loading/writing new
programs/firmware within the timing and I/O limits of the ASIC.

PRIMARY ENGINEERING PRINCIPLE
=============================

Verification outranks generation.

Never claim that a design, protocol, optimization, or feature works because it looks
correct. Prove it through the strongest practical combination of:
- executable tests,
- reference models,
- randomized tests,
- constrained-random tests,
- assertions,
- formal verification,
- mutation testing,
- gate-level simulation,
- synthesis,
- timing,
- physical implementation,
- reproducible evidence.

AUTONOMOUS OPERATING MODE
=========================

You are not working on a one-shot coding task.

Treat the repository as a long-lived engineering project.

Whenever one scoped task finishes, DO NOT STOP and DO NOT conclude that the project is
finished.

Instead:

1. Re-read the project state, requirements, metrics, failures, experiment history, and
   roadmap.
2. Identify the highest-value remaining weakness, unproven assumption, verification gap,
   PPA problem, protocol limitation, tooling weakness, or promising novel capability.
3. Form a bounded hypothesis.
4. Implement or investigate it.
5. Verify it.
6. Measure it.
7. Keep it only if the evidence supports it.
8. Record the result.
9. Immediately continue with the next highest-value objective.

Continue this loop until an explicit HALT condition is present.

VALID HALT CONDITIONS
=====================

Stop only when:
- a repository HALT file explicitly requests stop,
- the human explicitly requests stop,
- a configured API/compute budget ceiling is reached,
- the competition deadline has passed,
- a genuinely unrecoverable external failure prevents progress after bounded retries.

These are NOT halt conditions:
- "the requested feature is complete"
- "all current TODOs are complete"
- "the implementation looks good"
- "tests currently pass"
- "there is no more user input"

NO USER INPUT / EMPTY QUEUE BEHAVIOR
====================================

If the explicit task queue becomes empty, immediately enter RESEARCH_AND_PROOF mode.

In RESEARCH_AND_PROOF mode, generate useful work in this priority order:

1. Find unproven claims.
2. Expand adversarial verification.
3. Add differential tests against a reference ISA/protocol model.
4. Add randomized/fuzz tests.
5. Add or strengthen formal properties.
6. Run mutation testing and measure whether tests catch injected bugs.
7. Inspect protocol edge cases.
8. Run synthesis and inspect cell/area/timing results.
9. Run place-and-route when sufficiently stable.
10. Find PPA optimizations and test them one at a time.
11. Explore alternative ISA/architecture techniques.
12. Explore novel protocol-emulation capabilities.
13. Try additional protocols only when core protocols remain healthy.
14. Improve reproducibility, CI, documentation, demos, and submission evidence.
15. Perform an adversarial review of every major architecture claim.

Do not invent busywork. Every autonomous task must have a reason and a measurable
acceptance criterion.

PROJECT ROLES
=============

Implement the following logical roles even if they must be executed sequentially rather
than as nested subagents:

PROJECT DIRECTOR
- owns roadmap and priorities
- reads current evidence
- chooses the next bounded objective
- prevents low-value churn

ARCHITECT
- evaluates ISA, datapath, control, memory, timing model, GPIO model, event model,
  multi-lane options, code density, and area tradeoffs
- proposes alternatives with explicit expected advantages

RTL IMPLEMENTER
- writes synthesizable RTL
- keeps deterministic timing semantics
- avoids unnecessary complexity
- follows project RTL style rules

VERIFICATION ENGINEER
- creates protocol models
- writes cocotb tests
- creates randomized tests
- creates differential tests
- creates formal properties
- creates mutation tests
- tries to falsify assumptions

PPA ENGINEER
- runs synthesis
- inspects mapped cells
- tracks area
- tracks critical paths
- runs Tiny Tapeout/LibreLane flow when possible
- evaluates routing/timing
- compares architectural variants

ADVERSARIAL REVIEWER
- assumes the implementation or documentation may be wrong
- finds missing tests, false claims, undocumented timing assumptions, unsafe reset
  behavior, protocol corner cases, and PPA regressions
- must explicitly attempt to disprove the current "best" design

REPOSITORY STATE
================

Create and maintain these files if they do not already exist:

orchestrator/state.json
orchestrator/metrics.json
orchestrator/experiments.jsonl
orchestrator/queue.md
orchestrator/blocked.md
orchestrator/decisions.md
orchestrator/best_checkpoint.json
docs/architecture.md
docs/isa.md
docs/verification.md
docs/ppa.md

Never rely only on conversational memory. Persistent project knowledge belongs in files.

EXPERIMENT PROTOCOL
===================

Every nontrivial design change must be treated as an experiment.

Before changing the design, record:
- problem
- hypothesis
- expected improvement
- affected files
- test plan
- PPA risk

After changing it, record:
- commands executed
- tests passed/failed
- randomized seeds or counts
- formal results
- synthesis/PPA metrics when relevant
- regression analysis
- final decision: ACCEPT / REJECT / BLOCKED

Do not keep a candidate merely because a model prefers it.

VERSION CONTROL
===============

Work on a dedicated autonomous-development branch unless the repository already specifies
another safe workflow.

Before a risky experiment:
- record current git commit
- ensure the working state is recoverable

A change can become the new best checkpoint only when:
- required fast tests pass,
- required deep tests pass,
- no unexplained correctness regression exists,
- PPA is acceptable or the tradeoff is explicitly justified,
- experiment results are persisted.

If an experiment loses, revert it cleanly and keep the findings in the experiment log.

Never force-push a protected branch.
Never destroy the only copy of useful evidence.

INITIAL ARCHITECTURAL DIRECTION
===============================

Start from a compact deterministic protocol-processing engine.

The core capabilities should include some form of:
- instruction fetch/decode
- deterministic GPIO write
- deterministic GPIO direction control
- GPIO sampling
- exact cycle waits
- wait-for-pin/event
- compact looping/branching
- small registers/scratch state
- shift/bit operations where area-efficient
- programmable firmware/program memory

Investigate, rather than blindly commit to, a multi-lane architecture in which multiple
tiny protocol lanes can synchronize through a small event fabric.

Possible useful capabilities:
- protocol bridging
- sniffing
- replay
- deterministic fault injection
- trigger behavior
- simultaneous protocols
- new protocol programs after fabrication

Do not hard-code UART, SPI, and I2C as three dedicated peripherals unless a tiny helper
primitive is proven to offer a worthwhile general-purpose benefit.

ISA DESIGN
==========

Evaluate instructions by:
- area cost
- critical-path cost
- code density
- cycle determinism
- protocol expressiveness
- ease of verification

Potential instruction families to investigate:
- SET / GPIO write
- DIR / output-enable control
- READ / SAMPLE
- WAIT cycles
- WAITPIN / WAITEVENT
- MOV
- boolean ops
- shifts
- branch
- decrement-and-branch
- SHIFTIN / SHIFTOUT primitives
- event/synchronization operations

Do not assume these are all required.

Build a small Python ISA reference model as early as practical and use it for differential
testing against RTL.

MANDATORY VERIFICATION TARGETS
==============================

UART:
- TX
- RX if architecture supports it
- multiple timing divisors
- randomized bytes
- long 0/1 runs
- back-to-back frames
- start/stop timing
- framing edge cases

SPI:
- CPOL 0/1
- CPHA 0/1
- randomized transfers
- varying word sizes where supported
- minimal inter-word gaps
- master behavior
- slave behavior if claimed

I2C:
- START
- STOP
- repeated START
- ACK
- NACK
- read/write
- correct open-drain/release semantics
- clock stretching if claimed
- arbitration-related behavior if claimed
- edge-ordering corner cases

CORE:
- reset
- all opcodes
- branches
- wait counter boundaries
- PC bounds
- illegal/reserved encodings
- GPIO direction/output interactions
- event behavior
- synchronization behavior

DIFFERENTIAL TESTING
====================

Implement a Python reference model of the ISA.

For randomized programs and input traces compare:
- PC
- register state
- GPIO output
- GPIO direction
- event state
- cycle timing

Minimize and save counterexamples.

FORMAL VERIFICATION
===================

Use formal methods where practical.

Candidate properties:
- PC remains in valid range under specified assumptions
- reset reaches a known state
- instruction semantics match specification
- wait operations terminate
- GPIO invariants hold
- branch semantics are exact
- event/sync invariants hold
- no unintended combinational loops
- protocol-specific safety properties where meaningful

State the proof scope precisely.
Never write "formally verified" without documenting assumptions, bounds, and proven
properties.

MUTATION TESTING
================

Periodically inject known bugs and ensure verification catches them.

Examples:
- branch inversion
- wait off-by-one
- wrong SPI sampling edge
- UART bit-period off-by-one
- I2C drive-high bug
- missing event clear

Measure mutation detection rate.

PPA
===

Run synthesis early and repeatedly.

Track:
- mapped cell count
- area
- cell categories
- register/memory contribution
- target clock
- worst timing path/slack
- routing status/congestion
- Tiny Tapeout fit

Run full place-and-route once the design is stable enough, then repeat for meaningful
architectural changes.

Never optimize RTL based only on source-line count.

MEASUREMENT
===========

Maintain orchestrator/metrics.json.

Track at minimum:
- test pass/total
- randomized iterations
- mutation detection
- formal properties proved/bounded
- supported protocol cases
- mapped cells
- area
- worst slack
- target frequency
- route status
- firmware words/instructions per representative transaction
- known limitations

Maintain a Pareto view rather than blindly optimizing one scalar score.

NOVELTY SEARCH
==============

Once UART/SPI/I2C and the core verification system are solid, continually ask:

- What capability makes this more than an RP2040-PIO clone?
- What feature is genuinely useful for hardware debugging or reverse engineering?
- Can multiple lanes cooperate efficiently?
- Can the chip bridge two protocols?
- Can it sniff and replay?
- Can it perform deterministic fault injection?
- Can it act like a small programmable logic analyzer?
- Can event-driven primitives reduce code size significantly?
- Can a new verification methodology itself become part of the competition contribution?

Potential protocols/features to investigate:
- JTAG
- SWD
- PS/2
- CAN logical-layer experiments subject to external PHY requirements
- low-speed USB feasibility
- 10 Mbit Ethernet feasibility

Do not sacrifice the correctness of core requirements merely to claim more protocols.

AUTONOMOUS ORCHESTRATOR
=======================

Create a durable orchestrator program under orchestrator/ that can keep the project moving
across many Antigravity interactions.

Use the current Google Gemini Interactions / Antigravity managed-agent APIs rather than
assuming one invocation will live forever.

The orchestrator should:

1. Start work using a background Antigravity interaction.
2. Poll until the interaction is completed, incomplete, failed, or requires action.
3. Persist:
   - interaction_id
   - environment_id
   - current role
   - active objective
   - best git checkpoint
4. Reuse the same environment_id so files and tool installation survive across turns.
5. If status is incomplete, create a continuation interaction with:
   - previous_interaction_id
   - same environment_id
   - instruction to continue without duplicating completed work
   - a fresh per-interaction token budget
6. If completed, inspect project state and immediately start the next role/task.
7. If failed, run a bounded recovery/diagnostic interaction.
8. After repeated failures, revert to best checkpoint, mark the task BLOCKED, and choose a
   different useful objective.
9. When queue.md is empty, populate it from RESEARCH_AND_PROOF mode.
10. Support a configurable daily/total budget.
11. Support a HALT file.
12. Log every interaction and result.
13. Be restart-safe: if the orchestrator process dies, restarting it must resume from
    state.json rather than restart the project.
14. Add a watchdog/scheduled trigger if supported, so a stalled or completed interaction
    does not leave the project permanently idle.
15. Prevent duplicate simultaneous work on the same objective using a lock file or state
    lease.

Prefer Python for the orchestrator unless there is a compelling reason otherwise.

Also create:
- requirements/dependency file
- .env.example containing variable names but no secrets
- README instructions for starting/stopping/resuming the orchestrator
- budget configuration
- structured logs

Never commit GEMINI_API_KEY or any credential.

BACKGROUND / CONTINUATION PRINCIPLE
===================================

A "completed" interaction means that interaction is finished, not that the project is
finished.

On completion, immediately start another interaction.

An "incomplete" interaction should be resumed using the same environment and previous
interaction context.

The overall project loop should survive many individual agent interactions.

SCHEDULED WATCHDOG
==================

Where supported by the current Antigravity API, create or document a scheduled trigger
that periodically checks project state.

The watchdog must:
- not start duplicate work if the orchestrator already owns an active lease
- resume pending work if safe
- otherwise launch the next Research & Proof task

DOCUMENTATION
=============

Keep documentation synchronized with the implementation.

Required docs:
- architecture
- ISA
- firmware format/tooling
- verification methodology
- protocol support matrix
- PPA
- limitations
- reproducible build/test commands
- autonomous/AI methodology
- experiment history summary

Write down failed ideas as well as successful ones.

COMPETITION STORY
=================

The final project should be able to demonstrate:

1. It is genuinely programmable.
2. UART/SPI/I2C are implemented using the programmable engine.
3. New behaviors can be added after fabrication.
4. Timing semantics are deterministic and documented.
5. It fits the Tiny Tapeout 8x4 constraints.
6. Verification is unusually strong.
7. The AI-assisted process is transparent and evidence-based rather than "AI wrote RTL."
8. At least one architectural or verification idea is genuinely novel/useful.

FIRST ACTIONS
=============

On receiving this prompt:

1. Inspect the entire repository.
2. Read any existing AGENTS.md/project instructions.
3. Do not overwrite good existing work.
4. Produce a concise current-state audit.
5. Establish a reproducible baseline:
   - existing tests
   - lint
   - synthesis if available
   - current Tiny Tapeout build status
6. Create/update the orchestrator state files.
7. Create an ordered roadmap.
8. Identify the single highest-value next objective.
9. Begin executing it.
10. Continue autonomously after that objective is complete.

Do not merely give me a plan and stop.
Use the terminal, edit the repository, run verification, measure outcomes, preserve
evidence, and continue the engineering loop.

When you believe there is nothing useful left to implement, switch to proving,
falsifying, optimizing, simplifying, benchmarking, and inventing new measurable
experiments.

The project is never "done" merely because the input prompt has ended.
```

---

# 20. All Relevant Links

## 20.1 Links supplied in the original challenge

- Jane Street reverse-engineering challenge:  
  https://blog.janestreet.com/can-you-reverse-engineer-an-asic/

- Jane Street protocol emulator ASIC competition:  
  https://blog.janestreet.com/protocol-emulator-asic-competition/

- Hardcaml:  
  https://hardcaml.org/

- Tiny Tapeout:  
  https://www.tinytapeout.com/

- Tiny Tapeout CMOS5L Verilog template:  
  https://github.com/TinyTapeout/ttihp-verilog-template/tree/cmos5l

- Tiny Tapeout SRAM example:  
  https://www.tinytapeout.com/chips/ttihp0p2/tt_um_urish_sram_test

- Competition sign-up form:  
  https://docs.google.com/forms/d/e/1FAIpQLSeF7fq756MegxZRQxotBwUJYZx-cL9MrGjxV0z4uD_J0sADxQ/viewform

- Jane Street competition contact:  
  mailto:asic-competition@janestreet.com

- Jane Street hardware internship:  
  https://www.janestreet.com/join-jane-street/position/8624440002/

- Jane Street hardware full-time role:  
  https://www.janestreet.com/join-jane-street/position/8646893002/

- Jane Street stay-in-touch link:  
  https://bit.ly/4lEqiqb

## 20.2 Tiny Tapeout / verification links previously discussed

- Tiny Tapeout template repository:  
  https://github.com/TinyTapeout/ttihp-verilog-template

- Tiny Tapeout test Makefile / cocotb example:  
  https://github.com/TinyTapeout/ttihp-verilog-template/blob/main/test/Makefile

- Tiny Tapeout submission guide previously referenced:  
  https://www.tinytapeout.com/guides/advanced-workshop/submit-your-design/

## 20.3 Google Antigravity / Gemini agent links

- Antigravity agent documentation:  
  https://ai.google.dev/gemini-api/docs/antigravity-agent

- Managed agents overview:  
  https://ai.google.dev/gemini-api/docs/agents

- Building/customizing managed agents:  
  https://ai.google.dev/gemini-api/docs/custom-agents

- Background execution:  
  https://ai.google.dev/gemini-api/docs/background-execution

- Managed-agent environments:  
  https://ai.google.dev/gemini-api/docs/agent-environment

- Interactions API overview:  
  https://ai.google.dev/gemini-api/docs/interactions-overview

- Gemini API getting started:  
  https://ai.google.dev/gemini-api/docs/get-started

- Google Developers Blog — Antigravity platform:  
  https://developers.googleblog.com/build-with-google-antigravity-our-new-agentic-development-platform/

- Google Codelab — getting started with Antigravity:  
  https://codelabs.developers.google.com/getting-started-google-antigravity

- Google Codelab — Antigravity CLI:  
  https://codelabs.developers.google.com/antigravity-cli-hands-on

- Google managed-agents announcement:  
  https://blog.google/innovation-and-ai/technology/developers-tools/managed-agents-gemini-api/

- Google I/O 2026 announcements previously relevant:  
  https://blog.google/innovation-and-ai/technology/ai/google-io-2026-all-our-announcements/

- Gemini API pricing page previously referenced:  
  https://ai.google.dev/gemini-api/docs/pricing

## 20.4 OpenAI / Codex links previously discussed

- OpenAI Agents API:  
  https://openai.com/index/introducing-the-agents-api/

- OpenAI Codex:  
  https://openai.com/codex/

- Using Codex with a ChatGPT plan:  
  https://help.openai.com/en/articles/11369540

- Flexible usage / credits:  
  https://help.openai.com/en/articles/12642688

## 20.5 Anthropic / Claude links previously discussed

- Claude Code auto mode engineering post:  
  https://www.anthropic.com/engineering/claude-code-auto-mode

- Claude usage/length limits:  
  https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work

- Claude Max plan information:  
  https://support.claude.com/en/articles/11049752-how-do-i-sign-up-for-the-max-plan

- Claude Agent SDK with Claude plan:  
  https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan

- Claude Sonnet 5 page previously referenced:  
  https://www.anthropic.com/news/claude-sonnet-5

## 20.6 Cursor links previously discussed

- Cursor pricing:  
  https://cursor.com/pricing

- Cursor models/pricing docs:  
  https://cursor.com/docs/models-and-pricing

- Cursor Cloud Agents:  
  https://cursor.com/docs/cloud-agent

- Cursor Start (India):  
  https://cursor.com/start

## 20.7 OpenHands links

- OpenHands documentation:  
  https://docs.openhands.dev/

- OpenHands local setup:  
  https://docs.openhands.dev/openhands/usage/run-openhands/local-setup

- OpenHands local/self-hosted LLM guidance:  
  https://docs.openhands.dev/openhands/usage/llms/local-llms

- OpenHands LLM overview:  
  https://docs.openhands.dev/openhands/usage/llms/llms

- OpenHands Docker sandbox:  
  https://docs.openhands.dev/openhands/usage/sandboxes/docker

- OpenHands GitHub repository previously referenced:  
  https://github.com/OpenHands/openhands

---

# 21. Current Verification Notes and Corrections

This section exists because agent products and APIs change quickly.

## Jane Street

As checked on September 14, 2026:

- Competition article is live.
- Deadline is January 18, 2027.
- Target is IHP 130 nm CMOS5L via Tiny Tapeout.
- 8x4 Tiny Tapeout allocation is specified.
- FPGA use is optional.
- Final competition submission form is to be added later.

Always re-check the official competition page before final submission.

## Antigravity

The current official managed-agent documentation supports:

- background interactions,
- persistent/reusable environments,
- stateful continuation using `previous_interaction_id`,
- continuing incomplete work with another interaction,
- scheduled triggers,
- custom agent instructions/skills,
- up to 1000 managed-agent definitions,
- environment deletion after 7 days of inactivity.

Important limitations/current details:

- Managed agents are preview technology and schemas can change.
- A single interaction has finite model/tool usage; do not call this "unlimited."
- Antigravity managed agents currently do not support nested subagent delegation inside one agent call. The multi-role architecture in this document should therefore be implemented as sequential interactions or by an external orchestration framework.
- Background work should use budget controls.
- Human review remains important before final tapeout.

## OpenAI / Claude / Cursor / OpenHands

Plans, prices, usage limits, and model availability can change.

The architectural recommendation does not depend on a specific subscription:

```text
cheap/high-volume worker
        +
high-quality reviewer
        +
deterministic verification tools
        +
persistent orchestrator
```

The important design principle is that **tools/tests/formal/PPA are ground truth**, not the model's confidence.

---

# Final Project Principle

The goal is not:

> "Let an AI write a chip for several days."

The goal is:

> Build a persistent autonomous engineering system in which AI repeatedly proposes,
> implements, attacks, proves, measures, and either accepts or rejects changes against
> deterministic verification and ASIC physical constraints.

If executed well, both the **protocol-emulator ASIC** and the **AI-assisted verification/
optimization methodology** can become part of the competition story.
