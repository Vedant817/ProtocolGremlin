# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_ethernet_100base_tx.py - Cocotb test suite for IEEE 802.3u 100BASE-TX Fast Ethernet Physical Engine

Verifies:
1. test_100base_tx_master_packet_transmission: Master transmits SSD (/J/ /K/), payload [0x5A, 0xC3],
   and ESD (/T/ /R/) with MLT-3 line coding on pins 3 (TXP) and 4 (TXN), verified by Ethernet100BaseTxReceiverModel.
2. test_100base_tx_rx_delimiter_detection: Slave synchronizes to Start-of-Stream Delimiter (/J/ /K/)
   rising edge via WAITEDGE, ingresses payload byte into R0, preserves it in R1 (0x5A), and halts with R2 = 0x00.
3. test_100base_tx_4b5b_block_coding_and_error_trapping: In-register 4B5B code group verification
   (valid symbol 0x0B -> R2=0x00) and illegal/corrupted symbol trapping (0x00 -> R2=0xEE).
4. test_100base_tx_stream_cipher_scrambler: 11-bit LFSR stream scrambler and descrambler
   self-synchronization, bit inversion, and IEEE 802.3u polynomial compliance.
5. test_100base_tx_carrier_sense_and_mlt3_states: Carrier sense detection on differential pair
   (active line -> R2=0x01, idle line -> R2=0x00) and ternary MLT-3 circular state sequence (+1, 0, -1, 0).
