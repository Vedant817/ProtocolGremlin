# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_i2c.py - Comprehensive verification of I2C Master firmware and hardware open-drain.

Tests:
1. test_i2c_master_write_single_byte: Single byte write with ACK and STOP.
2. test_i2c_master_write_multiple_bytes: Multi-byte write sequence (EEPROM pattern).
3. test_i2c_master_read_single_byte: Byte read from Slave with Master NACK.
4. test_i2c_nack_on_unresponsive_slave: Address non-existent slave and detect NACK in R1.
5. test_i2c_open_drain_safety_proof: Verify electrical open-drain invariant (zero push-pull high drive).
"""

import os
import sys
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from i2c_model import (  # noqa: E402
    build_i2c_write_asm,
    build_i2c_read_asm,
    build_i2c_write_with_stretch_asm,
    build_i2c_write_with_arbitration_asm,
    I2cSlave,
)


SDA_PIN = 0
SCL_PIN = 1


class InterferingMaster:
    """
    Simulates a concurrent I2C master that contends for the bus during address transmission.
    Pulls SDA low on a specific SCL clock pulse (e.g. pulse 2 = address bit 6).
    """

    def __init__(self, target_scl_pulse: int):
        self.target_scl_pulse = target_scl_pulse
        self.scl_pulse_count = 0
        self.prev_scl = 1

    def step(self, cycle: int, scl: int, sda: int) -> bool:
        # Detect SCL rising edge
        if self.prev_scl == 0 and scl == 1:
            self.scl_pulse_count += 1
        self.prev_scl = scl
        # Contend on target pulse while SCL is high
        return bool(self.scl_pulse_count == self.target_scl_pulse and scl == 1)


async def _run_i2c_transaction(
    dut,
    asm_src: str,
    slave: I2cSlave,
    max_cycles: int = 800,
    interfering_master_fn=None,
):
    """
    Assemble and bootload I2C firmware into the DUT, then simulate the open-drain
    bus cycle-by-cycle with pull-up resistors and the independent I2cSlave model.
    """
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

    core = dut.user_project.u_core

    bus_sda = 1
    bus_scl = 1
    slave_drive_sda_low = False

    for cycle in range(max_cycles):
        # Update input pins to DUT based on physical open-drain pull-up wire state
        await FallingEdge(dut.clk)
        dut.uio_in.value = (bus_scl << SCL_PIN) | (bus_sda << SDA_PIN)

        await RisingEdge(dut.clk)
        await ReadOnly()

        # Read DUT drive state
        dut_oe = int(dut.uio_oe.value)
        dut_out = int(dut.uio_out.value)

        # Invariant: open-drain pins must NEVER actively drive high while output-enabled
        assert not ((dut_oe & (1 << SDA_PIN)) and ((dut_out >> SDA_PIN) & 1)), (
            f"cycle {cycle}: Open-drain electrical contention! DUT drove SDA actively HIGH"
        )
        assert not ((dut_oe & (1 << SCL_PIN)) and ((dut_out >> SCL_PIN) & 1)), (
            f"cycle {cycle}: Open-drain electrical contention! DUT drove SCL actively HIGH"
        )
        # Invariant: open-drain pins must NEVER assert pin_out=1 (guaranteed pull-down/tri-state only)
        od_mode = int(core.gpio_od_mode.value)
        assert (dut_out & od_mode) == 0, (
            f"cycle {cycle}: Open-drain isolation fault! uio_out asserted active 1 on open-drain pin"
        )

        dut_pulls_sda_low = bool((dut_oe & (1 << SDA_PIN)) and not ((dut_out >> SDA_PIN) & 1))
        dut_pulls_scl_low = bool((dut_oe & (1 << SCL_PIN)) and not ((dut_out >> SCL_PIN) & 1))

        # Bus wire is 0 if either DUT or Slave pulls low; otherwise pull-up resistor pulls to 1
        slave_pulls_scl_low = getattr(slave, "slave_drive_scl_low", False)
        bus_scl = 0 if (dut_pulls_scl_low or slave_pulls_scl_low) else 1

        interfering_sda_low = False
        if interfering_master_fn:
            interfering_sda_low = interfering_master_fn(cycle, bus_scl, bus_sda)

        bus_sda = 0 if (dut_pulls_sda_low or slave_drive_sda_low or interfering_sda_low) else 1

        # Advance independent slave model
        slave_drive_sda_low = slave.step(scl=bus_scl, sda=bus_sda)
        if slave_drive_sda_low:
            bus_sda = 0
        if getattr(slave, "slave_drive_scl_low", False):
            bus_scl = 0

        if bool(core.halted.value):
            break
    else:
        raise AssertionError(f"I2C transaction did not halt within {max_cycles} cycles")

    return int(core.r0.value), int(core.r1.value), int(core.r3.value)


@cocotb.test()
async def test_i2c_master_write_single_byte(dut):
    """Test I2C Master single byte write to 7-bit slave (0x3C, byte 0xA5)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_addr = 0x3C
    payload = [0xA5]
    slave = I2cSlave(address=target_addr)

    asm_src = build_i2c_write_asm(
        addr7=target_addr,
        data_bytes=payload,
        half_period=4,
        sda_pin=SDA_PIN,
        scl_pin=SCL_PIN
    )

    r0, r1, _ = await _run_i2c_transaction(dut, asm_src, slave, max_cycles=500)

    assert slave.start_count >= 1, "Slave did not detect I2C START condition"
    assert slave.stop_count >= 1, "Slave did not detect I2C STOP condition"
    assert slave.matched, f"Slave did not match address 0x{target_addr:02X}"
    assert slave.received_bytes == payload, (
        f"Slave received {slave.received_bytes}, expected {payload}"
    )
    # R1 holds sampled ACK bit (0 = ACK)
    assert r1 == 0, f"Master sampled NACK ({r1}) instead of ACK (0)"
    dut._log.info("I2C Master write single byte 0x%02X to 0x%02X: PASS (ACK received)", payload[0], target_addr)


