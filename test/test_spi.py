# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Verification of SPI Master firmware and independent SPI Slave model across all 4 modes."""

import os
import sys
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from spi_model import build_spi_master_asm, SpiSlave  # noqa: E402


CS_PIN = 3
SCLK_PIN = 4
MOSI_PIN = 5
MISO_PIN = 6


async def _run_spi_transfer(dut, tx_byte: int, mode: int, slave_tx_byte: int = 0x00, half_period: int = 4):
    """Reset DUT, bootload SPI firmware, and simulate transfer with independent SpiSlave."""
    asm_src = build_spi_master_asm(
        tx_byte=tx_byte,
        mode=mode,
        half_period=half_period,
        cs_pin=CS_PIN,
        sclk_pin=SCLK_PIN,
        mosi_pin=MOSI_PIN,
        miso_pin=MISO_PIN
    )
    words = assemble(asm_src)

    # Reset DUT
    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Bootload program over serial protocol
    await bootload(dut, words)

    slave = SpiSlave(
        mode=mode,
        tx_byte=slave_tx_byte,
        cs_pin=CS_PIN,
        sclk_pin=SCLK_PIN,
        mosi_pin=MOSI_PIN,
        miso_pin=MISO_PIN
    )

    core = dut.user_project.u_core
    max_cycles = 400
    miso_bit = 0

    for _ in range(max_cycles):
        await FallingEdge(dut.clk)
        dut.uio_in.value = (miso_bit << MISO_PIN)
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_out = int(dut.uio_out.value)
        miso_bit = slave.step(uio_out)

        if bool(core.halted.value):
            break

    assert bool(core.halted.value), f"SPI core did not halt within {max_cycles} cycles"
    return slave, int(core.r1.value)


@cocotb.test()
async def test_spi_master_all_modes(dut):
    """Test SPI Master transmission in all 4 modes (Mode 0, 1, 2, 3) against SpiSlave."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_payloads = [0xA5, 0x5A, 0x3C, 0xC3]

    for mode in [0, 1, 2, 3]:
        for payload in test_payloads:
            dut._log.info(f"Testing SPI Mode {mode} with payload 0x{payload:02X}")
            slave, _ = await _run_spi_transfer(dut, tx_byte=payload, mode=mode, half_period=3)
            assert slave.rx_bytes == [payload], (
                f"Mode {mode} payload 0x{payload:02X} failed: slave received {slave.rx_bytes}"
            )
            dut._log.info(f"Mode {mode} payload 0x{payload:02X} PASS")


@cocotb.test()
async def test_spi_full_duplex(dut):
    """Test full-duplex simultaneous SPI transfer: Master TX -> Slave RX, Slave TX -> Master RX."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    master_tx = 0x7E
    slave_tx = 0x42

    for mode in [0, 2]:
        dut._log.info(f"Testing SPI full duplex: Mode {mode}, Master TX=0x{master_tx:02X}, Slave TX=0x{slave_tx:02X}")
        slave, master_rx = await _run_spi_transfer(dut, tx_byte=master_tx, mode=mode, slave_tx_byte=slave_tx, half_period=4)

        assert slave.rx_bytes == [master_tx], f"Slave did not receive 0x{master_tx:02X} (got {slave.rx_bytes})"
        dut._log.info(f"Full duplex Mode {mode}: Master sent 0x{master_tx:02X}, received 0x{master_rx:02X}")


@cocotb.test()
async def test_spi_edge_cases_and_random(dut):
    """Test SPI Master edge cases (0x00, 0xFF) and pseudorandom bytes."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rng = random.Random(99999)
    edge_cases = [0x00, 0xFF, 0x55, 0xAA]
    random_cases = [rng.randint(0, 255) for _ in range(4)]

    for val in edge_cases + random_cases:
        mode = rng.choice([0, 1, 2, 3])
        slave, _ = await _run_spi_transfer(dut, tx_byte=val, mode=mode, half_period=3)
        assert slave.rx_bytes == [val], f"SPI test failed for 0x{val:02X} in mode {mode}: got {slave.rx_bytes}"
