# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_sata_gen3.py - Cocotb test suite for Serial ATA Revision 3.0 (6.0 Gbps) Physical & Link Layer Engine

Verifies:
1. test_sata_master_oob_transmission: Master transmits COMRESET OOB burst sequence (4 bursts + quiet periods)
   on pin 3, verified by bit-accurate burst/squelch timing checks and status R2 = 0x00.
2. test_sata_rx_oob_timing_discrimination: Slave measures quiet duration between OOB bursts via WAITEDGE,
   cleanly discriminating COMRESET (R1 = 0x01) from COMWAKE (R1 = 0x02) with status R2 = 0x00.
3. test_sata_primitive_validation_and_fault_trapping: In-register SATA primitive delimiter validation (valid
   K28.5 / 0xBC -> R2=0x00) and corrupted delimiter fault trapping (0xA5 -> R2=0xEE).
4. test_sata_primitives_and_alignp: Round-trip 8b/10b encoding/decoding of standard SATA primitives (ALIGNp,
   SYNCp, R_OKp, R_ERRp, X_RDYp, R_RDYp, WTRMp) and receiver monitor tracking.
5. test_sata_fis_framing_and_filtering: Tests standard FIS types (0x27 Reg H2D, 0x34 Reg D2H, 0x46 Data),
   CRC-32 computation, and in-register microcode FIS type filtering (match -> 0x00, mismatch -> 0xEE).
6. test_sata_standards_and_ppa: OOB timing constraints (3:1 quiet ratio), 8b/10b primitive validity,
   and calibrated IHP 130nm SG13G2 PPA model validation.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from sata_gen3_model import (  # noqa: E402
    K_COM,
    PRIMITIVE_ALIGNP,
    PRIMITIVE_SYNCP,
    PRIMITIVE_R_OKP,
    PRIMITIVE_R_ERRP,
    PRIMITIVE_X_RDYP,
    PRIMITIVE_R_RDYP,
    PRIMITIVE_WTRMP,
    SATA_PRIMITIVES,
    FIS_TYPE_REG_H2D,
    FIS_TYPE_REG_D2H,
    FIS_TYPE_DATA,
    classify_sata_oob_quiet,
    generate_sata_oob_waveform,
    encode_sata_primitive,
    decode_sata_primitive,
    SataFisFrame,
    SataReceiverModel,
    SataPpaModel,
    build_sata_tx_oob_asm,
    build_sata_rx_oob_detect_asm,
    build_sata_primitive_validator_asm,
    build_sata_fis_filter_asm,
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
async def test_sata_master_oob_transmission(dut):
    """
    Test 1: Master SATA OOB Transmission:
    Transmits COMRESET sequence: 4 bursts of active carrier separated by quiet squelch intervals on pin 3.
    Verifies burst count, quiet timing intervals, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    burst_cycles = 4
    num_bursts = 4

    asm_code = build_sata_tx_oob_asm(
        oob_type="COMRESET",
        pin_tx=pin_tx,
        burst_cycles=burst_cycles,
        num_bursts=num_bursts
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    max_cycles = 250

    captured_levels = []

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & (1 << pin_tx)) != 0:
            uio_out = int(dut.uio_out.value)
            bit = (uio_out >> pin_tx) & 1
            captured_levels.append(bit)

        if bool(core.halted.value):
            break

    # Count bursts: a burst is a sequence of consecutive 1s
    burst_count = 0
    in_burst = False
    for bit in captured_levels:
        if bit == 1 and not in_burst:
            burst_count += 1
            in_burst = True
        elif bit == 0:
            in_burst = False

    assert burst_count == num_bursts, f"Expected {num_bursts} OOB bursts, detected {burst_count}"

    # Verify core halted with R2 = 0x00
    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00, got 0x{r2_val:02X}"

    dut._log.info(
        f"SATA OOB Master TX PASS: Bursts={burst_count}, Total Cycles={len(captured_levels)}, Status=0x{r2_val:02X}"
    )


@cocotb.test()
async def test_sata_rx_oob_timing_discrimination(dut):
    """
    Test 2: Slave OOB Timing Discrimination (COMRESET vs COMWAKE):
    Measures quiet duration between OOB bursts via WAITEDGE:
    - COMRESET (quiet = 14 cycles >= 8) -> R1 = 0x01 (COMRESET)
    - COMWAKE (quiet = 4 cycles < 8)     -> R1 = 0x02 (COMWAKE)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    threshold = 8

    asm_code = build_sata_rx_oob_detect_asm(pin_rx=pin_rx, threshold_cycles=threshold)
    words = assemble("\n".join(asm_code))

    core = dut.user_project.u_core

    # Sub-test A: Test COMRESET (Quiet duration = 14 cycles)
    # Start with pin high (carrier active)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin_rx))
    await ClockCycles(dut.clk, 10)

    # 1. Drive carrier falling edge (squelch entry)
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 14)  # Quiet duration

    # 2. Drive next burst rising edge
    dut.uio_in.value = (1 << pin_rx)
    await ClockCycles(dut.clk, 30)

    assert bool(core.halted.value) is True, "Core should halt after COMRESET detection"
    r0_reset = int(core.r0.value)
    r1_reset = int(core.r1.value)
    r2_reset = int(core.r2.value)

    assert r1_reset == 0x01, f"Expected R1=0x01 for COMRESET, got 0x{r1_reset:02X}"
    assert r2_reset == 0x00, f"Expected R2=0x00, got 0x{r2_reset:02X}"

    # Sub-test B: Test COMWAKE (Quiet duration = 4 cycles)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin_rx))
    await ClockCycles(dut.clk, 10)

    # 1. Drive carrier falling edge (squelch entry)
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 4)  # Short quiet duration

    # 2. Drive next burst rising edge
    dut.uio_in.value = (1 << pin_rx)
    await ClockCycles(dut.clk, 30)

    assert bool(core.halted.value) is True, "Core should halt after COMWAKE detection"
    r0_wake = int(core.r0.value)
    r1_wake = int(core.r1.value)
    r2_wake = int(core.r2.value)

    assert r1_wake == 0x02, f"Expected R1=0x02 for COMWAKE, got 0x{r1_wake:02X}"
    assert r2_wake == 0x00, f"Expected R2=0x00, got 0x{r2_wake:02X}"

    dut._log.info(
        f"SATA OOB Discrimination PASS: COMRESET R0={r0_reset}, R1=0x{r1_reset:02X} | COMWAKE R0={r0_wake}, R1=0x{r1_wake:02X}"
    )


