# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for the Pure Firmware Autobaud Rate Auto-Discovery Engine.

Validates the core's ability to measure unknown external pulse durations
to single-cycle accuracy via WAITEDGE, detect pulse asymmetry / noise spikes,
classify baud rates into calibrated profiles, and decode subsequent data
payloads with zero bit jitter.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from autobaud_model import AutobaudTransmitterModel, build_autobaud_rx_asm  # noqa: E402
from bootload import bootload  # noqa: E402


AUTOBAUD_PIN = 3
PIN_MASK = 1 << AUTOBAUD_PIN


async def init_dut_and_bootload(dut, words: list[int]):
    """Reset DUT and perform clean bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = PIN_MASK
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    # Call bootload immediately after releasing reset (zero intermediate clk edges)
    await bootload(dut, words)

    # Allow core to initialize and enter WAITEDGE
    for _ in range(16):
        await FallingEdge(dut.clk)
        dut.uio_in.value = PIN_MASK
        await RisingEdge(dut.clk)


async def drive_levels(dut, levels: list[int]):
    """Drive cycle-by-cycle bit levels onto AUTOBAUD_PIN."""
    for lvl in levels:
        await FallingEdge(dut.clk)
        dut.uio_in.value = PIN_MASK if lvl else 0
        await RisingEdge(dut.clk)


@cocotb.test()
async def test_autobaud_rate_8_discovery(dut):
    """Verify discovery of 8 cycles/bit baud rate and payload recovery (0xA5)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_autobaud_rx_asm(pin=AUTOBAUD_PIN)
    await init_dut_and_bootload(dut, words)

    tx_model = AutobaudTransmitterModel(pin=AUTOBAUD_PIN)
    levels = tx_model.generate_sync_and_data(bit_period=8, data_byte=0xA5)

    dut._log.info("Driving Autobaud training (T=8) + data 0xA5...")
    cocotb.start_soon(drive_levels(dut, levels))

    core = dut.user_project.u_core
    for cycle in range(1000):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Timed out waiting for core to halt")

    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    dut._log.info(f"Autobaud Rate 8 Result: R0 (Payload) = 0x{r0:02X}, R1 (Profile) = 0x{r1:02X}, R2 (Status) = 0x{r2:02X}")
    assert r2 == 0x00, f"Expected Status SUCCESS (0x00), got 0x{r2:02X}"
    assert r1 == 0x01, f"Expected Profile ID 1 (Rate 8), got 0x{r1:02X}"
    assert r0 == 0xA5, f"Expected Payload 0xA5, got 0x{r0:02X}"


@cocotb.test()
async def test_autobaud_rate_16_discovery(dut):
    """Verify discovery of 16 cycles/bit baud rate and payload recovery (0x3C)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_autobaud_rx_asm(pin=AUTOBAUD_PIN)
    await init_dut_and_bootload(dut, words)

    tx_model = AutobaudTransmitterModel(pin=AUTOBAUD_PIN)
    levels = tx_model.generate_sync_and_data(bit_period=16, data_byte=0x3C)

    dut._log.info("Driving Autobaud training (T=16) + data 0x3C...")
    cocotb.start_soon(drive_levels(dut, levels))

    core = dut.user_project.u_core
    for cycle in range(1200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Timed out waiting for core to halt")

    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    dut._log.info(f"Autobaud Rate 16 Result: R0 = 0x{r0:02X}, R1 = 0x{r1:02X}, R2 = 0x{r2:02X}")
    assert r2 == 0x00, f"Expected Status SUCCESS (0x00), got 0x{r2:02X}"
    assert r1 == 0x02, f"Expected Profile ID 2 (Rate 16), got 0x{r1:02X}"
    assert r0 == 0x3C, f"Expected Payload 0x3C, got 0x{r0:02X}"


@cocotb.test()
async def test_autobaud_rate_32_discovery(dut):
    """Verify discovery of 32 cycles/bit baud rate and payload recovery (0x7E)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_autobaud_rx_asm(pin=AUTOBAUD_PIN)
    await init_dut_and_bootload(dut, words)

    tx_model = AutobaudTransmitterModel(pin=AUTOBAUD_PIN)
    levels = tx_model.generate_sync_and_data(bit_period=32, data_byte=0x7E)

    dut._log.info("Driving Autobaud training (T=32) + data 0x7E...")
    cocotb.start_soon(drive_levels(dut, levels))

    core = dut.user_project.u_core
    for cycle in range(2000):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Timed out waiting for core to halt")

    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    dut._log.info(f"Autobaud Rate 32 Result: R0 = 0x{r0:02X}, R1 = 0x{r1:02X}, R2 = 0x{r2:02X}")
    assert r2 == 0x00, f"Expected Status SUCCESS (0x00), got 0x{r2:02X}"
    assert r1 == 0x03, f"Expected Profile ID 3 (Rate 32), got 0x{r1:02X}"
    assert r0 == 0x7E, f"Expected Payload 0x7E, got 0x{r0:02X}"


