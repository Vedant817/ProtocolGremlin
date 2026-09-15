# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb verification test suite for Asynchronous Event Notification & Interrupt Controller Subsystem.

Test cases:
1. test_interrupt_edge_event_capture: Verifies single-cycle rising edge wake via WAITEDGE.
2. test_interrupt_level_event_handshake: Verifies level-sensitive IRQ detection and ACK pulse.
3. test_interrupt_priority_dispatcher: Verifies strict priority arbitration when multiple IRQs assert.
4. test_interrupt_nested_context_preservation: Verifies register context preservation across ISR execution.
5. test_interrupt_hardware_controller_model: Validates cycle-accurate HIC model and PPA scaling.
6. test_interrupt_pin_isolation_and_electrical_safety: Confirms bus isolation and High-Z safety.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from assembler import assemble
from bootload import bootload
from interrupt_model import (
    TriggerMode,
    InterruptControllerModel,
    InterruptPpaModel,
    build_edge_event_capture_asm,
    build_level_event_handler_asm,
    build_priority_event_dispatcher_asm,
    build_nested_context_preservation_asm,
)


async def _init_dut_and_bootload(dut, asm_text: str, initial_uio: int = 0x00):
    """Reset DUT and load assembled firmware into program RAM."""
    words = assemble(asm_text)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = initial_uio


@cocotb.test()
async def test_interrupt_edge_event_capture(dut):
    """Verify single-cycle rising edge event capture via WAITEDGE with cycle timestamp."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Wait for rising edge on pin 2 (edge_mode = 1)
    asm = build_edge_event_capture_asm(trigger_pin=2, edge_mode=1)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core reach WAITEDGE instruction
    for _ in range(15):
        await RisingEdge(dut.clk)

    # Confirm core is waiting in low-power stall
    assert not bool(core.halted.value), "Core halted prematurely before edge assertion"

    # Hold pin 2 low for 10 cycles, then assert rising edge
    for _ in range(10):
        await RisingEdge(dut.clk)
    dut.uio_in.value = 0x04  # Pin 2 high

    # Allow core to detect edge, increment R0, sample R1, and halt
    for _ in range(25):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core failed to wake from edge event and halt"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r3 = int(core.r3.value)

    assert r0 == 1, f"Expected event count R0=1, got {r0}"
    assert (r1 & 0x04) != 0, f"Expected pin 2 sampled high in R1, got 0x{r1:02X}"
    assert r3 > 0, f"Expected non-zero duration timestamp in R3, got {r3}"
    dut._log.info(f"Edge Event Capture PASS: R0={r0}, R1=0x{r1:02X}, R3={r3} cycles")


@cocotb.test()
async def test_interrupt_level_event_handshake(dut):
    """Verify level-sensitive event polling, ISR execution, and ACK handshake pulse."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # irq_pin = 2, ack_pin = 3
    asm = build_level_event_handler_asm(irq_pin=2, ack_pin=3)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    # Allow core to enter polling loop
    for _ in range(10):
        await RisingEdge(dut.clk)

    # External peripheral asserts level interrupt on pin 2
    dut.uio_in.value = 0x04  # irq_pin = 2 active high

    ack_pulse_detected = False
    for _ in range(40):
        await RisingEdge(dut.clk)
        uio_out = int(dut.uio_out.value)
        # ack_pin is pin 3 (0x08). During ACK pulse, it goes LOW (0)
        if (uio_out & 0x08) == 0:
            ack_pulse_detected = True
        if bool(core.halted.value):
            break

    assert ack_pulse_detected, "Core failed to produce ACK pulse on ack_pin (pin 3)"
    assert bool(core.halted.value), "Core did not halt cleanly after servicing interrupt"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)

    assert r0 == 42, f"Expected ISR payload execution R0=42, got {r0}"
    assert r2 == 0, f"Expected status R2=0, got {r2}"
    dut._log.info(f"Level Event Handshake PASS: R0={r0}, ACK pulse detected, R2={r2}")


@cocotb.test()
async def test_interrupt_priority_dispatcher(dut):
    """Verify strict priority arbitration: when Pin 0 and Pin 1 assert, Pin 0 runs first."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_priority_event_dispatcher_asm(prio0_pin=0, prio1_pin=1)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core start polling
    for _ in range(5):
        await RisingEdge(dut.clk)

    # Assert BOTH pin 0 (Priority 0) and pin 1 (Priority 1) simultaneously
    dut.uio_in.value = 0x03

    for _ in range(30):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt after priority dispatch"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)

    # Highest priority ISR0 increments R0 by 10; ISR1 increments R1 by 5
    assert r0 == 10, f"Expected high-priority Task 0 serviced first (R0=10), got {r0}"
    assert r1 == 0, f"Expected lower-priority Task 1 deferred (R1=0), got {r1}"
    dut._log.info(f"Priority Arbitration PASS: R0={r0} (Prio 0), R1={r1} (Prio 1 deferred)")


@cocotb.test()
async def test_interrupt_nested_context_preservation(dut):
    """Verify register context save and restore across simulated ISR execution."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_nested_context_preservation_asm()
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(30):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not complete context preservation program"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)

    assert r0 == 0x42, f"Expected restored background context R0=0x42, got 0x{r0:02X}"
    assert r1 == 0x11, f"Expected restored background context R1=0x11, got 0x{r1:02X}"
    dut._log.info(f"Context Preservation PASS: R0=0x{r0:02X}, R1=0x{r1:02X} fully restored")


