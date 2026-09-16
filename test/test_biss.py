"""test/test_biss.py - Cocotb testbench for Synchronous Serial Interface (SSI / BiSS-C) Engine

Verifies:
1. test_ssi_gray_to_binary_firmware: Reflected Gray code to binary decoding in ASIC ALU registers.
2. test_ssi_position_sampling: Synchronous SSI master clock generation (MA) and serial position sampling (SLO).
3. test_biss_frame_acquisition: BiSS-C master frame acquisition (Ack, Start, Position R0, Flags R1, CRC-6 R2).
4. test_biss_crc6_verification: Validation of BiSS-C CRC-6 polynomial integrity and corruption rejection.
5. test_biss_error_warning_handling: Detection of active-low error (nE) and warning (nW) status conditions.
6. test_biss_ppa_scaling: Physical PPA scaling validation for dedicated SSI/BiSS-C coprocessor macro on IHP 130nm SG13G2.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from bootload import bootload
from biss_model import (
    gray_to_binary,
    binary_to_gray,
    compute_biss_crc6,
    verify_biss_crc6,
    SsiEncoderModel,
    BissEncoderModel,
    BissPpaModel,
    build_ssi_gray_to_binary_asm,
    build_ssi_master_asm,
    build_biss_master_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x18):
    """Reset DUT and load assembled firmware into program RAM via standard bootloader.

    initial_uio: Pin 3 (MA) = 1, Pin 4 (SLO) = 1 (idle high for SSI / BiSS-C).
    """
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
async def test_ssi_gray_to_binary_firmware(dut):
    """Verify ALU-based Gray-to-Binary decoding directly executed on ASIC hardware."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Test distinct Gray code test patterns
    test_binaries = [0x00, 0x01, 0x02, 0x03, 0x15, 0x42, 0x7E, 0xAA, 0x55, 0xFF]

    for expected_bin in test_binaries:
        gray_input = binary_to_gray(expected_bin, 8)
        words = build_ssi_gray_to_binary_asm(gray_input)
        await _init_dut_and_bootload(dut, words, initial_uio=0x18)

        core = dut.user_project.u_core
        await _wait_until_halted(core, dut, max_cycles=1000)

        actual_bin = int(core.r2.value)
        assert actual_bin == expected_bin, (
            f"Gray decode mismatch for Gray 0x{gray_input:02X}: "
            f"expected 0x{expected_bin:02X}, got 0x{actual_bin:02X}"
        )

    dut._log.info(f"SSI Gray-to-Binary PASS: Tested {len(test_binaries)} vectors successfully")


@cocotb.test()
async def test_ssi_position_sampling(dut):
    """Verify SSI master clocking MA (pin 3) and sampling SLO serial stream (pin 4)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_pos = 0xD4
    encoder = SsiEncoderModel(position=test_pos, bits=8, gray_mode=False)
    words = build_ssi_master_asm(clk_pin=3, data_pin=4, num_bits=8)
    await _init_dut_and_bootload(dut, words, initial_uio=0x18)

    core = dut.user_project.u_core

    cycles = 0
    while not bool(core.halted.value) and cycles < 3000:
        await RisingEdge(dut.clk)
        ma_out = (int(dut.uio_out.value) >> 3) & 1
        slo_bit = encoder.step(ma_out)
        # Update uio_in: Pin 3 receives MA feedback, Pin 4 receives SLO
        dut.uio_in.value = (slo_bit << 4) | (ma_out << 3)
        cycles += 1

    assert bool(core.halted.value), "Core did not halt during SSI master read"
    received_pos = int(core.r0.value)
    assert received_pos == test_pos, (
        f"SSI position mismatch: expected 0x{test_pos:02X}, got 0x{received_pos:02X}"
    )
    dut._log.info(f"SSI Master Sampling PASS: Captured position 0x{received_pos:02X} successfully")


@cocotb.test()
async def test_biss_frame_acquisition(dut):
    """Verify BiSS-C frame acquisition: Ack, Start, Position (R0), Flags (R1), and CRC-6 (R2)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    expected_pos = 0x9B
    encoder = BissEncoderModel(position=expected_pos, bits=8, error=False, warning=False, cds=0)
    words = build_biss_master_asm(clk_pin=3, data_pin=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x18)

    core = dut.user_project.u_core

    cycles = 0
    while not bool(core.halted.value) and cycles < 3000:
        await RisingEdge(dut.clk)
        ma_out = (int(dut.uio_out.value) >> 3) & 1
        slo_bit = encoder.step(ma_out)
        dut.uio_in.value = (slo_bit << 4) | (ma_out << 3)
        cycles += 1

    assert bool(core.halted.value), "Core did not halt during BiSS-C acquisition"

    actual_pos = int(core.r0.value)
    actual_flags = int(core.r1.value) & 0x03
    actual_crc = int(core.r2.value) & 0x3F

    assert actual_pos == expected_pos, (
        f"BiSS-C Position mismatch: expected 0x{expected_pos:02X}, got 0x{actual_pos:02X}"
    )
    # nE = 1, nW = 1 -> actual_flags = 0x03
    assert actual_flags == 0x03, (
        f"BiSS-C Status mismatch: expected flags 0x03 (nE=1, nW=1), got 0x{actual_flags:02X}"
    )
    assert actual_crc == encoder.crc6, (
        f"BiSS-C CRC mismatch: expected 0x{encoder.crc6:02X}, got 0x{actual_crc:02X}"
    )

    dut._log.info(
        f"BiSS-C Frame PASS: Pos=0x{actual_pos:02X}, Flags=0x{actual_flags:02X}, CRC=0x{actual_crc:02X}"
    )


