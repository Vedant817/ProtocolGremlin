# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for the Autonomous Hardware Protocol Sniffer & Dynamic Pattern Classifier Engine.

Demonstrates the core's unique capability to passively monitor an unknown communications
bus using WAITEDGE single-cycle pulse measurement, extract timing signatures, and
autonomously classify the active protocol on the wire:
- UART (0x01)
- Manchester Biphase-L (0x02)
- Dallas 1-Wire (0x03)
- DMX512 (0x04)
- HDLC / SDLC (0x05)
- Unknown / Noise Rejection (0xFF)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from classifier_model import (  # noqa: E402
    build_protocol_sniffer_asm,
    PROTO_UART,
    PROTO_MANCHESTER,
    PROTO_ONEWIRE,
    PROTO_DMX512,
    PROTO_HDLC,
    PROTO_UNKNOWN,
)
from bootload import bootload  # noqa: E402

CLASSIFIER_PIN = 2
PIN_MASK = 1 << CLASSIFIER_PIN


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

    # Allow core to enter WAITEDGE
    for _ in range(16):
        await FallingEdge(dut.clk)
        dut.uio_in.value = PIN_MASK
        await RisingEdge(dut.clk)


async def drive_pulse_sequence(dut, low_cycles: int, high_cycles: int, post_low: int = 10):
    """Drives an initial idle high, then low pulse, then high pulse on CLASSIFIER_PIN."""
    # Ensure idle high
    await FallingEdge(dut.clk)
    dut.uio_in.value = PIN_MASK
    await RisingEdge(dut.clk)

    # 1. Low pulse
    for _ in range(low_cycles):
        await FallingEdge(dut.clk)
        dut.uio_in.value = 0
        await RisingEdge(dut.clk)

    # 2. High pulse
    for _ in range(high_cycles):
        await FallingEdge(dut.clk)
        dut.uio_in.value = PIN_MASK
        await RisingEdge(dut.clk)

    # 3. Trailing low edge to end high pulse measurement
    for _ in range(post_low):
        await FallingEdge(dut.clk)
        dut.uio_in.value = 0
        await RisingEdge(dut.clk)


@cocotb.test()
async def test_classify_uart(dut):
    """Verify autonomous classification of UART traffic (T_low=16, T_high=16 -> R0=0x01)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_protocol_sniffer_asm(pin=CLASSIFIER_PIN)
    await init_dut_and_bootload(dut, words)

    dut._log.info("Driving UART stimulus (T_low=16 cycles, T_high=16 cycles)...")
    driver_task = cocotb.start_soon(drive_pulse_sequence(dut, low_cycles=16, high_cycles=16))

    core = dut.user_project.u_core
    for cycle in range(500):
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    driver_task.cancel()
    assert core.halted.value == 1, "Core failed to halt during UART classification"
    r0_val = int(core.r0.value)
    t_low = int(core.r1.value)
    t_high = int(core.r2.value)

    dut._log.info(f"Classifier Result: R0=0x{r0_val:02X} (T_low={t_low}, T_high={t_high})")
    assert r0_val == PROTO_UART, f"Expected PROTO_UART (0x01), got 0x{r0_val:02X}"


@cocotb.test()
async def test_classify_manchester(dut):
    """Verify autonomous classification of Manchester Biphase-L (T_low=4, T_high=4 -> R0=0x02)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_protocol_sniffer_asm(pin=CLASSIFIER_PIN)
    await init_dut_and_bootload(dut, words)

    dut._log.info("Driving Manchester stimulus (T_low=4 cycles, T_high=4 cycles)...")
    driver_task = cocotb.start_soon(drive_pulse_sequence(dut, low_cycles=4, high_cycles=4))

    core = dut.user_project.u_core
    for cycle in range(500):
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    driver_task.cancel()
    assert core.halted.value == 1, "Core failed to halt during Manchester classification"
    r0_val = int(core.r0.value)
    t_low = int(core.r1.value)
    t_high = int(core.r2.value)

    dut._log.info(f"Classifier Result: R0=0x{r0_val:02X} (T_low={t_low}, T_high={t_high})")
    assert r0_val == PROTO_MANCHESTER, f"Expected PROTO_MANCHESTER (0x02), got 0x{r0_val:02X}"


