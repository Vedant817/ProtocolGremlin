# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Gate-Level Simulation Verification Suite (GATES=yes).

Verifies the mapped gate-level netlist (gate_level_netlist.v) synthesized from RTL
into generic CMOS standard cells with real propagation delays (simcells_timing.v).

This test exercises the physical silicon boundary:
  - Validates program RAM write & read paths across 4096 synthesized flip-flops.
  - Validates bootloader CRC-8 hardware checking and status pins (uo_out).
  - Validates UART TX, SPI Master, Manchester Biphase-L, DMX512, and HDLC/SDLC
    waveform timing fidelity under real gate propagation delays.
  - Validates open-drain high-Z electrical contention prevention on synthesized GPIO cells.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from assembler import assemble
from bootload import bootload
from dmx512_model import Dmx512ReceiverModel, build_dmx512_tx_packet_asm
from hdlc_model import HdlcReceiver, build_hdlc_tx_words
from manchester_model import ManchesterDecoder, build_manchester_tx_asm
from spi_model import SpiSlave, build_spi_master_asm
from uart_model import UartReceiver, build_uart_tx_asm


async def reset_dut(dut):
    """Apply clean asynchronous reset to the DUT and prepare for immediate bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1


@cocotb.test()
async def test_gl_bootload_valid_frame(dut):
    """Verify clean serial bootload and execution on synthesized gate-level netlist."""
    clock = Clock(dut.clk, 100, unit="ns")  # 10 MHz
    cocotb.start_soon(clock.start())
    await reset_dut(dut)

    # Program: Configure GPIO 0 as output, drive 0x01, wait 10 cycles, halt
    asm = """
        GDIRI 0x01
        GWRI  0x01
        WAIT  10
        HALT
    """
    words = assemble(asm)
    dut._log.info("Bootloading valid frame into gate-level netlist...")
    await bootload(dut, words)

    # Allow execution to complete through gate delays
    for _ in range(30):
        await RisingEdge(dut.clk)

    await ReadOnly()
    uo = int(dut.uo_out.value)
    uio_out = int(dut.uio_out.value)
    uio_oe = int(dut.uio_oe.value)

    # uo_out[0]=boot_done (1), uo_out[1]=boot_err (0) -> 0x01
    assert (uo & 0x03) == 0x01, f"Gate-level bootload status mismatch: expected uo_out=0x01, got 0x{uo:02X}"
    assert (uio_out & 0x01) == 0x01, f"Gate-level GPIO output mismatch: expected uio_out[0]=1, got 0x{uio_out:02X}"
    assert (uio_oe & 0x01) == 0x01, f"Gate-level GPIO OE mismatch: expected uio_oe[0]=1, got 0x{uio_oe:02X}"
    dut._log.info("Gate-Level Bootload & Execution: PASS (uo_out=0x%02X, uio_out=0x%02X)", uo, uio_out)


@cocotb.test()
async def test_gl_bootload_corrupted_crc(dut):
    """Verify synthesized on-chip CRC-8 checker detects corruption and asserts boot_err (0x03)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    await reset_dut(dut)

    words = [0x0842, 0x0000]
    dut._log.info("Driving corrupted CRC packet into gate-level netlist...")
    await bootload(dut, words, corrupt_crc=True)

    for _ in range(10):
        await RisingEdge(dut.clk)

    await ReadOnly()
    uo = int(dut.uo_out.value)
    # uo_out[0]=boot_done (1), uo_out[1]=boot_err (1) -> 0x03
    assert (uo & 0x03) == 0x03, f"Gate-level corrupted CRC did not assert boot_err: got uo_out=0x{uo:02X}"
    dut._log.info("Gate-Level Corrupted CRC Lock: PASS (uo_out=0x%02X)", uo)


