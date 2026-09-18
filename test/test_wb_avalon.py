# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_wb_avalon.py - Cocotb test suite for Wishbone B4 and Avalon-MM
On-Chip Interconnect & Pipelined Crossbar Engine.

Verifies:
1. test_wb_avalon_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by WB_READ opcode byte (0x01) and Target Address (0x24) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_wb_avalon_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples command byte MSB-first into R0 and preserves in R1 (0x05 AVALON_READ) with status R2 = 0x00.
3. test_wb_avalon_command_filter_and_fault_trapping: In-register validation of Wishbone/Avalon commands
   (valid 0x01 WB_READ, 0x02 WB_WRITE, 0x03 WB_PIPE_READ, 0x04 WB_PIPE_WRITE, 0x05 AVALON_READ, 0x06 AVALON_WRITE -> R2 = 0x00;
   illegal 0x7F trapped with R2 = 0xEE).
4. test_wb_avalon_credit_tracking_and_underflow_trapping: In-register validation of bus buffer
   credit increment (ACK returned), decrement (strobe issued), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_wb_avalon_packet_framing_burst_and_receiver: Packet encapsulation, incremental burst address computation,
   CCITT CRC-16 (0x1021) protection, and WbAvalonReceiverModel link lock state machine.
6. test_wb_avalon_standards_and_ppa: Cycle types, response codes, command opcodes, CRC-16 determinism,
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
from wb_avalon_model import (  # noqa: E402
    WbCycleType,
    WbResp,
    AvalonResp,
    WbAvalonOpCode,
    compute_avalon_burst_addresses,
    compute_wb_avalon_crc16,
    encode_wb_avalon_packet,
    decode_wb_avalon_packet,
    WbAvalonReceiverModel,
    WbAvalonPpaModel,
    build_wb_avalon_tx_beat_asm,
    build_wb_avalon_rx_beat_asm,
    build_wb_avalon_command_filter_asm,
    build_wb_avalon_credit_tracker_asm,
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
async def test_wb_avalon_master_packet_transmission(dut):
    """
    Test 1: Master Wishbone / Avalon Packet Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by WB_READ opcode (0x01) and Target Address (0x24)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(WbAvalonOpCode.SYNC_SOF)    # 0xA5 (0b10100101)
    expected_opcode = int(WbAvalonOpCode.WB_READ)   # 0x01
    expected_addr = 0x24                           # Target Address 0x24

    asm_code = build_wb_avalon_tx_beat_asm(
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
    assert byte1 == expected_opcode, f"Byte 1 (WB_READ): expected 0x{expected_opcode:02X}, got 0x{byte1:02X}"
    assert byte2 == expected_addr, f"Byte 2 (Target Address): expected 0x{expected_addr:02X}, got 0x{byte2:02X}"


@cocotb.test()
async def test_wb_avalon_rx_beat_ingress(dut):
    """
    Test 2: Slave Wishbone / Avalon SYNC_SOF Ingress & Command Sampling:
    Core synchronizes to SYNC_SOF delimiter rising edge on pin 3 via WAITEDGE (bit 7 = 1),
    strides to bit midpoint, samples 8 bits MSB-first into R0, preserves in R1 (0x05 AVALON_READ),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_delimiter = int(WbAvalonOpCode.SYNC_SOF)     # 0xA5 = 0b10100101
    test_command = int(WbAvalonOpCode.AVALON_READ)    # 0x05 = 0b00000101

    asm_code = build_wb_avalon_rx_beat_asm(
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
    assert int(core.r1.value) == test_command, f"Expected R1=0x{test_command:02X} (AVALON_READ), got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_wb_avalon_command_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register Wishbone / Avalon Command Filter & Fault Trapping:
    Verifies valid commands:
      0x01 (WB_READ)       -> R2 = 0x00
      0x02 (WB_WRITE)      -> R2 = 0x00
      0x03 (WB_PIPE_READ)  -> R2 = 0x00
      0x04 (WB_PIPE_WRITE) -> R2 = 0x00
      0x05 (AVALON_READ)   -> R2 = 0x00
      0x06 (AVALON_WRITE)  -> R2 = 0x00
    Verifies illegal command:
      0x7F -> trapped with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_test_cases = [
        (int(WbAvalonOpCode.WB_READ), "WB_READ"),
        (int(WbAvalonOpCode.WB_WRITE), "WB_WRITE"),
        (int(WbAvalonOpCode.WB_PIPE_READ), "WB_PIPE_READ"),
        (int(WbAvalonOpCode.WB_PIPE_WRITE), "WB_PIPE_WRITE"),
        (int(WbAvalonOpCode.AVALON_READ), "AVALON_READ"),
        (int(WbAvalonOpCode.AVALON_WRITE), "AVALON_WRITE"),
    ]

    for cmd_val, name in valid_test_cases:
        asm_code = build_wb_avalon_command_filter_asm(cmd_val)
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
    asm_code = build_wb_avalon_command_filter_asm(illegal_cmd)
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
async def test_wb_avalon_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register Bus Buffer Credit Tracking & Underflow Trapping:
    - Event 1: Bus ACK returned -> credit increment (4 -> 5), R2 = 0x00.
    - Event 2: Bus strobe issued with credits > 0 -> credit decrement (4 -> 3), R2 = 0x00.
    - Event 2: Bus strobe issued with credits == 0 -> underflow trap (R2 = 0xEE).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case A: Event 1 (Credit Return / ACK): 4 + 1 = 5
    asm_code_return = build_wb_avalon_credit_tracker_asm(event_type=1, initial_credits=4)
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

    # Case B: Event 2 (Credit Consume / Strobe): 4 - 1 = 3
    asm_code_send = build_wb_avalon_credit_tracker_asm(event_type=2, initial_credits=4)
    words_send = assemble("\n".join(asm_code_send))
    await _init_dut_and_bootload(dut, words_send)

    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on credit strobe"
    assert int(core.r0.value) == 3, f"Expected R0=3 after credit strobe, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case C: Event 2 with 0 credits -> Underflow Trap (R2 = 0xEE)
    asm_code_underflow = build_wb_avalon_credit_tracker_asm(event_type=2, initial_credits=0)
    words_underflow = assemble("\n".join(asm_code_underflow))
    await _init_dut_and_bootload(dut, words_underflow)

    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on underflow condition"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Underflow Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_wb_avalon_packet_framing_burst_and_receiver(dut):
    """
    Test 5: Wishbone / Avalon Framing, Burst Address Calculation & Receiver Model:
    - Tests 4-beat incremental burst address calculation: start_addr=0x10, burstcount=4 -> [0x10, 0x14, 0x18, 0x1C].
    - Tests 8-beat incremental burst address calculation: start_addr=0x40, burstcount=8 -> [0x40, 0x44, 0x48, 0x4C, 0x50, 0x54, 0x58, 0x5C].
    - Tests packet encapsulation, CRC-16 generation, and decoding.
    - Tests CRC corrupt packet detection.
    - Tests WbAvalonReceiverModel state transitions and link lock acquisition after 4 packets.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Burst Address Tests
    burst4 = compute_avalon_burst_addresses(0x10, 4, bytes_per_word=4)
    assert burst4 == [0x10, 0x14, 0x18, 0x1C], f"Burst4 error: {burst4}"

    burst8 = compute_avalon_burst_addresses(0x40, 8, bytes_per_word=4)
    assert burst8 == [0x40, 0x44, 0x48, 0x4C, 0x50, 0x54, 0x58, 0x5C], f"Burst8 error: {burst8}"

    # Packet Encoding & Decoding Tests
    pkt = encode_wb_avalon_packet(
        opcode=WbAvalonOpCode.WB_PIPE_READ,
        addr=0x54,
        data_or_attr=0x00,
        wb_resp=WbResp.ACK,
        avalon_resp=AvalonResp.OKAY,
        payload=b"\xCA\xFE",
    )
    raw_bytes = pkt["raw_bytes"]
    opcode, addr, data_or_attr, wb_resp, avalon_resp, payload, crc16, is_valid = decode_wb_avalon_packet(raw_bytes)

    assert is_valid is True, "Valid packet failed decoding"
    assert opcode == WbAvalonOpCode.WB_PIPE_READ, f"Expected WB_PIPE_READ, got {opcode}"
    assert addr == 0x54, f"Expected addr=0x54, got 0x{addr:02X}"
    assert wb_resp == WbResp.ACK, f"Expected ACK, got {wb_resp}"
    assert avalon_resp == AvalonResp.OKAY, f"Expected OKAY, got {avalon_resp}"
    assert payload == b"\xCA\xFE", f"Expected b'\\xCA\\xFE', got {payload}"

    # Corrupt packet test
    corrupted_bytes = bytearray(raw_bytes)
    corrupted_bytes[-1] ^= 0xFF
    _, _, _, _, _, _, _, is_valid_corrupt = decode_wb_avalon_packet(bytes(corrupted_bytes))
    assert is_valid_corrupt is False, "Corrupted packet incorrectly passed decoding"

    # Receiver Model Link Lock Test
    receiver = WbAvalonReceiverModel(initial_credits=4)
    assert receiver.link_lock is False, "Initial link lock should be False"

    # Feed 4 consecutive valid frames
    for i in range(4):
        test_pkt = encode_wb_avalon_packet(
            opcode=WbAvalonOpCode.WB_PIPE_READ,
            addr=0x10 + i * 4,
            data_or_attr=0x00,
        )
        success = receiver.process_packet(test_pkt["raw_bytes"])
        assert success is True, f"Failed processing packet {i}"

    assert receiver.link_lock is True, "Receiver should achieve link lock after 4 frames"
    assert receiver.packets_received == 4, f"Expected 4 packets received, got {receiver.packets_received}"
    assert receiver.crc_errors == 0, "Expected 0 CRC errors"

    # Feed Avalon read completion to return credit
    avalon_pkt = encode_wb_avalon_packet(
        opcode=WbAvalonOpCode.AVALON_READ,
        addr=0x10,
        data_or_attr=0xAA,
    )
    receiver.process_packet(avalon_pkt["raw_bytes"])
    assert receiver.avalon_reads == 1, f"Expected 1 Avalon read, got {receiver.avalon_reads}"


@cocotb.test()
async def test_wb_avalon_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance and PPA Model Validation:
    - Verifies Wishbone cycle types, response codes, and Avalon-MM responses.
    - Verifies CCITT CRC-16 polynomial determinism.
    - Verifies IHP 130nm SG13G2 calibrated PPA model metrics.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Cycle types
    assert WbCycleType.CLASSIC_SINGLE == 0x00
    assert WbCycleType.CLASSIC_BLOCK == 0x01
    assert WbCycleType.PIPELINED == 0x02

    # Response codes
    assert WbResp.ACK == 0x00
    assert WbResp.ERR == 0x01
    assert WbResp.RTY == 0x02
    assert AvalonResp.OKAY == 0x00
    assert AvalonResp.RESERVED == 0x01
    assert AvalonResp.SLAVEERROR == 0x02
    assert AvalonResp.DECODEERROR == 0x03

    # Command opcodes
    assert WbAvalonOpCode.WB_READ == 0x01
    assert WbAvalonOpCode.WB_WRITE == 0x02
    assert WbAvalonOpCode.WB_PIPE_READ == 0x03
    assert WbAvalonOpCode.WB_PIPE_WRITE == 0x04
    assert WbAvalonOpCode.AVALON_READ == 0x05
    assert WbAvalonOpCode.AVALON_WRITE == 0x06

    # CRC-16 determinism test
    test_data = b"Wishbone B4 and Avalon-MM Interconnect"
    crc1 = compute_wb_avalon_crc16(test_data)
    crc2 = compute_wb_avalon_crc16(test_data)
    assert crc1 == crc2, "CRC-16 non-deterministic"
    assert isinstance(crc1, int) and 0 <= crc1 <= 0xFFFF, f"CRC-16 out of range: {crc1}"

    # PPA Model validation
    ppa = WbAvalonPpaModel.get_metrics()
    assert ppa["macro_cells"] == 640, f"PPA macro cells mismatch: {ppa['macro_cells']}"
    assert ppa["macro_ge"] == 1255.0, f"PPA GE mismatch: {ppa['macro_ge']}"
    assert ppa["macro_area_um2"] == 4710.0, f"PPA area mismatch: {ppa['macro_area_um2']}"
    assert ppa["f_max_mhz"] == 800.0, f"PPA f_max mismatch: {ppa['f_max_mhz']}"
    assert ppa["nominal_power_uw_10mhz"] == 62.50, f"PPA power mismatch: {ppa['nominal_power_uw_10mhz']}"
    assert ppa["raw_throughput_mbps"] == 25600.0, f"PPA throughput mismatch: {ppa['raw_throughput_mbps']}"
    assert ppa["energy_pj_per_bit"] == 0.00098, f"PPA energy mismatch: {ppa['energy_pj_per_bit']}"
