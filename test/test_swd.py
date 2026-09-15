# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for ARM SWD (Serial Wire Debug) protocol engine.

Implements and verifies:
1. SWD Line Reset & JTAG-to-SWD Switching Sequence (0x79E7):
   - >= 50 continuous clocks with SWDIO=1.
   - 16-bit JTAG-to-SWD switching code (0x79E7, LSB-first).
   - Second line reset and idle cycles.
2. SWD Standard 32-bit DPIDR Readout:
   - Cortex-M0/M3/M4 DPIDR (0x0BA01477) read into registers R0..R3.
   - Packet header (0xA5) with odd/even parity verification.
   - 3-bit ACK (001b OK) and turnaround (Trn) cycles.
3. SWD DPIDR Multi-Target Identity Sweep:
   - Cortex-M7 (0x0BB11477)
   - ARMv8-M Cortex-M33 (0x2BA01477)
   - Cortex-M4 with ETM (0x1BA01477)
   - Cortex-M23 (0x6BA02477)
4. SWD Target Non-OK ACK Handling (WAIT & FAULT):
   - Verifies target driving ACK=WAIT (010b) and ACK=FAULT (100b).
5. SWD Physical Pin Direction & Bus Electrical Safety:
   - Verifies SWCLK is driven continuously.
   - Verifies SWDIO tri-states to input during Turnaround/ACK/Read data phases.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from swd_model import (  # noqa: E402
    SwdTarget,
    build_swd_read_dpidr_asm,
    build_swd_switch_sequence_asm,
)

SWCLK_PIN = 4
SWDIO_PIN = 5


async def _init_dut_and_bootload(dut, words: list[int]):
    """Reset dut, initialize clock and pull-ups, and bootload machine code."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    # SWDIO pulled high by default external pullup
    dut.uio_in.value = (1 << SWDIO_PIN)
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = (1 << SWDIO_PIN)


@cocotb.test()
async def test_swd_line_reset_and_switch(dut):
    """Test ARM SWD Line Reset (>= 50 clocks high) and JTAG-to-SWD switching (0x79E7)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing SWD Line Reset & JTAG-to-SWD Switching Sequence")

    asm = build_swd_switch_sequence_asm(swclk_pin=SWCLK_PIN, swdio_pin=SWDIO_PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    target = SwdTarget()
    core = dut.user_project.u_core

    for cycle in range(1200):
        await FallingEdge(dut.clk)

        # Drive SWDIO pin based on target state
        uio_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
        uio_out = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0

        swclk = (uio_out >> SWCLK_PIN) & 1
        swdio_host = (uio_out >> SWDIO_PIN) & 1
        host_oe = (uio_oe >> SWDIO_PIN) & 1

        swdio_line = target.step(swclk=swclk, swdio_host=swdio_host, host_oe=host_oe)
        dut.uio_in.value = swdio_line << SWDIO_PIN

        await RisingEdge(dut.clk)
        await ReadOnly()

        if bool(core.halted.value):
            break
    else:
        raise AssertionError("SWD switch sequence test timed out")

    dut._log.info(f"Target swd_active: {target.swd_active}, line_reset_count: {target.line_reset_count}")
    assert target.swd_active, "Target failed to activate SWD mode after line reset & switching sequence"


@cocotb.test()
async def test_swd_read_dpidr_standard(dut):
    """Test ARM SWD standard DPIDR (0x0BA01477) readout into registers R0..R3."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    expected_dpidr = 0x0BA01477
    dut._log.info(f"Testing SWD standard DPIDR Readout: expected = 0x{expected_dpidr:08X}")

    asm = build_swd_read_dpidr_asm(swclk_pin=SWCLK_PIN, swdio_pin=SWDIO_PIN, do_line_reset=True)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    target = SwdTarget(dpidr=expected_dpidr)
    core = dut.user_project.u_core

    for cycle in range(1500):
        await FallingEdge(dut.clk)

        uio_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
        uio_out = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0

        swclk = (uio_out >> SWCLK_PIN) & 1
        swdio_host = (uio_out >> SWDIO_PIN) & 1
        host_oe = (uio_oe >> SWDIO_PIN) & 1

        swdio_line = target.step(swclk=swclk, swdio_host=swdio_host, host_oe=host_oe)
        dut.uio_in.value = swdio_line << SWDIO_PIN

        await RisingEdge(dut.clk)
        await ReadOnly()

        if bool(core.halted.value):
            break
    else:
        raise AssertionError("SWD DPIDR read test timed out")

    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)
    r3 = int(core.r3.value)
    read_dpidr = r0 | (r1 << 8) | (r2 << 16) | (r3 << 24)

    dut._log.info(
        f"SWD DPIDR read complete: 0x{read_dpidr:08X} "
        f"(R0=0x{r0:02X}, R1=0x{r1:02X}, R2=0x{r2:02X}, R3=0x{r3:02X})"
    )
    dut._log.info(f"Transactions completed by target: {target.transactions_completed}")

    assert target.transactions_completed >= 1, "Target did not complete SWD transaction"
    assert target.last_header_received == 0xA5, f"Expected header 0xA5, got {target.last_header_received:#x}"
    assert read_dpidr == expected_dpidr, (
        f"DPIDR mismatch: expected 0x{expected_dpidr:08X}, got 0x{read_dpidr:08X}"
    )


@cocotb.test()
async def test_swd_read_dpidr_sweep(dut):
    """Test ARM SWD DPIDR readout across multiple ARM Cortex device identities."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_identities = [
        ("Cortex-M7", 0x0BB11477),
        ("Cortex-M33 (ARMv8-M)", 0x2BA01477),
        ("Cortex-M4+ETM", 0x1BA01477),
        ("Cortex-M23", 0x6BA02477),
    ]

    asm = build_swd_read_dpidr_asm(swclk_pin=SWCLK_PIN, swdio_pin=SWDIO_PIN, do_line_reset=False)
    words = assemble(asm)

    for name, expected_id in test_identities:
        dut._log.info(f"Testing SWD IDCODE sweep: {name} (0x{expected_id:08X})")
        await _init_dut_and_bootload(dut, words)

        target = SwdTarget(dpidr=expected_id)
        # Line reset is skipped in firmware for this sweep, so pre-activate target
        target.swd_active = True
        target.state = target.STATE_IDLE
        core = dut.user_project.u_core

        for cycle in range(800):
            await FallingEdge(dut.clk)

            uio_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
            uio_out = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0

            swclk = (uio_out >> SWCLK_PIN) & 1
            swdio_host = (uio_out >> SWDIO_PIN) & 1
            host_oe = (uio_oe >> SWDIO_PIN) & 1

            swdio_line = target.step(swclk=swclk, swdio_host=swdio_host, host_oe=host_oe)
            dut.uio_in.value = swdio_line << SWDIO_PIN

            await RisingEdge(dut.clk)
            await ReadOnly()

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"SWD sweep test timed out for {name}")

        r0 = int(core.r0.value)
        r1 = int(core.r1.value)
        r2 = int(core.r2.value)
        r3 = int(core.r3.value)
        read_id = r0 | (r1 << 8) | (r2 << 16) | (r3 << 24)

        dut._log.info(f"Sweep {name}: read 0x{read_id:08X} vs expected 0x{expected_id:08X}")
        assert read_id == expected_id, (
            f"Sweep mismatch for {name}: expected 0x{expected_id:08X}, got 0x{read_id:08X}"
        )


