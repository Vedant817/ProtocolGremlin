# Jane Street Protocol Emulator ASIC

An open-source, general-purpose, programmable protocol-emulator ASIC,
built for the [Jane Street protocol emulator ASIC
competition](https://blog.janestreet.com/protocol-emulator-asic-competition/),
targeting Tiny Tapeout / IHP 130nm CMOS5L (deadline: January 18, 2027).

The goal is a small deterministic CPU whose instruction set is designed for
reading/writing pins, counting cycles, and hitting timing precisely enough
to implement real protocols (UART, SPI, I2C, and beyond) in firmware after
fabrication - not a fixed UART+SPI+I2C peripheral block. See
`PROJECT_MASTER_PLAN.md` for the full brief, competition rules, and
long-term roadmap.

## Status

**v0 bootstrap.** A minimal core (ISA v0: 19 opcodes, 4 registers, a
programmable GPIO bus mapped onto Tiny Tapeout's `uio[7:0]` pins) is
implemented and differentially tested against an independent Python
reference model. UART/SPI/I2C firmware, formal verification, and
synthesis/PPA data do not exist yet. See `docs/limitations.md` for the
full, current, honest list, and `orchestrator/queue.md` for what's next.

## Repository layout

```text
src/            Synthesizable RTL (Tiny Tapeout wrapper + core + ALU + GPIO + program ROM)
firmware/       Assembly programs for the core
tools/          assembler.py (asm -> hex), isa_model.py (Python reference model)
test/           cocotb testbench (Tiny Tapeout template + our differential test)
formal/         Formal verification harness (not yet populated)
scripts/        setup_env.sh (toolchain), regress.sh (run tests)
docs/           architecture, ISA, verification, toolchain, PPA, limitations
orchestrator/   Durable project state for the continuous engineering loop
```

## Quick start

```bash
# One-time toolchain setup (no sudo required, see docs/toolchain.md)
bash scripts/setup_env.sh

# Run the regression suite
bash scripts/regress.sh
```

## Documentation

- `docs/architecture.md` - system architecture
- `docs/isa.md` - instruction set reference (ISA v0)
- `docs/verification.md` - what's tested and how
- `docs/toolchain.md` - how to reproduce the dev environment
- `docs/ppa.md` - power/performance/area (no data yet)
- `docs/limitations.md` - what's known to not work / not exist yet

## Continuous engineering process

This project is developed as a continuous, evidence-driven engineering
loop rather than a single implementation pass - see `AGENTS.md`,
`orchestrator/WARP_MASTER_PROMPT.md`, and `orchestrator/WARP_CYCLE_PROMPT.md`.
`orchestrator/state.json` and `orchestrator/queue.md` track current status
and the prioritized backlog.

## License

Apache-2.0 (matching the Tiny Tapeout template), see `LICENSE`.
