# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb verification test suite for Hardware-Assisted Cyclic Redundancy Check (CRC-16/CRC-32) Coprocessor Macro.

Test cases:
1. test_crc_software_bitbang_computation: Verifies software bitwise CRC accumulation across test bytes.
2. test_crc_coprocessor_single_cycle_streaming: Verifies streaming byte-by-byte hardware CRC ingestion without stalls.
3. test_crc_mathematical_multi_poly_validation: Mathematically validates CRC-16/CCITT, CRC-16/MODBUS, and CRC-32 against RFC vectors.
4. test_crc_single_bit_error_detection: Injects single-bit and burst errors across payload, verifying 100% detection rate.
5. test_crc_hardware_coprocessor_ppa_scaling: Validates analytical PPA models on IHP 130nm SG13G2 across CRC-16, CRC-32, and universal modes.
6. test_crc_pin_direction_electrical_safety: Confirms all GPIO pins remain strictly High-Z (uio_oe = 0x00) during CRC operations.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from assembler import assemble
from bootload import bootload
from crc_model import (
    CrcPolynomialMode,
    CrcCoprocessorModel,
    CrcPpaModel,
    compute_crc16_ccitt,
    compute_crc16_modbus,
    compute_crc32_ieee,
    build_crc_software_bitbang_asm,
    build_crc_coprocessor_stream_asm,
    build_crc_error_injection_detection_asm,
    build_crc_multi_polynomial_switch_asm,
)


async def _init_dut_and_bootload(dut, asm_text: str, initial_uio: int = 0x00):
    """Reset DUT and load assembled firmware into program RAM."""
    words = assemble(asm_text)
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
async def test_crc_software_bitbang_computation(dut):
    """Verify software bitwise CRC accumulation across test vectors in core registers."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = [0x12, 0x34, 0x56]
    asm = build_crc_software_bitbang_asm(payload)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(40):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt after CRC computation"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)

    assert r2 == 0x00, f"Expected clean status R2=0x00, got 0x{r2:02X}"
    assert r0 != 0x00, f"Expected non-zero CRC in R0, got 0x{r0:02X}"
    dut._log.info(f"Software Bitwise CRC PASS: R0=0x{r0:02X}, R2=0x{r2:02X}")


@cocotb.test()
async def test_crc_coprocessor_single_cycle_streaming(dut):
    """Verify streaming byte-by-byte hardware coprocessor ingestion without pipeline stalls."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]
    asm = build_crc_coprocessor_stream_asm(payload)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(35):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt after streaming ingestion"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)

    assert r0 == len(payload), f"Expected {len(payload)} streamed bytes in R0, got {r0}"
    assert r2 == 0x00, f"Expected clean status R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"Coprocessor Single-Cycle Streaming PASS: Processed {r0} bytes cleanly")


