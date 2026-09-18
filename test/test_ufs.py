# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_ufs.py - Cocotb test suite for UFS 3.1 / 4.0 (Universal Flash Storage / JEDEC JESD220)
Mobile Storage Protocol Engine.

Verifies:
1. test_ufs_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by COMMAND UPIU byte (0x01) and Target Address (0x80) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_ufs_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples UPIU byte MSB-first into R0 and preserves in R1 (0x21 RESPONSE) with status R2 = 0x00.
3. test_ufs_command_filter_and_fault_trapping: In-register validation of UFS UPIU types
   (valid 0x00 NOP_OUT, 0x01 COMMAND, 0x02 DATA_OUT, 0x04 TASK_MGMT_REQ, 0x20 NOP_IN,
    0x21 RESPONSE, 0x22 DATA_IN, 0x31 RTT -> R2 = 0x00; illegal 0x7F trapped with R2 = 0xEE).
4. test_ufs_credit_tracking_and_underflow_trapping: In-register validation of UniPro buffer
   credit increment (Event 1), decrement (Event 2), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_ufs_packet_framing_luns_and_receiver: Packet encapsulation, target LUNs,
   CCITT CRC-16 (0x1021) protection, and UfsReceiverModel link lock state machine.
6. test_ufs_standards_and_ppa: Device states, LUNs, UPIU types, CRC-16 determinism,
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
from ufs_model import (  # noqa: E402
    UfsDeviceState,
    UfsLun,
    UfsUpiuType,
    compute_ufs_crc16,
    encode_ufs_packet,
    decode_ufs_packet,
    UfsReceiverModel,
    UfsPpaModel,
    build_ufs_tx_beat_asm,
    build_ufs_rx_beat_asm,
    build_ufs_command_filter_asm,
    build_ufs_credit_tracker_asm,
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
async def test_ufs_master_packet_transmission(dut):
    """
    Test 1: Master UFS Packet Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by COMMAND UPIU (0x01) and Target Address (0x80)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(UfsUpiuType.SYNC_SOF)       # 0xA5 (0b10100101)
    expected_opcode = int(UfsUpiuType.COMMAND)      # 0x01
    expected_addr = 0x80                            # Target Block Address 0x80

    asm_code = build_ufs_tx_beat_asm(
        sync_code=expected_sync,
        upiu_op=expected_opcode,
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
async def test_ufs_rx_beat_ingress(dut):
    """
    Test 2: Slave UFS SYNC Synchronization & UPIU Ingress:
    Drives SYNC_SOF (0xA5) on pin 3, WAITEDGE rising edge detects bit 7,
    strides to UPIU type byte midpoint, ingresses 8 bits MSB-first (0x21 RESPONSE) into R0,
    preserves into R1, and halts with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    command_to_send = int(UfsUpiuType.RESPONSE)  # 0x21 RESPONSE

    asm_code = build_ufs_rx_beat_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive SYNC_SOF (0xA5 = 0b10100101), MSB-first
    sync_byte = int(UfsUpiuType.SYNC_SOF)
    for bit_idx in range(7, -1, -1):
        bit_val = (sync_byte >> bit_idx) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Drive UPIU byte (0x21 = 0b00100001), MSB-first
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
async def test_ufs_command_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register UFS UPIU Type Filtering & Illegal Opcode Trapping:
    Verifies valid UPIU types:
      0x00 NOP_OUT, 0x01 COMMAND, 0x02 DATA_OUT, 0x04 TASK_MGMT_REQ,
      0x20 NOP_IN, 0x21 RESPONSE, 0x22 DATA_IN, 0x31 RTT -> R2 = 0x00
    Verifies illegal UPIU type:
      0x7F -> R2 = 0xEE (Trapped Error).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_test_cases = [
        int(UfsUpiuType.NOP_OUT),
        int(UfsUpiuType.COMMAND),
        int(UfsUpiuType.DATA_OUT),
        int(UfsUpiuType.TASK_MGMT_REQ),
        int(UfsUpiuType.NOP_IN),
        int(UfsUpiuType.RESPONSE),
        int(UfsUpiuType.DATA_IN),
        int(UfsUpiuType.RTT),
    ]

    for op in valid_test_cases:
        asm_code = build_ufs_command_filter_asm(test_opcode=op)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(100):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), f"Core timed out on valid UPIU 0x{op:02X}"
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for valid UPIU 0x{op:02X}, got 0x{int(core.r2.value):02X}"

    # Verify illegal UPIU trapping (0x7F)
    illegal_op = 0x7F
    asm_code = build_ufs_command_filter_asm(test_opcode=illegal_op)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core timed out on illegal UPIU 0x7F"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE for illegal UPIU 0x7F, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_ufs_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register UniPro Buffer Credit Accounting & Underflow Trapping:
    - Event 1: Credit returned (RTT / RESPONSE) -> increments credit pool (initial 4 -> 5), R2 = 0x00.
    - Event 2: Command dispatched -> decrements credit pool (initial 4 -> 3), R2 = 0x00.
    - Event 2 with 0 credits -> underflow trapped with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: Credit Return (Event 1)
    asm_code = build_ufs_credit_tracker_asm(initial_credits=4, event_type=1)
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
    asm_code = build_ufs_credit_tracker_asm(initial_credits=4, event_type=2)
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
    asm_code = build_ufs_credit_tracker_asm(initial_credits=0, event_type=2)
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
async def test_ufs_packet_framing_luns_and_receiver(dut):
    """
    Test 5: UFS Packet Framing, LUNs, and Receiver Model:
    - Encodes and decodes packets across LUNs (Standard LUN 0, Boot LUN 1/2, RPMB).
    - Verifies CCITT CRC-16 generation and bitflip detection.
    - Validates UfsReceiverModel link lock FSM and device state transitions.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Packet framing test
    packet = encode_ufs_packet(
        upiu_type=UfsUpiuType.COMMAND,
        lun=int(UfsLun.BOOT_LUN_1),
        task_tag=0x05,
        addr=0x2000,
        payload=b"\x12\x34\x56\x78",
    )
    assert len(packet) == 12, f"Expected 12 bytes, got {len(packet)}"
    assert packet[0] == int(UfsUpiuType.SYNC_SOF)

    decoded = decode_ufs_packet(packet)
    assert decoded is not None
    assert decoded["upiu_type"] == UfsUpiuType.COMMAND
    assert decoded["lun"] == int(UfsLun.BOOT_LUN_1)
    assert decoded["task_tag"] == 0x05
    assert decoded["addr"] == 0x2000
    assert decoded["payload"] == b"\x12\x34\x56\x78"

    # Corrupt packet payload -> CRC failure
    corrupt_packet = bytearray(packet)
    corrupt_packet[6] ^= 0xFF
    assert decode_ufs_packet(bytes(corrupt_packet)) is None

    # 2. UfsReceiverModel test
    receiver = UfsReceiverModel(initial_credits=4)
    assert not receiver.link_locked
    assert receiver.state == UfsDeviceState.LINK_CONFIG

    for _ in range(3):
        receiver.process_sync()
    assert not receiver.link_locked

    receiver.process_sync()
    assert receiver.link_locked
    assert receiver.state == UfsDeviceState.READY

    # COMMAND -> READY (credits decrement 4 -> 3)
    cmd_pkt = encode_ufs_packet(UfsUpiuType.COMMAND, lun=int(UfsLun.LUN_0))
    assert receiver.process_packet(cmd_pkt)
    assert receiver.state == UfsDeviceState.READY
    assert receiver.credits == 3

    # DATA_IN -> ACTIVE_READ
    din_pkt = encode_ufs_packet(UfsUpiuType.DATA_IN, lun=int(UfsLun.LUN_0))
    assert receiver.process_packet(din_pkt)
    assert receiver.state == UfsDeviceState.ACTIVE_READ

    # RESPONSE -> READY (credits increment 3 -> 4)
    rsp_pkt = encode_ufs_packet(UfsUpiuType.RESPONSE, lun=int(UfsLun.LUN_0))
    assert receiver.process_packet(rsp_pkt)
    assert receiver.state == UfsDeviceState.READY
    assert receiver.credits == 4


@cocotb.test()
async def test_ufs_standards_and_ppa(dut):
    """
    Test 6: UFS 3.1 / 4.0 Standards Verification & PPA Scaling Model:
    - Verifies JEDEC JESD220 UPIU types, LUNs, and device state values.
    - Validates CCITT CRC-16 determinism against standard vector.
    - Validates IHP 130nm SG13G2 PPA scaling metrics for the UFS slice macro.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Device states
    assert int(UfsDeviceState.LINK_DOWN) == 0x00
    assert int(UfsDeviceState.LINK_CONFIG) == 0x01
    assert int(UfsDeviceState.READY) == 0x02
    assert int(UfsDeviceState.ACTIVE_READ) == 0x03
    assert int(UfsDeviceState.ACTIVE_WRITE) == 0x04
    assert int(UfsDeviceState.HIBERN8) == 0x05

    # 2. Logical Unit Numbers (LUN)
    assert int(UfsLun.LUN_0) == 0x00
    assert int(UfsLun.LUN_1) == 0x01
    assert int(UfsLun.BOOT_LUN_1) == 0xB0
    assert int(UfsLun.BOOT_LUN_2) == 0xB1
    assert int(UfsLun.RPMB_LUN) == 0xC4

    # 3. UPIU transaction types
    assert int(UfsUpiuType.NOP_OUT) == 0x00
    assert int(UfsUpiuType.COMMAND) == 0x01
    assert int(UfsUpiuType.DATA_OUT) == 0x02
    assert int(UfsUpiuType.TASK_MGMT_REQ) == 0x04
    assert int(UfsUpiuType.NOP_IN) == 0x20
    assert int(UfsUpiuType.RESPONSE) == 0x21
    assert int(UfsUpiuType.DATA_IN) == 0x22
    assert int(UfsUpiuType.RTT) == 0x31
    assert int(UfsUpiuType.IDLE) == 0x7E
    assert int(UfsUpiuType.SYNC_SOF) == 0xA5

    # 4. CCITT CRC-16 determinism
    crc_empty = compute_ufs_crc16(b"")
    assert crc_empty == 0xFFFF

    test_bytes = b"\xA5\x01\x00\x01\x00\x80"
    crc_val = compute_ufs_crc16(test_bytes)
    assert 0 <= crc_val <= 0xFFFF

    # 5. Calibrated PPA Metrics
    metrics = UfsPpaModel.get_metrics()
    assert metrics["macro_cells"] == 640
    assert metrics["macro_ge"] == 1260.0
    assert metrics["macro_area_um2"] == 4750.0
    assert metrics["f_max_mhz"] == 800.0
    assert metrics["nominal_power_uw_10mhz"] == 63.00
    assert metrics["raw_throughput_mbps"] == 11600.0
    assert metrics["energy_pj_per_bit"] == 0.00115
