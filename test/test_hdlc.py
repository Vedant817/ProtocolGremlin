# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for the High-Level Data Link Control (HDLC / SDLC - ISO/IEC 13239) Engine.

Validates the core's ability to transmit and receive bit-oriented HDLC frames with:
1. NRZI (Non-Return-to-Zero Inverted) line coding.
2. Dynamic zero-bit insertion (bit stuffing after five consecutive 1s).
3. Dynamic zero-bit deletion (bit destuffing).
4. Delimiting flag sequences (0x7E / 01111110).
5. Abort sequence detection (>= 7 consecutive 1s).
6. Cycle-accurate bit timing with zero jitter.
7. Tri-state pin direction safety.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from hdlc_model import (  # noqa: E402
    HdlcTransmitter,
    HdlcReceiver,
    build_hdlc_tx_words,
    build_hdlc_rx_words,
)
from bootload import bootload  # noqa: E402

HDLC_TX_PIN = 0
HDLC_RX_PIN = 1
BIT_PERIOD = 16


async def init_dut_and_bootload(dut, words: list[int], initial_uio: int = 0):
    """Reset DUT and perform clean bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    # Call bootload immediately after releasing reset (zero intermediate clk edges)
    await bootload(dut, words)

    # Settle bus
    for _ in range(8):
        await FallingEdge(dut.clk)
        dut.uio_in.value = initial_uio
        await RisingEdge(dut.clk)


async def drive_cycle_levels(dut, pin: int, levels: list[int]):
    """Drive cycle-by-cycle bit levels onto specified uio pin."""
    pin_mask = 1 << pin
    for lvl in levels:
        await FallingEdge(dut.clk)
        curr = int(dut.uio_in.value) if dut.uio_in.value.is_resolvable else 0
        if lvl:
            curr |= pin_mask
        else:
            curr &= ~pin_mask
        dut.uio_in.value = curr
        await RisingEdge(dut.clk)


@cocotb.test()
async def test_hdlc_tx_waveform_fidelity(dut):
    """Verify ASIC HDLC TX transmits opening flag, payload 0xA5, and closing flag with NRZI."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = 0xA5
    words = build_hdlc_tx_words(payload=payload, pin=HDLC_TX_PIN, bit_period=BIT_PERIOD)
    await init_dut_and_bootload(dut, words, initial_uio=0)

    # Monitor uio_out[HDLC_TX_PIN] cycle-by-cycle
    samples = []
    core = dut.user_project.u_core

    for cycle in range(1500):
        await ReadOnly()
        val = (int(dut.uio_out.value) >> HDLC_TX_PIN) & 1
        samples.append(val)
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    assert core.halted.value == 1, "Core failed to halt during HDLC TX"

    # Sample at center of each bit period
    # Idle mark is held for BIT_PERIOD cycles before first bit begins
    # Find the first transition (falling edge 1 -> 0 of opening flag bit 0)
    start_cycle = -1
    for c in range(1, len(samples)):
        if samples[c - 1] == 1 and samples[c] == 0:
            start_cycle = c
            break

    assert start_cycle != -1, "Opening flag falling edge not detected!"
    dut._log.info(f"Opening flag falling edge detected at cycle {start_cycle}")

    # Sample bit levels at bit centers: start_cycle + BIT_PERIOD//2 + i * BIT_PERIOD
    bit_levels = []
    c = start_cycle + (BIT_PERIOD // 2)
    while c < len(samples):
        bit_levels.append(samples[c])
        c += BIT_PERIOD

    # Decode through HdlcReceiver
    rx = HdlcReceiver(bit_period=BIT_PERIOD, pin=HDLC_TX_PIN, initial_level=1)
    logical_bits = rx.decode_nrzi(bit_levels, initial_level=1)
    recovered_payload, closing_flag, abort = rx.decode_frame(logical_bits)

    dut._log.info(f"HDLC TX recovered payload: {recovered_payload}, closing_flag: {closing_flag}")
    assert closing_flag, "Closing flag 0x7E was not found in transmitted frame!"
    assert not abort, "Unexpected abort sequence detected in transmitted frame!"
    assert recovered_payload == [payload], f"Payload mismatch: expected {[payload]}, got {recovered_payload}"


@cocotb.test()
async def test_hdlc_tx_dynamic_bit_stuffing(dut):
    """Verify ASIC HDLC TX correctly inserts zero bits after 5 ones (payloads 0xFF, 0x7E, 0x3F)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    stuffed_payloads = [0xFF, 0x7E, 0x3F]
    for payload in stuffed_payloads:
        dut._log.info(f"Testing HDLC TX bit stuffing on payload 0x{payload:02X}...")
        words = build_hdlc_tx_words(payload=payload, pin=HDLC_TX_PIN, bit_period=BIT_PERIOD)
        await init_dut_and_bootload(dut, words, initial_uio=0)

        samples = []
        core = dut.user_project.u_core

        for cycle in range(1500):
            await ReadOnly()
            val = (int(dut.uio_out.value) >> HDLC_TX_PIN) & 1
            samples.append(val)
            if core.halted.value == 1:
                break
            await RisingEdge(dut.clk)

        assert core.halted.value == 1, f"Core failed to halt for payload 0x{payload:02X}"

        # Find first falling edge (start of opening flag)
        start_cycle = -1
        for c in range(1, len(samples)):
            if samples[c - 1] == 1 and samples[c] == 0:
                start_cycle = c
                break

        assert start_cycle != -1, f"Opening flag edge not found for 0x{payload:02X}"

        # Sample at bit centers
        bit_levels = []
        c = start_cycle + (BIT_PERIOD // 2)
        while c < len(samples):
            bit_levels.append(samples[c])
            c += BIT_PERIOD

        rx = HdlcReceiver(bit_period=BIT_PERIOD, pin=HDLC_TX_PIN, initial_level=1)
        logical_bits = rx.decode_nrzi(bit_levels, initial_level=1)
        recovered_payload, closing_flag, abort = rx.decode_frame(logical_bits)

        dut._log.info(f"Payload 0x{payload:02X} -> decoded {recovered_payload}, closing_flag={closing_flag}")
        assert closing_flag, f"Closing flag missing for stuffed payload 0x{payload:02X}"
        assert not abort, f"Abort detected for stuffed payload 0x{payload:02X}"
        assert recovered_payload == [payload], f"Payload mismatch: expected {[payload]}, got {recovered_payload}"


@cocotb.test()
async def test_hdlc_tx_payload_sweep(dut):
    """Verify HDLC TX across diverse non-stuffed patterns (0x00, 0x55, 0xAA, 0x3C)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    patterns = [0x00, 0x55, 0xAA, 0x3C]
    for p in patterns:
        words = build_hdlc_tx_words(payload=p, pin=HDLC_TX_PIN, bit_period=BIT_PERIOD)
        await init_dut_and_bootload(dut, words, initial_uio=0)

        samples = []
        core = dut.user_project.u_core

        for cycle in range(1500):
            await ReadOnly()
            val = (int(dut.uio_out.value) >> HDLC_TX_PIN) & 1
            samples.append(val)
            if core.halted.value == 1:
                break
            await RisingEdge(dut.clk)

        assert core.halted.value == 1

        start_cycle = -1
        for c in range(1, len(samples)):
            if samples[c - 1] == 1 and samples[c] == 0:
                start_cycle = c
                break

        assert start_cycle != -1
        bit_levels = []
        c = start_cycle + (BIT_PERIOD // 2)
        while c < len(samples):
            bit_levels.append(samples[c])
            c += BIT_PERIOD

        rx = HdlcReceiver(bit_period=BIT_PERIOD, pin=HDLC_TX_PIN, initial_level=1)
        logical_bits = rx.decode_nrzi(bit_levels, initial_level=1)
        recovered_payload, closing_flag, abort = rx.decode_frame(logical_bits)

        assert closing_flag
        assert not abort
        assert recovered_payload == [p], f"Sweep failed for 0x{p:02X}: got {recovered_payload}"


@cocotb.test()
async def test_hdlc_rx_clean_frame(dut):
    """Verify ASIC HDLC RX receives opening flag, standard payload (0xA5), closing flag into R0."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = 0xA5
    words = build_hdlc_rx_words(pin=HDLC_RX_PIN, bit_period=BIT_PERIOD, expected_payload=payload)
    pin_mask = 1 << HDLC_RX_PIN
    await init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    # Generate stimulus levels using HdlcTransmitter
    tx = HdlcTransmitter(bit_period=BIT_PERIOD, pin=HDLC_RX_PIN, initial_level=1)
    cycle_levels = tx.generate_cycle_levels([payload])

    dut._log.info(f"Driving HDLC RX frame with payload 0x{payload:02X}...")
    driver_task = cocotb.start_soon(drive_cycle_levels(dut, HDLC_RX_PIN, cycle_levels))

    core = dut.user_project.u_core
    for cycle in range(2500):
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    driver_task.cancel()
    assert core.halted.value == 1, "Core did not halt during HDLC RX"
    r0_val = int(core.r0.value)
    r1_val = int(core.r1.value)

    dut._log.info(f"HDLC RX completed: R0 = 0x{r0_val:02X}, R1 (status) = 0x{r1_val:02X}")
    assert r0_val == payload, f"R0 mismatch: expected 0x{payload:02X}, got 0x{r0_val:02X}"
    assert r1_val == 0x00, f"R1 status mismatch: expected 0x00 (SUCCESS), got 0x{r1_val:02X}"


@cocotb.test()
async def test_hdlc_rx_zero_bit_destuffing(dut):
    """Verify ASIC HDLC RX correctly unstuffs zeros from payloads with >= 5 ones (0xFF, 0x7E, 0x3F)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    stuffed_payloads = [0xFF, 0x7E, 0x3F]
    pin_mask = 1 << HDLC_RX_PIN

    for payload in stuffed_payloads:
        dut._log.info(f"Testing HDLC RX destuffing for payload 0x{payload:02X}...")
        words = build_hdlc_rx_words(pin=HDLC_RX_PIN, bit_period=BIT_PERIOD, expected_payload=payload)
        await init_dut_and_bootload(dut, words, initial_uio=pin_mask)

        tx = HdlcTransmitter(bit_period=BIT_PERIOD, pin=HDLC_RX_PIN, initial_level=1)
        cycle_levels = tx.generate_cycle_levels([payload])

        driver_task = cocotb.start_soon(drive_cycle_levels(dut, HDLC_RX_PIN, cycle_levels))

        core = dut.user_project.u_core
        for cycle in range(2500):
            if core.halted.value == 1:
                break
            await RisingEdge(dut.clk)

        driver_task.cancel()
        assert core.halted.value == 1, f"Core did not halt for payload 0x{payload:02X}"
        r0_val = int(core.r0.value)
        r1_val = int(core.r1.value)

        dut._log.info(f"HDLC RX destuffing 0x{payload:02X}: R0=0x{r0_val:02X}, R1=0x{r1_val:02X}")
        assert r0_val == payload, f"Destuffing failed: expected 0x{payload:02X}, got 0x{r0_val:02X}"
        assert r1_val == 0x00, f"Status failed: expected 0x00, got 0x{r1_val:02X}"


@cocotb.test()
async def test_hdlc_rx_abort_detection(dut):
    """Verify ASIC HDLC RX detects Abort sequence (>= 7 ones) and reports R1 = 0xAB."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = 0xA5
    words = build_hdlc_rx_words(pin=HDLC_RX_PIN, bit_period=BIT_PERIOD, expected_payload=payload, expect_abort=True)
    pin_mask = 1 << HDLC_RX_PIN
    await init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    # Transmit frame with injected abort sequence
    tx = HdlcTransmitter(bit_period=BIT_PERIOD, pin=HDLC_RX_PIN, initial_level=1)
    cycle_levels = tx.generate_cycle_levels([payload], inject_abort=True)

    dut._log.info("Driving HDLC frame with abort sequence...")
    driver_task = cocotb.start_soon(drive_cycle_levels(dut, HDLC_RX_PIN, cycle_levels))

    core = dut.user_project.u_core
    for cycle in range(2500):
        if core.halted.value == 1:
            break
        await RisingEdge(dut.clk)

    driver_task.cancel()
    assert core.halted.value == 1, "Core failed to halt on abort sequence"
    r1_val = int(core.r1.value)

    dut._log.info(f"HDLC RX abort detection status: R1 = 0x{r1_val:02X}")
    assert r1_val == 0xAB, f"Expected R1 = 0xAB (ABORT DETECTED), got 0x{r1_val:02X}"


@cocotb.test()
async def test_hdlc_pin_direction_safety(dut):
    """Verify strict tri-state pin safety: RX pin is strictly input, TX pin is strictly output."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Check RX pin direction
    words_rx = build_hdlc_rx_words(pin=HDLC_RX_PIN, bit_period=BIT_PERIOD, expected_payload=0x55)
    await init_dut_and_bootload(dut, words_rx, initial_uio=1 << HDLC_RX_PIN)
    await ReadOnly()
    oe_rx = int(dut.uio_oe.value)
    assert (oe_rx & (1 << HDLC_RX_PIN)) == 0, f"HDLC RX pin was actively driven! uio_oe = 0x{oe_rx:02X}"

    # 2. Check TX pin direction
    words_tx = build_hdlc_tx_words(payload=0x55, pin=HDLC_TX_PIN, bit_period=BIT_PERIOD)
    await init_dut_and_bootload(dut, words_tx, initial_uio=0)
    for _ in range(10):
        await RisingEdge(dut.clk)
    await ReadOnly()
    oe_tx = int(dut.uio_oe.value)
    assert (oe_tx & (1 << HDLC_TX_PIN)) != 0, f"HDLC TX pin not enabled as output! uio_oe = 0x{oe_tx:02X}"
    assert (oe_tx & ~(1 << HDLC_TX_PIN)) == 0, f"Non-TX pins were driven as output! uio_oe = 0x{oe_tx:02X}"