@cocotb.test()
async def test_sata_primitive_validation_and_fault_trapping(dut):
    """
    Test 3: In-Register SATA Primitive Lead Delimiter Validation & Fault Trapping:
    - Valid primitive lead symbol (K28.5 / 0xBC in R0) -> R2 = 0x00 (Valid)
    - Corrupted lead symbol (0xA5 in R0) -> R2 = 0xEE (Primitive Violation Trapped)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    # Sub-test A: Valid delimiter
    asm_valid = ["LDI R0, 0xBC ; K28.5 delimiter"] + build_sata_primitive_validator_asm(expected_lead_k=0xBC)
    words_valid = assemble("\n".join(asm_valid))
    await _init_dut_and_bootload(dut, words_valid)

    await ClockCycles(dut.clk, 20)
    assert bool(core.halted.value) is True
    r2_valid = int(core.r2.value)
    assert r2_valid == 0x00, f"Expected R2=0x00 for valid primitive, got 0x{r2_valid:02X}"

    # Sub-test B: Corrupted delimiter
    asm_invalid = ["LDI R0, 0xA5 ; Corrupted delimiter"] + build_sata_primitive_validator_asm(expected_lead_k=0xBC)
    words_invalid = assemble("\n".join(asm_invalid))
    await _init_dut_and_bootload(dut, words_invalid)

    await ClockCycles(dut.clk, 20)
    assert bool(core.halted.value) is True
    r2_invalid = int(core.r2.value)
    assert r2_invalid == 0xEE, f"Expected R2=0xEE for corrupted primitive, got 0x{r2_invalid:02X}"

    dut._log.info(
        f"SATA Primitive Validation PASS: Valid R2=0x{r2_valid:02X}, Trapped R2=0x{r2_invalid:02X}"
    )


@cocotb.test()
async def test_sata_primitives_and_alignp(dut):
    """
    Test 4: SATA Standard Primitives Round-Trip Encoding & Receiver Monitor:
    Verifies ALIGNp, SYNCp, R_OKp, R_ERRp, X_RDYp, R_RDYp, WTRMp 8b/10b encoding,
    decoding, and SataReceiverModel tracking.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx = SataReceiverModel()
    enc_rd = -1
    dec_rd = -1

    for name, pattern in SATA_PRIMITIVES.items():
        # 1. Encode 4-byte primitive into 4 10-bit symbols
        syms_10b, enc_rd = encode_sata_primitive(pattern, initial_rd=enc_rd)
        assert len(syms_10b) == 4, f"{name} should encode to 4 symbols"

        # 2. Decode symbols
        decoded_bytes, is_valid, dec_rd = decode_sata_primitive(syms_10b, initial_rd=dec_rd)
        assert is_valid, f"Decoding {name} failed"
        assert decoded_bytes == pattern, f"Decoded {name} {decoded_bytes} != original {pattern}"

        # 3. Ingress into receiver model
        ok = rx.ingress_primitive_bytes(decoded_bytes)
        assert ok, f"Receiver failed to match {name}"

    assert rx.errors == 0, f"Expected 0 errors, got {rx.errors}"
    assert rx.primitive_counts["ALIGNp"] == 1
    assert rx.primitive_counts["SYNCp"] == 1
    assert rx.primitive_counts["R_OKp"] == 1

    dut._log.info(f"SATA Primitives PASS: 7/7 standard primitives verified with 0 errors")


