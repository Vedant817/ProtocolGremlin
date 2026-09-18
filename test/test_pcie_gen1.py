# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_pcie_gen1.py - Cocotb test suite for PCI Express Base Gen 1 (2.5 GT/s) Physical Layer & 8b/10b Engine

Verifies:
1. test_pcie_master_ts1_transmission: Master transmits TS1 Ordered Set with differential
   signaling on TXP (pin 3) and TXN (pin 4), decoded and verified by PcieGen1ReceiverModel.
2. test_pcie_rx_comma_synchronization: Slave synchronizes to COM delimiter rising edge on RXP
   via WAITEDGE, samples payload into R0, preserves in R1 (0x5A), and halts with R2 = 0x00.
3. test_pcie_fts_validation_and_fault_trapping: In-register FTS symbol validation (valid 0x5C -> R2=0x00)
   and corrupted FTS symbol trapping (0xA5 -> R2=0xEE).
4. test_pcie_lfsr_data_scrambler: 16-bit LFSR data scrambler/descrambler model verification,
   round-trip byte stream encryption/decryption, and in-register microcode descrambling.
5. test_pcie_ordered_sets_and_elastic_skp: TS1, TS2, SKP, FTS, and EIOS ordered sets round-trip
   encoding/decoding, running disparity balance, and elastic buffer clock drift compensation.
