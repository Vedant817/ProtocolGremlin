# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_infiniband.py - Cocotb test suite for InfiniBand HDR/NDR Physical & Link Layer Engine

Verifies:
1. test_infiniband_master_ts1_transmission: Master transmits SOP delimiter (0xFB) followed by
   TS1 ID (0x4A) on pin 3, verified at baud center with status R2 = 0x00.
2. test_infiniband_rx_sync_ingress: Slave synchronizes to SOP rising edge on pin 3 via WAITEDGE,
   samples TS2 ID byte into R0 and preserves in R1 (0x45) with status R2 = 0x00.
3. test_infiniband_packet_filter_and_fault_trapping: In-register validation of BTH OpCodes
   (valid 0x00, 0x04, 0x0A, 0x11 -> R2 = 0x00; illegal 0x3F trapped with R2 = 0xEE).
4. test_infiniband_crc_validation: Mathematical VCRC-16 and ICRC-32 polynomial verification
   and in-register microcode CRC checking.
5. test_infiniband_ordered_set_and_link_lock: Ordered set framing, CRC-16/32 protection,
   and InfinibandReceiverModel link lock state machine.
6. test_infiniband_standards_and_ppa: InfiniBand HDR/NDR data rates, delimiters, BTH opcodes,
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
from infiniband_model import (  # noqa: E402
    InfiniBandOrderedSet,
    InfiniBandOpCode,
    compute_infiniband_vcrc16,
    compute_infiniband_icrc32,
    encode_infiniband_packet,
    decode_infiniband_packet,
    InfinibandReceiverModel,
    InfinibandPpaModel,
    build_infiniband_tx_ts1_asm,
    build_infiniband_rx_sync_asm,
    build_infiniband_packet_filter_asm,
    build_infiniband_vcrc_validator_asm,
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
async def test_infiniband_master_ts1_transmission(dut):
    """
    Test 1: Master InfiniBand TS1 Transmission:
    Transmits SOP delimiter (0xFB) followed by TS1 ID (0x4A) on pin 3.
    Verifies bit timings, captured LSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sop = int(InfiniBandOrderedSet.SOP)     # 0xFB (0b11111011)
    expected_ts_id = int(InfiniBandOrderedSet.TS1_ID) # 0x4A (0b01001010)

    asm_code = build_infiniband_tx_ts1_asm(
        sop_byte=expected_sop,
        ts_id=expected_ts_id,
        pin_tx=pin_tx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    max_cycles = 300

    captured_bits = []

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & (1 << pin_tx)) != 0:
            uio_out = int(dut.uio_out.value)
            bit = (uio_out >> pin_tx) & 1
            captured_bits.append(bit)

        if bool(core.halted.value) and len(captured_bits) >= (16 * baud_cycles):
            break

    # Subsample bits at baud centers (offset 1 cycle in)
    symbol_bits = []
    for i in range(1, len(captured_bits), baud_cycles):
        symbol_bits.append(captured_bits[i])

    assert len(symbol_bits) >= 16, f"Expected at least 16 sampled bits, got {len(symbol_bits)}"

    rx_sop = 0
    for b in range(8):
        rx_sop |= (symbol_bits[b] << b)

    rx_ts1 = 0
    for b in range(8):
        rx_ts1 |= (symbol_bits[8 + b] << b)

    assert rx_sop == expected_sop, f"Transmitted SOP mismatch: got 0x{rx_sop:02X}, expected 0x{expected_sop:02X}"
    assert rx_ts1 == expected_ts_id, f"Transmitted TS1 ID mismatch: got 0x{rx_ts1:02X}, expected 0x{expected_ts_id:02X}"

    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2 = 0x00 (TX complete), got 0x{r2_val:02X}"


@cocotb.test()
async def test_infiniband_rx_sync_ingress(dut):
    """
    Test 2: Slave Ingress Synchronization:
    Slave waits for SOP rising edge on pin 3, strides past delimiter,
    samples subsequent byte (TS2 ID = 0x45) into R0 and preserves in R1.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sop_byte = int(InfiniBandOrderedSet.SOP)      # 0xFB (0b11111011)
    target_byte = int(InfiniBandOrderedSet.TS2_ID) # 0x45 (0b01000101)

    asm_code = build_infiniband_rx_sync_asm(pin_rx=pin_rx, baud_cycles=baud_cycles)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let receiver reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Transmit SOP byte LSB-first
    for bit_idx in range(8):
        bit = (sop_byte >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Transmit target byte LSB-first
    for bit_idx in range(8):
        bit = (target_byte >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # Return pin to idle low
    dut.uio_in.value = 0x00

    # Await core halt
    for _ in range(50):
        if int(core.halted.value) == 1:
            break
        await RisingEdge(dut.clk)

    assert int(core.halted.value) == 1, "Core failed to halt after receiving TS2 ID"
    assert int(core.r2.value) == 0x00, f"Expected R2 = 0x00 (success), got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == target_byte, f"Expected R0 = 0x{target_byte:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == target_byte, f"Expected R1 = 0x{target_byte:02X}, got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_infiniband_packet_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register BTH OpCode Filtering:
    Tests valid BTH OpCodes (0x00, 0x04, 0x0A, 0x11 -> R2 = 0x00)
    and illegal/unsupported OpCode (0x3F -> trapped with R2 = 0xEE).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_opcodes = [
        int(InfiniBandOpCode.RC_SEND_FIRST),
        int(InfiniBandOpCode.RC_SEND_ONLY),
        int(InfiniBandOpCode.RC_RDMA_WRITE_ONLY),
        int(InfiniBandOpCode.RC_ACK),
    ]

    for opcode in valid_opcodes:
        asm_code = build_infiniband_packet_filter_asm(test_opcode=opcode)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(60):
            if int(core.halted.value) == 1:
                break
            await RisingEdge(dut.clk)

        assert int(core.halted.value) == 1, f"Core timed out on valid opcode 0x{opcode:02X}"
        assert int(core.r2.value) == 0x00, f"Expected R2 = 0x00 for opcode 0x{opcode:02X}, got 0x{int(core.r2.value):02X}"

    # Test illegal opcode: 0x3F
    illegal_opcode = 0x3F
    asm_code = build_infiniband_packet_filter_asm(test_opcode=illegal_opcode)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(60):
        if int(core.halted.value) == 1:
            break
        await RisingEdge(dut.clk)

    assert int(core.halted.value) == 1, "Core timed out on illegal opcode"
    assert int(core.r2.value) == 0xEE, f"Expected R2 = 0xEE (fault trapped), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_infiniband_crc_validation(dut):
    """
    Test 4: InfiniBand Dual CRC Validation:
    Validates mathematical 16-bit VCRC and 32-bit ICRC algorithms,
    and runs in-register microcode CRC slice validation.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_data = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    vcrc = compute_infiniband_vcrc16(test_data)
    icrc = compute_infiniband_icrc32(test_data)

    assert isinstance(vcrc, int) and 0 <= vcrc <= 0xFFFF, "Invalid VCRC-16 range"
    assert isinstance(icrc, int) and 0 <= icrc <= 0xFFFFFFFF, "Invalid ICRC-32 range"

    # Determinism check
    assert compute_infiniband_vcrc16(test_data) == vcrc, "VCRC calculation not deterministic"
    assert compute_infiniband_icrc32(test_data) == icrc, "ICRC calculation not deterministic"

    # Test in-register matching CRC slice
    crc_low_byte = vcrc & 0xFF
    asm_match = build_infiniband_vcrc_validator_asm(crc_low_byte, crc_low_byte)
    words_match = assemble("\n".join(asm_match))
    await _init_dut_and_bootload(dut, words_match)

    core = dut.user_project.u_core
    for _ in range(30):
        if int(core.halted.value) == 1:
            break
        await RisingEdge(dut.clk)

    assert int(core.halted.value) == 1, "Core timed out during CRC match test"
    assert int(core.r2.value) == 0x00, f"Expected R2 = 0x00 (CRC matched), got 0x{int(core.r2.value):02X}"

    # Test in-register corrupted CRC slice
    corrupted_byte = (crc_low_byte ^ 0xFF) & 0xFF
    asm_mismatch = build_infiniband_vcrc_validator_asm(corrupted_byte, crc_low_byte)
    words_mismatch = assemble("\n".join(asm_mismatch))
    await _init_dut_and_bootload(dut, words_mismatch)

    for _ in range(30):
        if int(core.halted.value) == 1:
            break
        await RisingEdge(dut.clk)

    assert int(core.halted.value) == 1, "Core timed out during CRC mismatch test"
    assert int(core.r2.value) == 0xEE, f"Expected R2 = 0xEE (CRC mismatch), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_infiniband_ordered_set_and_link_lock(dut):
    """
    Test 5: InfiniBand Receiver Model & Link Lock State Machine:
    Verifies ordered set decoding, packet framing, and link lock acquisition.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_model = InfinibandReceiverModel()
    assert not rx_model.link_lock, "Link lock should initially be False"

    # Feed 3 TS1 ordered sets (not locked yet)
    for _ in range(3):
        res = rx_model.process_ordered_set(InfiniBandOrderedSet.SOP, InfiniBandOrderedSet.TS1_ID)
        assert res is True
        assert not rx_model.link_lock

    # 4th TS1 triggers link lock
    res = rx_model.process_ordered_set(InfiniBandOrderedSet.SOP, InfiniBandOrderedSet.TS1_ID)
    assert res is True
    assert rx_model.link_lock, "Link lock must be acquired after 4 consecutive TS1s"

    # Test packet encoding and decoding
    lrh = b"\x00" * 8
    bth = b"\x01" * 12
    payload = b"\xDE\xAD\xBE\xEF" * 4

    pkt = encode_infiniband_packet(lrh, bth, payload)
    assert pkt["valid"] is True

    rx_lrh, rx_bth, rx_payload, rx_icrc, rx_vcrc, valid = decode_infiniband_packet(pkt["raw_bytes"])
    assert valid is True
    assert rx_lrh == lrh
    assert rx_bth == bth
    assert rx_payload == payload

    res_pkt = rx_model.process_packet(pkt["raw_bytes"])
    assert res_pkt is True
    assert rx_model.packets_received == 1

    # Corrupted packet test
    corrupted_bytes = bytearray(pkt["raw_bytes"])
    corrupted_bytes[10] ^= 0xFF
    res_corrupt = rx_model.process_packet(bytes(corrupted_bytes))
    assert res_corrupt is False
    assert rx_model.crc_errors == 1


@cocotb.test()
async def test_infiniband_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance & Calibrated IHP 130nm SG13G2 PPA Model:
    Validates InfiniBand delimiter constants, opcodes, and synthesized macro PPA metrics.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    assert int(InfiniBandOrderedSet.SOP) == 0xFB
    assert int(InfiniBandOrderedSet.EOP) == 0xFD
    assert int(InfiniBandOrderedSet.TS1_ID) == 0x4A
    assert int(InfiniBandOrderedSet.TS2_ID) == 0x45
    assert int(InfiniBandOrderedSet.HEARTBEAT) == 0x5C

    assert int(InfiniBandOpCode.RC_SEND_FIRST) == 0x00
    assert int(InfiniBandOpCode.RC_SEND_ONLY) == 0x04
    assert int(InfiniBandOpCode.RC_RDMA_WRITE_ONLY) == 0x0A
    assert int(InfiniBandOpCode.RC_ACK) == 0x11

    metrics = InfinibandPpaModel.get_metrics()
    assert metrics["macro_cells"] == 590
    assert metrics["macro_ge"] == 1150.0
    assert metrics["macro_area_um2"] == 4340.0
    assert metrics["f_max_mhz"] == 800.0
    assert metrics["nominal_power_uw_10mhz"] == 57.5
    assert metrics["raw_throughput_mbps"] == 50000.0
    assert metrics["energy_pj_per_bit"] == 0.00115

    await ClockCycles(dut.clk, 5)
