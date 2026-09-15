# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for CAN 2.0A Controller Physical-Layer Protocol Engine.

Verifies:
1. CAN TX Standard Frame Transmission & ACK Reception:
   - ASIC transmits standard 11-bit ID frame (0x123) with data payload (0xA5).
   - External receiver model verifies bit stuffing and CRC-15.
   - External receiver asserts Dominant (0) on ACK slot.
   - Core confirms ACK and halts with R2 = 0x00.
2. CAN TX Arbitration Collision & Loss Detection:
   - ASIC transmits frame ID 0x123.
   - Competing node transmits higher priority frame ID 0x120 (pulls ID bit 1 dominant).
   - ASIC detects arbitration loss via GRD, releases bus, and halts with R2 = 0xAA.
3. CAN TX Missing ACK Error Handling:
   - ASIC transmits frame with no receiver acknowledging.
   - ASIC detects missing ACK and halts with R2 = 0xAE (ACK error).
4. CAN RX Standard Frame Reception & ACK Assertion:
   - External transmitter sends CAN frame (ID 0x555, Data 0x3C).
   - ASIC synchronizes via WAITEDGE, samples data into R0, and asserts Dominant on ACK slot.
   - Verifies R0 = 0x3C, R2 = 0x00, and ASIC driven ACK pulse.
5. CAN Open-Drain Electrical Safety:
   - Verifies hardware open-drain mode (GODRI) eliminates bus contention.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from can_model import (  # noqa: E402
    CanReceiverModel,
    build_can_frame_bits,
    build_can_tx_asm,
    build_can_rx_asm,
)

PIN = 4
BIT_PERIOD = 8


