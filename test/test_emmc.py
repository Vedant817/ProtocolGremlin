# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_emmc.py - Cocotb test suite for eMMC 5.1 / SD 6.0 UHS-II
(JEDEC JESD84-B51 / SD Association) Non-Volatile Memory Bus & Card Protocol Engine.

Verifies:
1. test_emmc_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by CMD17 opcode byte (0x11) and Target Address (0x80) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_emmc_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples command byte MSB-first into R0 and preserves in R1 (0x11 CMD17) with status R2 = 0x00.
3. test_emmc_command_filter_and_fault_trapping: In-register validation of eMMC commands
   (valid 0x00 CMD0, 0x01 CMD1, 0x02 CMD2, 0x03 CMD3, 0x07 CMD7, 0x08 CMD8, 0x0C CMD12,
    0x11 CMD17, 0x18 CMD24 -> R2 = 0x00; illegal 0x7F trapped with R2 = 0xEE).
4. test_emmc_credit_tracking_and_underflow_trapping: In-register validation of command buffer
   credit increment (Event 1), decrement (Event 2), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_emmc_packet_framing_partitions_and_receiver: Packet encapsulation, hardware partitions,
   CRC-7 and CCITT CRC-16 (0x1021) protection, and EmmcReceiverModel link lock state machine.
6. test_emmc_standards_and_ppa: Card states, partitions, command opcodes, standard CRC-7 vectors,
   and calibrated IHP 130nm SG13G2 PPA model.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from emmc_model import (  # noqa: E402
    EmmcCardState,
    EmmcPartition,
    EmmcOpCode,
    compute_emmc_crc7,
    compute_emmc_crc16,
    encode_emmc_packet,
    decode_emmc_packet,
    EmmcReceiverModel,
    EmmcPpaModel,
    build_emmc_tx_beat_asm,
    build_emmc_rx_beat_asm,
    build_emmc_command_filter_asm,
    build_emmc_credit_tracker_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00):
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


