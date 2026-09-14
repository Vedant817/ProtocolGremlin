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
