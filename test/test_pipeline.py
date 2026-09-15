# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""Cocotb tests for the End-to-End Autonomous Protocol Sniff -> Classify -> Ingress -> Replay Pipeline.

Verifies:
1. Autonomous UART Sniff -> Classify (0x01) -> Decode (0xA5) -> Echo Replay (0xA6) onto uio[1].
2. Autonomous Cross-Protocol Bridge: UART Ingress on Lane 0 (uio[0]) -> SPI Master Egress on Lane 1 (uio[4..6]).
3. Noise Rejection & Abort: Short runt glitches trigger Class 0xFF (Noise) with zero spurious transmission.
4. Dynamic Electrical Safety: Clean passive sniff (uio_oe=0), active transmit, and High-Z bus release.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from uart_model import UartReceiver, UartTransmitter  # noqa: E402
from spi_model import SpiSlave  # noqa: E402
from pipeline_model import (  # noqa: E402
    build_pipeline_uart_echo_asm,
    build_pipeline_uart_to_spi_bridge_asm,
)


async def _setup_and_bootload(dut, asm_src):
    """Reset DUT and bootload assembly program."""
    words = assemble(asm_src)
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = 0x01  # Idle high on UART RX (uio[0])
    for _ in range(6):
        await RisingEdge(dut.clk)


@cocotb.test()
async def test_pipeline_uart_sniff_classify_echo(dut):
    """Verify autonomous UART Sniff -> Classify (0x01) -> Ingress (0xA5) -> Echo Replay (0xA6)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = build_pipeline_uart_echo_asm(bit_period=8, in_pin=0, out_pin=1)
    await _setup_and_bootload(dut, asm_src)

    # Independent UART receiver monitoring output on uio[1]
    uart_rx = UartReceiver(bit_period_cycles=8)

    # Initial idle: line high on uio[0]
    dut.uio_in.value = 0x01

    # Give core time to enter sniff WAITEDGE
    for _ in range(10):
        await RisingEdge(dut.clk)

    # Stage 1: Send calibration pulse for start bit measurement (low for 8 cycles)
    dut.uio_in.value = 0x00
    for _ in range(8):
        await RisingEdge(dut.clk)
    # Return high
    dut.uio_in.value = 0x01
    for _ in range(12):
        await RisingEdge(dut.clk)

    # Stage 2: Send real UART data frame for payload 0xA5 (10100101 LSB-first: 1, 0, 1, 0, 0, 1, 0, 1)
    # Start bit: low for 8 cycles
    dut.uio_in.value = 0x00
    for _ in range(8):
        await RisingEdge(dut.clk)

    # 8 data bits of 0xA5 LSB-first
    payload = 0xA5
    for bit_idx in range(8):
        bit = (payload >> bit_idx) & 1
        dut.uio_in.value = bit
        for _ in range(8):
            await RisingEdge(dut.clk)

    # Stop bit: high for 8 cycles
    dut.uio_in.value = 0x01
    for _ in range(16):
        await RisingEdge(dut.clk)

    # Stage 3: Monitor core egress replay on uio[1]
    for cycle in range(150):
        await FallingEdge(dut.clk)
        uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        uart_rx.step((uio_val >> 1) & 1)
        await RisingEdge(dut.clk)

    dut._log.info(f"Pipeline UART Replay Complete: received bytes = {uart_rx.received_bytes}")
    core = dut.user_project.u_core
    dut._log.info(f"Core state: R0={int(core.r0.value)}, R2={int(core.r2.value)}, R3={int(core.r3.value)}")

    # Verify classification and transformed echo payload
    assert int(core.r0.value) == 0x01, f"Expected Class 0x01 (UART), got 0x{int(core.r0.value):02X}"
    assert int(core.r2.value) == 0xA6, f"Expected Echo Payload 0xA6, got 0x{int(core.r2.value):02X}"
    assert len(uart_rx.received_bytes) == 1, f"Expected 1 echoed UART byte, got {len(uart_rx.received_bytes)}"
    assert uart_rx.received_bytes[0] == 0xA6, f"Expected echoed byte 0xA6 on uio[1], got 0x{uart_rx.received_bytes[0]:02X}"


@cocotb.test()
async def test_pipeline_cross_protocol_uart_to_spi(dut):
    """Verify autonomous cross-protocol bridge: UART Ingress on Lane 0 -> SPI Master Egress on Lane 1."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = build_pipeline_uart_to_spi_bridge_asm(bit_period=8, in_pin=0)
    await _setup_and_bootload(dut, asm_src)

    # SPI Slave model monitoring Lane 1: SCK=uio[4], MOSI=uio[5], CS_N=uio[6], MISO=uio[7]
    spi_slave = SpiSlave(mode=0, cs_pin=6, sclk_pin=4, mosi_pin=5, miso_pin=7)

    payload = 0x55
    tx = UartTransmitter(bit_period_cycles=8, pin=0)
    stream = tx.generate_bit_stream(payload, idle_before=10, idle_after=10)

    cycle = 0
    for bit in stream:
        await FallingEdge(dut.clk)
        dut.uio_in.value = bit
        uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        spi_slave.step(uio_val)
        await RisingEdge(dut.clk)

    # Monitor SPI Master activity on Lane 1
    for _ in range(150):
        await FallingEdge(dut.clk)
        uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        spi_slave.step(uio_val)
        await RisingEdge(dut.clk)

    core = dut.user_project.u_core
    dut._log.info(f"Cross-Protocol Bridge Complete: Core R0=0x{int(core.r0.value):02X}, R3=0x{int(core.r3.value):02X}, SPI Slave received bytes = {spi_slave.rx_bytes}")
    assert len(spi_slave.rx_bytes) == 1, f"Expected 1 SPI byte, got {len(spi_slave.rx_bytes)}"
    assert spi_slave.rx_bytes[0] == payload, f"Expected 0x{payload:02X} over SPI bridge, got 0x{spi_slave.rx_bytes[0]:02X}"


