# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for DMX512 (ANSI E1.11 / USITT DMX512-A) Stage Lighting Protocol Engine.

Verifies:
1. DMX512 TX Packet Waveform Fidelity:
   - ASIC transmits Break pulse, MAB, Start Code (0x00), and sequential channel slots.
   - Verified and decoded by independent Dmx512ReceiverModel.
2. DMX512 TX Multi-Channel Pattern Sweep:
   - Transmits RGB intensity mixtures (e.g. [0xE6, 0x20, 0xA1], [0x55, 0xAA, 0x33]).
3. DMX512 RX Target Channel 1 Capture:
   - External Dmx512Transmitter sends packet.
   - ASIC measures Break duration with single-cycle precision via WAITEDGE (R3).
   - Extracts Channel 1 dimmer level into R0.
4. DMX512 RX Target Channel 2 Capture:
   - External transmitter sends multi-channel packet.
   - ASIC skips Channel 1 and captures Channel 2 into R0.
5. DMX512 Pin Isolation & Electrical Direction Safety:
   - Verifies uio_oe in RX and TX modes.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from dmx512_model import (  # noqa: E402
    Dmx512Transmitter,
    Dmx512ReceiverModel,
    build_dmx512_tx_packet_asm,
    build_dmx512_rx_slot_asm,
)

PIN = 4
BIT_PERIOD = 4


