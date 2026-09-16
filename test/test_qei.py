"""test/test_qei.py - Cocotb testbench for Quadrature Encoder Interface (QEI) Engine

Verifies:
1. test_qei_forward_rotation_1x: Forward (CW) rotation detection (A leads B), counter increments +4.
2. test_qei_reverse_rotation_1x: Reverse (CCW) rotation detection (B leads A), counter decrements -4 (0xFC).
3. test_qei_bidirectional_movement: Dynamic change of direction (+3 forward, then -2 reverse, net +1).
4. test_qei_index_homing_capture: Index pulse (Channel Z on uio[2]) detection and zero homing calibration.
5. test_qei_velocity_estimation_period: Period measurement between pulses using WAITEDGE elapsed cycle counter.
6. test_qei_ppa_and_hardware_model: Physical PPA scaling validation on IHP 130nm SG13G2.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from bootload import bootload
from qei_model import (
    QeiEncoder,
    QeiDecoder,
    QeiResolutionMode,
    QeiPpaModel,
    build_qei_1x_edge_asm,
    build_qei_index_homing_asm,
    build_qei_velocity_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00):
    """Reset DUT and load assembled firmware into program RAM via standard bootloader."""
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = initial_uio


async def _wait_until_halted(core, dut, max_cycles: int = 4000):
    """Waits until core halts or timeout occurs."""
    cycles = 0
    while not bool(core.halted.value):
        await RisingEdge(dut.clk)
        cycles += 1
        if cycles > max_cycles:
            break
    assert bool(core.halted.value), f"Core did not halt within {max_cycles} cycles"


@cocotb.test()
async def test_qei_forward_rotation_1x(dut):
    """Verify Forward (CW) rotation detection (A leads B) counting +4 edges."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    words = build_qei_1x_edge_asm(num_edges=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Drive 4 forward cycles: 00 -> 10 (A=1, B=0) -> 11 (A=1, B=1) -> 01 (A=0, B=1) -> 00 (A=0, B=0)
    for _ in range(4):
        # Step 1: A rises, B stays 0 (Forward indication)
        dut.uio_in.value = 0b01  # A=1, B=0
        await ClockCycles(dut.clk, 12)

        # Step 2: B rises
        dut.uio_in.value = 0b11  # A=1, B=1
        await ClockCycles(dut.clk, 12)

        # Step 3: A falls
        dut.uio_in.value = 0b10  # A=0, B=1
        await ClockCycles(dut.clk, 12)

        # Step 4: B falls
        dut.uio_in.value = 0b00  # A=0, B=0
        await ClockCycles(dut.clk, 12)

    await _wait_until_halted(core, dut)

    r3 = int(core.r3.value)
    assert r3 == 4, f"Expected position counter R3=4, got {r3}"
    dut._log.info("QEI Forward Rotation 1X PASS: R3 = 4 (CW)")


@cocotb.test()
async def test_qei_reverse_rotation_1x(dut):
    """Verify Reverse (CCW) rotation detection (B leads A) counting -4 edges (0xFC)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    words = build_qei_1x_edge_asm(num_edges=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Drive 4 reverse cycles: 00 -> 01 (A=0, B=1) -> 11 (A=1, B=1) -> 10 (A=1, B=0) -> 00 (A=0, B=0)
    for _ in range(4):
        # Step 1: B rises first
        dut.uio_in.value = 0b10  # A=0, B=1
        await ClockCycles(dut.clk, 12)

        # Step 2: A rises while B is already 1 (Reverse indication)
        dut.uio_in.value = 0b11  # A=1, B=1
        await ClockCycles(dut.clk, 12)

        # Step 3: B falls
        dut.uio_in.value = 0b01  # A=1, B=0
        await ClockCycles(dut.clk, 12)

        # Step 4: A falls
        dut.uio_in.value = 0b00  # A=0, B=0
        await ClockCycles(dut.clk, 12)

    await _wait_until_halted(core, dut)

    r3 = int(core.r3.value)
    expected_reverse = (256 - 4) & 0xFF  # 0xFC = 252
    assert r3 == expected_reverse, f"Expected position counter R3={expected_reverse} (-4), got {r3}"
    dut._log.info("QEI Reverse Rotation 1X PASS: R3 = 0xFC (-4, CCW)")


@cocotb.test()
async def test_qei_bidirectional_movement(dut):
    """Verify dynamic direction change: +3 forward (CW) followed by -2 reverse (CCW) -> net +1."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    words = build_qei_1x_edge_asm(num_edges=5)  # 3 forward + 2 reverse = 5 total rising edges on A
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Phase 1: 3 Forward edges
    for _ in range(3):
        dut.uio_in.value = 0b01  # A=1, B=0
        await ClockCycles(dut.clk, 12)
        dut.uio_in.value = 0b11  # A=1, B=1
        await ClockCycles(dut.clk, 12)
        dut.uio_in.value = 0b10  # A=0, B=1
        await ClockCycles(dut.clk, 12)
        dut.uio_in.value = 0b00  # A=0, B=0
        await ClockCycles(dut.clk, 12)

    # Phase 2: 2 Reverse edges
    for _ in range(2):
        dut.uio_in.value = 0b10  # A=0, B=1
        await ClockCycles(dut.clk, 12)
        dut.uio_in.value = 0b11  # A=1, B=1
        await ClockCycles(dut.clk, 12)
        dut.uio_in.value = 0b01  # A=1, B=0
        await ClockCycles(dut.clk, 12)
        dut.uio_in.value = 0b00  # A=0, B=0
        await ClockCycles(dut.clk, 12)

    await _wait_until_halted(core, dut)

    r3 = int(core.r3.value)
    assert r3 == 1, f"Expected net position counter R3=1 (+3 - 2), got {r3}"
    dut._log.info("QEI Bidirectional Movement PASS: Net R3 = 1 (+3 CW, -2 CCW)")