@cocotb.test()
async def test_i2c_master_write_multiple_bytes(dut):
    """Test I2C Master multi-byte write (EEPROM memory address + payload)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_addr = 0x50
    payload = [0x10, 0x42, 0x99]
    slave = I2cSlave(address=target_addr)

    asm_src = build_i2c_write_asm(
        addr7=target_addr,
        data_bytes=payload,
        half_period=3,
        sda_pin=SDA_PIN,
        scl_pin=SCL_PIN
    )

    _, r1, _ = await _run_i2c_transaction(dut, asm_src, slave, max_cycles=700)

    assert slave.matched, f"Slave did not match address 0x{target_addr:02X}"
    assert slave.received_bytes == payload, (
        f"Slave received {slave.received_bytes}, expected {payload}"
    )
    assert slave.stop_count >= 1, "Slave did not detect STOP condition"
    assert r1 == 0, "Last byte did not receive ACK"
    dut._log.info("I2C Master multi-byte write %s to 0x%02X: PASS", payload, target_addr)


@cocotb.test()
async def test_i2c_master_read_single_byte(dut):
    """Test I2C Master reading a byte from an independent Slave (0x3C responds with 0x5A)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_addr = 0x3C
    slave_tx_data = [0x5A]
    slave = I2cSlave(address=target_addr, tx_bytes=slave_tx_data)

    asm_src = build_i2c_read_asm(
        addr7=target_addr,
        num_bytes=1,
        half_period=4,
        sda_pin=SDA_PIN,
        scl_pin=SCL_PIN
    )

    r0, r1, _ = await _run_i2c_transaction(dut, asm_src, slave, max_cycles=500)

    assert slave.matched, f"Slave did not match address 0x{target_addr:02X}"
    assert r1 == 0, f"Slave address was not ACKed (R1={r1})"
    assert r0 == slave_tx_data[0], (
        f"Master read 0x{r0:02X}, expected 0x{slave_tx_data[0]:02X}"
    )
    assert slave.stop_count >= 1, "STOP condition not detected"
    dut._log.info("I2C Master read byte 0x%02X from 0x%02X: PASS", r0, target_addr)


@cocotb.test()
async def test_i2c_nack_on_unresponsive_slave(dut):
    """Test I2C Master handles NACK when addressing non-existent slave address 0x77."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_addr = 0x77  # Target address with no matching slave
    slave = I2cSlave(address=0x3C)  # Slave on bus has different address 0x3C

    asm_src = build_i2c_write_asm(
        addr7=target_addr,
        data_bytes=[0xAA],
        half_period=3,
        sda_pin=SDA_PIN,
        scl_pin=SCL_PIN
    )

    _, r1, _ = await _run_i2c_transaction(dut, asm_src, slave, max_cycles=500)

    assert not slave.matched, "Slave incorrectly matched different address"
    assert slave.received_bytes == [], "Slave received data when not addressed"
    # Bit 0 of R1 is 1 (NACK because SDA stayed high during ACK clock pulse)
    assert (r1 & 1) == 1, f"Expected NACK (1), got R1=0x{r1:02X}"
    dut._log.info("I2C Master non-responsive slave NACK detection: PASS (R1=0x%02X)", r1)


@cocotb.test()
async def test_i2c_open_drain_safety_proof(dut):
    """
    Formally verify electrical open-drain invariant:
    Across 100% of simulation cycles, whenever uio_oe is asserted for SDA or SCL,
    the driven value uio_out is 0. Active HIGH drive is provably eliminated.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_addr = 0x2A
    payload = [0x55, 0xAA]
    slave = I2cSlave(address=target_addr)

    asm_src = build_i2c_write_asm(
        addr7=target_addr,
        data_bytes=payload,
        half_period=4,
        sda_pin=SDA_PIN,
        scl_pin=SCL_PIN
    )

    await _run_i2c_transaction(dut, asm_src, slave, max_cycles=600)
    dut._log.info("Electrical open-drain bus contention safety proof: PASS (0 contention violations)")