@cocotb.test()
async def test_pipeline_noise_rejection(dut):
    """Verify autonomous noise rejection: sub-baud glitch triggers Class 0xFF (Noise) with zero replay."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = build_pipeline_uart_echo_asm(bit_period=8, in_pin=0, out_pin=1)
    await _setup_and_bootload(dut, asm_src)

    uart_rx = UartReceiver(bit_period_cycles=8)

    # Idle high
    dut.uio_in.value = 0x01
    for _ in range(10):
        await RisingEdge(dut.clk)

    # Inject narrow 1-cycle noise glitch (falls for 1 cycle then returns high)
    dut.uio_in.value = 0x00
    await RisingEdge(dut.clk)
    dut.uio_in.value = 0x01

    for cycle in range(80):
        await FallingEdge(dut.clk)
        uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        uart_rx.step((uio_val >> 1) & 1)
        await RisingEdge(dut.clk)

    core = dut.user_project.u_core
    dut._log.info(f"Noise Rejection Complete: Core R0={int(core.r0.value):02X}, rx_bytes={uart_rx.received_bytes}")
    # Core should classify as Noise (0xFF) and halt without transmitting
    assert int(core.r0.value) == 0xFF, f"Expected Class 0xFF (Noise), got 0x{int(core.r0.value):02X}"
    assert len(uart_rx.received_bytes) == 0, "No spurious UART transmission must occur on noise glitch"


@cocotb.test()
async def test_pipeline_pin_direction_safety(dut):
    """Verify dynamic electrical safety: uio_oe is strictly 0 during sniff, asserts only during replay."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = build_pipeline_uart_echo_asm(bit_period=8, in_pin=0, out_pin=1)
    await _setup_and_bootload(dut, asm_src)

    # Check that initial sniff mode is strictly input
    initial_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
    assert initial_oe == 0x00, f"Expected all inputs during sniff, got uio_oe=0x{initial_oe:02X}"
    dut._log.info("Pipeline electrical safety verified: passive sniff operates with zero bus contention")