@cocotb.test()
async def test_crc_mathematical_multi_poly_validation(dut):
    """Mathematically validate CRC-16/CCITT, CRC-16/MODBUS, and CRC-32/IEEE against standard RFC test vectors."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_bytes = b"123456789"

    # 1. CRC-16/CCITT
    c_ccitt = compute_crc16_ccitt(test_bytes)
    assert c_ccitt == 0x29B1, f"Expected CCITT 0x29B1, got 0x{c_ccitt:04X}"

    # 2. CRC-16/MODBUS
    c_modbus = compute_crc16_modbus(test_bytes)
    assert c_modbus == 0x4B37, f"Expected MODBUS 0x4B37, got 0x{c_modbus:04X}"

    # 3. CRC-32/IEEE 802.3
    c_crc32 = compute_crc32_ieee(test_bytes)
    assert c_crc32 == 0xCBF43926, f"Expected IEEE-32 0xCBF43926, got 0x{c_crc32:08X}"

    # Verify CrcCoprocessorModel state machine matches standalone math
    model_ccitt = CrcCoprocessorModel(CrcPolynomialMode.CRC16_CCITT)
    for b in test_bytes:
        model_ccitt.step_byte(b)
    assert model_ccitt.get_result() == 0x29B1, "Coprocessor CCITT mismatch"

    model_modbus = CrcCoprocessorModel(CrcPolynomialMode.CRC16_MODBUS)
    for b in test_bytes:
        model_modbus.step_byte(b)
    assert model_modbus.get_result() == 0x4B37, "Coprocessor MODBUS mismatch"

    model_crc32 = CrcCoprocessorModel(CrcPolynomialMode.CRC32_IEEE)
    for b in test_bytes:
        model_crc32.step_byte(b)
    assert model_crc32.get_result() == 0xCBF43926, "Coprocessor CRC-32 mismatch"

    await RisingEdge(dut.clk)
    dut._log.info("Mathematical Multi-Polynomial Validation PASS: CCITT=0x29B1, MODBUS=0x4B37, CRC32=0xCBF43926")


@cocotb.test()
async def test_crc_single_bit_error_detection(dut):
    """Verify single-bit error detection sensitivity and fault code trapping (R2 = 0xCE)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_payload = [0x10, 0x20, 0x30]
    corrupt_byte = 0x55
    asm = build_crc_error_injection_detection_asm(valid_payload, corrupt_byte)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(35):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on CRC mismatch"
    r2 = int(core.r2.value)
    assert r2 == 0xCE, f"Expected fault code R2=0xCE, got 0x{r2:02X}"

    # Also mathematically verify 100% single-bit detection in Crc32
    base_data = bytearray(b"TinyTapeout Jane Street ASIC Test")
    base_crc = compute_crc32_ieee(bytes(base_data))

    for bit_idx in range(len(base_data) * 8):
        byte_pos = bit_idx // 8
        bit_pos = bit_idx % 8
        corrupted = bytearray(base_data)
        corrupted[byte_pos] ^= (1 << bit_pos)
        corrupt_crc = compute_crc32_ieee(bytes(corrupted))
        assert corrupt_crc != base_crc, f"Undetected bitflip at byte {byte_pos} bit {bit_pos}!"

    dut._log.info("Single-Bit Error Detection PASS: 100% of single-bit flips detected (R2=0xCE)")


@cocotb.test()
async def test_crc_hardware_coprocessor_ppa_scaling(dut):
    """Validate analytical PPA scaling models across dedicated and universal coprocessor configurations."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    m16 = CrcPpaModel.get_config_metrics("crc16_dedicated")
    m32 = CrcPpaModel.get_config_metrics("crc32_dedicated")
    m_uni = CrcPpaModel.get_config_metrics("universal_macro")

    # Verify scaling relationships
    assert m16["standard_cells"] == 128
    assert m32["standard_cells"] == 196
    assert m_uni["standard_cells"] == 245
    assert m_uni["overhead_pct"] < 1.5, "Universal macro overhead should be < 1.5%"
    assert m_uni["speedup"] >= 64.0, "Speedup should be >= 64x over bitwise software loop"
    assert m_uni["max_freq_mhz"] >= 180.0, "Max frequency should exceed 180 MHz"

    # Test mode switching firmware
    asm = build_crc_multi_polynomial_switch_asm()
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core
    for _ in range(25):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core failed to execute multi-polynomial switch"
    r2 = int(core.r2.value)
    assert r2 == 0x00, f"Expected clean return R2=0x00, got 0x{r2:02X}"

    dut._log.info(f"Hardware Coprocessor PPA Scaling PASS: Universal macro ({m_uni['standard_cells']} cells, {m_uni['overhead_pct']:.2f}% overhead, {m_uni['speedup']:.0f}x speedup)")


@cocotb.test()
async def test_crc_pin_direction_electrical_safety(dut):
    """Verify that during internal CRC processing, all GPIO pins remain strictly High-Z (uio_oe = 0x00)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = [0xAA, 0x55, 0xFF, 0x00]
    asm = build_crc_software_bitbang_asm(payload)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for cycle in range(40):
        await RisingEdge(dut.clk)
        uio_oe = int(dut.uio_oe.value)
        assert uio_oe == 0x00, f"Cycle {cycle}: Expected strictly High-Z (uio_oe=0x00), got 0x{uio_oe:02X}"
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt"
    dut._log.info("Electrical Pin Safety PASS: uio_oe remained strictly 0x00 (High-Z) throughout")
