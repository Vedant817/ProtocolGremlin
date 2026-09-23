# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""test/test_sleep_controller.py - Cocotb test suite for Low-Power Sleep & Wakeup Subsystem.

Verifies:
1. test_sleep_state_transitions: Validates transitions across ACTIVE, IDLE_WAIT, STANDBY, and DEEP_SLEEP.
2. test_sleep_timer_wakeup: Bounded sleep timer countdown and deterministic timed wakeup.
3. test_sleep_gpio_edge_wakeup_rtl: Physical in-core RTL execution of low-power WAITEDGE stall and edge wakeup.
4. test_sleep_noise_glitch_rejection: Digital deglitching filter rejecting sub-filter runt pulses.
5. test_sleep_cause_status_register: Accurate latching of multi-source wakeup events and pin IDs.
6. test_sleep_controller_ppa_synthesis: Analytical PPA power and energy scaling on IHP 130nm SG13CMOS5L.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from sleep_controller_model import (  # noqa: E402
    SleepController,
    SleepPpaMetrics,
    SleepPowerState,
    WakeupSource,
    get_sleep_wakeup_microcode_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00):
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


@cocotb.test()
async def test_sleep_state_transitions(dut):
    """Test 1: Verify power state progression and dwell times (ACTIVE -> IDLE -> STANDBY -> DEEP_SLEEP)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing sleep state transitions...")
    ctrl = SleepController(glitch_filter_cycles=2)

    # 1. Start in ACTIVE
    assert ctrl.current_state == SleepPowerState.ACTIVE
    for _ in range(10):
        ctrl.tick_cycle()
    assert ctrl.cycles_in_state[SleepPowerState.ACTIVE] == 10

    # 2. Enter IDLE_WAIT
    ctrl.enter_sleep(SleepPowerState.IDLE_WAIT)
    assert ctrl.current_state == SleepPowerState.IDLE_WAIT
    for _ in range(5):
        ctrl.tick_cycle()
    assert ctrl.cycles_in_state[SleepPowerState.IDLE_WAIT] == 5

    # 3. Enter STANDBY_RETENTION
    ctrl.enter_sleep(SleepPowerState.STANDBY_RETENTION)
    assert ctrl.current_state == SleepPowerState.STANDBY_RETENTION
    for _ in range(15):
        ctrl.tick_cycle()
    assert ctrl.cycles_in_state[SleepPowerState.STANDBY_RETENTION] == 15

    # 4. Enter DEEP_SLEEP
    ctrl.enter_sleep(SleepPowerState.DEEP_SLEEP)
    assert ctrl.current_state == SleepPowerState.DEEP_SLEEP
    for _ in range(20):
        ctrl.tick_cycle()
    assert ctrl.cycles_in_state[SleepPowerState.DEEP_SLEEP] == 20

    dut._log.info("Sleep power state transitions and dwell cycle accounting verified.")


@cocotb.test()
async def test_sleep_timer_wakeup(dut):
    """Test 2: Verify sleep timer countdown and deterministic wakeup trigger."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing low-power sleep timer wakeup...")
    ctrl = SleepController()
    ctrl.enter_sleep(SleepPowerState.STANDBY_RETENTION)
    assert ctrl.current_state == SleepPowerState.STANDBY_RETENTION

    # Program timer for 25 cycles
    timer_duration = 25
    ctrl.set_sleep_timer(timer_duration)

    for c in range(timer_duration - 1):
        state, source = ctrl.tick_cycle()
        assert state == SleepPowerState.STANDBY_RETENTION
        assert source == WakeupSource.NONE

    # Last cycle: timer expires and wakes core up
    state, source = ctrl.tick_cycle()
    assert state == SleepPowerState.ACTIVE, "Core must return to ACTIVE state"
    assert source == WakeupSource.SLEEP_TIMER, f"Wakeup source should be SLEEP_TIMER, got {source}"
    assert ctrl.last_wakeup_cause == WakeupSource.SLEEP_TIMER
    assert ctrl.total_wakeups == 1
    dut._log.info("Sleep timer deterministic wakeup verified.")


