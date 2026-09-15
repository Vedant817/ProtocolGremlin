# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for Manchester Biphase-L (IEEE 802.3 / MIL-STD-1553) protocol engine.

Verifies:
1. Manchester TX Waveform Fidelity:
   - ASIC transmits IEEE 802.3 Manchester Biphase-L frames.
   - Cycle-exact verification of zero-jitter half-bit durations.
   - Decoded and validated via independent ManchesterDecoder model.
2. Manchester TX Pattern Sweep:
   - Verifies distinct bit patterns (0x00, 0xFF, 0x55, 0xAA, 0x3C, 0xA5).
3. Manchester RX Standard Byte Reception:
   - ASIC synchronizes to transmitter via WAITEDGE mid-bit falling edge.
   - Decodes bytes into R0 using SHIFTIN R0, pin, MSB with zero bit errors.
4. Manchester RX Pattern Sweep:
   - Sweep across pseudo-random and edge-case byte patterns.
5. Manchester Biphase Violation Detection:
   - Validates that the independent reference model detects biphase violations.
6. Manchester Electrical Direction Safety:
   - Verifies uio_oe states: purely input during RX, purely single-pin output during TX.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from manchester_model import (  # noqa: E402
    ManchesterDecoder,
    ManchesterTransmitter,
    build_manchester_tx_asm,
    build_manchester_rx_asm,
)

PIN = 4


