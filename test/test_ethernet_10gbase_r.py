# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_ethernet_10gbase_r.py - Cocotb test suite for IEEE 802.3ae 10GBASE-R PCS (64b/66b) Engine

Verifies:
1. test_10gbase_r_master_block_transmission: Master transmits 64b/66b block (sync header + lead data byte)
   on pin 3, captured and verified by bit-accurate receiver checks.
2. test_10gbase_r_rx_sync_ingress: Slave synchronizes to sync header rising edge on pin 3 via WAITEDGE,
   samples payload byte into R0, copies to R1 (0x5A), and halts with R2 = 0x00.
3. test_10gbase_r_sync_header_validation_and_fault_trapping: In-register 2-bit sync header validation:
   valid (2'b01, 2'b10 -> R2=0x00) and illegal headers (2'b00, 2'b11 -> R2=0xEE).
4. test_10gbase_r_scrambler_self_synchronization: 58-bit self-synchronizing scrambler/descrambler (G(x) = 1 + x^39 + x^58),
   self-lock property after 58 bits from arbitrary state, and in-register microcode descrambling.
5. test_10gbase_r_block_types_and_lock_fsm: Evaluates standard Block Types (0x1E, 0x78, 0x4B, 0x87..0xFF),
   microcode block type filter, and 64-block lock state machine.
6. test_10gbase_r_standards_and_ppa: 64b/66b line efficiency (96.97%), sync header Hamming distance (dH >= 2),
   receiver monitor, and calibrated IHP 130nm SG13G2 PPA model validation.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from ethernet_10gbase_r_model import (  # noqa: E402
    SYNC_DATA,
    SYNC_CTRL,
    SYNC_INVALID_00,
    SYNC_INVALID_11,
    BLOCK_TYPE_CTRL_ALL,
    BLOCK_TYPE_START_S0,
    BLOCK_TYPE_ORDERED_SET,
    BLOCK_TYPE_TERM_T7,
    BLOCK_TYPE_TERM_T0,
    is_valid_sync_header,
    encode_64b66b_data,
    encode_64b66b_control,
    decode_64b66b,
    Ethernet10GScrambler,
    Ethernet10GDescrambler,
    Ethernet10GBlockLockModel,
    Ethernet10GReceiverModel,
    Ethernet10GPpaModel,
    build_10gbase_r_tx_block_asm,
    build_10gbase_r_rx_sync_asm,
    build_10gbase_r_sync_validator_asm,
    build_10gbase_r_block_type_filter_asm,
    build_10gbase_r_descrambler_asm,
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
async def test_10gbase_r_master_block_transmission(dut):
    """
    Test 1: Master 10GBASE-R Block Transmission:
    Transmits 2-bit sync header (2'b01) followed by 8 data payload bits (0x5A) on pin 3.
    Verifies bit timings, captured LSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = SYNC_DATA  # 2'b01 -> bit0=1, bit1=0
    expected_data = 0x5A       # 0b01011010 -> LSB first: 0, 1, 0, 1, 1, 0, 1, 0

    asm_code = build_10gbase_r_tx_block_asm(
        sync_header=expected_sync,
        lead_data_byte=expected_data,
        pin_tx=pin_tx,
        baud_cycles=baud_cycles
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

    # Verify first 2 bits are sync header 2'b01: bit0=1, bit1=0
    assert len(symbol_bits) >= 10, f"Expected at least 10 transmitted bits, got {len(symbol_bits)}"
    rec_sync = (symbol_bits[0]) | (symbol_bits[1] << 1)
    assert rec_sync == expected_sync, f"Expected sync header 0x{expected_sync:02X}, got 0x{rec_sync:02X}"

    # Verify next 8 bits are data byte 0x5A
    rec_data = 0
    for idx in range(8):
        rec_data |= (symbol_bits[2 + idx] << idx)
    assert rec_data == expected_data, f"Expected payload data 0x{expected_data:02X}, got 0x{rec_data:02X}"

    # Verify core halted with R2 = 0x00
    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"

    dut._log.info(
        f"10GBASE-R TX PASS: Sync=2'b{rec_sync:02b}, Data=0x{rec_data:02X}, Status=0x{r2_val:02X}"
    )


@cocotb.test()
async def test_10gbase_r_rx_sync_ingress(dut):
    """
    Test 2: Slave 10GBASE-R Sync Ingress & Data Sampling:
    Core synchronizes to sync header rising edge on pin 3 via WAITEDGE,
    strides to bit midpoint, samples 8 bits into R0, preserves in R1 (0x5A),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    expected_byte = 0x5A

    asm_code = build_10gbase_r_rx_sync_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let the core settle into WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive sync header rising edge transition on pin_rx
    dut.uio_in.value = (1 << pin_rx)
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
        f"10GBASE-R Sync Ingress PASS: R0=0x{r0_val:02X}, R1=0x{r1_val:02X}, R2=0x{r2_val:02X}"
    )