@cocotb.test()
async def test_autobaud_payload_sweep(dut):
    """Sweep arbitrary data bytes across all three supported baud rates."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_autobaud_rx_asm(pin=AUTOBAUD_PIN)
    tx_model = AutobaudTransmitterModel(pin=AUTOBAUD_PIN)
    test_cases = [
        (8, 0xFF, 0x01),
        (8, 0x00, 0x01),
        (16, 0x55, 0x02),
        (16, 0xAA, 0x02),
        (32, 0x12, 0x03),
        (32, 0x89, 0x03),
    ]

    core = dut.user_project.u_core
    for bit_period, expected_payload, expected_profile in test_cases:
        await init_dut_and_bootload(dut, words)

        levels = tx_model.generate_sync_and_data(bit_period=bit_period, data_byte=expected_payload)
        task = cocotb.start_soon(drive_levels(dut, levels))

        for _ in range(bit_period * 40):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"Timeout waiting for halt at period {bit_period}")

        task.cancel()
        dut.uio_in.value = PIN_MASK
        await RisingEdge(dut.clk)

        assert int(core.r2.value) == 0x00
        assert int(core.r1.value) == expected_profile
        assert int(core.r0.value) == expected_payload
        dut._log.info(f"PASS: Period {bit_period} cycles -> Payload 0x{expected_payload:02X} matched!")


@cocotb.test()
async def test_autobaud_noise_symmetry_rejection(dut):
    """Verify noise glitch rejection when sync bit durations are asymmetric."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_autobaud_rx_asm(pin=AUTOBAUD_PIN)
    await init_dut_and_bootload(dut, words)

    tx_model = AutobaudTransmitterModel(pin=AUTOBAUD_PIN)
    # T0 will be 8 + 4 = 12 cycles, but T1 will be 8 cycles -> Asymmetric!
    levels = tx_model.generate_sync_and_data(bit_period=8, data_byte=0x42, corrupt_symmetry=True)

    dut._log.info("Driving asymmetric noise glitch training frame...")
    cocotb.start_soon(drive_levels(dut, levels))

    core = dut.user_project.u_core
    for _ in range(500):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Core should halt on error")

    r2 = int(core.r2.value)
    dut._log.info(f"Noise rejection status: R2 = 0x{r2:02X}")
    assert r2 == 0xEE, f"Expected Noise Error (0xEE), got 0x{r2:02X}"


@cocotb.test()
async def test_autobaud_unsupported_rate_rejection(dut):
    """Verify detection and rejection of unsupported baud rate (e.g. 50 cycles/bit)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_autobaud_rx_asm(pin=AUTOBAUD_PIN)
    await init_dut_and_bootload(dut, words)

    tx_model = AutobaudTransmitterModel(pin=AUTOBAUD_PIN)
    levels = tx_model.generate_sync_and_data(bit_period=50, data_byte=0x42)

    dut._log.info("Driving unsupported rate (T=50) training frame...")
    cocotb.start_soon(drive_levels(dut, levels))

    core = dut.user_project.u_core
    for _ in range(600):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Core should halt on error")

    r2 = int(core.r2.value)
    dut._log.info(f"Unsupported baud status: R2 = 0x{r2:02X}")
    assert r2 == 0xBF, f"Expected Baud Fault (0xBF), got 0x{r2:02X}"


@cocotb.test()
async def test_autobaud_framing_error_detection(dut):
    """Verify framing error detection when stop bit is held low."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_autobaud_rx_asm(pin=AUTOBAUD_PIN)
    await init_dut_and_bootload(dut, words)

    tx_model = AutobaudTransmitterModel(pin=AUTOBAUD_PIN)
    levels = tx_model.generate_sync_and_data(bit_period=16, data_byte=0x55, framing_error=True)

    dut._log.info("Driving data byte with framing error (stop bit low)...")
    cocotb.start_soon(drive_levels(dut, levels))

    core = dut.user_project.u_core
    for _ in range(1000):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Core should halt on error")

    r2 = int(core.r2.value)
    dut._log.info(f"Framing error status: R2 = 0x{r2:02X}")
    assert r2 == 0xFE, f"Expected Framing Error (0xFE), got 0x{r2:02X}"


@cocotb.test()
async def test_autobaud_pin_direction_safety(dut):
    """Verify that during Autobaud RX, pins are strictly configured as inputs."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_autobaud_rx_asm(pin=AUTOBAUD_PIN)
    await init_dut_and_bootload(dut, words)

    for _ in range(10):
        await RisingEdge(dut.clk)
        assert int(dut.uio_oe.value) == 0x00, f"Expected input mode (0x00), got 0x{int(dut.uio_oe.value):02X}"