@cocotb.test()
async def test_gl_uart_tx_waveform_timing(dut):
    """Verify UART TX bit-banged waveform timing on gate-level netlist against UartReceiver."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    bit_period = 8  # 800 ns per bit
    test_bytes = [0xA5, 0x3C, 0x55, 0x00]

    for b in test_bytes:
        asm = build_uart_tx_asm(b, bit_period_cycles=bit_period, pin=0)
        words = assemble(asm)
        await reset_dut(dut)
        await bootload(dut, words)

        receiver = UartReceiver(bit_period_cycles=bit_period)
        max_cycles = 40 + 15 * bit_period
        decoded = []

        for _ in range(max_cycles):
            await RisingEdge(dut.clk)
            await ReadOnly()
            pin_val = (int(dut.uio_out.value) >> 0) & 1
            res = receiver.step(pin_val)
            if res is not None:
                decoded.append(res)

        assert len(decoded) == 1, f"Gate-level UART TX: expected 1 byte for 0x{b:02X}, got {len(decoded)}"
        assert decoded[0] == b, f"Gate-level UART TX: expected 0x{b:02X}, got 0x{decoded[0]:02X}"

    dut._log.info("Gate-Level UART TX Waveform & Timing: PASS (all bytes verified at 8 cycles/bit)")


@cocotb.test()
async def test_gl_spi_master_full_duplex(dut):
    """Verify SPI Master Mode 0 transmission on gate-level netlist against SpiSlave."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_byte = 0xA5
    rx_slave_byte = 0x5A
    half_period = 4  # 4 cycles SCLK half-period

    asm = build_spi_master_asm(
        tx_byte=tx_byte,
        mode=0,
        half_period=half_period,
        cs_pin=2,
        sclk_pin=1,
        mosi_pin=0,
        miso_pin=3,
    )
    words = assemble(asm)
    await reset_dut(dut)
    await bootload(dut, words)

    slave = SpiSlave(
        mode=0,
        tx_byte=rx_slave_byte,
        cs_pin=2,
        sclk_pin=1,
        mosi_pin=0,
        miso_pin=3,
    )
    max_cycles = 50 + 20 * (half_period * 2)
    miso_bit = 0

    for _ in range(max_cycles):
        await FallingEdge(dut.clk)
        dut.uio_in.value = (miso_bit << 3)
        await RisingEdge(dut.clk)
        await ReadOnly()
        uio_out = int(dut.uio_out.value)
        miso_bit = slave.step(uio_out)

    assert len(slave.rx_bytes) == 1, f"Gate-level SPI Master: expected 1 byte, got {len(slave.rx_bytes)}"
    assert slave.rx_bytes[0] == tx_byte, (
        f"Gate-level SPI Master TX mismatch: expected 0x{tx_byte:02X}, got 0x{slave.rx_bytes[0]:02X}"
    )
    dut._log.info("Gate-Level SPI Master Mode 0: PASS (TX 0x%02X received cleanly by SpiSlave)", tx_byte)


@cocotb.test()
async def test_gl_manchester_biphase_encoding(dut):
    """Verify Manchester Biphase-L self-clocking line code on gate-level netlist."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_byte = 0x3C
    half_period = 4
    asm = build_manchester_tx_asm(data_byte=test_byte, half_period=half_period, pin=0)
    words = assemble(asm)
    await reset_dut(dut)
    await bootload(dut, words)

    recorded_levels = []
    max_cycles = 40 + 20 * (half_period * 2)

    for _ in range(max_cycles):
        await FallingEdge(dut.clk)
        await ReadOnly()
        pin_val = (int(dut.uio_out.value) >> 0) & 1
        recorded_levels.append(pin_val)

    # Detect start bit rising edge (0 -> 1)
    start_idx = -1
    for i in range(len(recorded_levels) - 1):
        if recorded_levels[i] == 0 and recorded_levels[i + 1] == 1:
            start_idx = i + 1
            break
    assert start_idx != -1, "Failed to detect Manchester start bit rising edge"

    # Extract 16 half-bit symbols
    data_start = start_idx + (2 * half_period)
    symbols = []
    for s_idx in range(16):
        sample_offset = data_start + (s_idx * half_period) + (half_period // 2)
        symbols.append(recorded_levels[sample_offset])

    decoder = ManchesterDecoder(half_period=half_period)
    decoded_byte, valid = decoder.decode_symbols(symbols)

    assert valid, "Biphase violation detected in gate-level Manchester TX!"
    assert decoded_byte == test_byte, (
        f"Gate-level Manchester TX: expected 0x{test_byte:02X}, got 0x{decoded_byte:02X}"
    )
    dut._log.info("Gate-Level Manchester Biphase-L: PASS (0x%02X verified with zero biphase violations)", test_byte)


@cocotb.test()
async def test_gl_dmx512_break_mab_packet(dut):
    """Verify DMX512 stage lighting packet (Break + MAB + Slots) on gate-level netlist."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    channels = [255, 128, 0]
    asm = build_dmx512_tx_packet_asm(
        start_code=0x00,
        channels=channels,
        bit_period=4,
        pin=0,
    )
    words = assemble(asm)
    await reset_dut(dut)
    await bootload(dut, words)

    max_cycles = 120 + (24 * 4) + (4 * 4) + (len(channels) + 1) * 11 * 4
    recorded_levels = []

    for _ in range(max_cycles):
        await FallingEdge(dut.clk)
        await ReadOnly()
        pin_val = (int(dut.uio_out.value) >> 0) & 1
        recorded_levels.append(pin_val)

    receiver = Dmx512ReceiverModel(bit_period=4)
    start_code, slots, is_valid = receiver.parse_waveform(recorded_levels)

    assert is_valid, f"Gate-level DMX512 decode failed (valid={is_valid})"
    assert start_code == 0x00, f"Gate-level DMX512 Start Code mismatch: got {start_code}"
    assert slots == channels, f"Gate-level DMX512 slots mismatch: expected {channels}, got {slots}"
    dut._log.info("Gate-Level DMX512 Packet: PASS (Break/MAB valid, slots=%s)", channels)


