# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_profibus.py
=====================
Cocotb testbench for Profibus DP (IEC 61158 / EN 50170) Master/Slave Fieldbus Protocol Engine.

Test suite covers:
  1. Profibus SD2 Telegram Master Transmission:
     ASIC serializes a 10-byte variable-length telegram:
     [SD2=0x68, LE=4, LEr=4, SD2=0x68, DA=0x04, SA=0x01, FC=0x49, Data=0x5A, FCS=0xA8, ED=0x16]
     over UART 8-N-1 on pin 3, verified by independent UartReceiver and ProfibusTelegram parser.
  2. Configured Station Address Match & Payload Acquisition:
     ASIC slave (configured station 0x04) receives SD2 telegram addressed to 0x04,
     validates HD=4 delimiters/length and FCS, latches payload (0x5A into R0), and asserts R2 = 0x00.
  3. Configured Station Address Mismatch & Bypass:
     ASIC slave receives telegram addressed to different station (0x07 vs 0x04),
     detects destination address mismatch, skips execution, and asserts bypass status R2 = 0xAA.
  4. Frame Check Sequence (FCS) Corruption & Error Trapping:
     ASIC slave receives telegram with corrupted FCS byte (0xFF vs 0xA8),
     detects checksum failure, and asserts fault code R2 = 0xEE.
  5. SD4 Token Passing Reception & Logical Ring Ringmaster Token Acceptance:
     ASIC receives SD4 token [SD4=0xDC, DA=0x04, SA=0x01], matches destination master address,
     latches predecessor SA into R0 (0x01), and asserts token possession status R2 = 0x01.
  6. IEC 61158 Profibus DP Protocol Standards, HD=4 Invariants & PPA Model Validation.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from bootload import bootload
from uart_model import UartReceiver, UartTransmitter
from profibus_model import (
    ProfibusDelimiter,
    ProfibusTelegram,
    ProfibusSlaveModel,
    ProfibusPpaModel,
    compute_profibus_fcs,
    build_profibus_tx_sd2_asm,
    build_profibus_slave_rx_asm,
    build_profibus_token_filter_asm,
    build_profibus_fcs_validator_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x10):
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
    await ClockCycles(dut.clk, 10)


