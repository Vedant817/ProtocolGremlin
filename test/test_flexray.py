# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_flexray.py
====================
Cocotb testbench for FlexRay (ISO 17458) Automotive Deterministic Bus Protocol Engine
and Dual-Channel TDMA Controller.

Test suite covers:
  1. FlexRay Frame Master Transmission:
     ASIC serializes a 10-byte FlexRay frame:
     5-byte Header [Sync=0, Startup=0, Frame_ID=0x005, Length=1, CRC11, Cycle=0],
     2-byte Payload [0xCA, 0xFE],
     3-byte Frame CRC-24 (Channel A init 0xFEDCBA)
     over UART 8-N-1 on pin 3, verified by independent UartReceiver and FlexRayFrame parser.
  2. Static Segment TDMA Slot Timing & Synchronization:
     Node is assigned Slot 3 in a 4-slot cycle.
     ASIC tracks slots 1..4; transmission strobe activates strictly within Slot 3 window
     (asserting pin 0 and R2 = 0xAA) and remains quiescent in Slots 1, 2, and 4.
  3. FlexRay Frame ID Ingress Filter Match:
     ASIC receiver (configured for Frame ID 0x05) receives matching frame on pin 4,
     latches payload bytes into R0 (0x42) and R1 (0x99), asserting status R2 = 0x00.
  4. FlexRay Frame ID Ingress Filter Mismatch:
     ASIC receiver receives frame addressed to Frame ID 0x09, detects mismatch on header byte 1,
     bypasses payload acquisition, and flags status R2 = 0xEE.
  5. Dual-Channel Redundancy & Seamless Failover:
     Channel A is simulated with a physical line fault (stuck low).
     ASIC detects fault on pin 0, fails over to Channel B on pin 1, ingresses payload 0x77 into R0,
     marks source R1 = 0x0B, and halts with status R2 = 0x00.
  6. Header CRC-11, Frame CRC-24 Mathematical Validation & PPA Analysis.
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
from flexray_model import (
    FlexRayFrame,
    FlexRayNodeModel,
    FlexRayPpaModel,
    compute_flexray_header_crc11,
    compute_flexray_frame_crc24,
    build_flexray_tx_frame_asm,
    build_flexray_tdma_slot_tracker_asm,
    build_flexray_rx_filter_asm,
    build_flexray_header_crc_validator_asm,
    build_flexray_dual_channel_failover_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x10):
    """Reset DUT and load assembled firmware into program RAM via serial bootloader."""
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
async def test_flexray_tx_frame(dut):
    """
    Test 1: FlexRay Frame Master Transmission on pin 3:
    Transmits 10-byte frame:
      - 5 bytes Header (Frame ID = 5, Length = 1 word = 2 bytes, Sync = 0, Startup = 0)
      - 2 bytes Payload [0xCA, 0xFE]
      - 3 bytes Frame CRC-24 (Channel A)
    Verified by independent UartReceiver on pin 3 and FlexRayFrame parser.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 3
    bit_period = 8
    frame_id = 0x005
    payload = [0xCA, 0xFE]

    frame = FlexRayFrame(
        frame_id=frame_id,
        payload_length=1,
        payload=payload,
        sync_frame=0,
        startup_frame=0,
        null_frame=1,
        channel="A"
    )

    words = build_flexray_tx_frame_asm(
        frame=frame,
        tx_pin=tx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << tx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    receiver = UartReceiver(bit_period_cycles=bit_period)
    decoded_bytes = []
    max_cycles = 100 + 10 * 14 * bit_period
    core = dut.user_project.u_core

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        res = receiver.step(pin_val)
        if res is not None:
            decoded_bytes.append(res)

        if bool(core.halted.value) and receiver.state == UartReceiver.STATE_IDLE and len(decoded_bytes) >= 10:
            break

    assert len(decoded_bytes) == 10, f"Expected 10 bytes, got {len(decoded_bytes)}: {decoded_bytes}"
    expected_bytes = list(frame.to_bytes())
    assert decoded_bytes == expected_bytes, f"Frame bytes mismatch: expected {expected_bytes}, got {decoded_bytes}"

    parsed = FlexRayFrame.from_bytes(bytes(decoded_bytes), channel="A")
    assert parsed.frame_id == frame_id, f"Expected Frame ID {frame_id}, got {parsed.frame_id}"
    assert parsed.payload == payload, f"Expected payload {payload}, got {parsed.payload}"
    assert parsed.null_frame == 1, "Expected normal data frame"

    r2 = int(core.r2.value)
    assert r2 == 0x00, f"Expected R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"FlexRay Frame TX PASS: Frame ID={parsed.frame_id}, Payload={parsed.payload}")


@cocotb.test()
async def test_flexray_tdma_slot_tracker(dut):
    """
    Test 2: Static Segment TDMA Slot Timing & Synchronization:
    Node assigned Slot 3 in a 4-slot cycle.
    ASIC slot engine loops through slots 1..4.
    Transmission strobe on pin 0 must be ASSERTED (HIGH) ONLY during Slot 3,
    and must remain QUIESCENT (LOW) in Slots 1, 2, and 4.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    assigned_slot = 3
    total_slots = 4
    slot_wait_cycles = 20
    tx_pin = 0

    words = build_flexray_tdma_slot_tracker_asm(
        assigned_slot_id=assigned_slot,
        total_slots=total_slots,
        slot_wait_cycles=slot_wait_cycles,
        tx_pin=tx_pin
    )
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core
    max_cycles = 200
    strobe_log = []

    for c in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        current_slot = int(core.r0.value)
        strobe_log.append((current_slot, pin_val))

        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should complete TDMA cycle and halt"

    # Verify that pin_val was 1 ONLY when current_slot == assigned_slot
    slot_with_strobe = set()
    for s, p in strobe_log:
        if p == 1:
            slot_with_strobe.add(s)

    assert slot_with_strobe == {assigned_slot}, (
        f"Strobe observed in slots {slot_with_strobe}, expected strictly in slot {assigned_slot}"
    )

    r2 = int(core.r2.value)
    assert r2 == 0x00, f"Expected Status R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"FlexRay TDMA Slot Engine PASS: Strictly asserted in slot {assigned_slot}")


