"""
test/test_arinc429.py
=====================
Cocotb testbench for ARINC 429 Mark 33 Digital Information Transfer System (DITS)
Avionic Protocol Engine.

Test suite covers:
  1. 32-bit ARINC 429 word transmission (Barometric Altitude: Label 0o203, SDI 1,
     Data 0x12345, SSM 3) with exact dual-rail Return-to-Zero (BPRZ) pulses on
     TXA (pin 3) and TXB (pin 4), decoded and validated by Arinc429ReceiverModel.
  2. Label filtering: Receiver ingresses 8-bit label, verifies match (Label 0o203)
     and halts with clean status code R2 = 0x00.
  3. Label mismatch rejection: Receiver detects mismatched label (0o310 vs 0o203),
     rejects word and halts with error code R2 = 0xEE.
  4. SDI (Source/Destination Identifier) filtering: Ingresses word, verifies SDI match
     (SDI = 2) and records status code R2 = 0x00.
  5. Line fault / short detection: Transceiver detects simultaneous assertion of
     DATA_A=1 and DATA_B=1, cleanly asserting alarm code R2 = 0xAA.
  6. Mathematical 32-bit odd parity validation across diverse avionic words and
     physical PPA scaling model validation on IHP 130nm SG13G2.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from bootload import bootload
from arinc429_model import (
    compute_arinc429_parity,
    build_arinc429_word,
    parse_arinc429_word,
    octal_to_label,
    Arinc429ReceiverModel,
    Arinc429PpaModel,
    build_arinc429_tx_word_asm,
    build_arinc429_rx_label_filter_asm,
    build_arinc429_sdi_filter_asm,
    build_arinc429_tamper_detector_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00):
    """Reset DUT and load assembled firmware into program RAM via standard bootloader."""
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = initial_uio
    await ClockCycles(dut.clk, 10)


@cocotb.test()
async def test_arinc429_tx_word_transmission(dut):
    """
    Test 1: Transmit 32-bit ARINC 429 word from ASIC (e.g. Air Data Inertial Reference Unit).
    Word parameters: Label = 0o203 (Altitude), SDI = 1, Data = 0x12345, SSM = 3.
    Verifies dual-rail Return-to-Zero pulses on TXA (pin 3) and TXB (pin 4),
    captured and validated by Arinc429ReceiverModel.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    label = 0o203  # 131
    sdi = 1
    data = 0x12345
    ssm = 3
    expected_word = build_arinc429_word(label, sdi, data, ssm)

    half_cycles = 4
    words = build_arinc429_tx_word_asm(expected_word, tx_a_pin=3, tx_b_pin=4, half_cycles=half_cycles)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    receiver = Arinc429ReceiverModel(t_bit=half_cycles * 2, t_half=half_cycles, gap_threshold=15)

    core = dut.user_project.u_core

    # Sample execution cycles
    for _ in range(600):
        await RisingEdge(dut.clk)
        uio_out = int(dut.uio_out.value)
        data_a = (uio_out >> 3) & 1
        data_b = (uio_out >> 4) & 1
        receiver.process_cycle(data_a, data_b)
        if bool(core.halted.value) and len(receiver.received_words) >= 1:
            break

    assert bool(core.halted.value), "Transmitter should halt after transmitting ARINC 429 word"
    assert len(receiver.received_words) == 1, f"Expected 1 ARINC word, got {len(receiver.received_words)}"

    rx_word = receiver.received_words[0]
    assert rx_word["is_valid"] is True, f"Parity invalid on received word: {hex(rx_word['raw'])}"
    assert rx_word["label"] == label, f"Label mismatch: expected {oct(label)}, got {oct(rx_word['label'])}"
    assert rx_word["sdi"] == sdi, f"SDI mismatch: expected {sdi}, got {rx_word['sdi']}"
    assert rx_word["data"] == data, f"Data mismatch: expected {hex(data)}, got {hex(rx_word['data'])}"
    assert rx_word["ssm"] == ssm, f"SSM mismatch: expected {ssm}, got {rx_word['ssm']}"
    assert rx_word["raw"] == expected_word, f"Raw mismatch: expected {hex(expected_word)}, got {hex(rx_word['raw'])}"
    dut._log.info(f"ARINC 429 Word Transmission PASS: {hex(rx_word['raw'])}, Label={rx_word['label_octal']}")


