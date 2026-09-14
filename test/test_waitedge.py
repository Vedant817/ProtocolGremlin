# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for WAITEDGE edge-capture and free-running cycle counter.

Demonstrates the novelty differentiator:
1. Autobaud / pulse-width discovery on unknown protocols:
   Measures external pulse durations to single-cycle precision, which RP2040 PIO
   cannot do without software poll-loop quantization error.
2. Free-running timestamp capture (WAITEDGE mode 3).
3. Cycle-by-cycle differential agreement between RTL and tools/isa_model.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from isa_model import CoreModel  # noqa: E402


@cocotb.test()
async def test_waitedge_pulse_measurement(dut):
    """Test measuring pulse widths of an unknown protocol to single-cycle precision."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Firmware on pin 3 (isolated from bootloader pins 0, 1, 2):
    # 1. Wait for falling edge on pin 3 (mode 0, pin 3 -> operand 0x03)
    # 2. Wait for rising edge on pin 3 (mode 1, pin 3 -> operand 0x0B) into R1
    # 3. HALT
    asm = """
        WAITEDGE R0, 0x03    ; wait for falling edge on pin 3
        WAITEDGE R1, 0x0B    ; wait for rising edge on pin 3, capture elapsed cycles into R1
        HALT
    """
    words = assemble(asm)

    PIN_MASK = 1 << 3
    test_pulse_widths = [5, 11, 23, 47]

    for pulse_width in test_pulse_widths:
        dut._log.info(f"Testing WAITEDGE pulse measurement: width = {pulse_width} cycles")
        await RisingEdge(dut.clk)
        dut.ena.value = 1
        dut.ui_in.value = 0
        dut.uio_in.value = PIN_MASK  # idle high on pin 3
        dut.rst_n.value = 0
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1

        await bootload(dut, words)
        await RisingEdge(dut.clk)
        dut.uio_in.value = PIN_MASK  # ensure pin 3 is idle high

        # Settle in idle high state
        for _ in range(6):
            await RisingEdge(dut.clk)

        # Drive falling edge on pin 3
        dut.uio_in.value = 0

        # Hold pin low for pulse_width clock cycles
        for _ in range(pulse_width):
            await RisingEdge(dut.clk)

        # Drive rising edge on pin 3
        dut.uio_in.value = PIN_MASK

        # Wait for halt
        core = dut.user_project.u_core
        for _ in range(30):
            await RisingEdge(dut.clk)
            await ReadOnly()
            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"Core did not halt for pulse_width={pulse_width}")

        measured_cycles = int(core.r1.value)
        dut._log.info(f"Pulse width {pulse_width} cycles -> measured {measured_cycles} cycles in R1")
        assert measured_cycles == pulse_width, (
            f"Measurement mismatch: injected {pulse_width} cycles, measured {measured_cycles} cycles"
        )


@cocotb.test()
async def test_waitedge_timestamp_capture(dut):
    """Test reading the free-running cycle counter timestamp into a register."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Mode 3 (operand 0x18) immediately writes cycle_cnt[7:0] into rd
    asm = """
        WAITEDGE R0, 0x18    ; t0 = timestamp
        WAIT 10              ; wait 10 cycles (+ 1 cycle for WAIT instruction = 11 cycles)
        WAITEDGE R1, 0x18    ; t1 = timestamp (+ 1 cycle for WAITEDGE = 12 cycles delta)
        HALT
    """
    words = assemble(asm)

    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Core did not halt in timestamp test")

    t0 = int(core.r0.value)
    t1 = int(core.r1.value)
    delta = (t1 - t0) & 0xFF
    # WAIT 10 takes 1 cycle to decode + 10 stall cycles + 1 cycle to execute WAITEDGE = 12 cycles delta
    dut._log.info(f"Timestamp t0={t0}, t1={t1}, delta={delta}")
    assert delta == 12, f"Expected timestamp delta of 12 cycles, got {delta}"


@cocotb.test()
async def test_waitedge_differential(dut):
    """Test cycle-by-cycle differential match with tools/isa_model.py on WAITEDGE."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = """
        WAITEDGE R0, 0x00    ; wait for falling edge pin 0
        WAITEDGE R1, 0x08    ; wait for rising edge pin 0
        HALT
    """
    words = assemble(asm)
    model = CoreModel(words)
    model.reset()

    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 1
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    dut.uio_in.value = 1
    await ReadOnly()

    core = dut.user_project.u_core
    # Sync initial model state
    model.state.pc = int(core.pc.value)
    model.state.regs = [int(core.r0.value), int(core.r1.value), int(core.r2.value), int(core.r3.value)]
    model.state.z = bool(core.z.value)
    model.state.halted = bool(core.halted.value)
    model.state.wait_remaining = int(core.wait_remaining.value)
    model.state.gpio_dir = int(dut.uio_oe.value)
    model.state.gpio_out = int(dut.uio_out.value)
    model.state.cycle_cnt = int(core.cycle_cnt.value)
    model.state.edge_wait_cnt = int(core.edge_wait_cnt.value)
    model.state.gpio_in_prev = int(core.gpio_in_prev.value)
    model.state.gpio_in_sync = int(core.u_gpio.in_sync.value)

    # Dynamic stimulus: pin is 1 for 4 cycles, 0 for 8 cycles, then 1
    stimulus = [1]*4 + [0]*8 + [1]*20

    for cycle, stim_val in enumerate(stimulus):
        await RisingEdge(dut.clk)
        dut.uio_in.value = stim_val
        await ReadOnly()
        model.step(gpio_in_pin=stim_val)

        expected = model.state.snapshot()
        assert int(core.pc.value) == expected["pc"], f"cycle {cycle}: pc mismatch"
        assert int(core.r0.value) == expected["r0"], f"cycle {cycle}: r0 mismatch"
        assert int(core.r1.value) == expected["r1"], f"cycle {cycle}: r1 mismatch"
        assert bool(core.halted.value) == expected["halted"], f"cycle {cycle}: halted mismatch"
        if expected["halted"]:
            break

    dut._log.info("WAITEDGE differential test passed: RTL == Python model!")
