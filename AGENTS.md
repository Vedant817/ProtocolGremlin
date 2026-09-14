# Jane Street Protocol Emulator ASIC

Before doing ANY substantial work, read:

- CONTEXT.md (start here - one-page orientation for this repo)
- PROJECT_MASTER_PLAN.md
- docs/architecture.md
- docs/isa.md
- docs/verification.md
- orchestrator/state.json
- orchestrator/queue.md
- orchestrator/experiments.jsonl

The repository, tests, synthesis reports and formal results are the source
of truth. Never treat model confidence as evidence.

This is a continuous engineering project. Completing one task does not mean
that the project is complete.

When the explicit task queue becomes empty, enter RESEARCH_AND_PROOF mode:
find verification gaps, attempt to falsify assumptions, improve formal
proofs, improve PPA, test alternative architectures and investigate novel
protocol capabilities.

## Mandatory: keep CONTEXT.md current

After completing ANY feature or task - before considering it done - update
CONTEXT.md's "Current status" and "What to work on next" sections to
reflect what changed. This applies to every agent or contributor working in
this repository, without exception. CONTEXT.md is what lets the next agent
or person get oriented in under a minute instead of re-deriving state from
scratch; a stale CONTEXT.md defeats that purpose. Keep the update short -
it is a summary, not a changelog. Full detail belongs in
orchestrator/decisions.md, orchestrator/experiments.jsonl, and docs/.