@cocotb.test()
async def test_biss_crc6_verification(dut):
    """Verify BiSS-C CRC-6 calculation, polynomial properties, and corruption detection."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    await RisingEdge(dut.clk)

    # 1. Test standard calculation over various data values
    for val in [0x00, 0x55, 0xAA, 0xFF, 0x12, 0x89]:
        bits = [(val >> i) & 1 for i in range(7, -1, -1)] + [1, 1]  # nE=1, nW=1
        crc = compute_biss_crc6(bits)
        assert 0 <= crc <= 0x3F, f"CRC 0x{crc:02X} out of 6-bit range"
        assert verify_biss_crc6(bits, crc), f"CRC verification failed for value 0x{val:02X}"

        # 2. Corrupt one bit and verify detection
        corrupted_bits = list(bits)
        corrupted_bits[3] ^= 1
        assert not verify_biss_crc6(corrupted_bits, crc), (
            f"CRC failed to detect 1-bit corruption on value 0x{val:02X}"
        )

    dut._log.info("BiSS-C CRC-6 Polynomial Integrity & Corruption Rejection PASS")


@cocotb.test()
async def test_biss_error_warning_handling(dut):
    """Verify BiSS-C active-low error (nE=0) and warning (nW=0) condition capture."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    fault_pos = 0x3C
    # error=True -> nE=0, warning=True -> nW=0
    encoder = BissEncoderModel(position=fault_pos, bits=8, error=True, warning=True, cds=0)
    words = build_biss_master_asm(clk_pin=3, data_pin=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x18)

    core = dut.user_project.u_core

    cycles = 0
    while not bool(core.halted.value) and cycles < 3000:
        await RisingEdge(dut.clk)
        ma_out = (int(dut.uio_out.value) >> 3) & 1
        slo_bit = encoder.step(ma_out)
        dut.uio_in.value = (slo_bit << 4) | (ma_out << 3)
        cycles += 1

    assert bool(core.halted.value), "Core did not halt during BiSS-C fault test"
    actual_pos = int(core.r0.value)
    actual_flags = int(core.r1.value) & 0x03
    actual_crc = int(core.r2.value) & 0x3F

    assert actual_pos == fault_pos, f"Position mismatch under fault: 0x{actual_pos:02X}"
    assert actual_flags == 0x00, f"Expected active-low faults (0x00), got 0x{actual_flags:02X}"
    assert actual_crc == encoder.crc6, f"CRC mismatch under fault: 0x{actual_crc:02X}"

    dut._log.info(f"BiSS-C Fault Ingress PASS: Position 0x{actual_pos:02X}, Flags 0x{actual_flags:02X}")


@cocotb.test()
async def test_biss_ppa_scaling(dut):
    """Verify PPA physical scaling metrics for dedicated SSI/BiSS-C macro on IHP 130nm SG13G2."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    await RisingEdge(dut.clk)

    ppa_model = BissPpaModel(position_bits=16)
    metrics = ppa_model.estimate_ppa()

    assert metrics["total_cells"] > 200, f"Unusually low cell count: {metrics['total_cells']}"
    assert metrics["area_overhead_pct"] < 5.0, (
        f"Area overhead excessive: {metrics['area_overhead_pct']}% > 5.0%"
    )
    assert metrics["f_max_mhz"] > 500.0, (
        f"f_max below 500 MHz: {metrics['f_max_mhz']} MHz"
    )

    dut._log.info(
        f"BiSS-C PPA Scaling PASS: Cells={metrics['total_cells']}, Area={metrics['silicon_area_um2']} um2 "
        f"({metrics['area_overhead_pct']}%), f_max={metrics['f_max_mhz']} MHz"
    )
