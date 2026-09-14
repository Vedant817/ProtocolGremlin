# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Differential test: run firmware/loop_demo.asm on the RTL (via cocotb) and
on the pure-Python ISA v0 reference model (tools/isa_model.py), and assert
they agree on every architectural signal after every clock cycle.

See docs/verification.md for the overall verification strategy and
docs/isa.md for the instruction set this exercises.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from isa_model import CoreModel  # noqa: E402

from bootload import bootload  # noqa: E402

ASM_PATH = os.path.join(os.path.dirname(__file__), "..", "firmware", "loop_demo.asm")

# Fixed external stimulus on the bidirectional bus, applied AFTER the
# bootloader finishes (during loading, uio_in carries the load protocol
# instead - see test/bootload.py). This test does not yet exercise
# time-varying external input - see orchestrator/queue.md.
EXTERNAL_UIO_IN = 0xA5

# Generous cycle budget; loop_demo.asm halts well within this.
MAX_CYCLES = 200


def _assemble_program() -> list[int]:
    with open(ASM_PATH, encoding="utf-8") as fh:
        source = fh.read()
    return assemble(source)


@cocotb.test()
async def test_isa_v1_differential(dut):
    dut._log.info("Assembling firmware/loop_demo.asm")
    words = _assemble_program()

    model = CoreModel(words)
    model.reset()

    clock = Clock(dut.clk, 100, unit="ns")  # 10 MHz nominal
    cocotb.start_soon(clock.start())

    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Reprogram the chip over its own GPIO bus - no $readmemh involved (see
    # docs/isa.md "Bootloader protocol" and orchestrator/decisions.md).
    await bootload(dut, words)
    dut.uio_in.value = EXTERNAL_UIO_IN
    await ReadOnly()

    core = dut.user_project.u_core

    # bootload() cannot return at the *exact* cycle the loader hands off to
    # normal execution (the handoff happens partway through driving the
    # final load bit), so a few real instructions may already have retired
    # by the time it returns. Rather than guess that count, snapshot the
    # RTL's actual post-load architectural state and use it as the model's
    # starting point - the cycle-by-cycle comparison loop below is then
    # exact and insensitive to bootload()'s internal timing.
    model.state.pc = int(core.pc.value)
    model.state.regs = [int(core.r0.value), int(core.r1.value), int(core.r2.value), int(core.r3.value)]
    model.state.z = bool(core.z.value)
    model.state.halted = bool(core.halted.value)
    model.state.wait_remaining = int(core.wait_remaining.value)
    model.state.gpio_dir = int(dut.uio_oe.value)
    model.state.gpio_out = int(dut.uio_out.value)

    cycle = 0
    for cycle in range(MAX_CYCLES):
        await RisingEdge(dut.clk)
        # Synchronize to the ReadOnly phase so this same edge's nonblocking
        # (NBA) register updates have definitely been applied before we
        # sample internal signals - reading immediately after RisingEdge can
        # otherwise race with the DUT's own always-block updates.
        await ReadOnly()
        model.step(gpio_in_pin=EXTERNAL_UIO_IN)
        expected = model.state.snapshot()

        assert int(core.pc.value) == expected["pc"], f"cycle {cycle}: pc mismatch"
        assert int(core.r0.value) == expected["r0"], f"cycle {cycle}: r0 mismatch"
        assert int(core.r1.value) == expected["r1"], f"cycle {cycle}: r1 mismatch"
        assert int(core.r2.value) == expected["r2"], f"cycle {cycle}: r2 mismatch"
        assert int(core.r3.value) == expected["r3"], f"cycle {cycle}: r3 mismatch"
        assert bool(core.z.value) == expected["z"], f"cycle {cycle}: z flag mismatch"
        assert bool(core.halted.value) == expected["halted"], f"cycle {cycle}: halted mismatch"
        assert int(dut.uio_oe.value) == expected["gpio_dir"], f"cycle {cycle}: uio_oe mismatch"
        assert int(dut.uio_out.value) == expected["gpio_out"], f"cycle {cycle}: uio_out mismatch"

        if expected["halted"]:
            break
    else:
        raise AssertionError(f"program did not halt within {MAX_CYCLES} cycles")

    dut._log.info("Differential test passed: RTL == Python ISA model for %d cycles", cycle + 1)
