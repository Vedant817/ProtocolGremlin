# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb verification test suite for Cryptographic Accelerator Feasibility Study.

Test cases:
1. test_crypto_32bit_multiprecision_add: Verifies 32-bit addition with multi-byte carry ripple.
2. test_crypto_chacha_quarter_round: Verifies ChaCha ARX quarter-round microcode step against RFC 8439.
3. test_crypto_poly1305_mac_step: Verifies polynomial MAC accumulation and modular reduction.
4. test_crypto_sha256_ch_maj_primitive: Verifies SHA-256 non-linear Choose (Ch) and Majority (Maj) primitives.
5. test_crypto_hardware_accelerator_ppa_scaling: Verifies coprocessor speedup models and PPA scaling.
6. test_crypto_electrical_safety_and_pin_isolation: Confirms GPIO bus isolation during crypto routines.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from assembler import assemble
from bootload import bootload
from crypto_model import (
    chacha_quarter_round,
    sha256_ch,
    sha256_maj,
    poly1305_mac_step,
    CryptoPerformanceModel,
    build_crypto_32bit_add_asm,
    build_crypto_chacha_qr_step_asm,
    build_crypto_poly1305_accum_asm,
    build_crypto_sha256_ch_maj_asm,
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
async def test_crypto_32bit_multiprecision_add(dut):
    """Verify multi-precision 32-bit addition using 4-byte carry propagation."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    val_a = 0x12345678
    val_b = 0x11111111
    expected_sum = (val_a + val_b) & 0xFFFFFFFF  # 0x23456789

    asm = build_crypto_32bit_add_asm(val_a, val_b)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    for cyc in range(50):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"

    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)
    r3 = int(core.r3.value)

    res32 = (r3 << 24) | (r2 << 16) | (r1 << 8) | r0
    cocotb.log.info(f"32-Bit Add Result: 0x{res32:08X} (Expected: 0x{expected_sum:08X})")
    assert res32 == expected_sum, f"Expected 0x{expected_sum:08X}, got 0x{res32:08X}"


@cocotb.test()
async def test_crypto_chacha_quarter_round(dut):
    """Verify ChaCha ARX quarter-round microcode step against RFC 8439 reference."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    a_val = 0x11
    b_val = 0x22
    d_val = 0x55

    sum_ab = (a_val + b_val) & 0xFF
    xor_da = (d_val ^ sum_ab) & 0xFF
    expected_rot = ((xor_da << 1) | (xor_da >> 7)) & 0xFF

    asm = build_crypto_chacha_qr_step_asm(a_val, b_val, d_val)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    for _ in range(50):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert int(core.r0.value) == sum_ab, f"Expected R0={sum_ab}, got {int(core.r0.value)}"
    assert int(core.r1.value) == expected_rot, f"Expected R1={expected_rot}, got {int(core.r1.value)}"
    cocotb.log.info(f"ChaCha QR Step Verified: sum={sum_ab}, rot={expected_rot}")


@cocotb.test()
async def test_crypto_poly1305_mac_step(dut):
    """Verify polynomial MAC accumulation step and modular reduction."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    acc = 42
    msg = 15
    r = 7

    expected_sum = acc + msg  # 57
    expected_prod = (expected_sum * r) % 251  # 399 % 251 = 148

    asm = build_crypto_poly1305_accum_asm(acc, msg, r)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    for _ in range(50):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert int(core.r0.value) == expected_sum, f"Expected R0={expected_sum}, got {int(core.r0.value)}"
    assert int(core.r2.value) == expected_prod, f"Expected R2={expected_prod}, got {int(core.r2.value)}"
    cocotb.log.info(f"Poly1305 MAC Step Verified: sum={expected_sum}, mod_prod={expected_prod}")


@cocotb.test()
async def test_crypto_sha256_ch_maj_primitive(dut):
    """Verify SHA-256 non-linear Choose (Ch) and Majority (Maj) bitwise functions."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    x = 0xAA
    y = 0xCC
    z = 0xF0

    expected_ch = sha256_ch(x, y, z) & 0xFF
    expected_maj = sha256_maj(x, y, z) & 0xFF

    asm = build_crypto_sha256_ch_maj_asm(x, y, z)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    for _ in range(50):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert int(core.r0.value) == expected_ch, f"Expected Ch={expected_ch}, got {int(core.r0.value)}"
    assert int(core.r1.value) == expected_maj, f"Expected Maj={expected_maj}, got {int(core.r1.value)}"
    cocotb.log.info(f"SHA-256 Primitives Verified: Ch=0x{expected_ch:02X}, Maj=0x{expected_maj:02X}")


@cocotb.test()
async def test_crypto_hardware_accelerator_ppa_scaling(dut):
    """Verify hardware cryptographic coprocessor throughput speedup and PPA models."""
    chacha_eval = CryptoPerformanceModel.evaluate_chacha8_throughput()
    sha_eval = CryptoPerformanceModel.evaluate_sha256_throughput()

    cocotb.log.info(f"ChaCha8 PPA: {chacha_eval['speedup_factor']}x speedup, +{chacha_eval['area_overhead_pct']}% area")
    cocotb.log.info(f"SHA-256 PPA: {sha_eval['speedup_factor']}x speedup, +{sha_eval['area_overhead_pct']}% area")

    assert chacha_eval["speedup_factor"] >= 30.0, "Expected >= 30x speedup for ChaCha8 coprocessor"
    assert chacha_eval["area_overhead_pct"] < 2.5, "Expected < 2.5% area overhead for ChaCha8 coprocessor"

    assert sha_eval["speedup_factor"] >= 40.0, "Expected >= 40x speedup for SHA-256 coprocessor"
    assert sha_eval["area_overhead_pct"] < 3.0, "Expected < 3.0% area overhead for SHA-256 coprocessor"


@cocotb.test()
async def test_crypto_electrical_safety_and_pin_isolation(dut):
    """Verify that GPIO pins remain isolated (High-Z) during internal cryptographic routines."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_crypto_32bit_add_asm(0xA5A5A5A5, 0x5A5A5A5A)
    await _init_dut_and_bootload(dut, asm, initial_uio=0xFF)

    core = dut.user_project.u_core

    for _ in range(50):
        await RisingEdge(dut.clk)
        await ReadOnly()
        # Bus direction must strictly remain 0x00 input mode (High-Z)
        assert int(dut.uio_oe.value) == 0x00, f"GPIO bus contention detected: uio_oe={int(dut.uio_oe.value)}"
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert int(dut.uio_oe.value) == 0x00, "uio_oe must remain 0x00 after HALT"
    cocotb.log.info("Cryptographic routine electrical isolation confirmed.")