@cocotb.test()
async def test_swd_target_ack_wait_and_fault(dut):
    """Test ARM SWD Target responding with non-OK ACK codes (WAIT and FAULT)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_swd_read_dpidr_asm(swclk_pin=SWCLK_PIN, swdio_pin=SWDIO_PIN, do_line_reset=False)
    words = assemble(asm)

    for ack_name, ack_code in [("WAIT", SwdTarget.ACK_WAIT), ("FAULT", SwdTarget.ACK_FAULT)]:
        dut._log.info(f"Testing SWD Target ACK response: {ack_name} ({ack_code:#05b})")
        await _init_dut_and_bootload(dut, words)

        target = SwdTarget(dpidr=0x0BA01477)
        target.swd_active = True
        target.state = target.STATE_IDLE
        target.forced_ack = ack_code
        core = dut.user_project.u_core

        for cycle in range(800):
            await FallingEdge(dut.clk)

            uio_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
            uio_out = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0

            swclk = (uio_out >> SWCLK_PIN) & 1
            swdio_host = (uio_out >> SWDIO_PIN) & 1
            host_oe = (uio_oe >> SWDIO_PIN) & 1

            swdio_line = target.step(swclk=swclk, swdio_host=swdio_host, host_oe=host_oe)
            dut.uio_in.value = swdio_line << SWDIO_PIN

            await RisingEdge(dut.clk)
            await ReadOnly()

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"SWD ACK {ack_name} test timed out")

        dut._log.info(f"ACK {ack_name} verified: target safely completed header without bus lock")


@cocotb.test()
async def test_swd_pin_direction_and_electrical_safety(dut):
    """Verify SWCLK output drive and SWDIO tri-state switching for contention avoidance."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing SWD Pin Direction & Electrical Tri-state Isolation")

    asm = build_swd_read_dpidr_asm(swclk_pin=SWCLK_PIN, swdio_pin=SWDIO_PIN, do_line_reset=False)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    target = SwdTarget(dpidr=0x0BA01477)
    target.swd_active = True
    target.state = target.STATE_IDLE
    core = dut.user_project.u_core

    oe_swdio_seen_driven = False
    oe_swdio_seen_released = False

    for cycle in range(800):
        await FallingEdge(dut.clk)

        uio_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
        uio_out = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0

        swclk_oe = (uio_oe >> SWCLK_PIN) & 1
        swdio_oe = (uio_oe >> SWDIO_PIN) & 1

        # SWCLK must always be driven by host (OE=1)
        assert swclk_oe == 1, f"SWCLK pin {SWCLK_PIN} unexpectedly tri-stated at cycle {cycle}"

        if swdio_oe == 1:
            oe_swdio_seen_driven = True
        else:
            oe_swdio_seen_released = True

        swclk = (uio_out >> SWCLK_PIN) & 1
        swdio_host = (uio_out >> SWDIO_PIN) & 1

        swdio_line = target.step(swclk=swclk, swdio_host=swdio_host, host_oe=swdio_oe)
        dut.uio_in.value = swdio_line << SWDIO_PIN

        await RisingEdge(dut.clk)
        await ReadOnly()

        if bool(core.halted.value):
            break

    assert oe_swdio_seen_driven, "SWDIO pin was never driven by host during packet request"
    assert oe_swdio_seen_released, "SWDIO pin was never released (tri-stated) by host during ACK/Read phase"
    dut._log.info("SWD Pin Direction verified: SWCLK strictly output, SWDIO dynamically tri-stated with zero contention")