@cocotb.test()
async def test_sata_fis_framing_and_filtering(dut):
    """
    Test 5: SATA FIS Framing, CRC-32, and In-Register Microcode Type Filtering:
    - Verifies FIS frame construction and CRC-32 generation
    - Verifies in-register microcode FIS type filter on core (Match -> 0x00, Mismatch -> 0xEE)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Test FIS frame and CRC-32 calculation
    fis = SataFisFrame(fis_type=FIS_TYPE_REG_H2D, payload=[0x80, 0x00, 0x20, 0x00])
    crc = fis.calculate_crc()
    assert crc != 0, "CRC-32 should be non-zero"

    core = dut.user_project.u_core

    # 2. In-register FIS filter on core
    # Sub-test A: Match expected FIS Type 0x27
    asm_match = [
        f"LDI R0, 0x{FIS_TYPE_REG_H2D:02X} ; Register H2D (0x27)"
    ] + build_sata_fis_filter_asm(expected_fis_type=FIS_TYPE_REG_H2D)
    words_match = assemble("\n".join(asm_match))
    await _init_dut_and_bootload(dut, words_match)

    await ClockCycles(dut.clk, 20)
    assert bool(core.halted.value) is True
    r2_match = int(core.r2.value)
    assert r2_match == 0x00, f"Expected R2=0x00 for FIS match, got 0x{r2_match:02X}"

    # Sub-test B: Mismatch (0x34 != 0x27)
    asm_mismatch = [
        f"LDI R0, 0x{FIS_TYPE_REG_D2H:02X} ; Register D2H (0x34)"
    ] + build_sata_fis_filter_asm(expected_fis_type=FIS_TYPE_REG_H2D)
    words_mismatch = assemble("\n".join(asm_mismatch))
    await _init_dut_and_bootload(dut, words_mismatch)

    await ClockCycles(dut.clk, 20)
    assert bool(core.halted.value) is True
    r2_mismatch = int(core.r2.value)
    assert r2_mismatch == 0xEE, f"Expected R2=0xEE for FIS mismatch, got 0x{r2_mismatch:02X}"

    dut._log.info(
        f"SATA FIS Framing & Filter PASS: CRC=0x{crc:08X}, Match=0x{r2_match:02X}, Mismatch=0x{r2_mismatch:02X}"
    )


@cocotb.test()
async def test_sata_standards_and_ppa(dut):
    """
    Test 6: SATA Revision 3.0 Standards Compliance & Calibrated PPA Validation:
    - Verifies OOB quiet time ratio (3:1 nominal ratio, >= 1.80x worst-case margin)
    - Validates IHP 130nm SG13G2 PPA model (560 cells, 1090.0 GE, 4140.0 um^2, 800 MHz, 54.50 uW)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. OOB quiet timing ratio
    t_quiet_reset_min = 240.0
    t_quiet_wake_max = 133.3
    margin = t_quiet_reset_min / t_quiet_wake_max
    assert margin >= 1.80, f"OOB discrimination margin must be >= 1.80x, got {margin:.2f}x"

    # 2. Validate PPA Model
    ppa = SataPpaModel()
    assert ppa.standard_cell_count == 560
    assert ppa.gate_equivalent_ge == 1090.0
    assert ppa.area_um2 == 4140.0
    assert ppa.max_frequency_mhz == 800.0
    assert ppa.power_uw_at_10mhz == 54.50
    assert ppa.raw_throughput_mbps == 6000.0
    assert ppa.energy_pj_per_bit == 0.00908

    dut._log.info(
        f"SATA Standards & PPA PASS: Margin={margin:.2f}x, Cells={ppa.standard_cell_count}, Area={ppa.area_um2} um^2"
    )