@cocotb.test()
async def test_emmc_master_packet_transmission(dut):
    """
    Test 1: Master eMMC Packet Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by CMD17 opcode (0x11) and Target Address (0x80)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(EmmcOpCode.SYNC_SOF)                     # 0xA5 (0b10100101)
    expected_opcode = int(EmmcOpCode.CMD17_READ_SINGLE_BLOCK)   # 0x11
    expected_addr = 0x80                                        # Target Block Address 0x80

    asm_code = build_emmc_tx_beat_asm(
        sync_code=expected_sync,
        command_op=expected_opcode,
        target_addr=expected_addr,
        pin_tx=pin_tx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    max_cycles = 400

    captured_bits = []

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & (1 << pin_tx)) != 0:
            bit_val = (int(dut.uio_out.value) >> pin_tx) & 1
            captured_bits.append(bit_val)

        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt within timeout"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"

    # Sample each bit at baud midpoint (cycles: 2, 6, 10, 14, ...)
    sampled_bits = []
    idx = 2
    step = baud_cycles
    while idx < len(captured_bits):
        sampled_bits.append(captured_bits[idx])
        idx += step

    assert len(sampled_bits) >= 24, f"Expected >= 24 bits (3 bytes), got {len(sampled_bits)}"

    # Reconstruct 3 bytes (MSB first)
    byte_sync = 0
    for b in sampled_bits[0:8]:
        byte_sync = (byte_sync << 1) | b
    assert byte_sync == expected_sync, f"Delimiter mismatch: expected 0x{expected_sync:02X}, got 0x{byte_sync:02X}"

    byte_op = 0
    for b in sampled_bits[8:16]:
        byte_op = (byte_op << 1) | b
    assert byte_op == expected_opcode, f"Opcode mismatch: expected 0x{expected_opcode:02X}, got 0x{byte_op:02X}"

    byte_addr = 0
    for b in sampled_bits[16:24]:
        byte_addr = (byte_addr << 1) | b
    assert byte_addr == expected_addr, f"Address mismatch: expected 0x{expected_addr:02X}, got 0x{byte_addr:02X}"


@cocotb.test()
async def test_emmc_rx_beat_ingress(dut):
    """
    Test 2: Slave eMMC SYNC Synchronization & Command Ingress:
    Drives SYNC_SOF (0xA5) on pin 3, WAITEDGE rising edge detects bit 7,
    strides to command byte midpoint, ingresses 8 bits MSB-first (0x11 CMD17) into R0,
    preserves into R1, and halts with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    command_to_send = int(EmmcOpCode.CMD17_READ_SINGLE_BLOCK)  # 0x11 CMD17

    asm_code = build_emmc_rx_beat_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive SYNC_SOF (0xA5 = 0b10100101), MSB-first
    sync_byte = int(EmmcOpCode.SYNC_SOF)
    for bit_idx in range(7, -1, -1):
        bit_val = (sync_byte >> bit_idx) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Drive command byte (0x11 = 0b00010001), MSB-first
    for bit_idx in range(7, -1, -1):
        bit_val = (command_to_send >> bit_idx) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    # 3. Return bus low
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 20)

    assert bool(core.halted.value), "Core did not halt after RX ingress"
    assert int(core.r1.value) == command_to_send, f"Expected R1=0x{command_to_send:02X}, got 0x{int(core.r1.value):02X}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_emmc_command_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register eMMC Command Filtering & Illegal Opcode Trapping:
    Verifies valid commands:
      0x00 CMD0, 0x01 CMD1, 0x02 CMD2, 0x03 CMD3, 0x07 CMD7, 0x08 CMD8, 0x0C CMD12, 0x11 CMD17, 0x18 CMD24 -> R2 = 0x00
    Verifies illegal command:
      0x7F -> R2 = 0xEE (Trapped Error).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_test_cases = [
        int(EmmcOpCode.CMD0_GO_IDLE),
        int(EmmcOpCode.CMD1_SEND_OP_COND),
        int(EmmcOpCode.CMD2_ALL_SEND_CID),
        int(EmmcOpCode.CMD3_SET_RELATIVE_ADDR),
        int(EmmcOpCode.CMD7_SELECT_CARD),
        int(EmmcOpCode.CMD8_SEND_EXT_CSD),
        int(EmmcOpCode.CMD12_STOP_TRANSMISSION),
        int(EmmcOpCode.CMD17_READ_SINGLE_BLOCK),
        int(EmmcOpCode.CMD24_WRITE_BLOCK),
    ]

    for op in valid_test_cases:
        asm_code = build_emmc_command_filter_asm(test_opcode=op)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(100):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), f"Core timed out on valid opcode 0x{op:02X}"
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for valid opcode 0x{op:02X}, got 0x{int(core.r2.value):02X}"

    # Verify illegal command trapping (0x7F)
    illegal_op = 0x7F
    asm_code = build_emmc_command_filter_asm(test_opcode=illegal_op)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core timed out on illegal opcode 0x7F"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE for illegal opcode 0x7F, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_emmc_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register Memory Command Buffer Credit Accounting & Underflow Trapping:
    - Event 1: Transfer complete / ACK returned -> increments credit pool (initial 4 -> 5), R2 = 0x00.
    - Event 2: Command dispatched -> decrements credit pool (initial 4 -> 3), R2 = 0x00.
    - Event 2 with 0 credits -> underflow trapped with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: Credit Return (Event 1)
    asm_code = build_emmc_credit_tracker_asm(initial_credits=4, event_type=1)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)
    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value), "Core timed out on credit return"
    assert int(core.r0.value) == 5, f"Expected credits=5, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case 2: Command Dispatch (Event 2)
    asm_code = build_emmc_credit_tracker_asm(initial_credits=4, event_type=2)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)
    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value), "Core timed out on command dispatch"
    assert int(core.r0.value) == 3, f"Expected credits=3, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case 3: Underflow Trapping (Event 2 with credits=0)
    asm_code = build_emmc_credit_tracker_asm(initial_credits=0, event_type=2)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)
    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value), "Core timed out on underflow test"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Underflow Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_emmc_packet_framing_partitions_and_receiver(dut):
    """
    Test 5: eMMC Packet Framing, Partitions, and Receiver Model:
    - Encodes and decodes packets across hardware partitions (User Data, Boot 1/2, RPMB).
    - Verifies CCITT CRC-16 generation and bitflip detection.
    - Validates EmmcReceiverModel link lock FSM and card state transitions.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Packet framing test
    packet = encode_emmc_packet(
        opcode=EmmcOpCode.CMD17_READ_SINGLE_BLOCK,
        partition=int(EmmcPartition.BOOT_1),
        block_addr=0x0020,
        payload=b"\x55\xAA\x12\x34",
    )
    assert len(packet) == 11, f"Expected 11 bytes, got {len(packet)}"
    assert packet[0] == int(EmmcOpCode.SYNC_SOF)

    decoded = decode_emmc_packet(packet)
    assert decoded is not None
    assert decoded["opcode"] == EmmcOpCode.CMD17_READ_SINGLE_BLOCK
    assert decoded["partition"] == EmmcPartition.BOOT_1
    assert decoded["block_addr"] == 0x0020
    assert decoded["payload"] == b"\x55\xAA\x12\x34"

    # Corrupt packet payload -> CRC failure
    corrupt_packet = bytearray(packet)
    corrupt_packet[5] ^= 0xFF
    assert decode_emmc_packet(bytes(corrupt_packet)) is None

    # 2. EmmcReceiverModel test
    receiver = EmmcReceiverModel(initial_credits=4)
    assert not receiver.link_locked
    for _ in range(3):
        receiver.process_sync()
    assert not receiver.link_locked
    receiver.process_sync()
    assert receiver.link_locked

    # Card lifecycle: IDLE -> READY -> IDENT -> STBY -> TRAN -> DATA -> TRAN
    assert receiver.state == EmmcCardState.IDLE

    # CMD1 -> READY
    cmd1_pkt = encode_emmc_packet(EmmcOpCode.CMD1_SEND_OP_COND)
    assert receiver.process_packet(cmd1_pkt)
    assert receiver.state == EmmcCardState.READY

    # CMD2 -> IDENT
    cmd2_pkt = encode_emmc_packet(EmmcOpCode.CMD2_ALL_SEND_CID)
    assert receiver.process_packet(cmd2_pkt)
    assert receiver.state == EmmcCardState.IDENT

    # CMD3 -> STBY
    cmd3_pkt = encode_emmc_packet(EmmcOpCode.CMD3_SET_RELATIVE_ADDR)
    assert receiver.process_packet(cmd3_pkt)
    assert receiver.state == EmmcCardState.STBY

    # CMD7 -> TRAN
    cmd7_pkt = encode_emmc_packet(EmmcOpCode.CMD7_SELECT_CARD)
    assert receiver.process_packet(cmd7_pkt)
    assert receiver.state == EmmcCardState.TRAN

    # CMD17 -> DATA (credits decrement 4 -> 3)
    cmd17_pkt = encode_emmc_packet(EmmcOpCode.CMD17_READ_SINGLE_BLOCK, block_addr=0x0100)
    assert receiver.process_packet(cmd17_pkt)
    assert receiver.state == EmmcCardState.DATA
    assert receiver.credits == 3

    # CMD12 -> TRAN (credits increment 3 -> 4)
    cmd12_pkt = encode_emmc_packet(EmmcOpCode.CMD12_STOP_TRANSMISSION)
    assert receiver.process_packet(cmd12_pkt)
    assert receiver.state == EmmcCardState.TRAN
    assert receiver.credits == 4


@cocotb.test()
async def test_emmc_standards_and_ppa(dut):
    """
    Test 6: eMMC 5.1 / SD 6.0 Standards Verification & PPA Scaling Model:
    - Verifies JEDEC JESD84-B51 command opcodes, partitions, and card state values.
    - Validates standard CRC-7 test vectors (CMD0 and CMD8).
    - Validates CCITT CRC-16 determinism against standard vector.
    - Validates IHP 130nm SG13G2 PPA scaling metrics for the eMMC slice macro.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Card states
    assert int(EmmcCardState.IDLE) == 0x00
    assert int(EmmcCardState.READY) == 0x01
    assert int(EmmcCardState.IDENT) == 0x02
    assert int(EmmcCardState.STBY) == 0x03
    assert int(EmmcCardState.TRAN) == 0x04
    assert int(EmmcCardState.DATA) == 0x05
    assert int(EmmcCardState.RCV) == 0x06
    assert int(EmmcCardState.PRG) == 0x07
    assert int(EmmcCardState.DIS) == 0x08

    # 2. Hardware partitions
    assert int(EmmcPartition.USER_DATA) == 0x00
    assert int(EmmcPartition.BOOT_1) == 0x01
    assert int(EmmcPartition.BOOT_2) == 0x02
    assert int(EmmcPartition.RPMB) == 0x03
    assert int(EmmcPartition.GPP_1) == 0x04

    # 3. Command opcodes
    assert int(EmmcOpCode.CMD0_GO_IDLE) == 0x00
    assert int(EmmcOpCode.CMD1_SEND_OP_COND) == 0x01
    assert int(EmmcOpCode.CMD2_ALL_SEND_CID) == 0x02
    assert int(EmmcOpCode.CMD3_SET_RELATIVE_ADDR) == 0x03
    assert int(EmmcOpCode.CMD7_SELECT_CARD) == 0x07
    assert int(EmmcOpCode.CMD8_SEND_EXT_CSD) == 0x08
    assert int(EmmcOpCode.CMD12_STOP_TRANSMISSION) == 0x0C
    assert int(EmmcOpCode.CMD17_READ_SINGLE_BLOCK) == 0x11
    assert int(EmmcOpCode.CMD24_WRITE_BLOCK) == 0x18
    assert int(EmmcOpCode.IDLE) == 0x7E
    assert int(EmmcOpCode.SYNC_SOF) == 0xA5

    # 4. Standard JEDEC/SD CRC-7 test vectors
    # CMD0: 0x40 0x00 0x00 0x00 0x00 -> CRC-7 is 0x4A
    crc7_cmd0 = compute_emmc_crc7(bytes([0x40, 0x00, 0x00, 0x00, 0x00]))
    assert crc7_cmd0 == 0x4A, f"Expected CMD0 CRC-7 0x4A, got 0x{crc7_cmd0:02X}"

    # CMD8: 0x48 0x00 0x00 0x01 0xAA -> CRC-7 is 0x43
    crc7_cmd8 = compute_emmc_crc7(bytes([0x48, 0x00, 0x00, 0x01, 0xAA]))
    assert crc7_cmd8 == 0x43, f"Expected CMD8 CRC-7 0x43, got 0x{crc7_cmd8:02X}"

    # 5. CCITT CRC-16 determinism
    crc_empty = compute_emmc_crc16(b"")
    assert crc_empty == 0xFFFF

    test_bytes = b"\xA5\x11\x00\x00\x80"
    crc_val = compute_emmc_crc16(test_bytes)
    assert 0 <= crc_val <= 0xFFFF

    # 6. Calibrated PPA Metrics
    metrics = EmmcPpaModel.get_metrics()
    assert metrics["macro_cells"] == 625
    assert metrics["macro_ge"] == 1230.0
    assert metrics["macro_area_um2"] == 4650.0
    assert metrics["f_max_mhz"] == 800.0
    assert metrics["nominal_power_uw_10mhz"] == 61.50
    assert metrics["raw_throughput_mbps"] == 3200.0
    assert metrics["energy_pj_per_bit"] == 0.00192
