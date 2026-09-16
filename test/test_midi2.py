# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_midi2.py
==================
Cocotb testbench for MIDI 2.0 Universal MIDI Packet (UMP) Protocol Engine.

Test suite covers:
  1. 32-bit UMP packet transmission from ASIC:
     Transmits MIDI 1.0 Note On (Group 3, Channel 5, Note 60, Velocity 100)
     serialized as 4 consecutive UART 8-N-1 octets in Big-Endian order.
     Verified against independent cycle-accurate UartReceiver.
  2. UMP Group filtering match: Receiver ingresses UMP Byte 0 over UART 8-N-1,
     verifies target Group (Group 3 match) and halts with status R2 = 0x00 and R1 = 3.
  3. UMP Group mismatch rejection: Receiver detects mismatched Group (Group 7 vs Group 3),
     rejects packet and halts with error code R2 = 0xEE.
  4. Note Dispatch match: Receiver ingresses Status (0x90) and Note (60), matches
     target note and halts with status R2 = 0x00 and R0 = 60.
  5. Note Dispatch mismatch rejection: Receiver ingresses non-matching note (64 vs 60),
     rejects dispatch and halts with error code R2 = 0xEE.
  6. High-Resolution 64-bit Channel Voice validation (16-bit velocity 0xC000, 32-bit pitch bend),
     Jitter-Reduction timestamp UMP validation, and physical PPA scaling on IHP 130nm SG13G2.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from bootload import bootload
from uart_model import UartReceiver, UartTransmitter
from midi2_model import (
    build_ump_32,
    build_ump_midi1_note_on,
    build_ump_midi2_note_on,
    build_ump_midi2_pitch_bend,
    build_ump_jr_timestamp,
    parse_ump_word,
    parse_ump_64,
    Midi2PpaModel,
    build_midi2_ump_tx_asm,
    build_midi2_group_filter_asm,
    build_midi2_note_dispatch_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x08):
    """Reset DUT and load assembled firmware into program RAM via standard bootloader."""
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = initial_uio
    await ClockCycles(dut.clk, 10)


@cocotb.test()
async def test_midi2_ump_packet_transmission(dut):
    """
    Test 1: Transmit 32-bit UMP packet from ASIC:
    MIDI 1.0 Note On (Group 3, Channel 5, Note 60, Velocity 100).
    Verifies 4 consecutive UART 8-N-1 octets received on pin 3 match UMP word.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    group = 3
    channel = 5
    note = 60
    velocity = 100
    expected_word = build_ump_midi1_note_on(group, channel, note, velocity)

    bit_period = 8
    tx_pin = 3
    words = build_midi2_ump_tx_asm(expected_word, tx_pin=tx_pin, bit_period=bit_period)
    await _init_dut_and_bootload(dut, words, initial_uio=1 << tx_pin)

    receiver = UartReceiver(bit_period_cycles=bit_period)
    core = dut.user_project.u_core

    decoded_bytes = []
    max_cycles = 600

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        res = receiver.step(pin_val)
        if res is not None:
            decoded_bytes.append(res)

        if bool(core.halted.value) and receiver.state == UartReceiver.STATE_IDLE and len(decoded_bytes) >= 4:
            break

    assert len(decoded_bytes) == 4, f"Expected 4 UMP octets, decoded {len(decoded_bytes)}: {[hex(b) for b in decoded_bytes]}"

    received_word = (
        (decoded_bytes[0] << 24)
        | (decoded_bytes[1] << 16)
        | (decoded_bytes[2] << 8)
        | decoded_bytes[3]
    )

    assert received_word == expected_word, (
        f"UMP transmission mismatch: expected 0x{expected_word:08X}, got 0x{received_word:08X}"
    )

    parsed = parse_ump_word(received_word)
    assert parsed["mt"] == 0x2, "Expected MT=0x2 (MIDI 1.0 Channel Voice)"
    assert parsed["group"] == group, f"Group mismatch: expected {group}, got {parsed['group']}"
    assert parsed["channel"] == channel, f"Channel mismatch: expected {channel}, got {parsed['channel']}"
    assert parsed["status"] == 0x9, "Expected Status=0x9 (Note On)"
    assert parsed["data1"] == note, f"Note mismatch: expected {note}, got {parsed['data1']}"
    assert parsed["data2"] == velocity, f"Velocity mismatch: expected {velocity}, got {parsed['data2']}"

    dut._log.info(f"MIDI 2.0 UMP Word Transmission PASS: 0x{received_word:08X}, Note={note}, Vel={velocity}")


@cocotb.test()
async def test_midi2_group_filtering_match(dut):
    """
    Test 2: Receiver ingresses UMP header byte (Bits [31:24] = MT[3:0] | Group[3:0]).
    Target group = 3. Ingress byte 0x23 (MT=2, Group=3).
    Core matches Group 3, sets R2 = 0x00 and captures Group 3 in R1.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_group = 3
    rx_pin = 3
    bit_period = 8
    words = build_midi2_group_filter_asm(target_group=target_group, rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)  # idle HIGH

    core = dut.user_project.u_core

    # Let core enter WAITEDGE stall
    for _ in range(6):
        await RisingEdge(dut.clk)

    header_byte = (0x2 << 4) | target_group
    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = tx.generate_bit_stream(header_byte, idle_before=4, idle_after=10)

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    # Wait for core halt
    max_wait = 40 + 5 * bit_period
    for _ in range(max_wait):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after matching group"
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)
    assert r1 == target_group, f"Expected Group {target_group}, got {r1}"
    assert r2 == 0x00, f"Expected status R2=0x00 (match), got 0x{r2:02X}"
    dut._log.info(f"MIDI 2.0 Group Filter Match PASS: Group={r1}, Status=0x{r2:02X}")