@cocotb.test()
async def test_10gbase_r_sync_header_validation_and_fault_trapping(dut):
    """
    Test 3: Sync Header Validation & Fault Trapping:
    Verifies microcode sync header validation:
    - Valid Data header (R0 = 0x01) -> R2 = 0x00 (Valid)
    - Valid Control header (R0 = 0x02) -> R2 = 0x00 (Valid)
    - Illegal header 2'b00 (R0 = 0x00) -> R2 = 0xEE (Sync Violation Trapped)
    - Illegal header 2'b11 (R0 = 0x03) -> R2 = 0xEE (Sync Violation Trapped)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    test_vectors = [
        (0x01, 0x00, "Valid Data (2'b01)"),
        (0x02, 0x00, "Valid Control (2'b10)"),
        (0x00, 0xEE, "Illegal Header (2'b00)"),
        (0x03, 0xEE, "Illegal Header (2'b11)"),
    ]

    for header_val, expected_status, desc in test_vectors:
        asm = [f"LDI R0, 0x{header_val:02X} ; Sync candidate {desc}"] + build_10gbase_r_sync_validator_asm()
        words = assemble("\n".join(asm))
        await _init_dut_and_bootload(dut, words)

        await ClockCycles(dut.clk, 25)
        assert bool(core.halted.value) is True, f"Core should halt for {desc}"
        status = int(core.r2.value)
        assert status == expected_status, f"Expected R2=0x{expected_status:02X} for {desc}, got 0x{status:02X}"

    dut._log.info("10GBASE-R Sync Header Validation PASS: 4/4 vectors verified correctly")


@cocotb.test()
async def test_10gbase_r_scrambler_self_synchronization(dut):
    """
    Test 4: 58-Bit Self-Synchronizing Scrambler / Descrambler (G(x) = 1 + x^39 + x^58):
    - Verifies scrambler and descrambler round-trip across 64-bit blocks
    - Verifies self-synchronization: descrambler initialized with wrong/arbitrary state locks
      within 58 bits and recovers subsequent data perfectly without negotiation
    - Verifies in-register microcode XOR bit manipulation on ASIC core
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    scrambler = Ethernet10GScrambler(initial_state=0x123456789ABCDEF)
    descrambler = Ethernet10GDescrambler(initial_state=0x123456789ABCDEF)

    # 1. Round-trip test with matching state
    test_blocks = [
        0x0123456789ABCDEF,
        0xFEDCBA9876543210,
        0x5555555555555555,
        0xAAAAAAAAAAAAAAAA,
        0x0011223344556677,
    ]

    for p in test_blocks:
        s = scrambler.scramble_64(p)
        assert s != p, f"Scrambled output 0x{s:016X} should not equal plaintext 0x{p:016X}"
        rec = descrambler.descramble_64(s)
        assert rec == p, f"Recovered 0x{rec:016X} != original 0x{p:016X}"

    # 2. Test self-synchronization from unknown initial state
    # Scramble 1 block of data (64 bits > 58 bits)
    unsynced_descrambler = Ethernet10GDescrambler(initial_state=0x0)  # Completely wrong state!
    warmup_data = 0xDEADBEEFCAFEFACE
    scrambled_warmup = scrambler.scramble_64(warmup_data)

    # Ingress scrambled warmup (64 bits primes the 58-bit delay line)
    _ = unsynced_descrambler.descramble_64(scrambled_warmup)

    # Now verify that subsequent block is decoded with 100% accuracy!
    target_data = 0x0102030405060708
    scrambled_target = scrambler.scramble_64(target_data)
    recovered_target = unsynced_descrambler.descramble_64(scrambled_target)
    assert recovered_target == target_data, (
        f"Self-synchronization failed: expected 0x{target_data:016X}, got 0x{recovered_target:016X}"
    )

    # 3. In-register microcode test on ASIC core
    # Validate XOR mask operation on core
    plain_byte = 0x5A
    mask_byte = 0x3C
    scrambled_byte = plain_byte ^ mask_byte

    asm_core = [
        f"LDI R0, 0x{scrambled_byte:02X} ; Ingress scrambled byte"
    ] + build_10gbase_r_descrambler_asm(mask_byte=mask_byte)
    words = assemble("\n".join(asm_core))
    await _init_dut_and_bootload(dut, words)

    await ClockCycles(dut.clk, 20)
    core = dut.user_project.u_core
    assert bool(core.halted.value) is True, "Core should halt after descrambling"
    r1_val = int(core.r1.value)
    r2_val = int(core.r2.value)

    assert r1_val == plain_byte, f"Expected recovered byte 0x{plain_byte:02X}, got 0x{r1_val:02X}"
    assert r2_val == 0x00, f"Expected R2=0x00, got 0x{r2_val:02X}"

    dut._log.info(
        f"10GBASE-R Scrambler PASS: Round-trip OK, Self-Lock Verified, Core Recovered=0x{r1_val:02X}"
    )