@cocotb.test()
async def test_qei_index_homing_capture(dut):
    """Verify Index pulse (Channel Z on uio[2]) detection and zero homing calibration."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    words = build_qei_index_homing_asm()
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Simulate encoder moving without index for 30 cycles
    dut.uio_in.value = 0b01  # Pin 0 active, Pin 2 (Index) low
    await ClockCycles(dut.clk, 15)
    dut.uio_in.value = 0b00
    await ClockCycles(dut.clk, 15)

    assert not bool(core.halted.value), "Core should still be waiting for index pulse"

    # Now fire the Index pulse on Pin 2 (uio[2] = 0x04)
    dut.uio_in.value = 0b100  # Pin 2 high
    await ClockCycles(dut.clk, 15)
    dut.uio_in.value = 0b000  # Index pulse ends

    await _wait_until_halted(core, dut)

    r2 = int(core.r2.value)
    r3 = int(core.r3.value)
    assert r2 == 1, f"Expected Index flag R2=1, got {r2}"
    assert r3 == 0x5A, f"Expected homing calibration status R3=0x5A, got {hex(r3)}"
    dut._log.info("QEI Index Homing Capture PASS: Calibrated with R2=1, R3=0x5A")


@cocotb.test()
async def test_qei_velocity_estimation_period(dut):
    """Verify period measurement between pulses using WAITEDGE elapsed cycle counter."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    words = build_qei_velocity_asm(num_pulses=2)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Pulse 1
    await ClockCycles(dut.clk, 20)
    dut.uio_in.value = 0b01
    await ClockCycles(dut.clk, 10)
    dut.uio_in.value = 0b00

    # Pulse 2 after 35 cycles
    await ClockCycles(dut.clk, 35)
    dut.uio_in.value = 0b01
    await ClockCycles(dut.clk, 10)
    dut.uio_in.value = 0b00

    await _wait_until_halted(core, dut)

    r3 = int(core.r3.value)
    # The elapsed cycles between rising edges should be non-zero and bounded
    assert r3 > 20, f"Expected elapsed pulse period R3 > 20 cycles, got {r3}"
    dut._log.info(f"QEI Velocity Estimation Period PASS: Measured period R3 = {r3} clock cycles")


@cocotb.test()
async def test_qei_ppa_and_hardware_model(dut):
    """Verify PPA scaling and reference software model consistency."""
    ppa_model = QeiPpaModel(filter_stages=4, counter_bits=16)
    metrics = ppa_model.compute_metrics()

    assert metrics["total_cells"] > 250, "PPA model cell count unexpectedly low"
    assert metrics["area_overhead_pct"] < 3.0, f"Area overhead {metrics['area_overhead_pct']}% exceeds 3% budget"
    assert metrics["firmware_area_overhead_pct"] == 0.0, "Firmware overhead must be zero"

    # Test software decoder reference model
    encoder = QeiEncoder(cpr=1024, index_interval=1024)
    decoder = QeiDecoder(mode=QeiResolutionMode.MODE_4X)

    # Rotate forward 8 steps
    for _ in range(8):
        a, b, z = encoder.step(+1)
        delta = decoder.update(a, b, z)
        assert delta == 1

    assert decoder.position == 8, f"Decoder position expected 8, got {decoder.position}"

    # Rotate reverse 3 steps
    for _ in range(3):
        a, b, z = encoder.step(-1)
        delta = decoder.update(a, b, z)
        assert delta == -1

    assert decoder.position == 5, f"Decoder position expected 5, got {decoder.position}"
    dut._log.info("QEI PPA & Reference Model Validation PASS")