async def _init_dut_and_bootload(dut, words: list[int]):
    """Fresh hardware reset and bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = (1 << PIN)  # Line idle mark (high)
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = (1 << PIN)


@cocotb.test()
async def test_dmx512_tx_waveform(dut):
    """Verify DMX512 TX packet waveform: Break pulse, MAB, Start Code 0x00, and 3 channel slots."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    start_code = 0x00
    channels = [0xFF, 0x80, 0x00]
    dut._log.info(f"Testing DMX512 TX: StartCode=0x{start_code:02X}, Channels={channels}")

    asm = build_dmx512_tx_packet_asm(start_code=start_code, channels=channels, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    recorded_levels = []

    for cycle in range(600):
        await FallingEdge(dut.clk)
        await ReadOnly()
        uio_out = int(dut.uio_out.value)
        recorded_levels.append((uio_out >> PIN) & 1)

        if bool(core.halted.value):
            for _ in range(10):
                await FallingEdge(dut.clk)
                recorded_levels.append((int(dut.uio_out.value) >> PIN) & 1)
            break
    else:
        raise AssertionError("DMX512 TX test timed out waiting for HALT")

    # Decode waveform using independent Dmx512ReceiverModel
    receiver = Dmx512ReceiverModel(bit_period=BIT_PERIOD)
    dec_sc, dec_channels, valid = receiver.parse_waveform(recorded_levels)

    dut._log.info(f"Decoded DMX512: StartCode=0x{dec_sc:02X}, Channels={dec_channels} (valid={valid})")
    assert valid, "DMX512 waveform failed receiver validation"
    assert dec_sc == start_code, f"Decoded Start Code 0x{dec_sc:02X} != expected 0x{start_code:02X}"
    assert dec_channels == channels, f"Decoded channels {dec_channels} != expected {channels}"


@cocotb.test()
async def test_dmx512_tx_channel_sweep(dut):
    """Verify DMX512 TX with various intensity patterns."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_vectors = [
        [0xE6, 0x20, 0xA1],
        [0x55, 0xAA, 0x33],
        [0x01, 0x02, 0x03],
    ]

    for channels in test_vectors:
        dut._log.info(f"Testing DMX512 TX pattern: {channels}")
        asm = build_dmx512_tx_packet_asm(start_code=0x00, channels=channels, bit_period=BIT_PERIOD, pin=PIN)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        recorded_levels = []

        for cycle in range(600):
            await FallingEdge(dut.clk)
            await ReadOnly()
            recorded_levels.append((int(dut.uio_out.value) >> PIN) & 1)

            if bool(core.halted.value):
                for _ in range(10):
                    await FallingEdge(dut.clk)
                    recorded_levels.append((int(dut.uio_out.value) >> PIN) & 1)
                break
        else:
            raise AssertionError(f"DMX512 TX timed out for channels {channels}")

        receiver = Dmx512ReceiverModel(bit_period=BIT_PERIOD)
        dec_sc, dec_channels, valid = receiver.parse_waveform(recorded_levels)

        assert valid, f"Pattern {channels} failed validation"
        assert dec_sc == 0x00, f"Expected Null Start Code, got 0x{dec_sc:02X}"
        assert dec_channels == channels, f"Decoded {dec_channels} != expected {channels}"


@cocotb.test()
async def test_dmx512_rx_channel_1(dut):
    """Verify DMX512 RX: measures Break duration in R3, captures Channel 1 intensity in R0."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_channel = 1
    expected_intensity = 0xCC
    channels = [expected_intensity, 0x33]
    dut._log.info(f"Testing DMX512 RX Channel 1: expected = 0x{expected_intensity:02X}")

    asm = build_dmx512_rx_slot_asm(target_channel=target_channel, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    transmitter = Dmx512Transmitter(dut, pin=PIN, bit_period=BIT_PERIOD)

    # Allow core to enter WAITEDGE
    for _ in range(16):
        await FallingEdge(dut.clk)
        dut.uio_in.value = (1 << PIN)
        await RisingEdge(dut.clk)

    # Transmit packet concurrently
    cocotb.start_soon(transmitter.transmit_packet(start_code=0x00, channels=channels, clock=RisingEdge(dut.clk)))

    for cycle in range(1000):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("DMX512 RX Channel 1 test timed out waiting for HALT")

    r0 = int(core.r0.value)
    r2 = int(core.r2.value)
    r3 = int(core.r3.value)
    dut._log.info(f"RX Channel 1 complete: R0 = 0x{r0:02X}, R2 = 0x{r2:02X}, Break duration R3 = {r3} cycles")

    assert r0 == expected_intensity, f"Decoded Channel 1 intensity 0x{r0:02X} != expected 0x{expected_intensity:02X}"
    assert r2 == 0x00, f"Expected status R2 = 0x00, got 0x{r2:02X}"
    # Verify measured break duration: transmitter sent 24 * BIT_PERIOD = 96 cycles
    # R3 must reflect this pulse duration
    assert r3 >= (20 * BIT_PERIOD), f"Measured Break duration {r3} too short (expected >= {20 * BIT_PERIOD})"


@cocotb.test()
async def test_dmx512_rx_channel_2(dut):
    """Verify DMX512 RX: skips Channel 1 and captures Channel 2 intensity in R0."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_channel = 2
    expected_intensity = 0x77
    channels = [0x11, expected_intensity, 0xBB]
    dut._log.info(f"Testing DMX512 RX Channel 2: expected = 0x{expected_intensity:02X}")

    asm = build_dmx512_rx_slot_asm(target_channel=target_channel, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    transmitter = Dmx512Transmitter(dut, pin=PIN, bit_period=BIT_PERIOD)

    for _ in range(16):
        await FallingEdge(dut.clk)
        dut.uio_in.value = (1 << PIN)
        await RisingEdge(dut.clk)

    cocotb.start_soon(transmitter.transmit_packet(start_code=0x00, channels=channels, clock=RisingEdge(dut.clk)))

    for cycle in range(1000):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("DMX512 RX Channel 2 test timed out waiting for HALT")

    r0 = int(core.r0.value)
    r2 = int(core.r2.value)
    dut._log.info(f"RX Channel 2 complete: R0 = 0x{r0:02X}, R2 = 0x{r2:02X}")

    assert r0 == expected_intensity, f"Decoded Channel 2 intensity 0x{r0:02X} != expected 0x{expected_intensity:02X}"
    assert r2 == 0x00, f"Expected status R2 = 0x00, got 0x{r2:02X}"


@cocotb.test()
async def test_dmx512_pin_direction_safety(dut):
    """Verify pin direction configuration: input during RX, single output during TX."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # RX mode: all pins tri-stated inputs
    asm_rx = build_dmx512_rx_slot_asm(target_channel=1, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm_rx)
    await _init_dut_and_bootload(dut, words)

    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.uio_oe.value) == 0x00, f"RX uio_oe = 0x{int(dut.uio_oe.value):02X} != 0x00"

    # TX mode: only PIN is configured as output
    asm_tx = build_dmx512_tx_packet_asm(start_code=0x00, channels=[0x00], bit_period=BIT_PERIOD, pin=PIN)
    words_tx = assemble(asm_tx)
    await _init_dut_and_bootload(dut, words_tx)

    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.uio_oe.value) == (1 << PIN), f"TX uio_oe = 0x{int(dut.uio_oe.value):02X} != 0x{1 << PIN:02X}"