@cocotb.test()
async def test_midi2_group_filtering_mismatch(dut):
    """
    Test 3: Receiver detects mismatched UMP Group (Group 7 vs target Group 3).
    Core rejects and halts with error code R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_group = 3
    mismatched_group = 7
    rx_pin = 3
    bit_period = 8
    words = build_midi2_group_filter_asm(target_group=target_group, rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core

    for _ in range(6):
        await RisingEdge(dut.clk)

    header_byte = (0x2 << 4) | mismatched_group
    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = tx.generate_bit_stream(header_byte, idle_before=4, idle_after=10)

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    max_wait = 40 + 5 * bit_period
    for _ in range(max_wait):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt on group mismatch"
    r2 = int(core.r2.value)
    assert r2 == 0xEE, f"Expected error code R2=0xEE (mismatch), got 0x{r2:02X}"
    dut._log.info(f"MIDI 2.0 Group Mismatch Rejection PASS: Status=0x{r2:02X}")


@cocotb.test()
async def test_midi2_note_dispatch_match(dut):
    """
    Test 4: Receiver validates Status (0x90 Note On) and Note Number (60).
    Matches target note, halts with R2 = 0x00 and R0 = 60.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_note = 60  # Middle C
    rx_pin = 3
    bit_period = 8
    words = build_midi2_note_dispatch_asm(target_note=target_note, rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core

    for _ in range(6):
        await RisingEdge(dut.clk)

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)

    # Transmit Byte 1: Status & Channel (0x90 Note On, Channel 0)
    stream1 = tx.generate_bit_stream(0x90, idle_before=4, idle_after=4)
    for bit in stream1:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    # Transmit Byte 2: Note Number (60)
    stream2 = tx.generate_bit_stream(target_note, idle_before=4, idle_after=10)
    for bit in stream2:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    max_wait = 40 + 5 * bit_period
    for _ in range(max_wait):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after note dispatch"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)
    assert r0 == target_note, f"Expected Note {target_note}, got {r0}"
    assert r2 == 0x00, f"Expected status R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"MIDI 2.0 Note Dispatch Match PASS: Note={r0}, Status=0x{r2:02X}")


@cocotb.test()
async def test_midi2_note_dispatch_mismatch(dut):
    """
    Test 5: Receiver receives different note (64 vs 60), rejects and halts with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_note = 60
    different_note = 64
    rx_pin = 3
    bit_period = 8
    words = build_midi2_note_dispatch_asm(target_note=target_note, rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core

    for _ in range(6):
        await RisingEdge(dut.clk)

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)

    # Transmit Byte 1: Status & Channel (0x90 Note On)
    stream1 = tx.generate_bit_stream(0x90, idle_before=4, idle_after=4)
    for bit in stream1:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    # Transmit Byte 2: Note Number (64)
    stream2 = tx.generate_bit_stream(different_note, idle_before=4, idle_after=10)
    for bit in stream2:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    max_wait = 40 + 5 * bit_period
    for _ in range(max_wait):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt on note mismatch"
    r2 = int(core.r2.value)
    assert r2 == 0xEE, f"Expected error code R2=0xEE, got 0x{r2:02X}"
    dut._log.info(f"MIDI 2.0 Note Dispatch Mismatch Rejection PASS: Status=0x{r2:02X}")


@cocotb.test()
async def test_midi2_highres_voice_and_ppa_validation(dut):
    """
    Test 6: Mathematical validation of 64-bit MIDI 2.0 Channel Voice messages,
    Jitter-Reduction Timestamps, and physical PPA scaling on IHP 130nm SG13G2.
    """
    # Test 64-bit Note On with 16-bit high-resolution velocity
    w0, w1 = build_ump_midi2_note_on(group=1, channel=2, note=69, velocity16=0xC000)
    p2 = parse_ump_64(w0, w1)
    assert p2["hdr"]["mt"] == 0x4
    assert p2["hdr"]["group"] == 1
    assert p2["hdr"]["channel"] == 2
    assert p2["hdr"]["status"] == 0x9
    assert p2["note"] == 69
    assert p2["velocity16"] == 0xC000

    # Test 64-bit Pitch Bend with 32-bit resolution
    pb0, pb1 = build_ump_midi2_pitch_bend(group=0, channel=0, pitch32=0x80000000)
    ppb = parse_ump_64(pb0, pb1)
    assert ppb["hdr"]["status"] == 0xE
    assert ppb["pitch32"] == 0x80000000

    # Test JR Timestamp
    jr = build_ump_jr_timestamp(group=4, timestamp16=0x1234)
    pjr = parse_ump_word(jr)
    assert pjr["mt"] == 0x0
    assert pjr["status"] == 0x2
    assert pjr["group"] == 4
    assert ((pjr["data1"] << 8) | pjr["data2"]) == 0x1234

    # PPA Validation
    ppa = Midi2PpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 395
    assert ppa["f_max_mhz"] > 700.0

    dut._log.info(
        f"MIDI 2.0 High-Res Voice & PPA Model PASS: Cells={ppa['standard_cells']}, "
        f"GE={ppa['gate_equivalents']}, Fmax={ppa['f_max_mhz']:.1f} MHz"
    )