async def _init_dut_and_bootload(dut, words: list[int]):
    """Fresh hardware reset and bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = (1 << PIN)  # Open-drain bus pulled high by default
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = (1 << PIN)


@cocotb.test()
async def test_can_tx_standard_frame(dut):
    """Verify standard CAN 2.0A frame transmission, bit stuffing, CRC-15, and ACK reception."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    can_id = 0x123
    data_byte = 0xA5
    dut._log.info(f"Testing CAN TX: ID=0x{can_id:03X}, Data=0x{data_byte:02X}")

    asm = build_can_tx_asm(can_id=can_id, data_byte=data_byte, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    stuffed_bits, crc15, _ = build_can_frame_bits(can_id, [data_byte])
    total_stuffed = len(stuffed_bits)

    # Frame timing: SOF begins after idle settling (cycle ~4).
    # Sample points: each bit cell is BIT_PERIOD cycles.
    sampled_bits = []
    bit_count = 0
    in_frame = False
    sof_cycle = -1

    for cycle in range(1200):
        await FallingEdge(dut.clk)

        # Bus resolution: open-drain wired-AND
        asic_out = (int(dut.uio_out.value) >> PIN) & 1 if dut.uio_out.value.is_resolvable else 1
        asic_oe = (int(dut.uio_oe.value) >> PIN) & 1 if dut.uio_oe.value.is_resolvable else 0
        asic_level = 0 if (asic_oe and asic_out == 0) else 1

        # Check for SOF: bus goes 1 -> 0
        if not in_frame and asic_level == 0:
            in_frame = True
            sof_cycle = cycle
            dut._log.info(f"SOF detected at cycle {cycle}")

        # In frame: sample each bit at 62.5% (cycle 5 of 8)
        ext_drive = 1
        if in_frame:
            elapsed = cycle - sof_cycle
            bit_idx = elapsed // BIT_PERIOD
            phase = elapsed % BIT_PERIOD

            # Check if we are at the ACK slot (after total_stuffed bits + 1 CRC delimiter)
            # Total pre-ACK bits = total_stuffed + 1. ACK slot is index total_stuffed + 1.
            ack_slot_idx = total_stuffed + 1
            if bit_idx == ack_slot_idx:
                # External receiver asserts Dominant (0) on ACK slot
                ext_drive = 0

            # Sample at phase == 5
            if phase == 5 and bit_idx < ack_slot_idx:
                sampled_bits.append(asic_level)

        actual_bus = asic_level & ext_drive
        dut.uio_in.value = (actual_bus << PIN)

        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("CAN TX test timed out waiting for HALT")

    r2 = int(core.r2.value)
    dut._log.info(f"CAN TX finished: R2 = 0x{r2:02X} (sampled {len(sampled_bits)} bits)")
    assert r2 == 0x00, f"CAN TX failed with error status R2 = 0x{r2:02X} (expected 0x00 SUCCESS)"

    # Decode sampled bitstream with CanReceiverModel
    receiver = CanReceiverModel(bit_period=BIT_PERIOD, pin=PIN)
    dec_id, dec_data, valid = receiver.decode_stream(sampled_bits)

    dut._log.info(f"Decoded: ID=0x{dec_id:03X}, Data={[hex(b) for b in dec_data]} (valid={valid})")
    assert valid, "CAN bitstream failed destuffing / framing validation"
    assert dec_id == can_id, f"Decoded ID 0x{dec_id:03X} != expected 0x{can_id:03X}"
    assert dec_data == [data_byte], f"Decoded data {dec_data} != expected {[data_byte]}"


@cocotb.test()
async def test_can_tx_arbitration_loss(dut):
    """Verify CAN bitwise arbitration: node detects dominant collision on recessive bit and aborts."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # ASIC transmits ID 0x123 (0b00100100011)
    # Competing node transmits higher-priority ID 0x120 (0b00100100000)
    # Collision will occur on ID bit 1 (where ASIC sends 1, competitor sends 0)
    can_id = 0x123
    data_byte = 0x42
    dut._log.info("Testing CAN Arbitration Loss Detection")

    asm = build_can_tx_asm(can_id=can_id, data_byte=data_byte, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core

    in_frame = False
    sof_cycle = -1

    for cycle in range(500):
        await FallingEdge(dut.clk)

        asic_out = (int(dut.uio_out.value) >> PIN) & 1 if dut.uio_out.value.is_resolvable else 1
        asic_oe = (int(dut.uio_oe.value) >> PIN) & 1 if dut.uio_oe.value.is_resolvable else 0
        asic_level = 0 if (asic_oe and asic_out == 0) else 1

        if not in_frame and asic_level == 0:
            in_frame = True
            sof_cycle = cycle

        ext_drive = 1
        if in_frame:
            elapsed = cycle - sof_cycle
            bit_idx = elapsed // BIT_PERIOD
            # In ID 0x123 (binary 001 0010 0011):
            # ID bit 1 is at bit_idx = 10 (SOF is 0, ID10..ID0 are 1..11)
            # ID bit 1 is recessive (1) in ASIC frame. Competitor pulls dominant (0).
            if bit_idx == 10:
                ext_drive = 0  # Competitor forces Dominant

        actual_bus = asic_level & ext_drive
        dut.uio_in.value = (actual_bus << PIN)

        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("CAN arbitration test timed out")

    r2 = int(core.r2.value)
    dut._log.info(f"Arbitration test finished: R2 = 0x{r2:02X}")
    assert r2 == 0xAA, f"Expected R2 = 0xAA (Arbitration Lost), got 0x{r2:02X}"


@cocotb.test()
async def test_can_tx_no_ack_error(dut):
    """Verify CAN transmitter detects missing ACK and flags error R2 = 0xAE."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    can_id = 0x345
    data_byte = 0x55
    dut._log.info("Testing CAN Missing ACK Detection")

    asm = build_can_tx_asm(can_id=can_id, data_byte=data_byte, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core

    # No external receiver: bus remains purely driven by ASIC (floating high on ACK slot)
    for cycle in range(1200):
        await FallingEdge(dut.clk)
        asic_out = (int(dut.uio_out.value) >> PIN) & 1 if dut.uio_out.value.is_resolvable else 1
        asic_oe = (int(dut.uio_oe.value) >> PIN) & 1 if dut.uio_oe.value.is_resolvable else 0
        asic_level = 0 if (asic_oe and asic_out == 0) else 1

        dut.uio_in.value = (asic_level << PIN)

        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("CAN no-ack test timed out")

    r2 = int(core.r2.value)
    dut._log.info(f"Missing ACK test finished: R2 = 0x{r2:02X}")
    assert r2 == 0xAE, f"Expected R2 = 0xAE (ACK Error), got 0x{r2:02X}"


@cocotb.test()
async def test_can_rx_standard_frame(dut):
    """Verify CAN receiver: synchronizes on SOF, extracts payload data into R0, and asserts ACK."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    can_id = 0x555
    data_byte = 0x3C
    dut._log.info(f"Testing CAN RX: ID=0x{can_id:03X}, Data=0x{data_byte:02X}")

    asm = build_can_rx_asm(can_id=can_id, data_byte=data_byte, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    stuffed_bits, crc15, post_ack = build_can_frame_bits(can_id, [data_byte])

    # Full transmit sequence: SOF through CRC15 + CRC Delimiter (1) + ACK Slot (1) + EOF (7)
    full_stream = list(stuffed_bits) + [1]  # + CRC delimiter
    ack_slot_index = len(full_stream)
    full_stream.append(1)  # ACK slot (transmitter sends recessive 1)
    full_stream.extend([1] * 8)  # ACK delimiter + EOF

    ack_asserted_by_asic = False

    # CAN Bus Idle state: hold recessive (1) for 16 cycles to allow core to enter WAITEDGE
    for _ in range(16):
        await FallingEdge(dut.clk)
        dut.uio_in.value = (1 << PIN)
        await RisingEdge(dut.clk)

    # External transmitter drives full_stream
    for bit_idx, bit_val in enumerate(full_stream):
        for sub_cycle in range(BIT_PERIOD):
            await FallingEdge(dut.clk)

            # Read ASIC output
            asic_out = (int(dut.uio_out.value) >> PIN) & 1 if dut.uio_out.value.is_resolvable else 1
            asic_oe = (int(dut.uio_oe.value) >> PIN) & 1 if dut.uio_oe.value.is_resolvable else 0
            asic_level = 0 if (asic_oe and asic_out == 0) else 1

            if bit_idx == ack_slot_index and asic_level == 0:
                ack_asserted_by_asic = True

            # Bus is wired-AND of external transmitter and ASIC
            actual_bus = bit_val & asic_level
            dut.uio_in.value = (actual_bus << PIN)

            await RisingEdge(dut.clk)

    # Wait for ASIC to halt
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    r0 = int(core.r0.value)
    r2 = int(core.r2.value)
    dut._log.info(f"CAN RX finished: R0 = 0x{r0:02X}, R2 = 0x{r2:02X}, ACK asserted = {ack_asserted_by_asic}")
    assert r0 == data_byte, f"CAN RX data mismatch: R0 = 0x{r0:02X} != expected 0x{data_byte:02X}"
    assert r2 == 0x00, f"CAN RX status mismatch: R2 = 0x{r2:02X} != expected 0x00"
    assert ack_asserted_by_asic, "ASIC failed to assert Dominant (0) on CAN ACK slot"


@cocotb.test()
async def test_can_open_drain_safety(dut):
    """Verify CAN open-drain configuration prevents active drive-high contention."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_can_tx_asm(can_id=0x100, data_byte=0x00, bit_period=BIT_PERIOD, pin=PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core

    # External bus is pulled down to 0 for entire test
    dut.uio_in.value = 0

    for cycle in range(50):
        await FallingEdge(dut.clk)
        await ReadOnly()
        # In open-drain mode (GODRI), pin_out must be 0 whenever pin_oe is 1
        uio_out = int(dut.uio_out.value)
        uio_oe = int(dut.uio_oe.value)

        oe_bit = (uio_oe >> PIN) & 1
        out_bit = (uio_out >> PIN) & 1

        # Zero electrical contention invariant:
        # If output is enabled, out_bit MUST NOT be 1 (can only pull low, never high)
        if oe_bit:
            assert out_bit == 0, f"Cycle {cycle}: Contention violation! pin_oe=1 and pin_out=1 in open-drain mode"

        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