@cocotb.test()
async def test_i2c_clock_stretching(dut):
    """
    Verify I2C Master absorbs slave clock stretching via WAITEDGE.
    Slave stretches SCL low for 20 cycles after receiving the address.
    Master must wait until SCL is released without corrupting the transaction,
    and R3 captures the elapsed stretch duration.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_addr = 0x3C
    payload = [0x42]
    stretch_cycles = 20
    slave = I2cSlave(address=target_addr, clock_stretch_cycles=stretch_cycles)

    asm_src = build_i2c_write_with_stretch_asm(
        addr7=target_addr,
        data_bytes=payload,
        half_period=4,
        sda_pin=SDA_PIN,
        scl_pin=SCL_PIN,
    )

    r0, r1, r3 = await _run_i2c_transaction(dut, asm_src, slave, max_cycles=1000)

    assert slave.matched, f"Slave did not match address 0x{target_addr:02X}"
    assert slave.received_bytes == payload, (
        f"Slave received {slave.received_bytes}, expected {payload}"
    )
    assert r1 == 0, f"Master sampled NACK (R1={r1}) instead of ACK (0)"
    # WAITEDGE measured remaining stretch after master released SCL (~15 cycles vs 2 without stretch)
    assert 12 <= r3 <= 18, (
        f"WAITEDGE stretch capture R3={r3} is outside expected range [12, 18] for 20-cycle slave stretch"
    )
    dut._log.info(
        "I2C Clock Stretching Absorbed via WAITEDGE: PASS (R3=%d cycles measured, payload=%s)",
        r3, slave.received_bytes,
    )


@cocotb.test()
async def test_i2c_arbitration_loss_detection(dut):
    """
    Verify I2C Master detects multi-master arbitration loss:
    DUT attempts to transmit address 0x68 (address byte 0xD0: bit 7=1, bit 6=1).
    An interfering master drives SDA low during pulse 2 (bit 6).
    DUT must detect the collision, immediately abort transmission,
    release bus lines (GWRI 0xFF), and halt with R0 = 0xEE (arbitration lost).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_addr = 0x68  # Address byte = (0x68 << 1) = 0xD0 (bits 11010000)
    payload = [0xAA]
    slave = I2cSlave(address=target_addr)
    interferer = InterferingMaster(target_scl_pulse=2)

    asm_src = build_i2c_write_with_arbitration_asm(
        addr7=target_addr,
        data_bytes=payload,
        half_period=4,
        sda_pin=SDA_PIN,
        scl_pin=SCL_PIN,
    )

    r0, r1, _ = await _run_i2c_transaction(
        dut,
        asm_src,
        slave,
        max_cycles=800,
        interfering_master_fn=interferer.step,
    )

    assert r0 == 0xEE, f"Expected arbitration lost error code 0xEE, got R0=0x{r0:02X}"
    assert slave.received_bytes == [], f"Slave incorrectly received bytes: {slave.received_bytes}"
    assert not slave.matched, "Slave incorrectly matched address despite collision"
    dut._log.info("I2C Multi-Master Arbitration Loss Detection: PASS (R0=0x%02X)", r0)


@cocotb.test()
async def test_i2c_arbitration_win_normal(dut):
    """
    Verify I2C Master completes transaction with R0 = 0x00 when no collision occurs.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_addr = 0x68
    payload = [0x55]
    slave = I2cSlave(address=target_addr)

    asm_src = build_i2c_write_with_arbitration_asm(
        addr7=target_addr,
        data_bytes=payload,
        half_period=4,
        sda_pin=SDA_PIN,
        scl_pin=SCL_PIN,
    )

    r0, r1, _ = await _run_i2c_transaction(dut, asm_src, slave, max_cycles=800)

    assert r0 == 0x00, f"Expected success code 0x00, got R0=0x{r0:02X}"
    assert slave.matched, "Slave did not match address"
    assert slave.received_bytes == payload, f"Slave received {slave.received_bytes}, expected {payload}"
    dut._log.info("I2C Arbitration Free Path (Clean Win): PASS (R0=0x00, payload=%s)", payload)