@cocotb.test()
async def test_sleep_gpio_edge_wakeup_rtl(dut):
    """Test 3: Verify physical in-core RTL WAITEDGE stall and rising edge instant wakeup."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Bootloading low-power WAITEDGE edge wakeup microcode onto RTL...")
    # Edge mode 1 = rising edge, Pin 0
    asm = get_sleep_wakeup_microcode_asm(pin=0, edge_mode=1)
    words = assemble(asm)

    # Initial pin 0 held low (0x00)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let the core execute GDIRI, LDI R2, LDI R3, and enter WAITEDGE
    await ClockCycles(dut.clk, 15)

    # Verify core is not halted, but waiting for edge
    assert not bool(core.halted.value), "Core should be in WAITEDGE stall, not HALT"
    assert int(core.r2.value) == 0x00, "R2 should be 0 before wakeup"

    # Wait an additional 20 cycles in sleep state (verifying sustained stall)
    await ClockCycles(dut.clk, 20)
    assert not bool(core.halted.value), "Core must remain in sleep stall"

    # Trigger external rising edge on pin 0
    dut._log.info("Triggering external wakeup rising edge on pin 0...")
    await FallingEdge(dut.clk)
    dut.uio_in.value = 0x01
    await RisingEdge(dut.clk)

    # Advance clock to let 2-cycle synchronizer and WAITEDGE complete
    await ClockCycles(dut.clk, 10)

    # Core must have awakened, set R2 = 0xAA, and halted
    assert bool(core.halted.value), "Core should have halted after waking up"
    r2_val = int(core.r2.value)
    r3_val = int(core.r3.value)
    dut._log.info(f"Wakeup verified on RTL: R2 = 0x{r2_val:02X}, elapsed cycles in R3 = {r3_val}")
    assert r2_val == 0xAA, f"Expected R2=0xAA (Wakeup Success), got 0x{r2_val:02X}"
    assert r3_val > 0, f"R3 must contain positive elapsed sleep cycles, got {r3_val}"


@cocotb.test()
async def test_sleep_noise_glitch_rejection(dut):
    """Test 4: Verify digital glitch filter suppresses runt noise pulses below threshold."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing noise glitch rejection...")
    ctrl = SleepController(glitch_filter_cycles=3)
    ctrl.enter_sleep(SleepPowerState.DEEP_SLEEP)

    # 1-cycle runt pulse (less than 3-cycle filter threshold)
    rejected_1cyc = ctrl.filter_glitch_test(pin=1, pulse_duration_cycles=1)
    assert rejected_1cyc, "1-cycle runt pulse must be suppressed by 3-cycle glitch filter"
    assert ctrl.current_state == SleepPowerState.DEEP_SLEEP

    # 2-cycle runt pulse (less than 3-cycle filter threshold)
    rejected_2cyc = ctrl.filter_glitch_test(pin=1, pulse_duration_cycles=2)
    assert rejected_2cyc, "2-cycle runt pulse must be suppressed by 3-cycle glitch filter"
    assert ctrl.current_state == SleepPowerState.DEEP_SLEEP
    assert ctrl.spurious_glitches_rejected >= 2

    # 3-cycle valid pulse (meets threshold -> qualifies valid wakeup)
    for _ in range(3):
        ctrl.tick_cycle({1: 1})
    assert ctrl.current_state == SleepPowerState.ACTIVE, "3-cycle pulse must qualify valid wakeup"
    assert ctrl.last_wakeup_cause == WakeupSource.PIN_EDGE_RISING
    dut._log.info("Noise glitch rejection and qualification verified.")


@cocotb.test()
async def test_sleep_cause_status_register(dut):
    """Test 5: Verify multi-source wakeup arbitration and pin ID latching."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing wakeup cause and pin ID latching...")
    ctrl = SleepController(glitch_filter_cycles=2)

    # Case A: Rising edge on Pin 3
    ctrl.enter_sleep(SleepPowerState.STANDBY_RETENTION)
    for _ in range(2):
        ctrl.tick_cycle({3: 1})
    assert ctrl.current_state == SleepPowerState.ACTIVE
    assert ctrl.last_wakeup_cause == WakeupSource.PIN_EDGE_RISING
    assert ctrl.wakeup_pin_id == 3

    # Case B: Falling edge on Pin 5
    ctrl.enter_sleep(SleepPowerState.STANDBY_RETENTION)
    ctrl.filtered_pin_states[5] = 1
    ctrl.pin_states[5] = 1
    for _ in range(2):
        ctrl.tick_cycle({5: 0})
    assert ctrl.current_state == SleepPowerState.ACTIVE
    assert ctrl.last_wakeup_cause == WakeupSource.PIN_EDGE_FALLING
    assert ctrl.wakeup_pin_id == 5

    dut._log.info("Wakeup cause status and pin ID latching verified.")


@cocotb.test()
async def test_sleep_controller_ppa_synthesis(dut):
    """Test 6: Analytical PPA power and energy scaling across sleep states on IHP 130nm."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Benchmarking Sleep Power Scaling on IHP 130nm SG13CMOS5L...")
    p_active = SleepController.get_ppa_metrics(SleepPowerState.ACTIVE)
    p_wait = SleepController.get_ppa_metrics(SleepPowerState.IDLE_WAIT)
    p_standby = SleepController.get_ppa_metrics(SleepPowerState.STANDBY_RETENTION)
    p_deep = SleepController.get_ppa_metrics(SleepPowerState.DEEP_SLEEP)

    dut._log.info(f"ACTIVE:            {p_active.total_power_uw:.2f} uW, latency: {p_active.wakeup_latency_cycles} cyc")
    dut._log.info(f"IDLE_WAIT:         {p_wait.total_power_uw:.2f} uW, latency: {p_wait.wakeup_latency_cycles} cyc")
    dut._log.info(f"STANDBY_RETENTION: {p_standby.total_power_uw:.4f} uW, latency: {p_standby.wakeup_latency_cycles} cyc")
    dut._log.info(f"DEEP_SLEEP:        {p_deep.total_power_uw:.4f} uW, latency: {p_deep.wakeup_latency_cycles} cyc")

    # Assertions on low-power scaling
    assert p_wait.total_power_uw < 0.02 * p_active.total_power_uw, "IDLE_WAIT must reduce total power by >98%"
    assert p_standby.total_power_uw < 0.005 * p_active.total_power_uw, "STANDBY must reduce total power by >99.5%"
    assert p_deep.total_power_uw < 0.10 * p_standby.total_power_uw, "DEEP_SLEEP must cut retention leakage by >90%"
    assert p_deep.total_power_uw < 0.05, "DEEP_SLEEP power must be < 50 nW quiescent"
    assert p_wait.wakeup_latency_cycles == 1, "IDLE_WAIT must wake in exactly 1 clock cycle"

    dut._log.info("Sleep controller PPA power benchmarks verified.")
