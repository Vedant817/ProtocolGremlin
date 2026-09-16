# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_spacewire.py
======================
Cocotb testbench for SpaceWire (ECSS-E-ST-50-52C) Data-Strobe (DS) Spacecraft
Serial Bus Protocol Engine on the Jane Street Protocol Emulator ASIC.

Test suite covers:
  1. SpaceWire Master Transmission: ASIC transmits NULL token, Data bytes [0x55, 0xAA, 0x3C],
     and EOP token using Data-Strobe line coding on uio[0] (D) and uio[1] (S);
     decoded and verified cycle-by-cycle against independent SpaceWireReceiverModel.
  2. SpaceWire Slave Reception: ASIC synchronizes on initial DS transition, ingresses
     10-bit Data character (0x96), verifies odd parity, and captures payload into R0
     with status R2 = 0x00.
  3. SpaceWire Control Token Reception: ASIC ingresses and classifies 4-bit Control Tokens
     (EOP token ID 1) with status R2 = 0x00.
  4. SpaceWire Parity Error Trapping: Inverted parity bit is detected during character
     reception and safely trapped with status code R2 = 0xEE.
  5. SpaceWire Credit Flow Control: Microcode credit accounting decrements credit buffer,
     trapping credit exhaustion (R2 = 0xCC) and restoring credits on FCT arrival.
  6. SpaceWire Standards Mathematical Invariant & Coprocessor PPA Scaling Validation.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from bootload import bootload
from spacewire_model import (
    CONTROL_FCT,
    CONTROL_EOP,
    CONTROL_EEP,
    CONTROL_ESC,
    STATUS_OK,
    STATUS_PARITY_ERROR,
    STATUS_CREDIT_EXHAUSTED,
    encode_control_char,
    encode_data_char,
    encode_null_token,
    encode_time_code,
    encode_ds_stream,
    decode_ds_stream,
    compute_spacewire_parity,
    verify_spacewire_parity,
    SpaceWireReceiverModel,
    SpaceWirePpaModel,
    build_spacewire_tx_packet_asm,
    build_spacewire_rx_char_asm,
    build_spacewire_rx_token_asm,
    build_spacewire_credit_tracker_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00, wait_settle: bool = False):
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

    if wait_settle:
        # Let the core settle into LD_DONE and enter user execution at PC 0
        for _ in range(6):
            await RisingEdge(dut.clk)


