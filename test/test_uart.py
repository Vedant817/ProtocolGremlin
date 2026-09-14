# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for real UART TX firmware verified against independent UartReceiver.

Exercises parameterized UART transmission across:
- Edge cases: 0x00, 0xFF, 0x55, 0xAA
- Fixed-seed pseudorandom bytes: 0x23, 0x7E, 0x89, 0xC4
- Multiple bit periods: 4, 8, 16 cycles/bit
Each test runs in a fresh reset + serial bootload cycle.
"""
import os
import sys
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from uart_model import build_uart_tx_asm, UartReceiver  # noqa: E402


async def _run_uart_tx_test(dut, byte_val: int, bit_period: int):
    """Run one UART transmission test with fresh reset and bootload."""
    asm_source = build_uart_tx_asm(byte_val, bit_period_cycles=bit_period, pin=0)
    words = assemble(asm_source)

    # Fresh reset - ensure we are not in ReadOnly phase
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Load program serially via bootloader
    await bootload(dut, words)
    await ReadOnly()

    receiver = UartReceiver(bit_period_cycles=bit_period)
    core = dut.user_project.u_core

    # Run until halted or timeout
    max_cycles = 50 + 15 * bit_period
    decoded = []

    for cycle in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        # Sample uio_out pin 0
        pin_val = (int(dut.uio_out.value) >> 0) & 1
        res = receiver.step(pin_val)
        if res is not None:
            decoded.append(res)

        # Halt check
        if bool(core.halted.value) and receiver.state == UartReceiver.STATE_IDLE:
            break
    else:
        raise AssertionError(
            f"UART TX (0x{byte_val:02X}, P={bit_period}) did not complete within {max_cycles} cycles "
            f"(decoded: {[hex(x) for x in decoded]}, rx_state: {receiver.state})"
        )

    assert decoded == [byte_val], (
        f"UART TX failed for byte 0x{byte_val:02X} (P={bit_period}): "
        f"expected {[hex(byte_val)]}, got {[hex(x) for x in decoded]}"
    )


@cocotb.test()
async def test_uart_tx_edge_cases(dut):
    """Test UART TX with edge cases (0x00, 0xFF, 0x55, 0xAA) across periods 4, 8, 16."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    edge_bytes = [0x00, 0xFF, 0x55, 0xAA]
    periods = [4, 8, 16]

    for p in periods:
        for b in edge_bytes:
            dut._log.info(f"Testing UART TX: byte=0x{b:02X}, period={p} cycles")
            await _run_uart_tx_test(dut, b, p)


@cocotb.test()
async def test_uart_tx_pseudorandom(dut):
    """Test UART TX with fixed-seed pseudorandom bytes at period 8."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rng = random.Random(0x1337)
    test_bytes = [rng.randint(0, 255) for _ in range(6)]

    for b in test_bytes:
        dut._log.info(f"Testing UART TX pseudorandom: byte=0x{b:02X}, period=8 cycles")
        await _run_uart_tx_test(dut, b, 8)
