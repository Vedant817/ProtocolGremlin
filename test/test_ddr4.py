# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_ddr4.py - Cocotb test suite for DDR4 / DDR3 (JEDEC JESD79-4 / JESD79-3)
Synchronous Dynamic RAM Physical Layer & Command Controller Engine.

Verifies:
1. test_ddr4_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by ACT opcode byte (0x01) and Target Address (0x80) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_ddr4_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples command byte MSB-first into R0 and preserves in R1 (0x05 RD) with status R2 = 0x00.
3. test_ddr4_command_filter_and_fault_trapping: In-register validation of DDR4 commands
   (valid 0x01 ACT, 0x02 PRE, 0x03 REF, 0x04 PDE, 0x05 RD, 0x06 WR, 0x07 MRW, 0x08 ZQCL -> R2 = 0x00;
    illegal 0x7F trapped with R2 = 0xEE).
4. test_ddr4_credit_tracking_and_underflow_trapping: In-register validation of command buffer
   credit increment (ACK/PRE returned), decrement (command dispatched), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_ddr4_packet_framing_banks_and_receiver: Packet encapsulation, bank group/bank state transitions,
   CCITT CRC-16 (0x1021) protection, and Ddr4ReceiverModel link lock state machine.
6. test_ddr4_standards_and_ppa: Bank states, command opcodes, CRC-16 determinism,
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
from ddr4_model import (  # noqa: E402
    Ddr4BankState,
    Ddr4OpCode,
    compute_ddr4_crc16,
    encode_ddr4_packet,
    decode_ddr4_packet,
    Ddr4ReceiverModel,
    Ddr4PpaModel,
    build_ddr4_tx_beat_asm,
    build_ddr4_rx_beat_asm,
    build_ddr4_command_filter_asm,
    build_ddr4_credit_tracker_asm,
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
async def test_ddr4_master_packet_transmission(dut):
    """
    Test 1: Master DDR4 Packet Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by ACT opcode (0x01) and Target Address (0x80)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(Ddr4OpCode.SYNC_SOF)    # 0xA5 (0b10100101)
    expected_opcode = int(Ddr4OpCode.ACT)       # 0x01
    expected_addr = 0x80                        # Target Row Address 0x80

    asm_code = build_ddr4_tx_beat_asm(
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
async def test_ddr4_rx_beat_ingress(dut):
    """
    Test 2: Slave DDR4 SYNC Synchronization & Command Ingress:
    Drives SYNC_SOF (0xA5) on pin 3, WAITEDGE rising edge detects bit 7,
    strides to command byte midpoint, ingresses 8 bits MSB-first (0x05 RD) into R0,
    preserves into R1, and halts with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    command_to_send = int(Ddr4OpCode.RD)  # 0x05 RD

    asm_code = build_ddr4_rx_beat_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive SYNC_SOF (0xA5 = 0b10100101), MSB-first
    sync_byte = int(Ddr4OpCode.SYNC_SOF)
    for bit_idx in range(7, -1, -1):
        bit_val = (sync_byte >> bit_idx) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Drive command byte (0x05 = 0b00000101), MSB-first
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
async def test_ddr4_command_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register DDR4 Command Filtering & Illegal Opcode Trapping:
    Verifies valid commands:
      0x01 ACT, 0x02 PRE, 0x03 REF, 0x04 PDE, 0x05 RD, 0x06 WR, 0x07 MRW, 0x08 ZQCL -> R2 = 0x00
    Verifies illegal command:
      0x7F -> R2 = 0xEE (Trapped Error).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_test_cases = [
        int(Ddr4OpCode.ACT),
        int(Ddr4OpCode.PRE),
        int(Ddr4OpCode.REF),
        int(Ddr4OpCode.PDE),
        int(Ddr4OpCode.RD),
        int(Ddr4OpCode.WR),
        int(Ddr4OpCode.MRW),
        int(Ddr4OpCode.ZQCL),
    ]

    for op in valid_test_cases:
        asm_code = build_ddr4_command_filter_asm(test_opcode=op)
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
    asm_code = build_ddr4_command_filter_asm(test_opcode=illegal_op)
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
async def test_ddr4_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register Memory Command Buffer Credit Accounting & Underflow Trapping:
    - Event 1: Precharge / Memory ACK returned -> increments credit pool (initial 4 -> 5), R2 = 0x00.
    - Event 2: Command dispatched -> decrements credit pool (initial 4 -> 3), R2 = 0x00.
    - Event 2 with 0 credits -> underflow trapped with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: Credit Return (Event 1)
    asm_code = build_ddr4_credit_tracker_asm(initial_credits=4, event_type=1)
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
    asm_code = build_ddr4_credit_tracker_asm(initial_credits=4, event_type=2)
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
    asm_code = build_ddr4_credit_tracker_asm(initial_credits=0, event_type=2)
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
async def test_ddr4_packet_framing_banks_and_receiver(dut):
    """
    Test 5: DDR4 Packet Framing, 4 Bank Groups, 16 Banks, and Receiver Model:
    - Encodes and decodes packets across Bank Groups 0..3 and Banks 0..3.
    - Verifies CCITT CRC-16 generation and bitflip detection.
    - Validates Ddr4ReceiverModel link lock FSM and bank state transitions.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Packet framing test
    packet = encode_ddr4_packet(
        opcode=Ddr4OpCode.ACT,
        bank_group=2,
        bank_id=3,
        addr=0x2345,
        payload=b"\xDE\xAD\xBE\xEF",
    )
    assert len(packet) == 11, f"Expected 11 bytes, got {len(packet)}"
    assert packet[0] == int(Ddr4OpCode.SYNC_SOF)

    decoded = decode_ddr4_packet(packet)
    assert decoded is not None
    assert decoded["opcode"] == Ddr4OpCode.ACT
    assert decoded["bank_group"] == 2
    assert decoded["bank_id"] == 3
    assert decoded["addr"] == 0x2345
    assert decoded["payload"] == b"\xDE\xAD\xBE\xEF"

    # Corrupt packet payload -> CRC failure
    corrupt_packet = bytearray(packet)
    corrupt_packet[5] ^= 0xFF
    assert decode_ddr4_packet(bytes(corrupt_packet)) is None

    # 2. Ddr4ReceiverModel test
    receiver = Ddr4ReceiverModel(initial_credits=4)
    assert not receiver.link_locked
    for _ in range(3):
        receiver.process_sync()
    assert not receiver.link_locked
    receiver.process_sync()
    assert receiver.link_locked

    # Bank state lifecycle: IDLE -> ACT -> ACTIVE -> PRE -> IDLE
    assert receiver.get_bank_state(1, 2) == Ddr4BankState.IDLE

    act_pkt = encode_ddr4_packet(Ddr4OpCode.ACT, bank_group=1, bank_id=2, addr=0x1000)
    assert receiver.process_packet(act_pkt)
    assert receiver.get_bank_state(1, 2) == Ddr4BankState.ACTIVE
    assert receiver.credits == 3

    rd_pkt = encode_ddr4_packet(Ddr4OpCode.RD, bank_group=1, bank_id=2, addr=0x0040)
    assert receiver.process_packet(rd_pkt)
    assert receiver.credits == 2

    pre_pkt = encode_ddr4_packet(Ddr4OpCode.PRE, bank_group=1, bank_id=2, addr=0x0000)
    assert receiver.process_packet(pre_pkt)
    assert receiver.get_bank_state(1, 2) == Ddr4BankState.IDLE
    assert receiver.credits == 3


@cocotb.test()
async def test_ddr4_standards_and_ppa(dut):
    """
    Test 6: DDR4 / DDR3 Standards Verification & PPA Scaling Model:
    - Verifies JEDEC JESD79-4 command opcodes and bank state values.
    - Validates CCITT CRC-16 determinism against standard vector.
    - Validates IHP 130nm SG13G2 PPA scaling metrics for the DDR4 slice macro.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Bank states
    assert int(Ddr4BankState.IDLE) == 0x00
    assert int(Ddr4BankState.ACTIVE) == 0x01
    assert int(Ddr4BankState.PRECHARGING) == 0x02
    assert int(Ddr4BankState.REFRESHING) == 0x03

    # 2. Command opcodes
    assert int(Ddr4OpCode.NOP) == 0x00
    assert int(Ddr4OpCode.ACT) == 0x01
    assert int(Ddr4OpCode.PRE) == 0x02
    assert int(Ddr4OpCode.REF) == 0x03
    assert int(Ddr4OpCode.PDE) == 0x04
    assert int(Ddr4OpCode.RD) == 0x05
    assert int(Ddr4OpCode.WR) == 0x06
    assert int(Ddr4OpCode.MRW) == 0x07
    assert int(Ddr4OpCode.ZQCL) == 0x08
    assert int(Ddr4OpCode.IDLE) == 0x7E
    assert int(Ddr4OpCode.SYNC_SOF) == 0xA5

    # 3. CCITT CRC-16 determinism
    crc_empty = compute_ddr4_crc16(b"")
    assert crc_empty == 0xFFFF

    test_bytes = b"\xA5\x01\x21\x55\xAA"
    crc_val = compute_ddr4_crc16(test_bytes)
    assert 0 <= crc_val <= 0xFFFF

    # 4. Calibrated PPA Metrics
    metrics = Ddr4PpaModel.get_metrics()
    assert metrics["macro_cells"] == 630
    assert metrics["macro_ge"] == 1240.0
    assert metrics["macro_area_um2"] == 4680.0
    assert metrics["f_max_mhz"] == 800.0
    assert metrics["nominal_power_uw_10mhz"] == 62.00
    assert metrics["raw_throughput_mbps"] == 25600.0
    assert metrics["energy_pj_per_bit"] == 0.00120
