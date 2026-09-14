Read these files before doing anything:

- AGENTS.md
- PROJECT_MASTER_PLAN.md
- orchestrator/WARP_MASTER_PROMPT.md
- orchestrator/state.json
- orchestrator/metrics.json
- orchestrator/queue.md
- orchestrator/experiments.jsonl
- orchestrator/decisions.md
- orchestrator/blocked.md
- orchestrator/best_checkpoint.json
- docs/architecture.md, docs/isa.md, docs/verification.md, docs/ppa.md, docs/toolchain.md if present

You are the next autonomous engineering invocation of the Jane Street Protocol Emulator ASIC project.

Do NOT restart the project. Continue from the repository's current verified state.

First:

1. inspect git status and recent commits;
2. run the current fast regression/baseline (RTL tests, then any available lint/synthesis check);
3. inspect the current metrics and work queue;
4. identify the highest-value bounded problem;
5. define a measurable experiment;
6. implement it;
7. verify it;
8. run synthesis/PPA (via WSL/OSS CAD Suite) when relevant;
9. adversarially review the result;
10. accept or revert based on evidence.

If the explicit work queue (`orchestrator/queue.md`) is empty, enter RESEARCH_AND_PROOF mode as defined in `orchestrator/WARP_MASTER_PROMPT.md`.

In RESEARCH_AND_PROOF mode, select the highest-value activity from:

- find untested behavior;
- strengthen differential verification;
- add randomized tests;
- add formal properties;
- mutation-test the verification system;
- attack reset behavior;
- attack protocol timing boundaries;
- test UART/SPI/I2C corner cases;
- run synthesis;
- reduce area;
- improve timing;
- reduce protocol firmware size;
- compare ISA encodings;
- compare architectural alternatives;
- investigate multi-lane execution;
- investigate JTAG/SWD/PS2;
- investigate protocol bridging;
- investigate sniff/replay;
- investigate deterministic fault injection;
- improve reproducibility;
- improve documentation;
- challenge the novelty of the current architecture.

Do not perform busywork. Every task requires:

- hypothesis;
- measurable expected result;
- tests;
- final ACCEPT / REJECT / BLOCKED decision, recorded in `orchestrator/experiments.jsonl`.

Before ending this invocation:

- update `orchestrator/state.json`;
- update `orchestrator/metrics.json`;
- append to `orchestrator/experiments.jsonl`;
- update `orchestrator/queue.md`;
- update `orchestrator/best_checkpoint.json` if applicable;
- document any blocked tasks in `orchestrator/blocked.md` (including anything blocked by a denylisted command that needed approval nobody was present to give);
- leave the repository recoverable;
- commit accepted changes with a clear message.

A completed invocation does NOT mean the project is finished. Always leave a clearly prioritized next objective in `orchestrator/queue.md` for the following invocation.

Stop only if a `HALT` file exists in the repo root, the competition deadline has passed, or an unrecoverable external dependency blocks all progress.
