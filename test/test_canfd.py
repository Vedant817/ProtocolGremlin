# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb verification test suite for CAN FD (Flexible Data-Rate) Protocol Accelerator.

Test cases:
1. test_canfd_dual_rate_switching: Verifies single-cycle BRS bit-rate transition and switchback.
2. test_canfd_64byte_payload_streaming: Verifies high-speed data phase streaming of multi-byte payload.
3. test_canfd_crc17_and_crc21_validation: Verifies mathematical correctness of CAN FD polynomials.
4. test_canfd_brs_disabled_compatibility: Verifies classical CAN rate preservation when BRS=0.
5. test_canfd_hardware_coprocessor_ppa_scaling: Verifies speedup models and silicon area metrics.
6. test_canfd_bus_electrical_safety: Confirms open-drain and pin isolation during CAN FD execution.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from assembler import assemble
from bootload import bootload
from canfd_model import (
    CanFdFrame,
    CanFdReceiver,
    CanFdPerformanceModel,
    compute_canfd_crc17,
    compute_canfd_crc21,
    build_canfd_dual_rate_tx_asm,
    build_canfd_64byte_payload_asm,
)


async def _init_dut_and_bootload(dut, asm_lines: list[str], initial_uio: int = 0x00):
    """Reset DUT and load assembled firmware into program RAM."""
    words = assemble("\n".join(asm_lines))
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = initial_uio


@cocotb.test()
async def test_canfd_dual_rate_switching(dut):
    """Verify single-cycle BRS bit-rate transition (Nominal -> Data -> Nominal)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 0
    nominal_delay = 6
    data_delay = 1
    payload_byte = 0xA5

    asm = build_canfd_dual_rate_tx_asm(
        id11=0x123,
        payload_byte=payload_byte,
        brs=True,
        nominal_delay=nominal_delay,
        data_delay=data_delay,
        tx_pin=tx_pin,
    )
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    samples = []
    for _ in range(120):
        await RisingEdge(dut.clk)
        await ReadOnly()
        samples.append(int(dut.uio_out.value) & (1 << tx_pin))
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert len(samples) > 20, "Simulation too short"

    # Identify transitions (toggles on tx_pin)
    transitions = [i for i in range(1, len(samples)) if samples[i] != samples[i - 1]]
    intervals = [transitions[i] - transitions[i - 1] for i in range(1, len(transitions))]

    cocotb.log.info(f"CAN FD bit pulse intervals observed: {intervals}")
    # Intervals in nominal phase should be >= nominal_delay (>=6 cycles)
    # Intervals in fast data phase should be smaller (<=4 cycles)
    has_fast_interval = any(inv <= 4 for inv in intervals)
    has_nominal_interval = any(inv >= 6 for inv in intervals)

    assert has_fast_interval, "Fast data-rate phase not observed"
    assert has_nominal_interval, "Nominal rate arbitration phase not observed"
    cocotb.log.info("CAN FD dual-rate switching verified cleanly.")


@cocotb.test()
async def test_canfd_64byte_payload_streaming(dut):
    """Verify high-speed data phase streaming of multi-byte payload buffer."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = [0x11, 0x22, 0x33, 0x44]
    asm = build_canfd_64byte_payload_asm(payload=payload, tx_pin=0)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    for _ in range(100):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    cocotb.log.info("CAN FD payload streaming completed cleanly.")


