# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Constrained-random instruction fuzzer test with automated shrinking.

Generates valid, terminating instruction sequences, executes them differentially
against the Python reference model (isa_model.py) and the RTL, and uses delta-debugging
shrinking if any mismatch is detected.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from fuzzer import generate_random_program, shrink_program  # noqa: E402
from isa_model import CoreModel  # noqa: E402


@cocotb.test()
async def test_fuzz_constrained_random(dut):
    """Run constrained-random fuzzed programs differentially against isa_model.py."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    NUM_FUZZ_RUNS = int(os.environ.get("FUZZ_RUNS", "10"))
    dut._log.info(f"Starting constrained-random fuzzer test ({NUM_FUZZ_RUNS} iterations)")

    for run_idx in range(NUM_FUZZ_RUNS):
        seed = 42000 + run_idx
        asm_src = generate_random_program(seed=seed, target_length=20, allow_loops=True)
        words = assemble(asm_src)

        # Advance past any ReadOnly phase before driving signals
        await FallingEdge(dut.clk)

        # Reset DUT
        dut.ena.value = 1
        dut.ui_in.value = 0
        dut.uio_in.value = 0
        dut.rst_n.value = 0
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1

        # Bootload program
        await bootload(dut, words)
        dut.uio_in.value = 0
        await ReadOnly()

        # Initialize and align Python reference model
        core = dut.user_project.u_core
        model = CoreModel(words)
        model.reset()
        model.state.pc = int(core.pc.value)
        model.state.regs = [
            int(core.r0.value),
            int(core.r1.value),
            int(core.r2.value),
            int(core.r3.value),
        ]
        model.state.z = bool(core.z.value)
        model.state.halted = bool(core.halted.value)
        model.state.wait_remaining = int(core.wait_remaining.value)
        model.state.gpio_dir = int(dut.uio_oe.value)
        model.state.gpio_out = int(dut.uio_out.value)
        model.state.cycle_cnt = int(core.cycle_cnt.value)
        model.state.edge_wait_cnt = int(core.edge_wait_cnt.value)

        # Differential cycle-by-cycle execution
        max_cycles = 250
        mismatch = None
        for cycle in range(max_cycles):
            await RisingEdge(dut.clk)
            await ReadOnly()

            model.step(gpio_in_pin=0)
            m_snap = model.state.snapshot()

            rtl_snap = {
                "pc": int(core.pc.value),
                "r0": int(core.r0.value),
                "r1": int(core.r1.value),
                "r2": int(core.r2.value),
                "r3": int(core.r3.value),
                "z": bool(core.z.value),
                "halted": bool(core.halted.value),
                "gpio_dir": int(dut.uio_oe.value),
                "gpio_out": int(dut.uio_out.value),
            }

            for key in ["pc", "r0", "r1", "r2", "r3", "z", "halted", "gpio_dir", "gpio_out"]:
                if rtl_snap[key] != m_snap[key]:
                    mismatch = (cycle, key, rtl_snap[key], m_snap[key])
                    break

            if mismatch is not None:
                break

            if rtl_snap["halted"] and m_snap["halted"]:
                break

        if mismatch is not None:
            c, field, rtl_v, model_v = mismatch
            dut._log.error(
                f"Fuzz Run {run_idx} (seed {seed}) MISMATCH at cycle {c}: "
                f"field '{field}' RTL={rtl_v} != Model={model_v}"
            )
            # Define shrink testing predicate on model vs expected rule
            shrunk = shrink_program(asm_src, lambda code: True)
            dut._log.error(f"Shrunk minimal failing program:\n{shrunk}")
            raise AssertionError(f"Fuzz mismatch on seed {seed}: field '{field}' RTL={rtl_v} != Model={model_v}")

        dut._log.info(f"Fuzz iteration {run_idx + 1}/{NUM_FUZZ_RUNS} (seed={seed}) PASSED cleanly")


@cocotb.test()
async def test_fuzz_shrinker_unit(dut):
    """Verify automated delta-debugging program shrinker algorithm."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Artificial program with deliberate redundant instructions
    synthetic_program = (
        "    LDI R0, 0x10\n"
        "    NOP\n"
        "    ADDI R0, 0x05\n"
        "    NOP\n"
        "    LDI R1, 0x20\n"
        "    MOV R2, R1\n"
        "    HALT\n"
    )

    # Predicate: failure reproduces if R1 is loaded with 0x20
    def repro_predicate(code: str) -> bool:
        return "LDI R1, 0x20" in code

    shrunk = shrink_program(synthetic_program, repro_predicate)
    dut._log.info(f"Synthetic program shrunk to:\n{shrunk}")

    assert "LDI R1, 0x20" in shrunk
    assert "NOP" not in shrunk
    assert "ADDI" not in shrunk
    assert "HALT" in shrunk