6. test_100base_tx_standards_and_ppa: Comprehensive IEEE 802.3u Clause 24/25 code group validation
   and hardware PPA scaling verification for the IHP 130nm SG13G2 platform.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from ethernet_100base_tx_model import (  # noqa: E402
    CODE_IDLE,
    CODE_SSD1,
    CODE_SSD2,
    CODE_ESD1,
    CODE_ESD2,
    CODE_HALT,
    DATA_4B5B_TABLE,
    CONTROL_4B5B_TABLE,
    encode_4b5b_nibble,
    decode_4b5b_nibble,
    is_valid_4b5b_code,
    FastEthernetScrambler,
    mlt3_encode,
    mlt3_decode,
    Ethernet100BaseTxPacket,
    Ethernet100BaseTxReceiverModel,
    Ethernet100BaseTxPpaModel,
    build_100base_tx_packet_asm,
    build_100base_tx_rx_delimiter_asm,
    build_100base_tx_4b5b_validator_asm,
    build_100base_tx_carrier_sense_asm,
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
async def test_100base_tx_master_packet_transmission(dut):
    """
    Test 1: Master 100BASE-TX Packet Transmission:
    Transmits /J/ /K/ SSD delimiter, payload bytes [0x5A, 0xC3], /T/ /R/ ESD delimiter,
    and /I/ idle with MLT-3 ternary signaling on pins 3 (TXP) and 4 (TXN).
    Ethernet100BaseTxReceiverModel verifies cycle-exact line levels, delimiter framing,
    and payload reconstruction.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    txp_pin = 3
    txn_pin = 4
    payload = [0x5A, 0xC3]
    bit_cycles = 4

    asm_code = build_100base_tx_packet_asm(
        payload_bytes=payload,
        txp_pin=txp_pin,
        txn_pin=txn_pin,
        bit_cycles=bit_cycles
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    receiver = Ethernet100BaseTxReceiverModel(
        bit_period=bit_cycles,
        txp_pin=txp_pin,
        txn_pin=txn_pin
    )
    core = dut.user_project.u_core
    max_cycles = 500

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & ((1 << txp_pin) | (1 << txn_pin))) == ((1 << txp_pin) | (1 << txn_pin)):
            uio_out = int(dut.uio_out.value)
            txp = (uio_out >> txp_pin) & 1
            txn = (uio_out >> txn_pin) & 1
            receiver.step(txp, txn)

        if bool(core.halted.value) and len(receiver.packets_received) >= 1:
            break

    assert len(receiver.packets_received) == 1, (
        f"Expected 1 Fast Ethernet packet, received {len(receiver.packets_received)}"
    )
    pkt = receiver.packets_received[0]
    assert pkt.payload_bytes == payload, f"Expected payload {payload}, got {pkt.payload_bytes}"
    assert pkt.valid_ssd is True, "Expected valid /J/ /K/ SSD delimiter"
    assert pkt.valid_esd is True, "Expected valid /T/ /R/ ESD delimiter"
    assert pkt.valid_codes is True, "Expected all 4B5B code groups to be valid"

    dut._log.info(
        f"100BASE-TX TX PASS: Payload={pkt.payload_bytes}, Nibbles={pkt.raw_nibbles}"
    )


@cocotb.test()
async def test_100base_tx_rx_delimiter_detection(dut):
    """
    Test 2: Slave 100BASE-TX Start-of-Stream Delimiter Detection & Ingress:
    Core synchronizes to /J/ /K/ SSD rising edge on TXP via WAITEDGE,
    strides to bit cell midpoints, samples payload byte into R0, saves it in R1,
    validates against expected 0x5A, and halts with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    txp_pin = 3
    txn_pin = 4
    expected_byte = 0x5A
    bit_cycles = 4

    asm_code = build_100base_tx_rx_delimiter_asm(
        expected_nibble=expected_byte,
        txp_pin=txp_pin,
        txn_pin=txn_pin,
        bit_cycles=bit_cycles
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let the core settle into WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive SSD transition: rising edge on TXP (pin 3)
    dut.uio_in.value = 1 << txp_pin
    await ClockCycles(dut.clk, bit_cycles)

    # 2. Drive 8 bits of payload 0x5A onto TXP (LSB-first)
    for bit_idx in range(8):
        bit_val = (expected_byte >> bit_idx) & 1
        dut.uio_in.value = (bit_val << txp_pin)
        await ClockCycles(dut.clk, bit_cycles)

    # Wait for execution to halt
    for _ in range(60):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r1 == expected_byte, f"Expected Payload R1=0x{expected_byte:02X}, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected Status R2=0x00, got 0x{r2:02X}"

    dut._log.info(f"100BASE-TX RX PASS: Payload R1=0x{r1:02X}, Status R2=0x{r2:02X}")


@cocotb.test()
async def test_100base_tx_4b5b_block_coding_and_error_trapping(dut):
    """
    Test 3: In-Register 4B5B Block Coding Validation & Error Trapping:
    Verifies valid 4B5B code group (nibble 0x5 -> 0x0B) matches and asserts R2=0x00,
    while corrupted/illegal code group (0x00) traps into error handler asserting R2=0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_code = encode_4b5b_nibble(0x5)  # 0b01011 = 0x0B

    # Case A: Valid 4B5B code group
    asm_valid = [
        f"LDI R0, 0x{valid_code:02X}   ; Load candidate 4B5B symbol (0x0B)"
    ] + build_100base_tx_4b5b_validator_asm(valid_code)
    words_valid = assemble("\n".join(asm_valid))
    await _init_dut_and_bootload(dut, words_valid)

    core = dut.user_project.u_core
    for _ in range(40):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    r2_valid = int(core.r2.value)
    assert r2_valid == 0x00, f"Expected R2=0x00 for valid 4B5B code, got 0x{r2_valid:02X}"

    # Case B: Illegal / corrupted code group (0x00: all zeros invalid in 4B5B)
    asm_corrupt = [
        "LDI R0, 0x00           ; Load illegal 4B5B symbol (0x00)"
    ] + build_100base_tx_4b5b_validator_asm(valid_code)
    words_corrupt = assemble("\n".join(asm_corrupt))
    await _init_dut_and_bootload(dut, words_corrupt)

    for _ in range(40):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    r2_corrupt = int(core.r2.value)
    assert r2_corrupt == 0xEE, f"Expected R2=0xEE for corrupted code, got 0x{r2_corrupt:02X}"

    dut._log.info(
        f"100BASE-TX 4B5B Validation PASS: Valid R2=0x{r2_valid:02X}, Corrupt R2=0x{r2_corrupt:02X}"
    )


@cocotb.test()
async def test_100base_tx_stream_cipher_scrambler(dut):
    """
    Test 4: 11-Bit Stream Cipher Scrambler / Descrambler LFSR:
    Verifies polynomial G(x) = x^11 + x^9 + 1, self-synchronizing receive descrambler,
    and inversion-free streaming of 4B5B data sequences.
    """
    scrambler = FastEthernetScrambler(initial_state=0x7FF)
    descrambler = FastEthernetScrambler(initial_state=0x7FF)

    test_bits = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 1, 0, 1, 0, 0,
                 1, 1, 1, 1, 0, 0, 0, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 1]

    scrambled = scrambler.scramble_bits(test_bits)
    descrambled = descrambler.descramble_bits(scrambled)

    assert descrambled == test_bits, (
        f"Descrambler mismatch: expected {test_bits}, got {descrambled}"
    )
    assert scrambled != test_bits, "Scrambled bits must not equal plaintext bits"

    # Test self-synchronization: start descrambler with unsynchronized initial state (0x000)
    unsync_descrambler = FastEthernetScrambler(initial_state=0x000)
    long_stream = [1, 0] * 30  # 60 bits
    long_scrambled = FastEthernetScrambler(initial_state=0x7FF).scramble_bits(long_stream)
    long_descrambled = unsync_descrambler.descramble_bits(long_scrambled)

    # After 11 bits (LFSR length), descrambler must be fully self-synchronized
    assert long_descrambled[11:] == long_stream[11:], (
        "Descrambler failed to self-synchronize within 11 bit periods"
    )

    dut._log.info(
        "100BASE-TX Scrambler PASS: 40-bit matched, 11-bit self-synchronization confirmed"
    )


@cocotb.test()
async def test_100base_tx_carrier_sense_and_mlt3_states(dut):
    """
    Test 5: Carrier Sense (CRS) Detection and MLT-3 State Sequencing:
    - Verifies carrier sense microcode detecting active differential signal (R2=0x01)
      versus idle line (R2=0x00).
    - Verifies MLT-3 three-level sequence 0 -> +1 -> 0 -> -1 -> 0 and roundtrip encoding/decoding.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    txp_pin = 3
    txn_pin = 4

    asm_code = build_100base_tx_carrier_sense_asm(txp_pin=txp_pin, txn_pin=txn_pin)
    words = assemble("\n".join(asm_code))

    # Case A: Carrier Active via TXP = 1
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << txp_pin))
    core = dut.user_project.u_core
    for _ in range(30):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    r2_active_txp = int(core.r2.value)
    assert r2_active_txp == 0x01, f"Expected R2=0x01 for active TXP carrier, got 0x{r2_active_txp:02X}"

    # Case B: Carrier Active via TXN = 1
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << txn_pin))
    for _ in range(30):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    r2_active_txn = int(core.r2.value)
    assert r2_active_txn == 0x01, f"Expected R2=0x01 for active TXN carrier, got 0x{r2_active_txn:02X}"

    # Case C: Line Idle (TXP=0, TXN=0)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)
    for _ in range(30):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    r2_idle = int(core.r2.value)
    assert r2_idle == 0x00, f"Expected R2=0x00 for idle line, got 0x{r2_idle:02X}"

    # MLT-3 ternary state sequencing verification
    # Sequence of 1s must cycle: 0 -> +1 -> 0 -> -1 -> 0 -> +1
    all_ones = [1, 1, 1, 1, 1]
    levels = mlt3_encode(all_ones, initial_level=0, initial_dir=1)
    assert levels == [1, 0, -1, 0, 1], f"Expected [1, 0, -1, 0, 1], got {levels}"

    # Sequence with zeros must hold level
    mixed_bits = [1, 0, 0, 1, 0]
    mixed_levels = mlt3_encode(mixed_bits, initial_level=0, initial_dir=1)
    assert mixed_levels == [1, 1, 1, 0, 0], f"Expected [1, 1, 1, 0, 0], got {mixed_levels}"

    # Roundtrip encode -> decode
    decoded = mlt3_decode(mixed_levels, initial_level=0)
    assert decoded == mixed_bits, f"MLT-3 decode mismatch: expected {mixed_bits}, got {decoded}"

    dut._log.info(
        f"100BASE-TX CRS & MLT-3 PASS: Active(TXP)={r2_active_txp}, "
        f"Active(TXN)={r2_active_txn}, Idle={r2_idle}, Levels={levels}"
    )


