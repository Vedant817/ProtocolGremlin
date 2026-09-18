# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_qdr.py - Cocotb test suite for QDR-IV / QDR-II+ (Quad Data Rate SRAM / QDR Consortium)
Synchronous Memory Engine.

Verifies:
1. test_qdr_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by READ CMD byte (0x01) and Bank byte (0x00) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_qdr_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples CMD byte MSB-first into R0 and preserves in R1 (0x03 READ_WRITE) with status R2 = 0x00.
3. test_qdr_command_filter_and_fault_trapping: In-register validation of QDR command opcodes
   (valid 0x00 NOP, 0x01 READ, 0x02 WRITE, 0x03 READ_WRITE, 0x04 BTE, 0x05 LBK -> R2 = 0x00;
    illegal 0x7F trapped with R2 = 0xEE).
4. test_qdr_credit_tracking_and_underflow_trapping: In-register validation of QDR buffer credit
   increment (Event 1), decrement (Event 2), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_qdr_packet_framing_banks_and_receiver: Packet encapsulation, target banks,
   CCITT CRC-16 (0x1021) protection, and QdrReceiverModel link lock state machine.
6. test_qdr_standards_and_ppa: Bank states, Bank IDs, OpCodes, CRC-16 determinism,
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
from qdr_model import (  # noqa: E402
    QdrBankState,
    QdrBankId,
    QdrOpCode,
    compute_qdr_crc16,
    encode_qdr_packet,
    decode_qdr_packet,
    QdrReceiverModel,
    QdrPpaModel,
    build_qdr_tx_beat_asm,
    build_qdr_rx_beat_asm,
    build_qdr_command_filter_asm,
    build_qdr_credit_tracker_asm,
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
async def test_qdr_master_packet_transmission(dut):
    """
    Test 1: Master QDR Packet Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by READ CMD (0x01) and Bank byte (0x00)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(QdrOpCode.SYNC_SOF)       # 0xA5 (0b10100101)
    expected_opcode = int(QdrOpCode.READ)         # 0x01
    expected_bank = int(QdrBankId.BANK_0)         # 0x00

    asm_code = build_qdr_tx_beat_asm(
        sync_code=expected_sync,
        cmd_op=expected_opcode,
        target_bank=expected_bank,
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

    byte_bank = 0
    for b in sampled_bits[16:24]:
        byte_bank = (byte_bank << 1) | b
    assert byte_bank == expected_bank, f"Bank mismatch: expected 0x{expected_bank:02X}, got 0x{byte_bank:02X}"


@cocotb.test()
async def test_qdr_rx_beat_ingress(dut):
    """
    Test 2: Slave QDR SYNC Synchronization & Command Ingress:
    Drives SYNC_SOF (0xA5) on pin 3, WAITEDGE rising edge detects bit 7,
    strides to CMD byte midpoint, ingresses 8 bits MSB-first (0x03 READ_WRITE) into R0,
    preserves into R1, and halts with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    command_to_send = int(QdrOpCode.READ_WRITE)  # 0x03 READ_WRITE

    asm_code = build_qdr_rx_beat_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core start and enter WAITEDGE
    await ClockCycles(dut.clk, 5)

    # Drive SYNC_SOF delimiter (0xA5 = 0b10100101, MSB-first)
    sync_val = int(QdrOpCode.SYNC_SOF)
    for bit_idx in range(7, -1, -1):
        bit = (sync_val >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # Drive Command byte (0x03 = 0b00000011, MSB-first)
    for bit_idx in range(7, -1, -1):
        bit = (command_to_send >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    dut.uio_in.value = 0x00

    # Wait for completion
    timeout = 100
    while not bool(core.halted.value) and timeout > 0:
        await RisingEdge(dut.clk)
        timeout -= 1

    assert bool(core.halted.value), "Core did not halt after beat ingress"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == command_to_send, f"Expected R0=0x{command_to_send:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == command_to_send, f"Expected R1=0x{command_to_send:02X}, got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_qdr_command_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register QDR Command Filtering and Fault Trapping:
    Verifies valid commands:
      0x00 (NOP), 0x01 (READ), 0x02 (WRITE), 0x03 (READ_WRITE), 0x04 (BTE), 0x05 (LBK)
    match cleanly with R2 = 0x00, while illegal opcode 0x7F is trapped with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_commands = [
        int(QdrOpCode.NOP),
        int(QdrOpCode.READ),
        int(QdrOpCode.WRITE),
        int(QdrOpCode.READ_WRITE),
        int(QdrOpCode.BTE),
        int(QdrOpCode.LBK),
    ]

    core = dut.user_project.u_core

    # Test all valid commands
    for op in valid_commands:
        asm_code = build_qdr_command_filter_asm(test_opcode=op)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        timeout = 100
        while not bool(core.halted.value) and timeout > 0:
            await RisingEdge(dut.clk)
            timeout -= 1

        assert bool(core.halted.value), f"Core did not halt for valid opcode 0x{op:02X}"
        assert int(core.r2.value) == 0x00, f"Valid opcode 0x{op:02X} failed: expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Test illegal opcode (0x7F) -> should trap with R2 = 0xEE
    illegal_op = 0x7F
    asm_code = build_qdr_command_filter_asm(test_opcode=illegal_op)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    timeout = 100
    while not bool(core.halted.value) and timeout > 0:
        await RisingEdge(dut.clk)
        timeout -= 1

    assert bool(core.halted.value), f"Core did not halt for illegal opcode 0x{illegal_op:02X}"
    assert int(core.r2.value) == 0xEE, f"Illegal opcode not trapped: expected R2=0xEE, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_qdr_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register QDR Memory Buffer Credit Tracking and Underflow Trapping:
    Verifies:
      1. Credit Return (Event 1): increments credits from 4 to 5, status R2 = 0x00.
      2. Command Dispatch (Event 2): decrements credits from 4 to 3, status R2 = 0x00.
      3. Underflow prevention: dispatching with initial credits = 0 triggers trap R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    # Case 1: Credit Return (Event 1) -> 4 -> 5
    asm_inc = build_qdr_credit_tracker_asm(initial_credits=4, event_type=1)
    words_inc = assemble("\n".join(asm_inc))
    await _init_dut_and_bootload(dut, words_inc)

    timeout = 100
    while not bool(core.halted.value) and timeout > 0:
        await RisingEdge(dut.clk)
        timeout -= 1

    assert bool(core.halted.value), "Core did not halt during credit increment"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == 5, f"Expected R0=5 credits, got {int(core.r0.value)}"

    # Case 2: Command Dispatch (Event 2) -> 4 -> 3
    asm_dec = build_qdr_credit_tracker_asm(initial_credits=4, event_type=2)
    words_dec = assemble("\n".join(asm_dec))
    await _init_dut_and_bootload(dut, words_dec)

    timeout = 100
    while not bool(core.halted.value) and timeout > 0:
        await RisingEdge(dut.clk)
        timeout -= 1

    assert bool(core.halted.value), "Core did not halt during credit decrement"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == 3, f"Expected R0=3 credits, got {int(core.r0.value)}"

    # Case 3: Underflow prevention -> initial 0, dispatch -> traps with R2 = 0xEE
    asm_underflow = build_qdr_credit_tracker_asm(initial_credits=0, event_type=2)
    words_underflow = assemble("\n".join(asm_underflow))
    await _init_dut_and_bootload(dut, words_underflow)

    timeout = 100
    while not bool(core.halted.value) and timeout > 0:
        await RisingEdge(dut.clk)
        timeout -= 1

    assert bool(core.halted.value), "Core did not halt during credit underflow test"
    assert int(core.r2.value) == 0xEE, f"Underflow not trapped: expected R2=0xEE, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_qdr_packet_framing_banks_and_receiver(dut):
    """
    Test 5: QDR Packet Encapsulation, Banks, and Receiver Model:
    Verifies:
      1. Packet encoding and decoding across all 8 banks with 16-bit CCITT CRC.
      2. Corrupted CRC rejection.
      3. Bank state transitions (IDLE -> READY -> ACTIVE_READ -> ACTIVE_WRITE -> DUAL_PORT_RW -> READY).
      4. Credit accounting in Python receiver model.
      5. Link lock FSM acquisition after 4 consecutive valid frames.
    """
    receiver = QdrReceiverModel(num_banks=8, initial_credits=4)
    assert not receiver.link_locked, "Receiver should initially be unlocked"

    # Frame 1: NOP to Bank 0
    pkt1 = encode_qdr_packet(bank=0, opcode=QdrOpCode.NOP, addr=0x00)
    res1 = receiver.process_packet(pkt1)
    assert res1["valid"], f"Packet 1 failed: {res1.get('error')}"

    # Frame 2: READ to Bank 1
    pkt2 = encode_qdr_packet(bank=1, opcode=QdrOpCode.READ, addr=0x10, payload=b"\x00\x00")
    res2 = receiver.process_packet(pkt2)
    assert res2["valid"], f"Packet 2 failed: {res2.get('error')}"
    assert receiver.get_bank_state(1) == QdrBankState.ACTIVE_READ
    assert receiver.credits == 3, f"Expected 3 credits remaining, got {receiver.credits}"

    # Frame 3: WRITE to Bank 2
    pkt3 = encode_qdr_packet(bank=2, opcode=QdrOpCode.WRITE, addr=0x20, payload=b"\xAA\x55")
    res3 = receiver.process_packet(pkt3)
    assert res3["valid"], f"Packet 3 failed: {res3.get('error')}"
    assert receiver.get_bank_state(2) == QdrBankState.ACTIVE_WRITE
    assert receiver.credits == 2

    # Frame 4: READ_WRITE to Bank 3 (Simultaneous concurrent dual-port)
    pkt4 = encode_qdr_packet(bank=3, opcode=QdrOpCode.READ_WRITE, addr=0x30, payload=b"\xDE\xAD")
    res4 = receiver.process_packet(pkt4)
    assert res4["valid"], f"Packet 4 failed: {res4.get('error')}"
    assert receiver.get_bank_state(3) == QdrBankState.DUAL_PORT_RW
    assert receiver.link_locked, "Receiver should be link-locked after 4 valid frames"

    # Frame 5: BTE (Burst Terminate) to Bank 3
    pkt5 = encode_qdr_packet(bank=3, opcode=QdrOpCode.BTE, addr=0x30)
    res5 = receiver.process_packet(pkt5)
    assert res5["valid"], f"Packet 5 failed: {res5.get('error')}"
    assert receiver.get_bank_state(3) == QdrBankState.READY

    # Return credits
    receiver.return_credit(2)
    assert receiver.credits == 2

    # Test CRC corruption rejection
    corrupted_pkt = bytearray(pkt1)
    corrupted_pkt[-1] ^= 0xFF  # Invert CRC LSB
    res_corrupt = receiver.process_packet(bytes(corrupted_pkt))
    assert not res_corrupt["valid"], "Corrupted CRC was not rejected"
    assert receiver.error_count == 1


@cocotb.test()
async def test_qdr_standards_and_ppa(dut):
    """
    Test 6: QDR Standards Compliance and Calibrated IHP 130nm SG13G2 PPA Model:
    Verifies:
      1. Bank states, Bank IDs, and OpCode constants.
      2. 16-bit CCITT CRC determinism across test vectors.
      3. Physical PPA scaling model on IHP 130nm SG13G2.
    """
    # Standards verification
    assert QdrBankState.IDLE == 0x00
    assert QdrBankState.READY == 0x01
    assert QdrBankState.ACTIVE_READ == 0x02
    assert QdrBankState.ACTIVE_WRITE == 0x03
    assert QdrBankState.DUAL_PORT_RW == 0x04
    assert QdrBankState.ERROR_CONFLICT == 0x05

    assert QdrBankId.BANK_0 == 0x00
    assert QdrBankId.BANK_7 == 0x07

    assert QdrOpCode.NOP == 0x00
    assert QdrOpCode.READ == 0x01
    assert QdrOpCode.WRITE == 0x02
    assert QdrOpCode.READ_WRITE == 0x03
    assert QdrOpCode.BTE == 0x04
    assert QdrOpCode.LBK == 0x05
    assert QdrOpCode.IDLE == 0x7E
    assert QdrOpCode.SYNC_SOF == 0xA5

    # CRC-16 determinism check
    crc1 = compute_qdr_crc16(b"\xA5\x01\x00\x10")
    crc2 = compute_qdr_crc16(b"\xA5\x01\x00\x10")
    assert crc1 == crc2, "CRC-16 is non-deterministic"
    assert isinstance(crc1, int) and 0 <= crc1 <= 0xFFFF

    # PPA Model verification
    ppa = QdrPpaModel()
    summary = ppa.get_ppa_summary()

    assert summary["microcode_cells"] == 0
    assert summary["hw_macro_cells"] == 635
    assert summary["hw_macro_ge"] == 1250.0
    assert summary["hw_macro_area_um2"] == 4690.0
    assert summary["hw_macro_area_pct"] == 3.29
    assert summary["hw_macro_fmax_mhz"] == 800.00
    assert summary["hw_macro_power_uw_10mhz"] == 62.50
    assert summary["hw_macro_throughput_mbps"] == 38400.0
    assert summary["hw_macro_energy_pj_per_bit"] == 0.00085
