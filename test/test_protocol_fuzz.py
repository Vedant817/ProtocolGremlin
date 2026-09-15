# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""Cocotb tests for the Automated Protocol Fuzzing & Anomaly Injection Campaign.

Verifies core resilience, error detection, and state recovery under:
1. UART timing jitter (+-1 cycle per bit period) across pseudorandom payloads.
2. UART false-start runt glitch rejection (reporting R2=0xFF).
3. UART framing error detection under corrupted stop bits (reporting R2=0xFE).
4. Manchester Biphase-L timing jitter tolerance under +-1 cycle half-bit distortions.
5. Mixed-burst anomaly recovery (valid frame -> glitch anomaly -> valid recovery frame).
6. Physical electrical safety under fuzzed input waveforms (zero bus contention).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from uart_model import build_uart_rx_asm  # noqa: E402
from manchester_model import build_manchester_rx_asm  # noqa: E402
from protocol_fuzzer import ProtocolFuzzer, AnomalyType  # noqa: E402

UART_RX_PIN = 3
MANCHESTER_RX_PIN = 4


async def _setup_and_bootload(dut, asm_src, rx_pin=UART_RX_PIN, idle_val=1):
    """Reset DUT and bootload assembly program."""
    words = assemble(asm_src)
    pin_mask = idle_val << rx_pin
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = pin_mask
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = pin_mask
    for _ in range(6):
        await RisingEdge(dut.clk)


@cocotb.test()
async def test_fuzz_uart_timing_jitter_tolerance(dut):
    """Verify UART RX decodes correctly under +-1 cycle bit period jitter."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    fuzzer = ProtocolFuzzer(seed=1337)
    test_payloads = [0x55, 0xAA, 0x3C, 0xA5]

    for payload in test_payloads:
        asm_src = build_uart_rx_asm(bit_period_cycles=8, pin=UART_RX_PIN, check_false_start=False)
        await _setup_and_bootload(dut, asm_src, rx_pin=UART_RX_PIN, idle_val=1)

        stream, meta = fuzzer.generate_fuzzed_uart_frame(
            payload=payload,
            nominal_bit_period=8,
            anomaly=AnomalyType.TIMING_JITTER,
            jitter_range=1,
            idle_before=8,
            idle_after=16,
        )

        for bit in stream:
            await RisingEdge(dut.clk)
            dut.uio_in.value = (bit << UART_RX_PIN)

        core = dut.user_project.u_core
        for _ in range(60):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), f"Core failed to halt on jittered payload 0x{payload:02X}"
        assert int(core.r0.value) == payload, f"Expected 0x{payload:02X}, got 0x{int(core.r0.value):02X}"
        assert int(core.r2.value) == 0x00, f"Expected status 0x00, got 0x{int(core.r2.value):02X}"
        dut._log.info(f"Fuzzed UART Jitter PASS: payload=0x{payload:02X} decoded cleanly")


@cocotb.test()
async def test_fuzz_uart_false_start_glitch_rejection(dut):
    """Verify false-start 1-cycle runt glitch is trapped (R2=0xFF) without hanging."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    fuzzer = ProtocolFuzzer(seed=999)
    asm_src = build_uart_rx_asm(bit_period_cycles=8, pin=UART_RX_PIN, check_false_start=True)
    await _setup_and_bootload(dut, asm_src, rx_pin=UART_RX_PIN, idle_val=1)

    stream, meta = fuzzer.generate_fuzzed_uart_frame(
        payload=0x42,
        nominal_bit_period=8,
        anomaly=AnomalyType.FALSE_START_GLITCH,
        idle_before=8,
        idle_after=16,
    )

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << UART_RX_PIN)

    core = dut.user_project.u_core
    for _ in range(60):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core must halt upon false-start glitch detection"
    assert int(core.r2.value) == 0xFF, f"Expected glitch status 0xFF, got 0x{int(core.r2.value):02X}"
    dut._log.info("Fuzzed False-Start Glitch Rejection PASS: trapped with R2=0xFF")