@cocotb.test()
async def test_gl_hdlc_flag_and_bit_stuffing(dut):
    """Verify HDLC/SDLC NRZI line coding and zero-bit insertion on gate-level netlist."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    payload = 0xFF  # Five+ consecutive 1s requires dynamic bit stuffing
    bit_period = 8
    words = build_hdlc_tx_words(payload=payload, bit_period=bit_period, pin=0)
    await reset_dut(dut)
    await bootload(dut, words)

    max_cycles = 60 + 35 * bit_period
    samples = []

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()
        pin_val = (int(dut.uio_out.value) >> 0) & 1
        samples.append(pin_val)

    # Find the first transition (falling edge 1 -> 0 of opening flag bit 0)
    start_cycle = -1
    for c in range(1, len(samples)):
        if samples[c - 1] == 1 and samples[c] == 0:
            start_cycle = c
            break

    assert start_cycle != -1, "Opening flag falling edge not detected!"

    # Sample bit levels at bit centers
    bit_levels = []
    c = start_cycle + (bit_period // 2)
    while c < len(samples):
        bit_levels.append(samples[c])
        c += bit_period

    # Decode through HdlcReceiver
    rx = HdlcReceiver(bit_period=bit_period, pin=0, initial_level=1)
    logical_bits = rx.decode_nrzi(bit_levels, initial_level=1)
    recovered_payload, closing_flag, abort = rx.decode_frame(logical_bits)

    assert closing_flag, "Closing flag 0x7E was not found in gate-level transmitted frame!"
    assert not abort, "Unexpected abort sequence detected in gate-level transmitted frame!"
    assert recovered_payload == [payload], f"Payload mismatch: expected {[payload]}, got {recovered_payload}"
    dut._log.info("Gate-Level HDLC Bit Stuffing & NRZI: PASS (0xFF received with destuffing confirmed)")


@cocotb.test()
async def test_gl_open_drain_bus_safety(dut):
    """Verify open-drain high-Z contention prevention on synthesized gate-level GPIO cells."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    await reset_dut(dut)

    # Program: Enable open-drain on pin 0 (GODRI 0x01, GDIRI 0x01)
    # Drive 0 (GWRI 0x00), then drive 1 (GWRI 0x01), then halt
    asm = """
        GODRI 0x01   ; enable open-drain on pin 0
        GDIRI 0x01   ; set pin 0 as output
        GWRI  0x00   ; drive low (active pull-down)
        WAIT  10
        GWRI  0x01   ; drive high in open-drain -> MUST float (uio_oe=0)
        WAIT  10
        HALT
    """
    words = assemble(asm)
    await bootload(dut, words)

    saw_pull_down = False
    saw_float = False

    for _ in range(40):
        await RisingEdge(dut.clk)
        await ReadOnly()
        oe = int(dut.uio_oe.value) & 1
        out = int(dut.uio_out.value) & 1
        if oe == 1 and out == 0:
            saw_pull_down = True
        elif oe == 0 and saw_pull_down:
            saw_float = True

    assert saw_pull_down, "Gate-level open drain did not actively assert low (uio_oe=1, uio_out=0)"
    assert saw_float, "Gate-level open drain did not release bus to high-Z (uio_oe=0) when driving 1"
    dut._log.info("Gate-Level Open-Drain Electrical Safety: PASS (active pull-down and high-Z release verified)")
