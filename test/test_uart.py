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
from uart_model import build_uart_tx_asm, build_uart_rx_asm, UartReceiver, UartTransmitter  # noqa: E402


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


async def _run_uart_rx_test(
    dut,
    byte_val: int,
    bit_period: int,
    pin: int = 3,
    stop_bit: int = 1,
    check_false_start: bool = False,
    inject_glitch: bool = False,
):
    """Run one UART reception test with fresh reset and bootload."""
    asm_source = build_uart_rx_asm(bit_period, pin=pin, check_false_start=check_false_start)
    words = assemble(asm_source)

    # Fresh reset
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    pin_mask = 1 << pin
    dut.uio_in.value = pin_mask  # idle high on RX pin
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Load program serially via bootloader
    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = pin_mask  # ensure pin is idle high

    core = dut.user_project.u_core

    # Let the core settle into LD_DONE and enter WAITEDGE stall
    for _ in range(6):
        await RisingEdge(dut.clk)

    if inject_glitch:
        # 1-cycle glitch low, then return high
        await RisingEdge(dut.clk)
        dut.uio_in.value = 0
        await RisingEdge(dut.clk)
        dut.uio_in.value = pin_mask
        for _ in range(30):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break
        assert bool(core.halted.value), "Core did not halt on false-start glitch"
        assert int(core.r2.value) == 0xFF, f"Expected 0xFF glitch status, got 0x{int(core.r2.value):02X}"
        return

    tx = UartTransmitter(bit_period, pin=pin)
    stream = tx.generate_bit_stream(byte_val, stop_bit=stop_bit, idle_before=4, idle_after=10)

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << pin)

    # Wait for core to halt
    max_wait = 30 + 5 * bit_period
    for _ in range(max_wait):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError(
            f"UART RX (0x{byte_val:02X}, P={bit_period}) did not halt within timeout "
            f"(R0=0x{int(core.r0.value):02X}, R2=0x{int(core.r2.value):02X})"
        )

    if stop_bit == 0:
        assert int(core.r2.value) == 0xFE, f"Expected framing error 0xFE, got 0x{int(core.r2.value):02X}"
    else:
        assert int(core.r2.value) == 0x00, f"Expected status 0x00, got 0x{int(core.r2.value):02X}"
        assert int(core.r0.value) == byte_val, f"Expected R0=0x{byte_val:02X}, got 0x{int(core.r0.value):02X}"


@cocotb.test()
async def test_uart_rx_edge_cases(dut):
    """Test UART RX with edge cases (0x00, 0xFF, 0x55, 0xAA) across periods 8 and 16."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    edge_bytes = [0x00, 0xFF, 0x55, 0xAA]
    periods = [8, 16]

    for p in periods:
        for b in edge_bytes:
            dut._log.info(f"Testing UART RX edge cases: byte=0x{b:02X}, period={p} cycles")
            await _run_uart_rx_test(dut, b, p, pin=3)


@cocotb.test()
async def test_uart_rx_pseudorandom(dut):
    """Test UART RX with fixed-seed pseudorandom bytes at period 8."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rng = random.Random(0x1337)
    test_bytes = [rng.randint(0, 255) for _ in range(6)]

    for b in test_bytes:
        dut._log.info(f"Testing UART RX pseudorandom: byte=0x{b:02X}, period=8 cycles")
        await _run_uart_rx_test(dut, b, 8, pin=3)


@cocotb.test()
async def test_uart_rx_framing_error(dut):
    """Test UART RX detecting framing error (stop bit held low) and reporting 0xFE."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing UART RX framing error detection")
    await _run_uart_rx_test(dut, 0x42, 8, pin=3, stop_bit=0)


@cocotb.test()
async def test_uart_rx_glitch_rejection(dut):
    """Test UART RX rejecting noise glitch on start bit and reporting 0xFF."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing UART RX false-start glitch rejection")
    await _run_uart_rx_test(dut, 0x00, 8, pin=3, check_false_start=True, inject_glitch=True)

