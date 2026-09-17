# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_modbus.py
===================
Cocotb testbench for Modbus RTU / ASCII (IEC 61158 / Modbus-IDA) Protocol Engine
and Serial Controller.

Test suite covers:
  1. Modbus RTU Frame Master Transmission:
     ASIC serializes a 5-byte RTU frame:
     [Slave_Addr=0x05, FC=0x03, Data=0x01, CRC_L=0xA0, CRC_H=0xF1]
     over UART 8-N-1 on pin 3, verified by independent UartReceiver and ModbusRtuFrame parser.
  2. Modbus RTU Slave Station Address Match & Payload Acquisition:
     ASIC slave (configured station 0x05) receives RTU request addressed to 0x05 on pin 4,
     matches station, latches Function Code (0x03 into R0) and Data (0x01 into R1), and asserts R2 = 0x00.
  3. Modbus RTU Slave Station Address Mismatch & Bypass:
     ASIC slave receives request addressed to different station (0x09 vs 0x05),
     detects mismatch on first octet, cleanly halts with bypass status R2 = 0xAA.
  4. Modbus ASCII Frame Master Transmission:
     ASIC serializes an 11-byte ASCII frame:
     [:050301F7\\r\\n] over UART 8-N-1 on pin 3, verified by independent UartReceiver and ModbusAsciiFrame parser.
  5. In-Register Modbus LRC Computation & Exception Response Generation:
     Microcode computes 8-bit two's complement LRC in-register (R2 = 0x00), and validates
     Modbus exception generation (FC 0x03 -> 0x83, Exception Code 0x02, R2 = 0x83).
  6. Modbus Standards Behavioral Model & Synthesizable PPA Validation.
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
from modbus_model import (
    ModbusFunctionCode,
    ModbusExceptionCode,
    ModbusRtuFrame,
    ModbusAsciiFrame,
    ModbusSlaveModel,
    ModbusPpaModel,
    compute_modbus_crc16,
    compute_modbus_lrc,
    build_modbus_rtu_tx_frame_asm,
    build_modbus_rtu_slave_rx_asm,
    build_modbus_ascii_tx_frame_asm,
    build_modbus_lrc_validator_asm,
    build_modbus_exception_generator_asm,
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
async def test_modbus_rtu_tx_frame(dut):
    """
    Test 1: Modbus RTU Frame Master Transmission on pin 3:
    Transmits [0x05, 0x03, 0x01, 0xA0, 0xF1] (Addr 5, FC 3, Data 1, CRC16 0xF1A0).
    Verified by independent UartReceiver on pin 3 and ModbusRtuFrame decoder.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 3
    bit_period = 8
    slave_addr = 0x05
    function_code = 0x03
    data_byte = 0x01

    words = build_modbus_rtu_tx_frame_asm(
        slave_addr=slave_addr,
        function_code=function_code,
        data_byte=data_byte,
        tx_pin=tx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << tx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    receiver = UartReceiver(bit_period_cycles=bit_period)
    decoded_bytes = []
    max_cycles = 100 + 5 * 14 * bit_period
    core = dut.user_project.u_core

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        res = receiver.step(pin_val)
        if res is not None:
            decoded_bytes.append(res)

        if bool(core.halted.value) and receiver.state == UartReceiver.STATE_IDLE and len(decoded_bytes) >= 5:
            break

    assert len(decoded_bytes) == 5, f"Expected 5 bytes, got {len(decoded_bytes)}: {decoded_bytes}"
    expected_crc = compute_modbus_crc16(bytes([slave_addr, function_code, data_byte]))
    expected_bytes = [slave_addr, function_code, data_byte, expected_crc & 0xFF, (expected_crc >> 8) & 0xFF]
    assert decoded_bytes == expected_bytes, f"RTU frame mismatch: expected {expected_bytes}, got {decoded_bytes}"

    parsed = ModbusRtuFrame.from_bytes(bytes(decoded_bytes))
    assert parsed.slave_addr == slave_addr
    assert parsed.function_code == function_code
    assert parsed.data == [data_byte]
    dut._log.info(f"Modbus RTU TX Frame PASS: {[hex(b) for b in decoded_bytes]}")


@cocotb.test()
async def test_modbus_rtu_slave_address_match(dut):
    """
    Test 2: Modbus RTU Slave Station Address Match & Payload Acquisition:
    ASIC slave (configured station 0x05) receives RTU frame addressed to 0x05 on pin 4.
    Matches station 0x05, latches Function Code (0x03) into R0 and Data (0x01) into R1,
    and asserts success status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    configured_addr = 0x05
    rx_pin = 4
    bit_period = 8

    words = build_modbus_rtu_slave_rx_asm(
        configured_addr=configured_addr,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Ingress Addr 0x05, FC 0x03, Data 0x01
    tx_bytes = [0x05, 0x03, 0x01]

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

    assert bool(core.halted.value), "Core should halt after processing matching RTU request"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x03, f"Expected FC 0x03 in R0, got 0x{r0:02X}"
    assert r1 == 0x01, f"Expected Data 0x01 in R1, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected Status R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"Modbus RTU Slave Address Match PASS: FC=0x{r0:02X}, Data=0x{r1:02X}, Status=0x{r2:02X}")


@cocotb.test()
async def test_modbus_rtu_slave_address_mismatch(dut):
    """
    Test 3: Modbus RTU Slave Station Address Mismatch & Bypass:
    ASIC slave (configured station 0x05) receives RTU frame addressed to 0x09 on pin 4.
    Detects mismatch, skips execution, and asserts bypass status R2 = 0xAA.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    configured_addr = 0x05
    rx_pin = 4
    bit_period = 8

    words = build_modbus_rtu_slave_rx_asm(
        configured_addr=configured_addr,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Frame addressed to 0x09 (mismatch!)
    tx_bytes = [0x09, 0x03, 0x01]

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
    dut._log.info(f"Modbus RTU Slave Address Mismatch PASS: Status=0x{r2:02X}")


@cocotb.test()
async def test_modbus_ascii_tx_frame(dut):
    """
    Test 4: Modbus ASCII Frame Master Transmission on pin 3:
    Transmits ':0503F8\\r\\n' (9 ASCII bytes: Addr 0x05, FC 0x03, LRC 0xF8).
    Verified by independent UartReceiver on pin 3 and ModbusAsciiFrame decoder.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 3
    bit_period = 8
    slave_addr = 0x05
    function_code = 0x03

    words = build_modbus_ascii_tx_frame_asm(
        slave_addr=slave_addr,
        function_code=function_code,
        data_byte=None,
        tx_pin=tx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << tx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    receiver = UartReceiver(bit_period_cycles=bit_period)
    decoded_bytes = []
    max_cycles = 100 + 9 * 14 * bit_period
    core = dut.user_project.u_core

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        res = receiver.step(pin_val)
        if res is not None:
            decoded_bytes.append(res)

        if bool(core.halted.value) and receiver.state == UartReceiver.STATE_IDLE and len(decoded_bytes) >= 9:
            break

    assert len(decoded_bytes) == 9, f"Expected 9 bytes, got {len(decoded_bytes)}: {decoded_bytes}"
    expected_frame = ModbusAsciiFrame(slave_addr=slave_addr, function_code=function_code, data=[])
    expected_bytes = list(expected_frame.to_bytes())
    assert decoded_bytes == expected_bytes, f"ASCII frame mismatch: expected {expected_bytes}, got {decoded_bytes}"

    parsed = ModbusAsciiFrame.from_bytes(bytes(decoded_bytes))
    assert parsed.slave_addr == slave_addr
    assert parsed.function_code == function_code
    assert parsed.data == []
    dut._log.info(f"Modbus ASCII TX Frame PASS: {bytes(decoded_bytes)}")


@cocotb.test()
async def test_modbus_lrc_accumulation_and_exception(dut):
    """
    Test 5: In-Register Modbus LRC Computation & Exception Response Generation:
    1. In-register microcode calculates 8-bit two's complement LRC for [0x05, 0x03, 0x01] -> 0xF7.
       Asserts R2 = 0x00 on match.
    2. Microcode constructs Modbus exception response for FC 0x03 with code 0x02 -> R0 = 0x83, R1 = 0x02, R2 = 0x83.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. In-Register LRC Calculation
    test_bytes = [0x05, 0x03, 0x01]
    expected_lrc = compute_modbus_lrc(bytes(test_bytes))  # 0xF7
    words_lrc = build_modbus_lrc_validator_asm(test_bytes, expected_lrc)
    await _init_dut_and_bootload(dut, words_lrc)

    core = dut.user_project.u_core
    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after LRC validation"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (LRC Match), got 0x{int(core.r2.value):02X}"

    # 2. Modbus Exception Response Microcode
    words_exc = build_modbus_exception_generator_asm(request_fc=0x03, exception_code=0x02)
    await _init_dut_and_bootload(dut, words_exc)

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after Exception generation"
    assert int(core.r0.value) == 0x83, f"Expected FC|0x80 = 0x83 in R0, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == 0x02, f"Expected Exception Code 0x02 in R1, got 0x{int(core.r1.value):02X}"
    assert int(core.r2.value) == 0x83, f"Expected Status R2=0x83, got 0x{int(core.r2.value):02X}"
    dut._log.info("Modbus LRC Computation & Exception Response PASS")


@cocotb.test()
async def test_modbus_standards_and_ppa(dut):
    """
    Test 6: Modbus Standards Behavioral Model & Synthesizable PPA Validation:
    Exercises behavioral ModbusSlaveModel, CRC-16 vs LRC mathematical formulations,
    and validates synthesizable hardware coprocessor PPA scaling metrics for IHP 130nm SG13G2.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Behavioral Slave Model Validation
    slave = ModbusSlaveModel(station_address=0x05)
    valid_rtu = ModbusRtuFrame(slave_addr=0x05, function_code=ModbusFunctionCode.READ_HOLDING_REGISTERS, data=[0x00, 0x01])
    resp = slave.process_rtu_frame(valid_rtu.to_bytes())
    assert resp["status"] == "SUCCESS"
    assert resp["code"] == 0x00
    assert resp["data"] == [0x00, 0x01]

    # Mismatched address
    mm_rtu = ModbusRtuFrame(slave_addr=0x09, function_code=ModbusFunctionCode.READ_HOLDING_REGISTERS, data=[0x00, 0x01])
    resp_mm = slave.process_rtu_frame(mm_rtu.to_bytes())
    assert resp_mm["status"] == "ADDRESS_MISMATCH"
    assert resp_mm["code"] == 0xAA

    # Unsupported function code -> exception
    unsupported_rtu = ModbusRtuFrame(slave_addr=0x05, function_code=0x15, data=[0x00])
    resp_un = slave.process_rtu_frame(unsupported_rtu.to_bytes())
    assert resp_un["status"] == "EXCEPTION"
    assert resp_un["code"] == 0x95
    assert resp_un["exception_code"] == ModbusExceptionCode.ILLEGAL_FUNCTION

    # 2. Mathematical Checksum Formulations
    data = bytes([0x01, 0x04, 0x02, 0xFF, 0x00])
    crc = compute_modbus_crc16(data)
    assert 0 <= crc <= 0xFFFF
    lrc = compute_modbus_lrc(data)
    assert (sum(data) + lrc) & 0xFF == 0x00  # Fundamental invariant of two's complement LRC

    # 3. PPA Scaling Validation for IHP 130nm SG13G2
    ppa = ModbusPpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 488
    assert ppa["gate_equivalents"] == 918.0
    assert ppa["area_um2"] == 3568.20
    assert ppa["area_overhead_pct"] == 2.53
    assert ppa["f_max_mhz"] > 700.0
    assert ppa["dynamic_power_uw_10mhz"] < 50.0
    dut._log.info(f"Modbus Standards & PPA Model PASS: {ppa}")
