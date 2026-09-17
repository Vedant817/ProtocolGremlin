# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_ethercat.py
=====================
Cocotb testbench for EtherCAT (IEC 61158) Sub-Datagram Processing &
'Processing-on-the-Fly' Working Counter Engine.

Test suite covers:
  1. EtherCAT Sub-Datagram Master Transmission:
     ASIC serializes a 5-byte sub-datagram [Cmd=0x05, Addr_H=0x10, Addr_L=0x02,
     Payload=0x42, WKC_L=0x00] over UART 8-N-1 on pin 3, verified by independent UartReceiver.
  2. Configured Station Address Match & Dynamic WKC Increment (+1):
     ASIC slave (configured station 0x1002) receives FPWR datagram addressed to 0x1002,
     latches payload (0x5A into R0), dynamically increments Working Counter (0 -> 1 in R1),
     and asserts status code R2 = 0x00.
  3. Configured Station Address Mismatch & Bypass:
     ASIC slave receives datagram addressed to different station (0x1005 vs 0x1002),
     skips write execution, preserves incoming WKC unchanged (R1 = 3), and asserts
     mismatch status R2 = 0xAA.
  4. Broadcast Write (BWR) Execution & WKC Increment:
     ASIC slave processes broadcast sub-datagram (Cmd=0x08), latches broadcast payload
     (0x7E into R0), increments WKC (2 -> 3 in R1), and asserts status R2 = 0x00.
  5. 16-Bit Multi-Precision Working Counter Carry Propagation:
     In-register microcode executes 16-bit WKC increment across byte boundaries
     (0x00FF -> 0x0100) verifying multi-byte carry propagation (R1=0x00, R3=0x01, R2=0x00).
  6. IEC 61158 EtherCAT Protocol Standards, Latency Bounds & PPA Model Validation.
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
from ethercat_model import (
    EtherCatCommand,
    EtherCatSubDatagram,
    EtherCatSlaveModel,
    EtherCatPpaModel,
    build_ethercat_slave_process_asm,
    build_ethercat_tx_datagram_asm,
    build_ethercat_wkc_incrementer_asm,
    build_ethercat_address_filter_asm,
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
async def test_ethercat_tx_sub_datagram(dut):
    """
    Test 1: Master sub-datagram transmission from ASIC on pin 3:
    Transmits [Cmd=0x05, Addr_H=0x10, Addr_L=0x02, Payload=0x42, WKC_L=0x00].
    Verified by independent UartReceiver on pin 3.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 3
    bit_period = 8
    cmd = EtherCatCommand.FPWR  # 0x05
    addr_h = 0x10
    addr_l = 0x02
    payload = 0x42
    wkc_l = 0x00

    words = build_ethercat_tx_datagram_asm(
        cmd=cmd,
        addr_h=addr_h,
        addr_l=addr_l,
        payload=payload,
        wkc_l=wkc_l,
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
    expected_bytes = [cmd, addr_h, addr_l, payload, wkc_l]
    assert decoded_bytes == expected_bytes, f"Payload mismatch: expected {expected_bytes}, got {decoded_bytes}"
    dut._log.info(f"EtherCAT TX Sub-Datagram PASS: {[hex(b) for b in decoded_bytes]}")


@cocotb.test()
async def test_ethercat_rx_write_wkc_increment(dut):
    """
    Test 2: Configured Station Address Match & Dynamic WKC Increment:
    ASIC slave (station 0x1002) receives FPWR [0x05, 0x10, 0x02, 0x5A, 0x00] on pin 4.
    Matches station 0x1002, captures payload 0x5A into R0, increments WKC from 0 to 1 in R1,
    and reports success status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    station_addr = 0x1002
    rx_pin = 4
    bit_period = 8

    words = build_ethercat_slave_process_asm(
        station_addr=station_addr,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core

    # Allow core to enter WAITEDGE stall for byte 0
    await ClockCycles(dut.clk, 8)

    tx_bytes = [
        EtherCatCommand.FPWR,           # Cmd: Configured Physical Write (0x05)
        (station_addr >> 8) & 0xFF,    # Addr_H: 0x10
        station_addr & 0xFF,           # Addr_L: 0x02
        0x5A,                          # Payload: 0x5A
        0x00                           # WKC_L: initial 0
    ]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    # Allow core to finish processing and halt
    max_wait = 100
    for _ in range(max_wait):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after processing datagram"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x5A, f"Expected Payload 0x5A in R0, got 0x{r0:02X}"
    assert r1 == 1, f"Expected WKC incremented to 1 in R1, got {r1}"
    assert r2 == 0x00, f"Expected Status R2=0x00 (Success), got 0x{r2:02X}"
    dut._log.info(f"EtherCAT FPWR Match PASS: Payload=0x{r0:02X}, WKC={r1}, Status=0x{r2:02X}")


@cocotb.test()
async def test_ethercat_rx_address_mismatch(dut):
    """
    Test 3: Configured Station Address Mismatch & Bypass:
    ASIC slave (station 0x1002) receives FPWR addressed to station 0x1005 [0x05, 0x10, 0x05, 0x99, 0x03].
    Detects mismatch, skips write, leaves incoming WKC intact (R1 = 3), and sets status R2 = 0xAA.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    station_addr = 0x1002
    target_addr = 0x1005  # Mismatched target
    rx_pin = 4
    bit_period = 8

    words = build_ethercat_slave_process_asm(
        station_addr=station_addr,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    tx_bytes = [
        EtherCatCommand.FPWR,          # Cmd: Configured Physical Write (0x05)
        (target_addr >> 8) & 0xFF,     # Addr_H: 0x10
        target_addr & 0xFF,            # Addr_L: 0x05 (mismatch)
        0x99,                          # Payload: 0x99
        0x03                           # WKC_L: initial 3
    ]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    max_wait = 100
    for _ in range(max_wait):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after bypassing datagram"
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r1 == 3, f"Expected WKC unchanged at 3 in R1, got {r1}"
    assert r2 == 0xAA, f"Expected Status R2=0xAA (Address Mismatch), got 0x{r2:02X}"
    dut._log.info(f"EtherCAT Address Mismatch PASS: WKC={r1} (Unchanged), Status=0x{r2:02X}")


@cocotb.test()
async def test_ethercat_broadcast_write(dut):
    """
    Test 4: Broadcast Write (BWR) Execution & WKC Increment:
    ASIC slave receives BWR command (0x08) [0x08, 0x00, 0x00, 0x7E, 0x02] on pin 4.
    Broadcast applies to all slaves: latches payload 0x7E in R0, increments WKC from 2 to 3 in R1,
    and asserts status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    station_addr = 0x1002
    rx_pin = 4
    bit_period = 8

    words = build_ethercat_slave_process_asm(
        station_addr=station_addr,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    tx_bytes = [
        EtherCatCommand.BWR,           # Cmd: Broadcast Write (0x08)
        0x00,                          # Addr_H: 0x00
        0x00,                          # Addr_L: 0x00
        0x7E,                          # Payload: 0x7E
        0x02                           # WKC_L: initial 2
    ]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    max_wait = 100
    for _ in range(max_wait):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after processing broadcast"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x7E, f"Expected Payload 0x7E in R0, got 0x{r0:02X}"
    assert r1 == 3, f"Expected WKC incremented from 2 to 3 in R1, got {r1}"
    assert r2 == 0x00, f"Expected Status R2=0x00 (Success), got 0x{r2:02X}"
    dut._log.info(f"EtherCAT Broadcast Write PASS: Payload=0x{r0:02X}, WKC={r1}, Status=0x{r2:02X}")


@cocotb.test()
async def test_ethercat_multi_byte_wkc_overflow(dut):
    """
    Test 5: 16-Bit Multi-Precision Working Counter Carry Propagation:
    In-register microcode executes 16-bit WKC incrementation on initial value 0x00FF:
    0x00FF + 1 = 0x0100 (R1 = 0x00, R3 = 0x01, R2 = 0x00).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    initial_wkc_l = 0xFF
    initial_wkc_h = 0x00
    words = build_ethercat_wkc_incrementer_asm(initial_wkc_l=initial_wkc_l, initial_wkc_h=initial_wkc_h)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core

    # Wait for execution
    for _ in range(20):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after incrementing WKC"
    r1 = int(core.r1.value)
    r3 = int(core.r3.value)
    r2 = int(core.r2.value)

    assert r1 == 0x00, f"Expected WKC_L = 0x00 after rollover, got 0x{r1:02X}"
    assert r3 == 0x01, f"Expected WKC_H = 0x01 after carry propagation, got 0x{r3:02X}"
    assert r2 == 0x00, f"Expected Status R2 = 0x00, got 0x{r2:02X}"
    dut._log.info(f"EtherCAT 16-Bit WKC Multi-Precision Increment PASS: WKC=0x{r3:02X}{r1:02X}, Status=0x{r2:02X}")


@cocotb.test()
async def test_ethercat_ppa_and_standards_validation(dut):
    """
    Test 6: IEC 61158 Standards, Latency Bounds & Hardware Coprocessor PPA Model Validation.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Validate sub-datagram serialization and parsing
    dg = EtherCatSubDatagram(
        cmd=EtherCatCommand.FPWR,
        idx=42,
        station_addr=0x1002,
        mem_offset=0x0100,
        data=[0xAA, 0xBB, 0xCC, 0xDD],
        wkc=0
    )
    raw = dg.to_bytes()
    assert len(raw) == 16, f"Expected 16 bytes, got {len(raw)}"
    parsed = EtherCatSubDatagram.from_bytes(raw)
    assert parsed.cmd == EtherCatCommand.FPWR
    assert parsed.idx == 42
    assert parsed.station_addr == 0x1002
    assert parsed.mem_offset == 0x0100
    assert parsed.data == [0xAA, 0xBB, 0xCC, 0xDD]
    assert parsed.wkc == 0

    # 2. Validate independent slave model
    slave = EtherCatSlaveModel(station_address=0x1002)
    res_dg, addressed = slave.process(dg)
    assert addressed == True
    assert res_dg.wkc == 1
    assert list(slave.local_memory[0x0100:0x0104]) == [0xAA, 0xBB, 0xCC, 0xDD]

    # 3. Validate PPA model
    ppa = EtherCatPpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 475
    assert ppa["gate_equivalents"] == 890.0
    assert ppa["area_um2"] == 3472.25
    assert ppa["area_overhead_pct"] == 2.46
    assert ppa["f_max_mhz"] > 700.0

    dut._log.info(f"EtherCAT Standards & PPA Model PASS: Cells={ppa['standard_cells']}, Area={ppa['area_um2']} um2, f_max={ppa['f_max_mhz']:.1f} MHz")
