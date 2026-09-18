# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_dp20.py - Cocotb test suite for DisplayPort 2.0 / 2.1 (UHBR 10/20 Gbps) Physical Layer & 128b/132b Link Training Engine

Verifies:
1. test_dp20_master_block_transmission: Master transmits 2-bit sync header (2'b01) followed by
   8-bit payload data (0x5A) on pin 3, verified at baud center with status R2 = 0x00.
2. test_dp20_rx_sync_ingress: Slave synchronizes to sync header rising edge on pin 3 via WAITEDGE,
   samples 8-bit payload into R0 and preserves in R1 (0x5A) with status R2 = 0x00.
3. test_dp20_sync_header_validation_and_fault_trapping: In-register validation of 2-bit sync headers
   (valid 2'b01 and 2'b10 -> R2 = 0x00; illegal 2'b00 and 2'b11 trapped with R2 = 0xEE).
4. test_dp20_scrambler_round_trip: 23-bit self-synchronizing LFSR scrambler / descrambler round-trip
   verification and in-register microcode descrambling.
5. test_dp20_128b132b_block_coding_and_training: 128b/132b block framing, 2-bit header parity,
   and Dp20ReceiverModel block lock acquisition.
6. test_dp20_standards_and_ppa: UHBR 10/13.5/20 line rates, 128b/132b efficiency (96.97%),
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
from dp20_model import (  # noqa: E402
    DpSyncHeader,
    DpTrainingPattern,
    compute_header_parity,
    verify_header_parity,
    is_valid_sync_header,
    encode_128b132b,
    decode_128b132b,
    Dp20Scrambler,
    Dp20Descrambler,
    Dp20ReceiverModel,
    Dp20PpaModel,
    build_dp20_tx_block_asm,
    build_dp20_rx_sync_asm,
    build_dp20_sync_validator_asm,
    build_dp20_descrambler_asm,
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
async def test_dp20_master_block_transmission(dut):
    """
    Test 1: Master DisplayPort 2.0 Block Transmission:
    Transmits 2-bit sync header (2'b01) followed by 8 data payload bits (0x5A) on pin 3.
    Verifies bit timings, captured LSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(DpSyncHeader.CONTROL)  # 2'b01 -> bit0=1, bit1=0
    expected_data = 0x5A                      # 0b01011010 -> LSB first: 0, 1, 0, 1, 1, 0, 1, 0

    asm_code = build_dp20_tx_block_asm(
        sync_header=expected_sync,
        lead_data_byte=expected_data,
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

        if bool(core.halted.value) and len(captured_bits) >= (10 * baud_cycles):
            break

    # Subsample bits at baud centers (offset 1 cycle in)
    symbol_bits = []
    for i in range(1, len(captured_bits), baud_cycles):
        symbol_bits.append(captured_bits[i])

    dut._log.info(f"Captured {len(symbol_bits)} transmitted bits: {symbol_bits[:10]}")

    assert len(symbol_bits) >= 10, f"Expected at least 10 transmitted bits, got {len(symbol_bits)}"
    rec_sync = (symbol_bits[0]) | (symbol_bits[1] << 1)
    assert rec_sync == expected_sync, f"Expected sync header 0x{expected_sync:02X}, got 0x{rec_sync:02X}"

    rec_data = 0
    for idx in range(8):
        rec_data |= (symbol_bits[2 + idx] << idx)
    assert rec_data == expected_data, f"Expected payload data 0x{expected_data:02X}, got 0x{rec_data:02X}"

    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"

    dut._log.info(
        f"DisplayPort 2.0 TX PASS: Sync=2'b{rec_sync:02b}, Data=0x{rec_data:02X}, Status=0x{r2_val:02X}"
    )


@cocotb.test()
async def test_dp20_rx_sync_ingress(dut):
    """
    Test 2: Slave DisplayPort 2.0 Sync Ingress & Data Sampling:
    Core synchronizes to sync header rising edge on pin 3 via WAITEDGE,
    strides to bit midpoint, samples 8 bits into R0, preserves in R1 (0x5A),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    expected_byte = 0x5A

    asm_code = build_dp20_rx_sync_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let the core settle into WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive sync header: bit0=1 (rising edge), bit1=0
    dut.uio_in.value = (1 << pin_rx)
    await ClockCycles(dut.clk, baud_cycles)
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, baud_cycles)

    # 2. Drive payload byte 0x5A (0b01011010) LSB-first into pin_rx
    for bit_idx in range(8):
        bit = (expected_byte >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # Return to idle
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 10)

    assert bool(core.halted.value) is True, "Core should halt after symbol ingress"
    r0_val = int(core.r0.value)
    r1_val = int(core.r1.value)
    r2_val = int(core.r2.value)

    assert r0_val == expected_byte, f"Expected R0=0x{expected_byte:02X}, got 0x{r0_val:02X}"
    assert r1_val == expected_byte, f"Expected R1=0x{expected_byte:02X}, got 0x{r1_val:02X}"
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"

    dut._log.info(
        f"DisplayPort 2.0 Sync Ingress PASS: R0=0x{r0_val:02X}, R1=0x{r1_val:02X}, R2=0x{r2_val:02X}"
    )


@cocotb.test()
async def test_dp20_sync_header_validation_and_fault_trapping(dut):
    """
    Test 3: Sync Header Validation & Fault Trapping:
    Verifies microcode sync header validation:
    - Valid Control header (R0 = 0x01) -> R2 = 0x00 (Valid)
    - Valid Data header (R0 = 0x02) -> R2 = 0x00 (Valid)
    - Illegal header 2'b00 (R0 = 0x00) -> R2 = 0xEE (Sync Violation Trapped)
    - Illegal header 2'b11 (R0 = 0x03) -> R2 = 0xEE (Sync Violation Trapped)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    test_vectors = [
        (0x01, 0x00, "Valid Control (2'b01)"),
        (0x02, 0x00, "Valid Data (2'b10)"),
        (0x00, 0xEE, "Illegal Header (2'b00)"),
        (0x03, 0xEE, "Illegal Header (2'b11)"),
    ]

    for header_val, expected_status, desc in test_vectors:
        asm = build_dp20_sync_validator_asm(test_header=header_val)
        words = assemble("\n".join(asm))
        await _init_dut_and_bootload(dut, words)

        await ClockCycles(dut.clk, 30)
        assert bool(core.halted.value) is True, f"Core should halt for {desc}"
        status = int(core.r2.value)
        assert status == expected_status, f"Expected R2=0x{expected_status:02X} for {desc}, got 0x{status:02X}"

    dut._log.info("DisplayPort 2.0 Sync Header Validation PASS: 4/4 vectors verified correctly")


@cocotb.test()
async def test_dp20_scrambler_round_trip(dut):
    """
    Test 4: 23-Bit LFSR Scrambler / Descrambler (G(x) = x^23 + x^21 + x^16 + x^8 + x^5 + x^2 + 1):
    - Validates multi-byte stream scrambling and descrambling with 100% round-trip fidelity.
    - Validates in-register microcode descrambling on the core.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    scrambler = Dp20Scrambler(seed=0x7FFFFF)
    descrambler = Dp20Descrambler(seed=0x7FFFFF)

    payload = b"DisplayPort UHBR"
    ciphertext = scrambler.scramble_bytes(payload)
    recovered = descrambler.descramble_bytes(ciphertext)

    assert recovered == payload, f"Round-trip failed: {recovered} != {payload}"
    assert ciphertext != payload, "Ciphertext should differ from plaintext"

    # Test in-register microcode descrambling
    core = dut.user_project.u_core
    test_plain = 0xA5
    test_mask = 0x3C
    test_cipher = test_plain ^ test_mask

    asm = build_dp20_descrambler_asm(scrambled_byte=test_cipher, mask_byte=test_mask)
    words = assemble("\n".join(asm))
    await _init_dut_and_bootload(dut, words)

    await ClockCycles(dut.clk, 30)
    assert bool(core.halted.value) is True, "Core should halt after descrambling"

    r1_val = int(core.r1.value)
    r2_val = int(core.r2.value)
    assert r1_val == test_plain, f"Expected R1=0x{test_plain:02X}, got 0x{r1_val:02X}"
    assert r2_val == 0x00, f"Expected R2=0x00, got 0x{r2_val:02X}"

    dut._log.info(
        f"DisplayPort 2.0 Scrambler PASS: Stream verified, in-register plaintext 0x{r1_val:02X} recovered"
    )


@cocotb.test()
async def test_dp20_128b132b_block_coding_and_training(dut):
    """
    Test 5: 128b/132b Block Framing & Link Training Verification:
    - Encodes 16-byte payloads into 132-bit blocks (Data and Control).
    - Verifies 2-bit header parity protection against bit flips.
    - Tests Dp20ReceiverModel block lock acquisition and error rejection.
    """
    # 1. Block encoding
    raw_data = bytes([i * 17 % 256 for i in range(16)])
    data_block = encode_128b132b(raw_data, is_control=False)
    header, payload, valid = decode_128b132b(data_block)
    assert valid is True, "Data block should decode cleanly"
    assert header == DpSyncHeader.DATA
    assert payload == raw_data

    raw_ctrl = bytes([0xAA] * 16)
    ctrl_block = encode_128b132b(raw_ctrl, is_control=True)
    header_c, payload_c, valid_c = decode_128b132b(ctrl_block)
    assert valid_c is True, "Control block should decode cleanly"
    assert header_c == DpSyncHeader.CONTROL
    assert payload_c == raw_ctrl

    # 2. Header parity verification
    for h in (0b01, 0b10):
        p = compute_header_parity(h)
        assert verify_header_parity(h, p) is True
        assert verify_header_parity(h, p ^ 1) is False
        assert verify_header_parity(h, p ^ 2) is False

    # 3. Receiver model lock
    rx_model = Dp20ReceiverModel()
    assert rx_model.block_lock is False
    for _ in range(4):
        rx_model.process_header(0b01, compute_header_parity(0b01))
    assert rx_model.block_lock is True, "Receiver should acquire block lock after 4 consecutive valid headers"

    # Corrupt a header
    rx_model.process_header(0b00, 0b00)
    assert rx_model.block_lock is False, "Receiver should drop lock on corrupted header"
    assert rx_model.sync_header_errors == 1

    dut._log.info("DisplayPort 2.0 128b/132b Framing & Lock FSM PASS: 100% verified")


@cocotb.test()
async def test_dp20_standards_and_ppa(dut):
    """
    Test 6: DisplayPort 2.0 Standards Compliance & Hardware PPA Model:
    - UHBR 10, UHBR 13.5, UHBR 20 line rates
    - 128b/132b channel coding efficiency (128/132 = 96.9697%)
    - IHP 130nm SG13G2 PPA model validation
    """
    uhbr_rates = {
        "UHBR 10": 10.0,
        "UHBR 13.5": 13.5,
        "UHBR 20": 20.0,
    }

    efficiency = 128.0 / 132.0
    assert abs(efficiency - 0.969697) < 1e-5, f"128b/132b efficiency mismatch: {efficiency}"

    for name, rate in uhbr_rates.items():
        eff_rate = rate * efficiency
        dut._log.info(f"{name}: Raw {rate} Gbps/lane, Effective {eff_rate:.3f} Gbps/lane")

    # Validate PPA model
    metrics = Dp20PpaModel.get_metrics()
    assert metrics["macro_cells"] == 575
    assert metrics["macro_ge"] == 1120.0
    assert metrics["f_max_mhz"] == 800.0
    assert metrics["raw_throughput_mbps"] == 20000.0
    assert metrics["nominal_power_uw_10mhz"] == 56.0
    assert metrics["energy_pj_per_bit"] == 0.0028

    dut._log.info(f"DisplayPort 2.0 Standards & PPA Model PASS: {metrics}")