@cocotb.test()
async def test_profibus_tx_sd2_telegram(dut):
    """
    Test 1: Master SD2 telegram transmission from ASIC on pin 3:
    Transmits [0x68, 0x04, 0x04, 0x68, 0x04, 0x01, 0x49, 0x5A, 0xA8, 0x16].
    Verified by independent UartReceiver on pin 3 and ProfibusTelegram decoder.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 3
    bit_period = 8
    da = 0x04
    sa = 0x01
    fc = 0x49
    payload = [0x5A]

    words = build_profibus_tx_sd2_asm(
        da=da,
        sa=sa,
        fc=fc,
        data_bytes=payload,
        tx_pin=tx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << tx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    receiver = UartReceiver(bit_period_cycles=bit_period)
    decoded_bytes = []
    max_cycles = 100 + 10 * 14 * bit_period
    core = dut.user_project.u_core

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        res = receiver.step(pin_val)
        if res is not None:
            decoded_bytes.append(res)

        if bool(core.halted.value) and receiver.state == UartReceiver.STATE_IDLE and len(decoded_bytes) >= 10:
            break

    assert len(decoded_bytes) == 10, f"Expected 10 bytes, got {len(decoded_bytes)}: {decoded_bytes}"
    expected_fcs = compute_profibus_fcs([da, sa, fc, *payload])
    expected_bytes = [
        ProfibusDelimiter.SD2,
        0x04,
        0x04,
        ProfibusDelimiter.SD2,
        da,
        sa,
        fc,
        *payload,
        expected_fcs,
        ProfibusDelimiter.ED
    ]
    assert decoded_bytes == expected_bytes, f"Telegram mismatch: expected {expected_bytes}, got {decoded_bytes}"

    # Verify parsing through ProfibusTelegram class
    parsed_tg = ProfibusTelegram.from_bytes(bytes(decoded_bytes))
    assert parsed_tg.delimiter == ProfibusDelimiter.SD2
    assert parsed_tg.da == da
    assert parsed_tg.sa == sa
    assert parsed_tg.fc == fc
    assert parsed_tg.data == payload
    dut._log.info(f"Profibus TX SD2 Telegram PASS: {[hex(b) for b in decoded_bytes]}")


@cocotb.test()
async def test_profibus_slave_address_match(dut):
    """
    Test 2: Configured Station Address Match & Payload Acquisition:
    ASIC slave (station 0x04) receives SD2 telegram addressed to 0x04 on pin 4.
    Matches station 0x04, validates HD=4 delimiters, captures payload 0x5A into R0,
    and reports success status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    station_addr = 0x04
    rx_pin = 4
    bit_period = 8
    expected_len = 4
    expected_fcs = 0xA8  # sum(0x04 + 0x01 + 0x49 + 0x5A) = 0xA8

    words = build_profibus_slave_rx_asm(
        station_addr=station_addr,
        expected_len=expected_len,
        expected_fcs=expected_fcs,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core

    # Allow core to enter WAITEDGE stall for byte 0
    await ClockCycles(dut.clk, 8)

    tx_bytes = [
        ProfibusDelimiter.SD2,  # 0x68
        0x04,                   # LE
        0x04,                   # LEr
        ProfibusDelimiter.SD2,  # 0x68
        0x04,                   # DA
        0x01,                   # SA
        0x49,                   # FC
        0x5A,                   # Data
        expected_fcs,           # FCS (0xA8)
        ProfibusDelimiter.ED    # 0x16
    ]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    # Allow core to finish processing and halt
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after processing SD2 telegram"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)

    assert r0 == 0x5A, f"Expected Payload 0x5A in R0, got 0x{r0:02X}"
    assert r2 == 0x00, f"Expected Status R2=0x00 (Success), got 0x{r2:02X}"
    dut._log.info(f"Profibus Slave Address Match PASS: Payload=0x{r0:02X}, Status=0x{r2:02X}")


@cocotb.test()
async def test_profibus_slave_address_mismatch(dut):
    """
    Test 3: Configured Station Address Mismatch & Bypass:
    ASIC slave (station 0x04) receives SD2 telegram addressed to station 0x07 on pin 4.
    Detects mismatch, skips payload processing, and halts with bypass status R2 = 0xAA.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    station_addr = 0x04
    rx_pin = 4
    bit_period = 8
    expected_len = 4
    expected_fcs = 0xA8

    words = build_profibus_slave_rx_asm(
        station_addr=station_addr,
        expected_len=expected_len,
        expected_fcs=expected_fcs,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Transmit telegram with DA = 0x07 (mismatch against station 0x04)
    tx_bytes = [
        ProfibusDelimiter.SD2,  # 0x68
        0x04,                   # LE
        0x04,                   # LEr
        ProfibusDelimiter.SD2,  # 0x68
        0x07,                   # DA = 0x07 (mismatch!)
        0x01,                   # SA
        0x49,                   # FC
        0x5A,                   # Data
        0xAB,                   # FCS
        ProfibusDelimiter.ED    # 0x16
    ]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt on address mismatch"
    r2 = int(core.r2.value)
    assert r2 == 0xAA, f"Expected Bypass Status R2=0xAA, got 0x{r2:02X}"
    dut._log.info(f"Profibus Slave Address Mismatch PASS: Status=0x{r2:02X}")


@cocotb.test()
async def test_profibus_fcs_error_detection(dut):
    """
    Test 4: Frame Check Sequence (FCS) Corruption & Error Trapping:
    ASIC slave (station 0x04) receives SD2 telegram with corrupted FCS (0xFF instead of 0xA8).
    Detects checksum mismatch, halts with fault code R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    station_addr = 0x04
    rx_pin = 4
    bit_period = 8
    expected_len = 4
    expected_fcs = 0xA8

    words = build_profibus_slave_rx_asm(
        station_addr=station_addr,
        expected_len=expected_len,
        expected_fcs=expected_fcs,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Corrupted FCS byte: 0xFF
    tx_bytes = [
        ProfibusDelimiter.SD2,
        0x04,
        0x04,
        ProfibusDelimiter.SD2,
        0x04,                   # DA matches
        0x01,
        0x49,
        0x5A,
        0xFF,                   # CORRUPTED FCS!
        ProfibusDelimiter.ED
    ]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt on FCS error"
    r2 = int(core.r2.value)
    assert r2 == 0xEE, f"Expected FCS Error Status R2=0xEE, got 0x{r2:02X}"
    dut._log.info(f"Profibus FCS Error Trapping PASS: Status=0x{r2:02X}")


@cocotb.test()
async def test_profibus_token_reception(dut):
    """
    Test 5: SD4 Token Passing Reception:
    ASIC receives SD4 token [SD4=0xDC, DA=0x04, SA=0x01].
    Matches destination master 0x04, latches predecessor SA (0x01) into R0,
    and asserts token possession status R2 = 0x01.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    station_addr = 0x04
    rx_pin = 4
    bit_period = 8

    words = build_profibus_token_filter_asm(
        station_addr=station_addr,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Token telegram: SD4=0xDC, DA=0x04, SA=0x01
    tx_bytes = [
        ProfibusDelimiter.SD4,  # 0xDC
        0x04,                   # DA
        0x01                    # SA
    ]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after token reception"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)

    assert r0 == 0x01, f"Expected Predecessor SA 0x01 in R0, got 0x{r0:02X}"
    assert r2 == 0x01, f"Expected Token Accepted Status R2=0x01, got 0x{r2:02X}"
    dut._log.info(f"Profibus Token Reception PASS: SA=0x{r0:02X}, Status=0x{r2:02X}")


@cocotb.test()
async def test_profibus_standards_and_ppa(dut):
    """
    Test 6: IEC 61158 Standards, FCS Modulo-256 Validation & PPA Scaling:
    Exercises behavioral ProfibusSlaveModel, in-register FCS validator microcode,
    and validates synthesizable hardware macro PPA scaling metrics.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Behavioral Slave Model Validation
    slave = ProfibusSlaveModel(station_address=0x04)
    valid_tg = ProfibusTelegram(da=0x04, sa=0x01, fc=0x49, data=[0x5A, 0xA5])
    raw_bytes = valid_tg.to_bytes()
    resp = slave.process_telegram(raw_bytes)
    assert resp["status"] == "PROCESSED"
    assert resp["code"] == 0x00
    assert resp["data"] == [0x5A, 0xA5]

    # Mismatch telegram
    mismatch_tg = ProfibusTelegram(da=0x09, sa=0x01, fc=0x49, data=[0x12])
    resp_mm = slave.process_telegram(mismatch_tg.to_bytes())
    assert resp_mm["status"] == "ADDRESS_MISMATCH"
    assert resp_mm["code"] == 0xAA

    # Token telegram
    token_tg = ProfibusTelegram(delimiter=ProfibusDelimiter.SD4, da=0x04, sa=0x02)
    resp_tok = slave.process_telegram(token_tg.to_bytes())
    assert resp_tok["status"] == "TOKEN_ACCEPTED"
    assert resp_tok["code"] == 0x01

    # 2. In-Register FCS Checksum Accumulation on ASIC core
    test_bytes = [0x04, 0x01, 0x49, 0x5A]
    expected_fcs = compute_profibus_fcs(test_bytes)  # 0xA8
    fcs_words = build_profibus_fcs_validator_asm(test_bytes, expected_fcs)
    await _init_dut_and_bootload(dut, fcs_words)

    core = dut.user_project.u_core
    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after FCS validation"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (FCS Match), got 0x{int(core.r2.value):02X}"

    # 3. PPA Scaling Validation for IHP 130nm SG13G2
    ppa = ProfibusPpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 485
    assert ppa["gate_equivalents"] == 910.0
    assert ppa["area_um2"] == 3545.35
    assert ppa["area_overhead_pct"] == 2.51
    assert ppa["f_max_mhz"] > 700.0
    assert ppa["dynamic_power_uw_10mhz"] < 50.0
    dut._log.info(f"Profibus Standards & PPA Model PASS: {ppa}")