@cocotb.test()
async def test_classify_dmx512(dut):
    """Verify autonomous classification of DMX512 (Break=96 cycles, MAB=16 cycles -> R0=0x04)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_protocol_sniffer_asm(pin=CLASSIFIER_PIN)
    await init_dut_and_bootload(dut, words)

    dut._log.info("Driving DMX512 stimulus (Break=96 cycles, MAB=16 cycles)...")
    driver_task = cocotb.start_soon(drive_pulse_sequence(dut, low_cycles=96, high_cycles=16))

    core = dut.user_project.u_core
    for cycle in range(500):
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    driver_task.cancel()
    assert core.halted.value == 1, "Core failed to halt during DMX512 classification"
    r0_val = int(core.r0.value)
    t_low = int(core.r1.value)
    t_high = int(core.r2.value)

    dut._log.info(f"Classifier Result: R0=0x{r0_val:02X} (T_low={t_low}, T_high={t_high})")
    assert r0_val == PROTO_DMX512, f"Expected PROTO_DMX512 (0x04), got 0x{r0_val:02X}"


@cocotb.test()
async def test_classify_onewire(dut):
    """Verify autonomous classification of Dallas 1-Wire (Reset=96 cycles, Recovery=40 cycles -> R0=0x03)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_protocol_sniffer_asm(pin=CLASSIFIER_PIN)
    await init_dut_and_bootload(dut, words)

    dut._log.info("Driving 1-Wire stimulus (Reset=96 cycles, Delay=40 cycles)...")
    driver_task = cocotb.start_soon(drive_pulse_sequence(dut, low_cycles=96, high_cycles=40))

    core = dut.user_project.u_core
    for cycle in range(500):
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    driver_task.cancel()
    assert core.halted.value == 1, "Core failed to halt during 1-Wire classification"
    r0_val = int(core.r0.value)
    t_low = int(core.r1.value)
    t_high = int(core.r2.value)

    dut._log.info(f"Classifier Result: R0=0x{r0_val:02X} (T_low={t_low}, T_high={t_high})")
    assert r0_val == PROTO_ONEWIRE, f"Expected PROTO_ONEWIRE (0x03), got 0x{r0_val:02X}"


@cocotb.test()
async def test_classify_hdlc(dut):
    """Verify autonomous classification of HDLC flag hold (T_low=56 cycles, T_high=16 cycles -> R0=0x05)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_protocol_sniffer_asm(pin=CLASSIFIER_PIN)
    await init_dut_and_bootload(dut, words)

    dut._log.info("Driving HDLC flag stimulus (T_low=56 cycles, T_high=16 cycles)...")
    driver_task = cocotb.start_soon(drive_pulse_sequence(dut, low_cycles=56, high_cycles=16))

    core = dut.user_project.u_core
    for cycle in range(500):
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    driver_task.cancel()
    assert core.halted.value == 1, "Core failed to halt during HDLC classification"
    r0_val = int(core.r0.value)
    t_low = int(core.r1.value)
    t_high = int(core.r2.value)

    dut._log.info(f"Classifier Result: R0=0x{r0_val:02X} (T_low={t_low}, T_high={t_high})")
    assert r0_val == PROTO_HDLC, f"Expected PROTO_HDLC (0x05), got 0x{r0_val:02X}"


@cocotb.test()
async def test_classify_noise_rejection(dut):
    """Verify rejection of uncalibrated noise pulse (T_low=2 cycles, T_high=35 cycles -> R0=0xFF)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_protocol_sniffer_asm(pin=CLASSIFIER_PIN)
    await init_dut_and_bootload(dut, words)

    dut._log.info("Driving Noise pulse (T_low=2 cycles, T_high=35 cycles)...")
    driver_task = cocotb.start_soon(drive_pulse_sequence(dut, low_cycles=2, high_cycles=35))

    core = dut.user_project.u_core
    for cycle in range(500):
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    driver_task.cancel()
    assert core.halted.value == 1, "Core failed to halt during noise rejection"
    r0_val = int(core.r0.value)
    t_low = int(core.r1.value)
    t_high = int(core.r2.value)

    dut._log.info(f"Classifier Result: R0=0x{r0_val:02X} (T_low={t_low}, T_high={t_high})")
    assert r0_val == PROTO_UNKNOWN, f"Expected PROTO_UNKNOWN (0xFF), got 0x{r0_val:02X}"


@cocotb.test()
async def test_classifier_pin_direction_safety(dut):
    """Verify strictly passive sniffing: all pins remain configured as inputs."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_protocol_sniffer_asm(pin=CLASSIFIER_PIN)
    await init_dut_and_bootload(dut, words)

    await ReadOnly()
    oe_val = int(dut.uio_oe.value)
    assert oe_val == 0x00, f"Classifier was actively driving pins! uio_oe = 0x{oe_val:02X}"