@cocotb.test()
async def test_10gbase_r_block_types_and_lock_fsm(dut):
    """
    Test 5: Standard Block Types & Block Lock State Machine:
    - Verifies standard Block Types (0x1E Ctrl, 0x78 Start, 0x4B Ordered Set, 0x87 Terminate)
    - Verifies in-register microcode block type filter on core (Match -> 0x00, Mismatch -> 0xEE)
    - Verifies 64-block lock acquisition threshold and error window lock drop
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    # 1. Block Type Filter Verification on Core
    # Sub-test A: Match Start-of-Packet (0x78)
    asm_match = [
        f"LDI R0, 0x{BLOCK_TYPE_START_S0:02X} ; Block type 0x78"
    ] + build_10gbase_r_block_type_filter_asm(expected_type=BLOCK_TYPE_START_S0)
    words_match = assemble("\n".join(asm_match))
    await _init_dut_and_bootload(dut, words_match)

    await ClockCycles(dut.clk, 20)
    assert bool(core.halted.value) is True
    r2_match = int(core.r2.value)
    assert r2_match == 0x00, f"Expected R2=0x00 for block type match, got 0x{r2_match:02X}"

    # Sub-test B: Mismatch (0x1E != 0x78)
    asm_mismatch = [
        f"LDI R0, 0x{BLOCK_TYPE_CTRL_ALL:02X} ; Block type 0x1E"
    ] + build_10gbase_r_block_type_filter_asm(expected_type=BLOCK_TYPE_START_S0)
    words_mismatch = assemble("\n".join(asm_mismatch))
    await _init_dut_and_bootload(dut, words_mismatch)

    await ClockCycles(dut.clk, 20)
    assert bool(core.halted.value) is True
    r2_mismatch = int(core.r2.value)
    assert r2_mismatch == 0xEE, f"Expected R2=0xEE for block type mismatch, got 0x{r2_mismatch:02X}"

    # 2. Block Lock State Machine Verification
    lock_fsm = Ethernet10GBlockLockModel(lock_threshold=64, error_threshold=16)

    # 63 valid headers should NOT declare lock
    for _ in range(63):
        locked = lock_fsm.process_sync_header(SYNC_DATA)
        assert not locked, "Lock should not be declared before 64 valid headers"

    # 64th valid header declares lock
    locked = lock_fsm.process_sync_header(SYNC_DATA)
    assert locked, "Lock must be declared upon 64th consecutive valid header"

    # An occasional error should not immediately drop lock
    for _ in range(15):
        locked = lock_fsm.process_sync_header(SYNC_INVALID_00)
        assert locked, "Lock should be maintained when invalid headers < error threshold (16)"

    # 16th error drops lock
    locked = lock_fsm.process_sync_header(SYNC_INVALID_11)
    assert not locked, "Lock must be dropped when 16 invalid headers occur"

    dut._log.info("10GBASE-R Block Types & Lock FSM PASS: Type Filter & 64-block lock confirmed")


@cocotb.test()
async def test_10gbase_r_standards_and_ppa(dut):
    """
    Test 6: 10GBASE-R Standards Compliance & Calibrated PPA Validation:
    - Verifies 64b/66b line coding efficiency (64/66 = 96.9697%)
    - Verifies Hamming distance dH >= 2 between valid sync headers
    - Evaluates full receiver monitor with valid and corrupt blocks
    - Validates IHP 130nm SG13G2 PPA model (550 cells, 1070.0 GE, 4066.0 um^2, 800 MHz, 53.50 uW)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Line coding efficiency
    raw_bits = 66
    payload_bits = 64
    efficiency = payload_bits / raw_bits
    assert efficiency > 0.9696 and efficiency < 0.9697, f"Unexpected efficiency {efficiency}"
    overhead = (raw_bits - payload_bits) / raw_bits
    assert overhead > 0.0303 and overhead < 0.0304, f"Unexpected overhead {overhead}"

    # 2. Sync header Hamming distance
    diff = SYNC_DATA ^ SYNC_CTRL
    hamming_dist = bin(diff).count("1")
    assert hamming_dist == 2, f"Hamming distance between 2'b01 and 2'b10 must be 2, got {hamming_dist}"

    # 3. Receiver monitor evaluation
    rx = Ethernet10GReceiverModel()

    # Ingress valid data block
    data_octets = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]
    data_blk = encode_64b66b_data(data_octets)
    scrambler = Ethernet10GScrambler()
    scrambled_data_blk = scrambler.scramble_block(data_blk)

    # Ingress 64 blocks to lock receiver
    for _ in range(64):
        rx.ingress_block(scrambled_data_blk)

    assert rx.lock_engine.is_locked is True, "Receiver should achieve block lock after 64 valid blocks"
    assert rx.sync_errors == 0, f"Expected 0 sync errors, got {rx.sync_errors}"

    # Ingress invalid sync header block
    invalid_blk = (0x1234567890ABCDEF << 2) | SYNC_INVALID_00
    _, _, is_valid = rx.ingress_block(invalid_blk)
    assert not is_valid, "Invalid sync header block must be flagged"
    assert rx.sync_errors == 1, f"Expected 1 sync error, got {rx.sync_errors}"

    # 4. Validate PPA Model
    ppa = Ethernet10GPpaModel()
    assert ppa.standard_cell_count == 550
    assert ppa.gate_equivalent_ge == 1070.0
    assert ppa.area_um2 == 4066.0
    assert ppa.max_frequency_mhz == 800.0
    assert ppa.power_uw_at_10mhz == 53.50
    assert ppa.energy_pj_per_bit == 0.00535

    dut._log.info(
        f"10GBASE-R Standards & PPA PASS: Efficiency={efficiency*100:.2f}%, dH={hamming_dist}, Area={ppa.area_um2} um^2"
    )