@cocotb.test()
async def test_interrupt_hardware_controller_model(dut):
    """Validate cycle-accurate InterruptControllerModel and PPA scaling model."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Initialize 4-channel controller model
    hic = InterruptControllerModel(num_channels=4)
    hic.set_trigger_mode(0, TriggerMode.RISING_EDGE)
    hic.set_trigger_mode(1, TriggerMode.FALLING_EDGE)
    hic.set_trigger_mode(2, TriggerMode.ACTIVE_HIGH)
    hic.set_trigger_mode(3, TriggerMode.ACTIVE_LOW)

    # Initial state: no pending interrupts
    assert hic.ipr == 0, "Initial IPR should be 0"
    assert hic.get_highest_priority_pending() is None, "No interrupt should be pending"

    # Step 1: Trigger Channel 0 (Rising Edge)
    hic.update_inputs({0: 1})
    assert (hic.ipr & 0x01) != 0, "Channel 0 should be pending on rising edge"

    # Step 2: Trigger Channel 2 (Active High)
    hic.update_inputs({2: 1})
    assert (hic.ipr & 0x05) == 0x05, "Channels 0 and 2 should be pending"

    # Priority check: Channel 0 has priority 0 (higher than Channel 2)
    highest = hic.get_highest_priority_pending()
    assert highest is not None and highest.channel_id == 0, "Channel 0 should take priority"

    # Acknowledge Channel 0
    vec0 = hic.acknowledge_interrupt(0)
    assert vec0 == 0x10, f"Expected vector 0x10, got {vec0}"
    assert (hic.ipr & 0x01) == 0, "Channel 0 should be cleared after ack"

    # Now Channel 2 should be highest
    highest2 = hic.get_highest_priority_pending()
    assert highest2 is not None and highest2.channel_id == 2, "Channel 2 should be next highest"

    # Mask Channel 2
    hic.set_mask(0x0B)  # Disable bit 2 (1011b)
    highest_masked = hic.get_highest_priority_pending()
    assert highest_masked is not None and highest_masked.channel_id == 3, "Channel 3 should take over when Channel 2 is masked"

    # Mask both Channel 2 and 3
    hic.set_mask(0x03)  # Disable bits 2 and 3 (0011b)
    assert hic.get_highest_priority_pending() is None, "All pending channels should now be masked out"

    # Verify PPA metrics
    m4 = InterruptPpaModel.get_config_metrics("hic_4channel")
    assert m4["cells"] == 145, f"Expected 145 cells, got {m4['cells']}"
    assert m4["area_overhead_pct"] < 1.0, "4-channel overhead should be < 1%"
    assert m4["speedup_vs_software"] > 1.5, "Expected speedup > 1.5x"

    m8 = InterruptPpaModel.get_config_metrics("hic_8channel")
    assert m8["cells"] == 260, f"Expected 260 cells, got {m8['cells']}"
    assert m8["area_overhead_pct"] < 1.5, "8-channel overhead should be < 1.5%"

    # Short dummy DUT step to satisfy cocotb
    await RisingEdge(dut.clk)
    dut._log.info(f"HIC Model & PPA Validation PASS: 4-channel (+{m4['cells']} cells, {m4['speedup_vs_software']:.2f}x speedup)")


@cocotb.test()
async def test_interrupt_pin_isolation_and_electrical_safety(dut):
    """Verify GPIO pin direction electrical safety during interrupt polling and dispatch."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_priority_event_dispatcher_asm(prio0_pin=0, prio1_pin=1)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    # Check throughout polling that all pins remain inputs (uio_oe == 0x00)
    for _ in range(15):
        await RisingEdge(dut.clk)
        uio_oe = int(dut.uio_oe.value)
        assert uio_oe == 0x00, f"Expected strictly High-Z input mode (uio_oe=0x00), got 0x{uio_oe:02X}"

    # Assert IRQ and verify execution
    dut.uio_in.value = 0x01
    for _ in range(25):
        await RisingEdge(dut.clk)
        uio_oe = int(dut.uio_oe.value)
        assert uio_oe == 0x00, f"Expected strictly High-Z during ISR (uio_oe=0x00), got 0x{uio_oe:02X}"
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core failed to complete execution"
    dut._log.info("Electrical Safety PASS: uio_oe remained strictly 0x00 (High-Z) throughout")