async def _init_dut_and_bootload(dut, words: list[int]):
    """Fresh hardware reset and bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = 0


@cocotb.test()
async def test_manchester_tx_waveform(dut):
    """Verify Manchester TX waveform timing, duty cycle, and decoding against ManchesterDecoder."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_byte = 0xA5  # 0b10100101
    half_period = 4
    asm = build_manchester_tx_asm(data_byte=test_byte, half_period=half_period, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    recorded_levels = []

    halted_count = 0
    for cycle in range(300):
        await FallingEdge(dut.clk)
        await ReadOnly()
        uio_out = int(dut.uio_out.value)
        bit_val = (uio_out >> PIN) & 1
        recorded_levels.append(bit_val)

        if bool(core.halted.value):
            halted_count += 1
            if halted_count > 10:
                break
    else:
        raise AssertionError("Manchester TX test timed out waiting for HALT")



    # Find the start bit: line goes from 0 to 1
    start_idx = -1
    for i in range(len(recorded_levels) - 1):
        if recorded_levels[i] == 0 and recorded_levels[i + 1] == 1:
            start_idx = i + 1
            break
    assert start_idx != -1, "Failed to detect Manchester start bit rising edge"

    dut._log.info(f"Detected start bit at cycle {start_idx}")

    # Verify start bit: 4 cycles high, 4 cycles low
    for k in range(half_period):
        assert recorded_levels[start_idx + k] == 1, f"Start bit first half corrupted at +{k}"
        assert recorded_levels[start_idx + half_period + k] == 0, f"Start bit second half corrupted at +{k}"

    # Extract 16 half-bit symbols by sampling at center of each half-bit
    data_start = start_idx + (2 * half_period)
    symbols = []
    for s_idx in range(16):
        sample_offset = data_start + (s_idx * half_period) + (half_period // 2)
        symbols.append(recorded_levels[sample_offset])

        # Verify all cycles in this half-bit are uniform (jitter-free)
        base = data_start + (s_idx * half_period)
        for c in range(half_period):
            assert recorded_levels[base + c] == symbols[-1], (
                f"Half-bit {s_idx} non-uniform level at sub-cycle {c}"
            )

    decoder = ManchesterDecoder(half_period=half_period)
    decoded_byte, valid = decoder.decode_symbols(symbols)

    dut._log.info(f"Decoded byte = 0x{decoded_byte:02X} (valid={valid})")
    assert valid, "Biphase violation detected in TX output!"
    assert decoded_byte == test_byte, f"Decoded 0x{decoded_byte:02X} != expected 0x{test_byte:02X}"


@cocotb.test()
async def test_manchester_tx_patterns(dut):
    """Verify Manchester TX across multiple characteristic bit patterns."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_patterns = [0x00, 0xFF, 0x55, 0xAA, 0x3C]
    half_period = 4

    for test_byte in test_patterns:
        dut._log.info(f"Testing Manchester TX for byte 0x{test_byte:02X}")
        asm = build_manchester_tx_asm(data_byte=test_byte, half_period=half_period, pin=PIN)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        recorded_levels = []

        halted_count = 0
        for cycle in range(300):
            await FallingEdge(dut.clk)
            await ReadOnly()
            uio_out = int(dut.uio_out.value)
            recorded_levels.append((uio_out >> PIN) & 1)

            if bool(core.halted.value):
                halted_count += 1
                if halted_count > 10:
                    break
        else:
            raise AssertionError(f"Manchester TX timed out for byte 0x{test_byte:02X}")

        # Find start bit
        start_idx = -1
        for i in range(len(recorded_levels) - 1):
            if recorded_levels[i] == 0 and recorded_levels[i + 1] == 1:
                start_idx = i + 1
                break
        assert start_idx != -1, f"No start bit found for 0x{test_byte:02X}"

        # Extract symbols
        data_start = start_idx + (2 * half_period)
        symbols = []
        for s_idx in range(16):
            sample_offset = data_start + (s_idx * half_period) + (half_period // 2)
            symbols.append(recorded_levels[sample_offset])

        decoder = ManchesterDecoder(half_period=half_period)
        decoded_byte, valid = decoder.decode_symbols(symbols)

        assert valid, f"Pattern 0x{test_byte:02X} failed biphase validity"
        assert decoded_byte == test_byte, f"Decoded 0x{decoded_byte:02X} != expected 0x{test_byte:02X}"


@cocotb.test()
async def test_manchester_rx_standard(dut):
    """Verify Manchester RX receiver across standard bytes using ManchesterTransmitter."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    half_period = 8
    standard_bytes = [0x55, 0xAA, 0xA5, 0x00, 0xFF]

    for byte_val in standard_bytes:
        dut._log.info(f"Testing Manchester RX for byte 0x{byte_val:02X}")
        asm = build_manchester_rx_asm(half_period=half_period, pin=PIN)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        transmitter = ManchesterTransmitter(dut, pin=PIN, half_period=half_period)
        core = dut.user_project.u_core

        # Start transmission concurrently
        cocotb.start_soon(transmitter.transmit_frame(byte_val, RisingEdge(dut.clk)))

        for cycle in range(500):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"Manchester RX timed out for byte 0x{byte_val:02X}")

        r0 = int(core.r0.value)
        dut._log.info(f"RX completed: R0 = 0x{r0:02X}, expected = 0x{byte_val:02X}")
        assert r0 == byte_val, f"Manchester RX mismatch: received 0x{r0:02X} != expected 0x{byte_val:02X}"


@cocotb.test()
async def test_manchester_rx_sweep(dut):
    """Sweep pseudo-random and edge-case byte patterns through Manchester RX."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    half_period = 8
    sweep_bytes = [0x12, 0x34, 0x7E, 0x81, 0xC3, 0xE7, 0x5A, 0xF0]

    for byte_val in sweep_bytes:
        dut._log.info(f"Testing Manchester RX sweep for byte 0x{byte_val:02X}")
        asm = build_manchester_rx_asm(half_period=half_period, pin=PIN)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        transmitter = ManchesterTransmitter(dut, pin=PIN, half_period=half_period)
        core = dut.user_project.u_core

        cocotb.start_soon(transmitter.transmit_frame(byte_val, RisingEdge(dut.clk)))

        for cycle in range(500):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"Manchester RX sweep timed out for byte 0x{byte_val:02X}")

        r0 = int(core.r0.value)
        assert r0 == byte_val, f"Sweep RX mismatch: received 0x{r0:02X} != expected 0x{byte_val:02X}"


@cocotb.test()
async def test_manchester_violation_detection_model(dut):
    """Verify that the independent ManchesterDecoder flags biphase violations."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Create symbols with intentional violation in bit 3 (first_half == second_half)
    valid_symbols = [1, 0,  0, 1,  1, 0,  1, 1,  0, 1,  1, 0,  0, 1,  1, 0]
    decoder = ManchesterDecoder(half_period=4)
    val, is_valid = decoder.decode_symbols(valid_symbols)

    assert not is_valid, "Decoder failed to flag biphase violation for symbol pair [1, 1]"
    assert decoder.phase_violations == 1, "Phase violation count mismatch"


@cocotb.test()
async def test_manchester_direction_safety(dut):
    """Verify electrical pin direction safety in both RX and TX configurations."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Test RX: pins must be configured as input (uio_oe == 0x00)
    asm_rx = build_manchester_rx_asm(half_period=8, pin=PIN)
    words = assemble(asm_rx)
    await _init_dut_and_bootload(dut, words)

    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.uio_oe.value) == 0x00, "RX pin direction not tri-stated (uio_oe != 0)"

    # 2. Test TX: only pin PIN is configured as output
    asm_tx = build_manchester_tx_asm(data_byte=0x42, half_period=4, pin=PIN)
    words_tx = assemble(asm_tx)
    await _init_dut_and_bootload(dut, words_tx)

    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.uio_oe.value) == (1 << PIN), f"TX uio_oe = 0x{int(dut.uio_oe.value):02X} != 0x{1 << PIN:02X}"