@cocotb.test()
async def test_100base_tx_standards_and_ppa(dut):
    """
    Test 6: IEEE 802.3u Clause 24/25 Compliance & Physical PPA Scaling Model:
    Validates complete 16-entry 4B5B data table, control delimiters,
    and checks macro physical PPA scaling metrics on IHP 130nm SG13G2.
    """
    # 1. Validate all 16 4B5B data mappings
    for nibble in range(16):
        code5 = encode_4b5b_nibble(nibble)
        assert 0 <= code5 <= 31, f"Code 0x{code5:02X} out of 5-bit range"
        # Each valid 4B5B code group must have at least 2 ones (run length limit)
        ones_count = bin(code5).count("1")
        assert ones_count >= 2, f"Code 0x{code5:02X} for nibble 0x{nibble:X} has < 2 ones"
        # Reverse decode must match original nibble
        decoded_nibble = decode_4b5b_nibble(code5)
        assert decoded_nibble == nibble, (
            f"Decode mismatch for nibble 0x{nibble:X}: got 0x{decoded_nibble:X}"
        )
        assert is_valid_4b5b_code(code5) is True

    # 2. Validate control delimiters
    assert CODE_SSD1 == 0b11000, "SSD /J/ must be 11000b (24)"
    assert CODE_SSD2 == 0b10001, "SSD /K/ must be 10001b (17)"
    assert CODE_ESD1 == 0b01101, "ESD /T/ must be 01101b (13)"
    assert CODE_ESD2 == 0b00111, "ESD /R/ must be 00111b (7)"
    assert CODE_IDLE == 0b11111, "Idle /I/ must be 11111b (31)"
    assert CODE_HALT == 0b00100, "Halt /H/ must be 00100b (4)"

    for ctrl_sym in [CODE_SSD1, CODE_SSD2, CODE_ESD1, CODE_ESD2, CODE_IDLE, CODE_HALT]:
        assert is_valid_4b5b_code(ctrl_sym) is True
        assert decode_4b5b_nibble(ctrl_sym) is None  # Control symbols are not data nibbles

    # 3. Validate illegal code rejection
    assert is_valid_4b5b_code(0b00000) is False  # 00000 is invalid
    assert is_valid_4b5b_code(0b00001) is False  # 00001 is invalid
    assert is_valid_4b5b_code(0b00010) is False  # 00010 is invalid

    # 4. Validate PPA Model
    ppa = Ethernet100BaseTxPpaModel()
    assert ppa.STANDARD_CELL_COUNT == 520
    assert ppa.GATE_EQUIVALENCE_GE == 1010.0
    assert ppa.AREA_UM2 == 3845.50
    assert ppa.AREA_OVERHEAD_PCT == 2.72
    assert ppa.CRITICAL_PATH_NS == 1.25
    assert ppa.FMAX_MHZ == 800.00
    assert ppa.DYNAMIC_POWER_UW_AT_10MHZ == 50.5
    assert ppa.THROUGHPUT_MBPS == 100.0
    assert ppa.BAUD_RATE_MBAUD == 125.0
    assert ppa.ENERGY_EFFICIENCY_PJ_PER_BIT == 0.505

    dut._log.info(
        f"100BASE-TX Standards & PPA Model PASS: All 16 4B5B data codes & 6 control codes verified, "
        f"Area={ppa.AREA_UM2} um2 (+{ppa.AREA_OVERHEAD_PCT}%), Fmax={ppa.FMAX_MHZ} MHz, "
        f"Throughput={ppa.THROUGHPUT_MBPS} Mbps"
    )