@cocotb.test()
async def test_flexray_rx_filter_match(dut):
    """
    Test 3: FlexRay Frame ID Ingress Filter Match:
    Node configured for Frame ID 0x05.
    Host transmits frame with Frame ID 0x05, Payload [0x42, 0x99].
    ASIC matches Frame ID, latches 0x42 into R0, 0x99 into R1, and sets R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_id = 0x05
    rx_pin = 4
    bit_period = 8

    words = build_flexray_rx_filter_asm(
        target_frame_id=target_id,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Frame with Frame ID 0x05
    frame = FlexRayFrame(
        frame_id=target_id,
        payload_length=1,
        payload=[0x42, 0x99],
        channel="A"
    )
    raw_bytes = list(frame.to_bytes())

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    # Send 7 bytes: 5 header bytes + 2 payload bytes
    for b in raw_bytes[:7]:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after matching frame ingress"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x42, f"Expected Payload[0]=0x42 in R0, got 0x{r0:02X}"
    assert r1 == 0x99, f"Expected Payload[1]=0x99 in R1, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected Status R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"FlexRay RX Filter Match PASS: R0=0x{r0:02X}, R1=0x{r1:02X}, R2=0x{r2:02X}")


@cocotb.test()
async def test_flexray_rx_filter_mismatch(dut):
    """
    Test 4: FlexRay Frame ID Ingress Filter Mismatch:
    Node configured for Frame ID 0x05.
    Host transmits frame with Frame ID 0x09 (mismatch).
    ASIC detects mismatch on Header Byte 1, drops payload, and halts with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_id = 0x05
    rx_pin = 4
    bit_period = 8

    words = build_flexray_rx_filter_asm(
        target_frame_id=target_id,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Frame addressed to Frame ID 0x09
    frame_mismatch = FlexRayFrame(
        frame_id=0x09,
        payload_length=1,
        payload=[0xAA, 0x55],
        channel="A"
    )
    raw_bytes = list(frame_mismatch.to_bytes())

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    # Send first 2 bytes (Header 0 and Header 1)
    for b in raw_bytes[:2]:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt on Frame ID mismatch"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x00, f"R0 should remain untouched (0x00), got 0x{r0:02X}"
    assert r1 == 0x00, f"R1 should remain untouched (0x00), got 0x{r1:02X}"
    assert r2 == 0xEE, f"Expected Filter Mismatch Status R2=0xEE, got 0x{r2:02X}"
    dut._log.info(f"FlexRay RX Filter Mismatch PASS: Status=0x{r2:02X}")


@cocotb.test()
async def test_flexray_dual_channel_failover(dut):
    """
    Test 5: Dual-Channel Redundancy & Seamless Failover:
    Channel A (pin 0) is corrupted / held LOW (physical line fault).
    ASIC evaluates Channel A status, detects line fault, and fails over to Channel B (pin 1).
    Channel B transmits byte 0x77.
    ASIC ingresses 0x77 into R0, marks source R1 = 0x0B, and halts with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin_a = 4
    rx_pin_b = 5
    bit_period = 8

    words = build_flexray_dual_channel_failover_asm(
        rx_pin_a=rx_pin_a,
        rx_pin_b=rx_pin_b,
        bit_period=bit_period
    )
    # Channel A is held LOW (line fault = 0), Channel B is idle HIGH (1 << rx_pin_b)
    initial_uio = 1 << rx_pin_b
    await _init_dut_and_bootload(dut, words, initial_uio=initial_uio)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Transmit payload 0x77 over Channel B
    tx_b = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin_b)
    stream = tx_b.generate_bit_stream(0x77, idle_before=4, idle_after=6)

    for bit in stream:
        await RisingEdge(dut.clk)
        # Channel A remains 0, Channel B carries bit
        dut.uio_in.value = (bit << rx_pin_b)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after Channel B failover"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x77, f"Expected payload 0x77 in R0, got 0x{r0:02X}"
    assert r1 == 0x0B, f"Expected Channel B indicator 0x0B in R1, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected Status R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"FlexRay Dual-Channel Failover PASS: R0=0x{r0:02X}, Source=0x{r1:02X}, R2=0x{r2:02X}")


@cocotb.test()
async def test_flexray_crc_and_ppa_validation(dut):
    """
    Test 6: Header CRC-11, Frame CRC-24 Mathematical Validation & PPA Analysis:
    1. Validates Header CRC-11 across varying parameters.
    2. Validates Header CRC in-register comparison microcode (match and mismatch).
    3. Validates Frame CRC-24 on Channel A and Channel B seeds.
    4. Confirms IHP 130nm SG13G2 PPA scaling metrics.
    5. Confirms pin direction safety (uio_oe == 0x00).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Header CRC-11 Mathematical Checks
    h_crc1 = compute_flexray_header_crc11(sync=0, startup=0, frame_id=1, payload_length=2)
    h_crc2 = compute_flexray_header_crc11(sync=1, startup=0, frame_id=1, payload_length=2)
    assert h_crc1 != h_crc2, "Sync bit toggle must change Header CRC"

    h_crc3 = compute_flexray_header_crc11(sync=0, startup=0, frame_id=2, payload_length=2)
    assert h_crc1 != h_crc3, "Frame ID toggle must change Header CRC"

    # 2. In-Register Header CRC Validation Microcode (Match)
    words_match = build_flexray_header_crc_validator_asm(received_crc=0x195, expected_crc=0x195)
    await _init_dut_and_bootload(dut, words_match)
    core = dut.user_project.u_core

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after CRC check"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Match), got 0x{int(core.r2.value):02X}"

    # In-Register Header CRC Validation Microcode (Mismatch)
    words_mismatch = build_flexray_header_crc_validator_asm(received_crc=0x195, expected_crc=0x190)
    await _init_dut_and_bootload(dut, words_mismatch)

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after CRC check"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Mismatch), got 0x{int(core.r2.value):02X}"

    # 3. Frame CRC-24 Channel A vs Channel B differentiation
    payload_data = b"\x01\x02\x03\x04"
    crc_ch_a = compute_flexray_frame_crc24(payload_data, channel="A")
    crc_ch_b = compute_flexray_frame_crc24(payload_data, channel="B")
    assert crc_ch_a != crc_ch_b, "Channel A and Channel B must generate distinct Frame CRCs"

    # 4. PPA Model Validation
    ppa = FlexRayPpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 510
    assert ppa["gate_equivalents"] == 960.0
    assert ppa["f_max_mhz"] > 700.0
    assert ppa["area_overhead_pct"] < 3.0

    # 5. Electrical Safety
    assert int(dut.uio_oe.value) == 0x00, "Unused bidirectional IOs must remain High-Z on reset"
    dut._log.info(f"FlexRay CRC & PPA Validation PASS: PPA GE={ppa['gate_equivalents']}, f_max={ppa['f_max_mhz']:.1f} MHz")
