# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_gddr6.py - Cocotb test suite for GDDR6 / GDDR6X (JEDEC JESD250)
High-Speed Graphics Memory Physical Layer & Command Engine.

Verifies:
1. test_gddr6_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by ACT opcode byte (0x01) and Target Address (0x80) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_gddr6_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples command byte MSB-first into R0 and preserves in R1 (0x05 RD) with status R2 = 0x00.
3. test_gddr6_command_filter_and_fault_trapping: In-register validation of GDDR6 commands
   (valid 0x01 ACT, 0x02 PRE, 0x03 REF, 0x04 PDE, 0x05 RD, 0x06 WR, 0x07 WOM, 0x08 MRW -> R2 = 0x00;
    illegal 0x7F trapped with R2 = 0xEE).
4. test_gddr6_credit_tracking_and_underflow_trapping: In-register validation of command buffer
   credit increment (ACK/PRE returned), decrement (command dispatched), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_gddr6_packet_framing_banks_and_receiver: Packet encapsulation, channel/bank group/bank state transitions,
   CCITT CRC-16 (0x1021) protection, and Gddr6ReceiverModel link lock state machine.
6. test_gddr6_standards_and_ppa: Bank states, command opcodes, CRC-16 determinism,
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
from gddr6_model import (  # noqa: E402
    Gddr6BankState,
    Gddr6OpCode,
    compute_gddr6_crc16,
    encode_gddr6_packet,
    decode_gddr6_packet,
    Gddr6ReceiverModel,
    Gddr6PpaModel,
    build_gddr6_tx_beat_asm,
    build_gddr6_rx_beat_asm,
    build_gddr6_command_filter_asm,
    build_gddr6_credit_tracker_asm,
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
async def test_gddr6_master_packet_transmission(dut):
    """
    Test 1: Master GDDR6 Packet Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by ACT opcode (0x01) and Target Address (0x80)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(Gddr6OpCode.SYNC_SOF)    # 0xA5 (0b10100101)
    expected_opcode = int(Gddr6OpCode.ACT)       # 0x01
    expected_addr = 0x80                        # Target Row Address 0x80

    asm_code = build_gddr6_tx_beat_asm(
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

    # Reconstruct bytes MSB-first
    byte0 = sum(sampled_bits[b] << (7 - b) for b in range(8))
    byte1 = sum(sampled_bits[8 + b] << (7 - b) for b in range(8))
    byte2 = sum(sampled_bits[16 + b] << (7 - b) for b in range(8))

    assert byte0 == expected_sync, f"Byte 0 (SYNC_SOF): expected 0x{expected_sync:02X}, got 0x{byte0:02X}"
    assert byte1 == expected_opcode, f"Byte 1 (ACT): expected 0x{expected_opcode:02X}, got 0x{byte1:02X}"
    assert byte2 == expected_addr, f"Byte 2 (Target Address): expected 0x{expected_addr:02X}, got 0x{byte2:02X}"


@cocotb.test()
async def test_gddr6_rx_beat_ingress(dut):
    """
    Test 2: Slave GDDR6 SYNC_SOF Ingress & Command Sampling:
    Core synchronizes to SYNC_SOF delimiter rising edge on pin 3 via WAITEDGE (bit 7 = 1),
    strides to bit midpoint, samples 8 bits MSB-first into R0, preserves in R1 (0x05 RD),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_delimiter = int(Gddr6OpCode.SYNC_SOF)   # 0xA5 = 0b10100101
    test_command = int(Gddr6OpCode.RD)          # 0x05 = 0b00000101

    asm_code = build_gddr6_rx_beat_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core start and enter WAITEDGE
    await ClockCycles(dut.clk, 10)

    # Deliver SYNC_SOF (0xA5) MSB-first: bit 7 is 1, causing rising edge
    for bit_i in range(8):
        bit_val = (sync_delimiter >> (7 - bit_i)) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    # Deliver test_command (0x05) MSB-first
    for bit_i in range(8):
        bit_val = (test_command >> (7 - bit_i)) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    dut.uio_in.value = 0x00

    # Wait for completion
    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt after RX beat ingress"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"
    assert int(core.r1.value) == test_command, f"Expected R1=0x{test_command:02X} (RD), got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_gddr6_command_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register GDDR6 Command Filter & Fault Trapping:
    Verifies valid commands:
      0x01 (ACT) -> R2 = 0x00
      0x02 (PRE) -> R2 = 0x00
      0x03 (REF) -> R2 = 0x00
      0x04 (PDE) -> R2 = 0x00
      0x05 (RD)  -> R2 = 0x00
      0x06 (WR)  -> R2 = 0x00
      0x07 (WOM) -> R2 = 0x00
      0x08 (MRW) -> R2 = 0x00
    Verifies illegal command:
      0x7F -> trapped with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_test_cases = [
        (int(Gddr6OpCode.ACT), "ACT"),
        (int(Gddr6OpCode.PRE), "PRE"),
        (int(Gddr6OpCode.REF), "REF"),
        (int(Gddr6OpCode.PDE), "PDE"),
        (int(Gddr6OpCode.RD), "RD"),
        (int(Gddr6OpCode.WR), "WR"),
        (int(Gddr6OpCode.WOM), "WOM"),
        (int(Gddr6OpCode.MRW), "MRW"),
    ]

    for cmd_val, name in valid_test_cases:
        asm_code = build_gddr6_command_filter_asm(cmd_val)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(200):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), f"Core did not halt for valid command {name}"
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for {name}, got 0x{int(core.r2.value):02X}"

    # Test illegal command: 0x7F
    illegal_cmd = 0x7F
    asm_code = build_gddr6_command_filter_asm(illegal_cmd)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt for illegal command"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Fault Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_gddr6_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register GDDR6 Command Buffer Credit Tracking & Underflow Trapping:
    - Event 1: Memory ACK/PRE returned -> credit increment (4 -> 5), R2 = 0x00.
    - Event 2: Command dispatched with credits > 0 -> credit decrement (4 -> 3), R2 = 0x00.
    - Event 2: Command dispatched with credits == 0 -> underflow trap (R2 = 0xEE).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case A: Event 1 (Credit Return / PRE): 4 + 1 = 5
    asm_code_return = build_gddr6_credit_tracker_asm(event_type=1, initial_credits=4)
    words_return = assemble("\n".join(asm_code_return))
    await _init_dut_and_bootload(dut, words_return)

    core = dut.user_project.u_core
    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on credit return"
    assert int(core.r0.value) == 5, f"Expected R0=5 after credit return, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case B: Event 2 (Credit Consume / Command dispatch): 4 - 1 = 3
    asm_code_send = build_gddr6_credit_tracker_asm(event_type=2, initial_credits=4)
    words_send = assemble("\n".join(asm_code_send))
    await _init_dut_and_bootload(dut, words_send)

    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on command dispatch"
    assert int(core.r0.value) == 3, f"Expected R0=3 after command dispatch, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case C: Event 2 with 0 credits -> Underflow Trap (R2 = 0xEE)
    asm_code_underflow = build_gddr6_credit_tracker_asm(event_type=2, initial_credits=0)
    words_underflow = assemble("\n".join(asm_code_underflow))
    await _init_dut_and_bootload(dut, words_underflow)

    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on underflow condition"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Underflow Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_gddr6_packet_framing_banks_and_receiver(dut):
    """
    Test 5: GDDR6 Framing, Channel/Bank Group/Bank State Transitions & Receiver Model:
    - Tests packet encapsulation, CRC-16 generation, and decoding.
    - Tests CRC corrupt packet detection.
    - Tests Gddr6ReceiverModel bank state transitions (IDLE -> ACTIVE -> IDLE) and link lock acquisition.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Packet Encoding & Decoding Tests
    pkt = encode_gddr6_packet(
        opcode=Gddr6OpCode.ACT,
        channel_id=1,
        bank_group=3,
        bank_id=2,
        addr=0x1234,
        payload=b"\xCA\xFE",
    )
    frame_bytes = pkt["frame_bytes"]
    decoded = decode_gddr6_packet(frame_bytes)

    assert decoded is not None, "Valid GDDR6 packet failed decoding"
    assert decoded["opcode"] == Gddr6OpCode.ACT, f"Expected ACT, got {decoded['opcode']}"
    assert decoded["channel_id"] == 1, f"Expected channel_id=1, got {decoded['channel_id']}"
    assert decoded["bank_group"] == 3, f"Expected bank_group=3, got {decoded['bank_group']}"
    assert decoded["bank_id"] == 2, f"Expected bank_id=2, got {decoded['bank_id']}"
    assert (decoded["addr"] & 0x0FFF) == (0x1234 & 0x0FFF), f"Expected addr 0x234, got 0x{decoded['addr']:04X}"
    assert decoded["payload"] == b"\xCA\xFE", f"Expected payload b'\\xCA\\xFE', got {decoded['payload']}"

    # Corrupt packet test
    corrupted_bytes = bytearray(frame_bytes)
    corrupted_bytes[-1] ^= 0xFF
    decoded_corrupt = decode_gddr6_packet(bytes(corrupted_bytes))
    assert decoded_corrupt is None, "Corrupted packet incorrectly passed decoding"

    # Receiver Model Link Lock and Bank State Test
    receiver = Gddr6ReceiverModel(num_channels=2, bank_groups_per_ch=4, banks_per_group=4)
    assert receiver.link_locked is False, "Initial link lock should be False"

    # Feed 4 consecutive SYNCs to acquire link lock
    for _ in range(4):
        receiver.process_sync()
    assert receiver.link_locked is True, "Receiver should achieve link lock after 4 SYNCs"

    # Process ACT command: Channel 0, BG 1, Bank 2
    act_pkt = encode_gddr6_packet(
        opcode=Gddr6OpCode.ACT,
        channel_id=0,
        bank_group=1,
        bank_id=2,
        addr=0x0100,
    )
    success = receiver.process_packet(act_pkt["frame_bytes"])
    assert success is True, "Failed to process ACT packet"
    assert receiver.get_bank_state(0, 1, 2) == Gddr6BankState.ACTIVE, "Bank state should be ACTIVE after ACT"
    assert receiver.act_count == 1, f"Expected act_count=1, got {receiver.act_count}"
    assert receiver.credits == 3, f"Expected credits=3 after ACT, got {receiver.credits}"

    # Process PRE command: Channel 0, BG 1, Bank 2
    pre_pkt = encode_gddr6_packet(
        opcode=Gddr6OpCode.PRE,
        channel_id=0,
        bank_group=1,
        bank_id=2,
        addr=0x0000,
    )
    success_pre = receiver.process_packet(pre_pkt["frame_bytes"])
    assert success_pre is True, "Failed to process PRE packet"
    assert receiver.get_bank_state(0, 1, 2) == Gddr6BankState.IDLE, "Bank state should be IDLE after PRE"
    assert receiver.pre_count == 1, f"Expected pre_count=1, got {receiver.pre_count}"
    assert receiver.credits == 4, f"Expected credits=4 after PRE, got {receiver.credits}"


@cocotb.test()
async def test_gddr6_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance and PPA Model Validation:
    - Verifies GDDR6 bank states and command opcodes.
    - Verifies CCITT CRC-16 polynomial determinism.
    - Verifies IHP 130nm SG13G2 calibrated PPA model metrics.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Bank states
    assert Gddr6BankState.IDLE == 0x00
    assert Gddr6BankState.ACTIVE == 0x01
    assert Gddr6BankState.PRECHARGING == 0x02
    assert Gddr6BankState.REFRESHING == 0x03

    # Command opcodes
    assert Gddr6OpCode.NOP == 0x00
    assert Gddr6OpCode.ACT == 0x01
    assert Gddr6OpCode.PRE == 0x02
    assert Gddr6OpCode.REF == 0x03
    assert Gddr6OpCode.PDE == 0x04
    assert Gddr6OpCode.RD == 0x05
    assert Gddr6OpCode.WR == 0x06
    assert Gddr6OpCode.WOM == 0x07
    assert Gddr6OpCode.MRW == 0x08
    assert Gddr6OpCode.IDLE == 0x7E
    assert Gddr6OpCode.SYNC_SOF == 0xA5

    # CRC-16 determinism test
    test_data = b"GDDR6 / GDDR6X JEDEC JESD250 Command & Physical Layer Engine"
    crc1 = compute_gddr6_crc16(test_data)
    crc2 = compute_gddr6_crc16(test_data)
    assert crc1 == crc2, "CRC-16 non-deterministic"
    assert isinstance(crc1, int) and 0 <= crc1 <= 0xFFFF, f"CRC-16 out of range: {crc1}"

    # PPA Model validation
    ppa = Gddr6PpaModel.get_metrics()
    assert ppa["macro_cells"] == 660, f"PPA macro cells mismatch: {ppa['macro_cells']}"
    assert ppa["macro_ge"] == 1295.0, f"PPA GE mismatch: {ppa['macro_ge']}"
    assert ppa["macro_area_um2"] == 4850.0, f"PPA area mismatch: {ppa['macro_area_um2']}"
    assert ppa["f_max_mhz"] == 800.0, f"PPA f_max mismatch: {ppa['f_max_mhz']}"
    assert ppa["nominal_power_uw_10mhz"] == 64.50, f"PPA power mismatch: {ppa['nominal_power_uw_10mhz']}"
    assert ppa["raw_throughput_mbps"] == 38400.0, f"PPA throughput mismatch: {ppa['raw_throughput_mbps']}"
    assert ppa["energy_pj_per_bit"] == 0.00095, f"PPA energy mismatch: {ppa['energy_pj_per_bit']}"