@cocotb.test()
async def test_spacewire_master_tx_packet(dut):
    """
    Test 1: ASIC transmits SpaceWire packet (NULL + Data [0x55, 0xAA, 0x3C] + EOP)
    via Data-Strobe signaling on uio[0] (Data) and uio[1] (Strobe).
    Verified cycle-by-cycle against independent SpaceWireReceiverModel.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = [0x55, 0xAA, 0x3C]
    bit_period = 4
    words = build_spacewire_tx_packet_asm(payload, bit_period=bit_period)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    receiver = SpaceWireReceiverModel(init_d=0, init_s=0)
    core = dut.user_project.u_core

    max_cycles = 600
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_out = int(dut.uio_out.value)
        d = uio_out & 1
        s = (uio_out >> 1) & 1

        receiver.step(d, s)

        if int(core.halted.value) == 1:
            break

    assert int(core.halted.value) == 1, "SpaceWire transmitter failed to complete/halt"
    assert receiver.received_packet_bytes == payload, (
        f"Mismatch in received packet bytes: expected {payload}, got {receiver.received_packet_bytes}"
    )
    assert receiver.parity_errors == 0, f"SpaceWire parity errors detected: {receiver.parity_errors}"

    # Verify that NULL token (ESC + FCT) was decoded prior to data
    token_types = [t["type"] for t in receiver.decoded_tokens]
    assert token_types[0] == "ESC", f"Expected ESC token, got {token_types[0]}"
    assert token_types[1] == "FCT", f"Expected FCT token, got {token_types[1]}"
    assert token_types[-1] == "EOP", f"Expected EOP token at end, got {token_types[-1]}"

    # Verify physical electrical safety: bus released to High-Z upon completion
    assert int(dut.uio_oe.value) == 0x00, "uio pins not released to High-Z after transmission"


@cocotb.test()
async def test_spacewire_rx_data_character(dut):
    """
    Test 2: ASIC acts as SpaceWire receiver. Testbench drives a 10-bit Data character
    (0x96) on uio[2] (Data) and uio[3] (Strobe) using DS encoding. Core synchronizes,
    ingresses data, checks odd parity, and captures 0x96 into R0 with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    bit_period = 8
    words = build_spacewire_rx_char_asm(bit_period=bit_period)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00, wait_settle=True)

    # Encode Data Character 0x96: [P, C=0, D0..D7]
    # 0x96 = 0b10010110 (4 ones -> popcount even -> P = 1)
    char_bits = encode_data_char(0x96)
    symbols = encode_ds_stream(char_bits, init_d=0, init_s=0)

    # Drive symbols onto uio[4] (Data) and uio[5] (Strobe)
    for d, s in symbols:
        val = (s << 5) | (d << 4)
        dut.uio_in.value = val
        await ClockCycles(dut.clk, bit_period)

    # Allow core to finish parity check and halt
    core = dut.user_project.u_core
    for _ in range(50):
        if int(core.halted.value) == 1:
            break
        await ClockCycles(dut.clk, 1)

    assert int(core.halted.value) == 1, "SpaceWire RX character core did not halt"
    assert int(core.r0.value) == 0x96, f"Expected R0=0x96, got 0x{int(core.r0.value):02X}"
    assert int(core.r2.value) == STATUS_OK, f"Expected R2=STATUS_OK (0x00), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_spacewire_rx_control_tokens(dut):
    """
    Test 3: ASIC ingresses and classifies SpaceWire 4-bit Control Tokens on uio[4] & uio[5].
    Transmits EOP token (ID 1: [P=1, C=1, b0=0, b1=1]); core returns R1 = 1, R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    bit_period = 8
    words = build_spacewire_rx_token_asm(bit_period=bit_period, data_pin=4, strobe_pin=5)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00, wait_settle=True)

    # Encode EOP token: ID 1 -> [P=1, C=1, 0, 1]
    eop_bits = encode_control_char(CONTROL_EOP)
    symbols = encode_ds_stream(eop_bits, init_d=0, init_s=0)

    for d, s in symbols:
        val = (s << 5) | (d << 4)
        dut.uio_in.value = val
        await ClockCycles(dut.clk, bit_period)

    core = dut.user_project.u_core
    for _ in range(50):
        if int(core.halted.value) == 1:
            break
        await ClockCycles(dut.clk, 1)

    assert int(core.halted.value) == 1, "SpaceWire RX token core did not halt"
    assert int(core.r1.value) == CONTROL_EOP, f"Expected R1={CONTROL_EOP} (EOP), got {int(core.r1.value)}"
    assert int(core.r2.value) == STATUS_OK, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_spacewire_parity_error_trap(dut):
    """
    Test 4: Parity Error Trapping.
    Testbench injects an illegal parity bit (P=0 instead of P=1 for 0x96).
    The ASIC core detects the odd parity violation and halts with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    bit_period = 8
    words = build_spacewire_rx_char_asm(bit_period=bit_period, data_pin=4, strobe_pin=5)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00, wait_settle=True)

    # Corrupt the parity bit: set P=0 (for 0x96, valid P is 1)
    corrupted_bits = [0, 0] + [(0x96 >> i) & 1 for i in range(8)]
    assert not verify_spacewire_parity(corrupted_bits), "Sanity check: test bits must have even parity"

    symbols = encode_ds_stream(corrupted_bits, init_d=0, init_s=0)

    for d, s in symbols:
        val = (s << 5) | (d << 4)
        dut.uio_in.value = val
        await ClockCycles(dut.clk, bit_period)

    core = dut.user_project.u_core
    for _ in range(50):
        if int(core.halted.value) == 1:
            break
        await ClockCycles(dut.clk, 1)

    assert int(core.halted.value) == 1, "Core failed to halt upon parity error"
    assert int(core.r2.value) == STATUS_PARITY_ERROR, (
        f"Expected parity trap R2=0xEE, got 0x{int(core.r2.value):02X}"
    )