@cocotb.test()
async def test_arinc429_label_filter_match(dut):
    """
    Test 2: Receiver ingresses 8-bit label, verifies match against expected (0o203).
    Core halts with success status R2 = 0x00 and captures label into R0.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_label = 0o203  # 131 = 0b10000011
    words = build_arinc429_rx_label_filter_asm(expected_label=target_label, rx_a_pin=3, rx_b_pin=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Initial null delay
    await ClockCycles(dut.clk, 10)

    # Drive 8 bits of label (bit 0 to bit 7)
    for bit_idx in range(8):
        bit_val = (target_label >> bit_idx) & 1
        active_mask = 0x08 if bit_val == 1 else 0x10  # pin 3 is A, pin 4 is B
        dut.uio_in.value = active_mask
        await ClockCycles(dut.clk, 6)
        dut.uio_in.value = 0x00  # return to null
        await ClockCycles(dut.clk, 6)

    await ClockCycles(dut.clk, 15)

    assert bool(core.halted.value), "Core should halt after matching label"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)
    assert r0 == target_label, f"Expected captured label {oct(target_label)}, got {oct(r0)}"
    assert r2 == 0x00, f"Expected status R2=0x00 (match), got {hex(r2)}"
    dut._log.info(f"ARINC 429 Label Match PASS: Label={oct(r0)}, Status={hex(r2)}")


@cocotb.test()
async def test_arinc429_label_filter_mismatch(dut):
    """
    Test 3: Receiver detects mismatched label (0o310 vs 0o203), rejects and
    halts with error code R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    expected_label = 0o203  # 131
    mismatched_label = 0o310  # 200 = 0b11001000
    words = build_arinc429_rx_label_filter_asm(expected_label=expected_label, rx_a_pin=3, rx_b_pin=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    await ClockCycles(dut.clk, 10)

    # Drive mismatched label bits
    for bit_idx in range(8):
        bit_val = (mismatched_label >> bit_idx) & 1
        active_mask = 0x08 if bit_val == 1 else 0x10
        dut.uio_in.value = active_mask
        await ClockCycles(dut.clk, 6)
        dut.uio_in.value = 0x00
        await ClockCycles(dut.clk, 6)

    await ClockCycles(dut.clk, 15)

    assert bool(core.halted.value), "Core should halt on label mismatch"
    r2 = int(core.r2.value)
    assert r2 == 0xEE, f"Expected error code R2=0xEE (mismatch), got {hex(r2)}"
    dut._log.info(f"ARINC 429 Label Mismatch Rejection PASS: Status={hex(r2)}")


@cocotb.test()
async def test_arinc429_sdi_filtering(dut):
    """
    Test 4: Receiver skips 8 label bits and verifies SDI match (SDI = 2).
    Halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_sdi = 2  # 0b10 (bit 0 is 0, bit 1 is 1)
    words = build_arinc429_sdi_filter_asm(target_sdi=target_sdi, rx_a_pin=3, rx_b_pin=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    await ClockCycles(dut.clk, 10)

    # Send 8 label bits (e.g. all 1s)
    for _ in range(8):
        dut.uio_in.value = 0x08  # Pin 3 high
        await ClockCycles(dut.clk, 5)
        dut.uio_in.value = 0x00
        await ClockCycles(dut.clk, 5)

    # Send 2 SDI bits: bit 0 is 0 (pin 4 pulse), bit 1 is 1 (pin 3 pulse)
    # Bit 0: 0
    dut.uio_in.value = 0x10
    await ClockCycles(dut.clk, 5)
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 5)

    # Bit 1: 1
    dut.uio_in.value = 0x08
    await ClockCycles(dut.clk, 5)
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 5)

    await ClockCycles(dut.clk, 15)

    assert bool(core.halted.value), "Core should halt after matching SDI"
    r2 = int(core.r2.value)
    assert r2 == 0x00, f"Expected status R2=0x00 (SDI match), got {hex(r2)}"
    dut._log.info(f"ARINC 429 SDI Filter Match PASS: Status={hex(r2)}")


@cocotb.test()
async def test_arinc429_tamper_short_detection(dut):
    """
    Test 5: Transceiver line fault / short detection.
    When DATA_A=1 and DATA_B=1 are asserted simultaneously, core traps line short
    alarm code R2 = 0xAA and halts.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_arinc429_tamper_detector_asm(rx_a_pin=3, rx_b_pin=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Normal idle for 10 cycles
    await ClockCycles(dut.clk, 10)
    assert not bool(core.halted.value), "Core should remain active during normal line states"

    # Inject line short fault: assert both Pin 3 (0x08) and Pin 4 (0x10) simultaneously
    dut.uio_in.value = 0x18
    await ClockCycles(dut.clk, 10)

    assert bool(core.halted.value), "Core should halt immediately upon line short fault"
    r2 = int(core.r2.value)
    assert r2 == 0xAA, f"Expected alarm code R2=0xAA, got {hex(r2)}"
    dut._log.info(f"ARINC 429 Line Fault / Short Detection PASS: Status={hex(r2)}")


@cocotb.test()
async def test_arinc429_odd_parity_mathematical_validation_and_ppa(dut):
    """
    Test 6: Mathematical validation of 32-bit odd parity across diverse avionic words,
    100% single-bit error rejection across all 32 bit positions, and physical PPA scaling.
    """
    test_vectors = [
        (0o000, 0, 0x00000, 0),
        (0o203, 1, 0x12345, 3),
        (0o310, 2, 0x7FFFF, 1),
        (0o377, 3, 0x55555, 2),
        (0o123, 0, 0x2AAAA, 3),
    ]

    for lbl, sdi, data, ssm in test_vectors:
        word32 = build_arinc429_word(lbl, sdi, data, ssm)
        parsed = parse_arinc429_word(word32)
        assert parsed["is_valid"] is True, f"Parity check failed on {hex(word32)}"
        assert parsed["label"] == lbl
        assert parsed["sdi"] == sdi
        assert parsed["data"] == data
        assert parsed["ssm"] == ssm

        # Single-bit corruption across all 32 bits
        for bit_pos in range(32):
            corrupted = word32 ^ (1 << bit_pos)
            bad_parsed = parse_arinc429_word(corrupted)
            assert bad_parsed["is_valid"] is False, f"Corruption undetected at bit {bit_pos}"

    # PPA Model validation
    ppa = Arinc429PpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 412
    assert ppa["critical_path_ns"] < 2.0
    assert ppa["f_max_mhz"] > 500.0

    dut._log.info(
        f"ARINC 429 Mathematical Parity (100% Single-Bit Rejection) & PPA Model PASS: "
        f"Cells={ppa['standard_cells']}, GE={ppa['gate_equivalents']}, Fmax={ppa['f_max_mhz']:.1f} MHz"
    )
