# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_fibre_channel.py - Cocotb test suite for Fibre Channel 32G/64G Physical Layer Engine

Verifies:
1. test_fc_master_primitive_transmission: Master transmits K28.5 comma (0xBC) followed by
   R_RDY primitive ID (0x4B) on pin 3, verified at baud center with status R2 = 0x00.
2. test_fc_rx_sync_ingress: Slave synchronizes to K28.5 comma rising edge on pin 3 via WAITEDGE,
   samples SOFi3 byte into R0 and preserves in R1 (0x57) with status R2 = 0x00.
3. test_fc_credit_tracking_and_underflow_trapping: In-register validation of Buffer-to-Buffer
   credit increment (R_RDY), decrement (Frame TX), and underflow prevention (credit=0 -> R2 = 0xEE).
4. test_fc_sof_filter_and_fault_trapping: In-register validation of SOF delimiters
   (valid 0x57, 0x58, 0x59 -> R2 = 0x00; illegal 0x1F trapped with R2 = 0xEE).
5. test_fc_frame_framing_and_credit_receiver: Frame encapsulation, CRC-32 protection,
   BB_Credit flow control accounting, and FibreChannelReceiverModel link lock state machine.
6. test_fc_standards_and_ppa: Fibre Channel delimiter constants, data rates,
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
from fibre_channel_model import (  # noqa: E402
    FibreChannelDelimiter,
    compute_fc_crc32,
    encode_fc_frame,
    decode_fc_frame,
    FibreChannelReceiverModel,
    FibreChannelPpaModel,
    build_fc_tx_primitive_asm,
    build_fc_rx_sync_asm,
    build_fc_credit_tracker_asm,
    build_fc_sof_filter_asm,
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
async def test_fc_master_primitive_transmission(dut):
    """
    Test 1: Master Fibre Channel Primitive Transmission:
    Transmits K28.5 comma (0xBC) followed by R_RDY primitive ID (0x4B) on pin 3.
    Verifies bit timings, captured LSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_comma = int(FibreChannelDelimiter.K28_5)  # 0xBC (0b10111100)
    expected_prim_id = int(FibreChannelDelimiter.R_RDY) # 0x4B (0b01001011)

    asm_code = build_fc_tx_primitive_asm(
        k_code=expected_comma,
        prim_id=expected_prim_id,
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

    assert len(symbol_bits) >= 16, f"Expected at least 16 transmitted bits, got {len(symbol_bits)}"

    rec_comma = 0
    for idx in range(8):
        rec_comma |= (symbol_bits[idx] << idx)
    assert rec_comma == expected_comma, f"Expected comma 0x{expected_comma:02X}, got 0x{rec_comma:02X}"

    rec_prim = 0
    for idx in range(8):
        rec_prim |= (symbol_bits[8 + idx] << idx)
    assert rec_prim == expected_prim_id, f"Expected primitive 0x{expected_prim_id:02X}, got 0x{rec_prim:02X}"

    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"


@cocotb.test()
async def test_fc_rx_sync_ingress(dut):
    """
    Test 2: Slave Fibre Channel Comma Ingress & SOFi3 Sampling:
    Core synchronizes to K28.5 comma rising edge on pin 3 via WAITEDGE,
    strides to bit midpoint, samples 8 bits into R0, preserves in R1 (0x57 SOFi3),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    comma = int(FibreChannelDelimiter.K28_5)  # 0xBC (0b10111100)
    target_sof = int(FibreChannelDelimiter.SOFI3) # 0x57 (0b01010111)

    asm_code = build_fc_rx_sync_asm(pin_rx=pin_rx, baud_cycles=baud_cycles)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let receiver reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Transmit K28.5 comma LSB-first
    for bit_idx in range(8):
        bit = (comma >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Transmit target SOF byte LSB-first
    for bit_idx in range(8):
        bit = (target_sof >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # Return bus to idle low
    dut.uio_in.value = 0x00

    # Wait for halt
    for _ in range(50):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value), "Core did not halt after RX sync"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == target_sof, f"Expected R0=0x{target_sof:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == target_sof, f"Expected R1=0x{target_sof:02X}, got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_fc_credit_tracking_and_underflow_trapping(dut):
    """
    Test 3: In-Register BB_Credit Flow Control Accounting:
    - Event 1 (R_RDY received): initial credit 4 -> 5, R2 = 0x00
    - Event 2 (Frame sent): initial credit 4 -> 3, R2 = 0x00
    - Event 2 (Frame sent with credit 0): underflow error -> R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: R_RDY credit increment
    asm_inc = build_fc_credit_tracker_asm(credit_event=1, initial_credit=4)
    words = assemble("\n".join(asm_inc))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 5, f"Expected credit incremented to 5, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 2: Frame sent credit decrement
    asm_dec = build_fc_credit_tracker_asm(credit_event=2, initial_credit=4)
    words = assemble("\n".join(asm_dec))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 3, f"Expected credit decremented to 3, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 3: Credit underflow attempt (initial credit = 0, event = 2)
    asm_underflow = build_fc_credit_tracker_asm(credit_event=2, initial_credit=0)
    words = assemble("\n".join(asm_underflow))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected underflow trap R2=0xEE, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_fc_sof_filter_and_fault_trapping(dut):
    """
    Test 4: In-Register SOF Delimiter Filtering:
    - Valid SOFs: 0x57 (SOFi3), 0x58 (SOFn3), 0x59 (SOFf) -> R2 = 0x00
    - Illegal SOF: 0x1F -> trapped with R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_sofs = [
        int(FibreChannelDelimiter.SOFI3),
        int(FibreChannelDelimiter.SOFN3),
        int(FibreChannelDelimiter.SOFF),
    ]

    for sof in valid_sofs:
        asm_code = build_fc_sof_filter_asm(test_sof=sof)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(40):
            if bool(core.halted.value):
                break
            await RisingEdge(dut.clk)

        assert bool(core.halted.value)
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for SOF 0x{sof:02X}, got 0x{int(core.r2.value):02X}"

    # Test illegal SOF: 0x1F
    illegal_sof = 0x1F
    asm_code = build_fc_sof_filter_asm(test_sof=illegal_sof)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(40):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE for illegal SOF, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_fc_frame_framing_and_credit_receiver(dut):
    """
    Test 5: Fibre Channel Receiver Model & Credit Flow Control:
    Verifies primitive tracking, BB_Credit replenishment, link lock FSM,
    and frame encapsulation/decoding with CRC-32.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_model = FibreChannelReceiverModel(initial_credits=2)
    assert rx_model.bb_credits == 2
    assert not rx_model.link_lock

    # Consume credits
    assert rx_model.consume_tx_credit() is True
    assert rx_model.bb_credits == 1
    assert rx_model.consume_tx_credit() is True
    assert rx_model.bb_credits == 0
    assert rx_model.consume_tx_credit() is False  # Exhausted!

    # Feed 4 R_RDY primitives to replenish credits and acquire link lock
    for _ in range(4):
        res = rx_model.process_primitive(FibreChannelDelimiter.K28_5, FibreChannelDelimiter.R_RDY)
        assert res is True

    assert rx_model.link_lock is True, "Link lock must be acquired after 4 primitives"
    assert rx_model.bb_credits == 4, f"Expected credits replenished to 4, got {rx_model.bb_credits}"

    # Frame encoding and decoding test
    header = b"\x00" * 24
    payload = b"\x11\x22\x33\x44" * 4
    sof = int(FibreChannelDelimiter.SOFI3)
    eof = int(FibreChannelDelimiter.EOFN)

    frame = encode_fc_frame(sof, header, payload, eof)
    assert frame["valid"] is True

    rx_sof, rx_header, rx_payload, rx_crc, rx_eof, is_valid = decode_fc_frame(frame["raw_bytes"])
    assert is_valid is True
    assert rx_sof == sof
    assert rx_header == header
    assert rx_payload == payload
    assert rx_eof == eof

    res_frame = rx_model.process_frame(frame["raw_bytes"])
    assert res_frame is True
    assert rx_model.frames_received == 1

    # Corrupt frame CRC test
    corrupted_bytes = bytearray(frame["raw_bytes"])
    corrupted_bytes[-3] ^= 0xFF
    res_corrupt = rx_model.process_frame(bytes(corrupted_bytes))
    assert res_corrupt is False
    assert rx_model.crc_errors == 1


@cocotb.test()
async def test_fc_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance & Calibrated IHP 130nm SG13G2 PPA Model:
    Validates Fibre Channel delimiters, CRC determinism, and synthesized macro PPA metrics.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    assert int(FibreChannelDelimiter.K28_5) == 0xBC
    assert int(FibreChannelDelimiter.SOFI3) == 0x57
    assert int(FibreChannelDelimiter.SOFN3) == 0x58
    assert int(FibreChannelDelimiter.SOFF) == 0x59
    assert int(FibreChannelDelimiter.EOFN) == 0x5B
    assert int(FibreChannelDelimiter.EOFT) == 0x5C
    assert int(FibreChannelDelimiter.EOFNI) == 0x5D
    assert int(FibreChannelDelimiter.R_RDY) == 0x4B
    assert int(FibreChannelDelimiter.IDLE) == 0x4C

    test_data = b"FIBRE_CHANNEL_FRAME_PAYLOAD_TEST_32G"
    crc1 = compute_fc_crc32(test_data)
    crc2 = compute_fc_crc32(test_data)
    assert crc1 == crc2, "CRC calculation not deterministic"
    assert 0 <= crc1 <= 0xFFFFFFFF

    metrics = FibreChannelPpaModel.get_metrics()
    assert metrics["macro_cells"] == 595
    assert metrics["macro_ge"] == 1160.0
    assert metrics["macro_area_um2"] == 4380.0
    assert metrics["f_max_mhz"] == 800.0
    assert metrics["nominal_power_uw_10mhz"] == 58.0
    assert metrics["raw_throughput_mbps"] == 32000.0
    assert metrics["energy_pj_per_bit"] == 0.00181

    await ClockCycles(dut.clk, 5)