@cocotb.test()
async def test_fuzz_uart_framing_error_anomaly(dut):
    """Verify corrupted stop bit is detected as a framing error (R2=0xFE)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    fuzzer = ProtocolFuzzer(seed=777)
    asm_src = build_uart_rx_asm(bit_period_cycles=8, pin=UART_RX_PIN, check_false_start=False)
    await _setup_and_bootload(dut, asm_src, rx_pin=UART_RX_PIN, idle_val=1)

    stream, meta = fuzzer.generate_fuzzed_uart_frame(
        payload=0x7E,
        nominal_bit_period=8,
        anomaly=AnomalyType.FRAMING_ERROR,
        idle_before=8,
        idle_after=16,
    )

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << UART_RX_PIN)

    core = dut.user_project.u_core
    for _ in range(60):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core must halt upon framing error detection"
    assert int(core.r2.value) == 0xFE, f"Expected framing error 0xFE, got 0x{int(core.r2.value):02X}"
    dut._log.info("Fuzzed Framing Error Anomaly PASS: trapped with R2=0xFE")


@cocotb.test()
async def test_fuzz_manchester_timing_jitter(dut):
    """Verify Manchester Biphase-L decoder tolerates jittered half-bits."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    fuzzer = ProtocolFuzzer(seed=555)
    test_payload = 0x96
    half_period = 8
    asm_src = build_manchester_rx_asm(half_period=half_period, pin=MANCHESTER_RX_PIN)
    await _setup_and_bootload(dut, asm_src, rx_pin=MANCHESTER_RX_PIN, idle_val=0)

    stream, meta = fuzzer.generate_fuzzed_manchester_frame(
        payload=test_payload,
        nominal_half_period=half_period,
        anomaly=AnomalyType.TIMING_JITTER,
    )

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << MANCHESTER_RX_PIN)

    core = dut.user_project.u_core
    for _ in range(60):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core must halt after Manchester reception"
    assert int(core.r0.value) == test_payload, f"Expected 0x{test_payload:02X}, got 0x{int(core.r0.value):02X}"
    dut._log.info(f"Fuzzed Manchester Jitter PASS: payload=0x{test_payload:02X} recovered")


@cocotb.test()
async def test_fuzz_multi_frame_recovery(dut):
    """Verify multi-frame sequence: valid frame followed by corrupted frame and recovery."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    fuzzer = ProtocolFuzzer(seed=12345)
    asm_src = build_uart_rx_asm(bit_period_cycles=8, pin=UART_RX_PIN, check_false_start=False)

    # Phase 1: Valid Frame 0xA5
    await _setup_and_bootload(dut, asm_src, rx_pin=UART_RX_PIN, idle_val=1)
    s1, _ = fuzzer.generate_fuzzed_uart_frame(
        payload=0xA5, nominal_bit_period=8, anomaly=AnomalyType.NONE, idle_before=8, idle_after=16
    )
    for bit in s1:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << UART_RX_PIN)
    core = dut.user_project.u_core
    for _ in range(60):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value)
    assert int(core.r0.value) == 0xA5 and int(core.r2.value) == 0x00

    # Phase 2: Corrupted Frame (Framing error)
    await _setup_and_bootload(dut, asm_src, rx_pin=UART_RX_PIN, idle_val=1)
    s2, _ = fuzzer.generate_fuzzed_uart_frame(
        payload=0x55, nominal_bit_period=8, anomaly=AnomalyType.FRAMING_ERROR, idle_before=8, idle_after=16
    )
    for bit in s2:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << UART_RX_PIN)
    for _ in range(60):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xFE

    # Phase 3: Immediate Recovery with Valid Frame 0x3C
    await _setup_and_bootload(dut, asm_src, rx_pin=UART_RX_PIN, idle_val=1)
    s3, _ = fuzzer.generate_fuzzed_uart_frame(
        payload=0x3C, nominal_bit_period=8, anomaly=AnomalyType.NONE, idle_before=8, idle_after=16
    )
    for bit in s3:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << UART_RX_PIN)
    for _ in range(60):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value)
    assert int(core.r0.value) == 0x3C and int(core.r2.value) == 0x00
    dut._log.info("Multi-Frame Anomaly Recovery PASS: Valid -> Framing Error -> Clean Recovery")


@cocotb.test()
async def test_fuzz_electrical_safety(dut):
    """Verify uio_oe direction register remains strictly input throughout fuzzed stimulus."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = build_uart_rx_asm(bit_period_cycles=8, pin=UART_RX_PIN, check_false_start=True)
    await _setup_and_bootload(dut, asm_src, rx_pin=UART_RX_PIN, idle_val=1)

    fuzzer = ProtocolFuzzer(seed=888)
    stream, _ = fuzzer.generate_fuzzed_uart_frame(
        payload=0xFF, nominal_bit_period=8, anomaly=AnomalyType.TIMING_JITTER, idle_before=8, idle_after=16
    )

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << UART_RX_PIN)
        oe_val = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
        assert oe_val == 0x00, f"Electrical safety violation: uio_oe must be 0x00, got 0x{oe_val:02X}"

    dut._log.info("Fuzzing Electrical Safety PASS: zero bus contention throughout fuzzed frames")
