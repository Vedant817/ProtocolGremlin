# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for Dallas 1-Wire Master protocol engine.

Verifies:
1. 1-Wire Reset & Presence Pulse Discovery with single-cycle precision via WAITEDGE.
2. 1-Wire Read Timeslots: Master reads scratchpad bytes from OneWireSlave into R0.
3. 1-Wire Write Timeslots: Master writes commands (0xCC Skip ROM, 0x44 Convert T) to slave.
4. Physical open-drain electrical safety: Zero contention, master never actively drives high.
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
from onewire_model import (  # noqa: E402
    OneWireSlave,
    build_onewire_reset_presence_asm,
    build_onewire_read_byte_asm,
    build_onewire_write_byte_asm,
)

PIN = 3
PIN_MASK = 1 << PIN


async def _init_dut_and_bootload(dut, words: list[int]):
    """Fresh hardware reset and bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = PIN_MASK  # 1-Wire pullup pulls idle bus high
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = PIN_MASK


@cocotb.test()
async def test_onewire_reset_and_presence(dut):
    """Test 1-Wire Master reset pulse and presence pulse measurement across durations."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_durations = [15, 20, 25, 30]

    for expected_duration in test_durations:
        dut._log.info(f"Testing 1-Wire Reset & Presence: duration = {expected_duration} cycles")
        asm = build_onewire_reset_presence_asm(pin=PIN, reset_low_cycles=40)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        slave = OneWireSlave(
            reset_threshold=25,
            presence_delay=8,
            presence_duration=expected_duration,
        )
        core = dut.user_project.u_core

        bus_bit = 1
        for cycle in range(300):
            await FallingEdge(dut.clk)
            dut.uio_in.value = bus_bit << PIN

            await RisingEdge(dut.clk)
            await ReadOnly()

            uio_oe = int(dut.uio_oe.value)
            uio_out = int(dut.uio_out.value)
            # In open-drain mode, master pulls low when output is enabled and low
            master_drives_low = bool((uio_oe & PIN_MASK) and not (uio_out & PIN_MASK))

            # Electrical safety: master must never actively drive HIGH in open drain
            assert not ((uio_oe & PIN_MASK) and (uio_out & PIN_MASK)), (
                f"Contention bug: master drove active high on open-drain bus at cycle {cycle}"
            )

            slave_drives_low = slave.step(master_drives_low)
            bus_low = master_drives_low or slave_drives_low
            bus_bit = 0 if bus_low else 1

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"1-Wire presence test timed out for duration {expected_duration}")

        measured_duration = int(core.r1.value)
        dut._log.info(
            f"1-Wire Presence Detect: expected={expected_duration} cycles, "
            f"measured (R1)={measured_duration} cycles"
        )
        assert measured_duration == expected_duration, (
            f"Presence pulse mismatch: expected {expected_duration}, got {measured_duration}"
        )


@cocotb.test()
async def test_onewire_read_byte(dut):
    """Test 1-Wire Master read timeslots receiving bytes from OneWireSlave."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_bytes = [0x05, 0x55, 0xAA, 0x00, 0xFF, 0x3C]

    for expected_byte in test_bytes:
        dut._log.info(f"Testing 1-Wire Read Byte: expected=0x{expected_byte:02X}")
        asm = build_onewire_read_byte_asm(pin=PIN, slot_total_cycles=30)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        slave = OneWireSlave(tx_bytes=[expected_byte])
        core = dut.user_project.u_core

        bus_bit = 1
        for cycle in range(400):
            await FallingEdge(dut.clk)
            dut.uio_in.value = bus_bit << PIN

            await RisingEdge(dut.clk)
            await ReadOnly()

            uio_oe = int(dut.uio_oe.value)
            uio_out = int(dut.uio_out.value)
            master_drives_low = bool((uio_oe & PIN_MASK) and not (uio_out & PIN_MASK))

            slave_drives_low = slave.step(master_drives_low)
            bus_low = master_drives_low or slave_drives_low
            bus_bit = 0 if bus_low else 1

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"1-Wire read timed out for byte 0x{expected_byte:02X}")

        received_byte = int(core.r0.value)
        dut._log.info(f"1-Wire Read Byte: expected=0x{expected_byte:02X}, received (R0)=0x{received_byte:02X}")
        assert received_byte == expected_byte, (
            f"1-Wire read byte mismatch: expected 0x{expected_byte:02X}, got 0x{received_byte:02X}"
        )


@cocotb.test()
async def test_onewire_write_byte(dut):
    """Test 1-Wire Master write timeslots sending commands to OneWireSlave."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    commands = [0xCC, 0x44, 0xBE, 0x55, 0xAA]

    for cmd in commands:
        dut._log.info(f"Testing 1-Wire Write Byte: command=0x{cmd:02X}")
        asm = build_onewire_write_byte_asm(byte_val=cmd, pin=PIN, slot_total_cycles=30)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        slave = OneWireSlave()
        core = dut.user_project.u_core

        bus_bit = 1
        for cycle in range(400):
            await FallingEdge(dut.clk)
            dut.uio_in.value = bus_bit << PIN

            await RisingEdge(dut.clk)
            await ReadOnly()

            uio_oe = int(dut.uio_oe.value)
            uio_out = int(dut.uio_out.value)
            master_drives_low = bool((uio_oe & PIN_MASK) and not (uio_out & PIN_MASK))

            slave_drives_low = slave.step(master_drives_low)
            bus_low = master_drives_low or slave_drives_low
            bus_bit = 0 if bus_low else 1

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"1-Wire write timed out for command 0x{cmd:02X}")

        assert slave.rx_bytes == [cmd], (
            f"1-Wire write mismatch: expected {[hex(cmd)]}, slave received {[hex(x) for x in slave.rx_bytes]}"
        )
        dut._log.info(f"1-Wire Write: command 0x{cmd:02X} received accurately by slave")


@cocotb.test()
async def test_onewire_electrical_safety(dut):
    """Verify cycle-by-cycle that 1-Wire master never creates electrical contention on external pull-down."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_onewire_read_byte_asm(pin=PIN, slot_total_cycles=30)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core

    # External device forces bus low throughout the entire execution
    for cycle in range(250):
        await FallingEdge(dut.clk)
        dut.uio_in.value = 0  # Forced low externally

        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        uio_out = int(dut.uio_out.value)
        # Verify master never asserts uio_out=1 while uio_oe=1 on open-drain pin
        assert not ((uio_oe & PIN_MASK) and ((uio_out >> PIN) & 1)), (
            f"Contention bug at cycle {cycle}: master actively drove 1 against external pull-down!"
        )

        if bool(core.halted.value):
            break

    dut._log.info("1-Wire open-drain electrical safety verified: zero bus contention under forced external pull-down.")