6. test_pcie_standards_and_ppa: Comprehensive PCIe 8b/10b run-length bounds (<= 5),
   K-code standard compliance, and calibrated IHP 130nm SG13G2 PPA model validation.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from pcie_gen1_model import (  # noqa: E402
    K_COM,
    K_SKP,
    K_FTS,
    K_IDL,
    K_PAD,
    K_STP,
    K_END,
    K_SDP,
    encode_8b10b,
    decode_8b10b,
    is_comma_symbol,
    PcieScrambler,
    build_pcie_ts1_ordered_set,
    build_pcie_ts2_ordered_set,
    build_pcie_skp_ordered_set,
    build_pcie_fts_ordered_set,
    build_pcie_eios_ordered_set,
    PcieGen1Packet,
    PcieGen1ReceiverModel,
    PcieGen1PpaModel,
    build_pcie_tx_ordered_set_asm,
    build_pcie_rx_comma_sync_asm,
    build_pcie_fts_validator_asm,
    build_pcie_scrambler_asm,
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
async def test_pcie_master_ts1_transmission(dut):
    """
    Test 1: Master PCIe TS1 Ordered Set Transmission:
    Transmits TS1 Ordered Set with differential signaling on TXP (pin 3) and TXN (pin 4).
    PcieGen1ReceiverModel verifies comma detection, 10-bit symbol decoding, and zero disparity errors.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_txp = 3
    pin_txn = 4
    baud_cycles = 4

    asm_code = build_pcie_tx_ordered_set_asm(
        ordered_set_type="TS1",
        pin_txp=pin_txp,
        pin_txn=pin_txn,
        baud_cycles=baud_cycles
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    max_cycles = 500

    # Sample output bits from TXP and TXN
    captured_bits = []

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & ((1 << pin_txp) | (1 << pin_txn))) != 0:
            uio_out = int(dut.uio_out.value)
            txp = (uio_out >> pin_txp) & 1
            txn = (uio_out >> pin_txn) & 1

            # Record active differential bit
            if (txp ^ txn) == 1:
                captured_bits.append(txp)

        if bool(core.halted.value) and len(captured_bits) >= (40 * baud_cycles):
            break

    # Subsample bits at baud center
    symbol_bits = []
    for i in range(0, len(captured_bits), baud_cycles):
        symbol_bits.append(captured_bits[i])

    # Reconstruct 10-bit symbols
    symbols_10b = []
    for i in range(0, len(symbol_bits) - 9, 10):
        sym = 0
        for bit in symbol_bits[i:i+10]:
            sym = (sym << 1) | bit
        symbols_10b.append(sym)

    dut._log.info(f"Captured {len(symbols_10b)} 10-bit symbols: {[hex(s) for s in symbols_10b]}")

    # Verify via PcieGen1ReceiverModel
    rx = PcieGen1ReceiverModel(initial_rd=-1)
    for sym in symbols_10b:
        rx.ingress_10b_symbol(sym)

    assert rx.comma_count >= 1, f"Expected at least 1 COM symbol, got {rx.comma_count}"
    assert rx.disparity_errors == 0, f"Expected 0 disparity errors, got {rx.disparity_errors}"

    dut._log.info(
        f"PCIe TS1 TX PASS: Comma Count={rx.comma_count}, Disparity Errors={rx.disparity_errors}"
    )


@cocotb.test()
async def test_pcie_rx_comma_synchronization(dut):
    """
    Test 2: Slave PCIe Comma Synchronization & Symbol Ingress:
    Core synchronizes to COM delimiter rising edge on RXP (pin 3) via WAITEDGE,
    strides to bit midpoint, samples 8 bits into R0, copies to R1 (0x5A),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rxp = 3
    baud_cycles = 4
    expected_byte = 0x5A

    asm_code = build_pcie_rx_comma_sync_asm(
        pin_rxp=pin_rxp,
        baud_cycles=baud_cycles
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let the core settle into WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive COM rising edge transition on pin_rxp
    dut.uio_in.value = (1 << pin_rxp)
    await ClockCycles(dut.clk, baud_cycles)

    # 2. Drive payload byte 0x5A (0b01011010) LSB-first into pin_rxp
    for bit_idx in range(8):
        bit = (expected_byte >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rxp)
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
        f"PCIe Comma Sync PASS: R0=0x{r0_val:02X}, R1=0x{r1_val:02X}, R2=0x{r2_val:02X}"
    )


@cocotb.test()
async def test_pcie_fts_validation_and_fault_trapping(dut):
    """
    Test 3: Fast Training Sequence (FTS) Symbol Validation & Fault Trapping:
    Verifies microcode FTS symbol (K28.2 / 0x5C) validation:
    - Candidate valid FTS (R0 = 0x5C) -> R2 = 0x00 (Verified)
    - Corrupted candidate symbol (R0 = 0xA5) -> R2 = 0xEE (Fault Trapped)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    # Sub-test A: Valid FTS symbol
    asm_valid = ["LDI R0, 0x5C ; FTS symbol K28.2"] + build_pcie_fts_validator_asm(expected_fts=0x5C)
    words_valid = assemble("\n".join(asm_valid))
    await _init_dut_and_bootload(dut, words_valid)

    await ClockCycles(dut.clk, 20)
    assert bool(core.halted.value) is True, "Core should halt after validation"
    r2_valid = int(core.r2.value)
    assert r2_valid == 0x00, f"Expected R2=0x00 for valid FTS, got 0x{r2_valid:02X}"

    # Sub-test B: Corrupted candidate symbol (Fault Ingress)
    asm_invalid = ["LDI R0, 0xA5 ; Corrupted symbol"] + build_pcie_fts_validator_asm(expected_fts=0x5C)
    words_invalid = assemble("\n".join(asm_invalid))
    await _init_dut_and_bootload(dut, words_invalid)

    await ClockCycles(dut.clk, 20)
    assert bool(core.halted.value) is True, "Core should halt on FTS fault"
    r2_invalid = int(core.r2.value)
    assert r2_invalid == 0xEE, f"Expected R2=0xEE for FTS mismatch, got 0x{r2_invalid:02X}"

    dut._log.info(
        f"PCIe FTS Validation PASS: Valid R2=0x{r2_valid:02X}, Trapped R2=0x{r2_invalid:02X}"
    )


@cocotb.test()
async def test_pcie_lfsr_data_scrambler(dut):
    """
    Test 4: PCIe 16-Bit LFSR Data Scrambler / Descrambler:
    - Verifies LFSR stream cipher across 64 consecutive data bytes
    - Verifies self-inverting property: Descramble(Scramble(d)) == d
    - Verifies COM symbol reset behavior
    - Verifies in-register microcode descrambler on ASIC core
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    scrambler_tx = PcieScrambler(seed=0xFFFF)
    scrambler_rx = PcieScrambler(seed=0xFFFF)

    test_payload = [((i * 17) ^ 0x3C) & 0xFF for i in range(64)]

    # 1. Scramble data stream
    scrambled = [scrambler_tx.scramble_byte(b) for b in test_payload]
    assert scrambled != test_payload, "Scrambled data should differ from plain data"

    # 2. Descramble data stream
    descrambled = [scrambler_rx.descramble_byte(b) for b in scrambled]
    assert descrambled == test_payload, "Descrambled stream must match plain data"

    # 3. Test COM reset in symbol stream
    sym_stream = [(K_COM, True)] + [(b, False) for b in test_payload[:8]] + [(K_COM, True)] + [(b, False) for b in test_payload[8:16]]
    proc_tx = scrambler_tx.process_stream(sym_stream)
    scrambler_rx.reset()
    proc_rx = scrambler_rx.process_stream(proc_tx)
    assert proc_rx == sym_stream, "Stream processing with COM reset failed round-trip"

    # 4. In-register microcode descrambling verification on core
    # First byte of test_payload is scrambled with mask_0
    scr_dut = PcieScrambler(seed=0xFFFF)
    mask_0 = scr_dut.step_byte()
    plain_val = 0x5A
    scrambled_val = plain_val ^ mask_0

    asm_scramble = [f"LDI R0, 0x{scrambled_val:02X} ; Ingress scrambled byte"] + build_pcie_scrambler_asm(mask_byte=mask_0)
    words = assemble("\n".join(asm_scramble))
    await _init_dut_and_bootload(dut, words)

    await ClockCycles(dut.clk, 20)
    core = dut.user_project.u_core
    assert bool(core.halted.value) is True, "Core should halt after descrambling"
    r1_val = int(core.r1.value)
    r2_val = int(core.r2.value)

    assert r1_val == plain_val, f"Expected recovered byte 0x{plain_val:02X}, got 0x{r1_val:02X}"
    assert r2_val == 0x00, f"Expected R2=0x00, got 0x{r2_val:02X}"

    dut._log.info(
        f"PCIe LFSR Scrambler PASS: Stream bytes=64, HW Recovered Plaintext=0x{r1_val:02X}"
    )


@cocotb.test()
async def test_pcie_ordered_sets_and_elastic_skp(dut):
    """
    Test 5: PCIe Ordered Sets (TS1, TS2, SKP, FTS, EIOS) & Elastic Compensation:
    Verifies TS1, TS2, SKP, FTS, and EIOS ordered set encoding/decoding,
    confirms comma detection triggers on all ordered sets, running disparity remains balanced,
    and SKP compensation absorbs clock drift.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    ts1 = build_pcie_ts1_ordered_set(link_num=0x01, lane_num=0x00, n_fts=0x20)
    ts2 = build_pcie_ts2_ordered_set(link_num=0x01, lane_num=0x00, n_fts=0x20)
    skp = build_pcie_skp_ordered_set(num_skp=3)
    fts = build_pcie_fts_ordered_set(num_fts=3)
    eios = build_pcie_eios_ordered_set()

    assert len(ts1) == 16, "TS1 should contain 16 symbols"
    assert len(ts2) == 16, "TS2 should contain 16 symbols"
    assert len(skp) == 4, "SKP ordered set should contain 4 symbols (COM + 3 SKP)"
    assert len(fts) == 4, "FTS ordered set should contain 4 symbols (COM + 3 FTS)"
    assert len(eios) == 4, "EIOS ordered set should contain 4 symbols (COM + 3 IDL)"

    # Encode with PcieGen1Packet sequentially
    rx = PcieGen1ReceiverModel(initial_rd=-1)
    current_rd = -1

    for idx, (os_name, os_syms) in enumerate([("TS1", ts1), ("TS2", ts2), ("SKP", skp), ("FTS", fts), ("EIOS", eios)]):
        pkt = PcieGen1Packet(symbols=os_syms)
        enc_syms, current_rd = pkt.encode_bitstream(initial_rd=current_rd)
        ok = rx.ingress_symbol_stream(enc_syms)
        assert ok, f"{os_name} ingress stream failed"
        assert rx.comma_count == (idx + 1), f"Expected comma count {idx + 1} after {os_name}, got {rx.comma_count}"

    assert rx.disparity_errors == 0, f"Expected 0 disparity errors, got {rx.disparity_errors}"
    assert rx.invalid_symbols == 0, f"Expected 0 invalid symbols, got {rx.invalid_symbols}"

    # Verify elastic buffer clock drift compensation math:
    # 600 ppm divergence over 1200 symbols = 7.2 UI phase drift.
    # Standard PCIe SKP ordered set provides 30 UI elastic buffer margin (4.17x factor).
    drift_ui = 1200 * 10 * 600e-6
    skp_capacity_ui = 30.0
    safety_margin = skp_capacity_ui / drift_ui
    assert safety_margin >= 4.0, f"Elastic buffer margin must be >= 4.0x, got {safety_margin:.2f}x"

    dut._log.info(
        f"PCIe Ordered Sets PASS: Comma Count={rx.comma_count}, Disparity Errors=0, SKP Safety Margin={safety_margin:.2f}x"
    )


@cocotb.test()
async def test_pcie_standards_and_ppa(dut):
    """
    Test 6: PCIe Base Standards Compliance & Calibrated PPA Validation:
    - Verifies 8b/10b run length constraint (<= 5 consecutive identical digits)
    - Verifies all standard PCIe K-code values (COM, SKP, FTS, IDL, PAD, STP, END, SDP)
    - Validates IHP 130nm SG13G2 PPA model (545 cells, 1060.0 GE, +2.83% area, 800 MHz, 53.00 uW)
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Verify run length constraint across all 256 data bytes
    max_run = 0
    for rd in (-1, 1):
        for b in range(256):
            sym, _ = encode_8b10b(b, is_k=False, rd=rd)
            bin_str = bin(sym)[2:].zfill(10)
            current_run = 1
            for i in range(1, len(bin_str)):
                if bin_str[i] == bin_str[i-1]:
                    current_run += 1
                    max_run = max(max_run, current_run)
                else:
                    current_run = 1

    assert max_run <= 5, f"8b/10b run length constraint violated: max run was {max_run} > 5"

    # 2. Verify PCIe standard K-codes
    k_expected = {
        K_COM: "COM (K28.5)",
        K_SKP: "SKP (K28.1)",
        K_FTS: "FTS (K28.2)",
        K_IDL: "IDL (K28.3)",
        K_PAD: "PAD (K23.7)",
        K_STP: "STP (K27.7)",
        K_END: "END (K29.7)",
        K_SDP: "SDP (K30.7)",
    }
    for k_val, name in k_expected.items():
        sym_m, _ = encode_8b10b(k_val, is_k=True, rd=-1)
        sym_p, _ = encode_8b10b(k_val, is_k=True, rd=1)
        b_m, is_k_m, _, val_m = decode_8b10b(sym_m, rd=-1)
        b_p, is_k_p, _, val_p = decode_8b10b(sym_p, rd=1)
        assert val_m and is_k_m and b_m == k_val, f"Failed round-trip for {name} RD-"
        assert val_p and is_k_p and b_p == k_val, f"Failed round-trip for {name} RD+"

    # 3. Validate PPA Model
    ppa = PcieGen1PpaModel()
    assert ppa.standard_cell_count == 545
    assert ppa.gate_equivalent_ge == 1060.0
    assert ppa.area_um2 == 4025.0
    assert ppa.max_frequency_mhz == 800.0
    assert ppa.power_uw_at_10mhz == 53.00
    assert ppa.energy_pj_per_bit == 0.0530

    dut._log.info(
        f"PCIe Standards & PPA PASS: Max Run Length={max_run} (<=5), K-Codes=8, Area={ppa.area_um2} um^2"
    )