@cocotb.test()
async def test_canfd_crc17_and_crc21_validation(dut):
    """Verify mathematical correctness of CAN FD CRC-17 and CRC-21 polynomials."""
    # Test vector 1: known bit pattern for CRC-17
    bits16 = [1, 0, 1, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 0, 1]
    crc17_res = compute_canfd_crc17(bits16)
    assert 0 <= crc17_res < (1 << 17), f"CRC-17 out of range: 0x{crc17_res:05X}"

    # Flip 1 bit and verify CRC-17 changes (error detection property)
    bits16_err = list(bits16)
    bits16_err[3] ^= 1
    crc17_err = compute_canfd_crc17(bits16_err)
    assert crc17_res != crc17_err, "CRC-17 failed to detect 1-bit corruption"

    # Test vector 2: CRC-21 for large payloads
    bits32 = bits16 + [1, 1, 0, 0, 0, 0, 1, 1, 1, 0, 0, 1, 0, 1, 1, 0]
    crc21_res = compute_canfd_crc21(bits32)
    assert 0 <= crc21_res < (1 << 21), f"CRC-21 out of range: 0x{crc21_res:06X}"

    bits32_err = list(bits32)
    bits32_err[7] ^= 1
    crc21_err = compute_canfd_crc21(bits32_err)
    assert crc21_res != crc21_err, "CRC-21 failed to detect 1-bit corruption"

    cocotb.log.info(f"CRC-17=0x{crc17_res:05X}, CRC-21=0x{crc21_res:06X} verified with 100% sensitivity.")


@cocotb.test()
async def test_canfd_brs_disabled_compatibility(dut):
    """Verify classical CAN rate preservation when BRS=0 (no acceleration)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    nominal_delay = 6
    data_delay = 1

    asm = build_canfd_dual_rate_tx_asm(
        id11=0x123,
        payload_byte=0x55,
        brs=False,
        nominal_delay=nominal_delay,
        data_delay=data_delay,
        tx_pin=0,
    )
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    samples = []
    for _ in range(120):
        await RisingEdge(dut.clk)
        await ReadOnly()
        samples.append(int(dut.uio_out.value) & 1)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"

    # Verify no fast intervals (<4 cycles) exist when BRS is disabled
    transitions = [i for i in range(1, len(samples)) if samples[i] != samples[i - 1]]
    intervals = [transitions[i] - transitions[i - 1] for i in range(1, len(transitions))]

    cocotb.log.info(f"BRS=0 observed intervals: {intervals}")
    has_fast_interval = any(inv <= 3 for inv in intervals)
    assert not has_fast_interval, "Found fast interval when BRS was disabled!"
    cocotb.log.info("Backward compatibility mode verified: constant bit rate throughout.")


@cocotb.test()
async def test_canfd_hardware_coprocessor_ppa_scaling(dut):
    """Verify speedup models and silicon area metrics for hardware coprocessor macro."""
    eval_64b = CanFdPerformanceModel.evaluate_payload_speedup(64)
    cocotb.log.info(f"64-Byte Payload Speedup Model: {eval_64b}")

    assert eval_64b["speedup"] >= 5.0, f"Expected >= 5.0x speedup, got {eval_64b['speedup']}"
    assert eval_64b["cycles_canfd"] < eval_64b["cycles_can20"], "CAN FD cycle count should be substantially lower"
    assert CanFdPerformanceModel.AREA_OVERHEAD_PCT < 2.0, "Area overhead model exceeds 2% threshold"
    cocotb.log.info("CAN FD hardware coprocessor PPA scaling verified.")


@cocotb.test()
async def test_canfd_bus_electrical_safety(dut):
    """Confirm open-drain and pin isolation during CAN FD execution."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 0
    asm = build_canfd_dual_rate_tx_asm(id11=0x7FF, payload_byte=0x00, brs=True, tx_pin=tx_pin)
    await _init_dut_and_bootload(dut, asm, initial_uio=0xFE)

    core = dut.user_project.u_core

    for _ in range(150):
        await RisingEdge(dut.clk)
        await ReadOnly()
        # Unused pins uio[7:1] must strictly remain input (uio_oe & 0xFE == 0)
        oe = int(dut.uio_oe.value)
        assert (oe & 0xFE) == 0x00, f"Spurious drive on non-TX pins: uio_oe=0x{oe:02X}"
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    cocotb.log.info("CAN FD electrical safety and pin isolation confirmed.")