@cocotb.test()
async def test_spacewire_credit_flow_control(dut):
    """
    Test 5: Credit-based Flow Control.
    Verifies that the ASIC transmitter tracks receive buffer credit:
      - Consumes credits on byte transmission.
      - Traps credit exhaustion with R2 = 0xCC if transmission exceeds available credits.
      - Replenishes credits (+8) upon receiving an FCT token.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case A: Out-of-credit trap (8 initial credits, attempts to send 9)
    words_exhaust = build_spacewire_credit_tracker_asm(
        initial_credits=8,
        transmit_count=9,
        receive_fct=False
    )
    await _init_dut_and_bootload(dut, words_exhaust, initial_uio=0x00)

    core = dut.user_project.u_core
    for _ in range(100):
        if int(core.halted.value) == 1:
            break
        await ClockCycles(dut.clk, 1)

    assert int(core.halted.value) == 1, "Credit exhaust core did not halt"
    assert int(core.r2.value) == STATUS_CREDIT_EXHAUSTED, (
        f"Expected credit exhausted R2=0xCC, got 0x{int(core.r2.value):02X}"
    )

    # Case B: Credit replenishment via FCT token (+8 credits)
    words_fct = build_spacewire_credit_tracker_asm(
        initial_credits=8,
        transmit_count=8,
        receive_fct=True
    )
    await _init_dut_and_bootload(dut, words_fct, initial_uio=0x00)

    for _ in range(100):
        if int(core.halted.value) == 1:
            break
        await ClockCycles(dut.clk, 1)

    assert int(core.halted.value) == 1, "Credit flow FCT core did not halt"
    assert int(core.r1.value) == 8, f"Expected R1=8 credits remaining, got {int(core.r1.value)}"
    assert int(core.r2.value) == STATUS_OK, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_spacewire_standards_and_ppa_validation(dut):
    """
    Test 6: Standards Mathematical Invariant & Dedicated Coprocessor PPA Scaling.
    - Validates SpaceWire character encodings and odd parity across all control codes.
    - Validates Data-Strobe line coding invariant (delta(D) XOR delta(S) == 1) across
      100 pseudorandom bit sequences.
    - Validates IHP 130nm SG13G2 hardware coprocessor macro PPA metrics.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Validate All Control Characters and Parity
    for ctrl_id in [CONTROL_FCT, CONTROL_EOP, CONTROL_EEP, CONTROL_ESC]:
        bits = encode_control_char(ctrl_id)
        assert len(bits) == 4, f"Control char {ctrl_id} must be 4 bits"
        assert bits[1] == 1, f"Control flag must be 1 for control char {ctrl_id}"
        assert verify_spacewire_parity(bits), f"Control char {ctrl_id} parity must be odd"

    # 2. Validate Composite Tokens: NULL and Time-Code
    null_bits = encode_null_token()
    assert len(null_bits) == 8, "NULL token must be 8 bits (ESC + FCT)"
    assert verify_spacewire_parity(null_bits[:4]), "ESC in NULL must have odd parity"
    assert verify_spacewire_parity(null_bits[4:]), "FCT in NULL must have odd parity"

    time_bits = encode_time_code(0x3F)
    assert len(time_bits) == 14, "Time-Code must be 14 bits (ESC + Data)"
    assert verify_spacewire_parity(time_bits[:4]), "ESC in Time-Code must have odd parity"
    assert verify_spacewire_parity(time_bits[4:]), "Data in Time-Code must have odd parity"

    # 3. Validate Data-Strobe Encoding Invariants across diverse bit patterns
    import random
    rng = random.Random(0x5A1CE)
    test_stream = [rng.randint(0, 1) for _ in range(100)]
    ds_stream = encode_ds_stream(test_stream)
    recovered = decode_ds_stream(ds_stream)
    assert recovered == test_stream, "Decoded DS bit stream mismatch"

    # Check that exactly one line toggles per transition
    prev_d, prev_s = 0, 0
    for d, s in ds_stream:
        d_chg = (d != prev_d)
        s_chg = (s != prev_s)
        assert d_chg ^ s_chg, f"DS violation: d_chg={d_chg}, s_chg={s_chg} (must be XOR)"
        prev_d, prev_s = d, s

    # 4. Validate PPA Model Metrics on IHP 130nm SG13G2
    metrics = SpaceWirePpaModel.get_coprocessor_metrics()
    assert metrics["macro_cells"] == 456, f"Unexpected cell count: {metrics['macro_cells']}"
    assert metrics["macro_gate_equivalents"] == 880.0, f"Unexpected GE: {metrics['macro_gate_equivalents']}"
    assert metrics["area_overhead_pct"] < 2.50, f"Area overhead too high: {metrics['area_overhead_pct']:.2f}%"
    assert metrics["max_frequency_mhz"] > 700.0, f"Frequency too low: {metrics['max_frequency_mhz']} MHz"

    # Quick dummy execution on DUT to complete test cycle
    await _init_dut_and_bootload(dut, [0x9000], initial_uio=0x00)  # HALT
    await ClockCycles(dut.clk, 5)
    assert int(dut.user_project.u_core.halted.value) == 1
