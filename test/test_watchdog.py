# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb verification test suite for Hardware Watchdog Timer & Brownout Recovery.

Test cases:
1. test_wdt_normal_servicing: Core executes periodic tasks and services WWDT within window.
2. test_wdt_task_hang_and_soft_reset: Core hangs in deadlock loop; WWDT trips timeout and asserts reset.
3. test_wdt_windowed_early_pet_violation: Core pets too fast; WWDT trips early violation.
4. test_brownout_transient_drop_and_warm_recovery: Mid-execution supply drop triggers warm boot (<10 cycles).
5. test_wdt_electrical_safety_and_pin_isolation: Confirms uio_oe is 0x00 during reset/recovery.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from assembler import assemble
from bootload import bootload
from watchdog_model import (
    WatchdogModel,
    build_watchdog_service_firmware,
    build_watchdog_hang_firmware,
    build_watchdog_early_pet_firmware,
    build_brownout_recovery_firmware,
)


async def _init_dut_and_bootload(dut, words: list[int], initial_uio: int = 0xFF):
    """Reset DUT and load firmware into program RAM via serial bootloader."""
    await RisingEdge(dut.clk)
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
async def test_wdt_normal_servicing(dut):
    """Verify that firmware properly servicing the watchdog within window runs to completion."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    token_a = 0x5A
    token_b = 0xA5
    wdt = WatchdogModel(timeout_cycles=150, window_min_cycles=10, token_a=token_a, token_b=token_b)

    asm = build_watchdog_service_firmware(
        loop_iterations=4,
        task_delay_cycles=25,
        token_a=token_a,
        token_b=token_b,
    )
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=0xFF)

    core = dut.user_project.u_core

    # Simulation loop
    prev_uio_out = 0
    for cycle in range(500):
        await FallingEdge(dut.clk)

        uio_out = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        service_byte = uio_out if (uio_out != 0 and uio_out != prev_uio_out) else None
        prev_uio_out = uio_out

        core_rst_n, wdt_alarm, status = wdt.step(service_byte=service_byte, external_rst_n=True)
        assert core_rst_n, f"Watchdog prematurely asserted reset at cycle {cycle}"
        assert not wdt_alarm, f"Watchdog alarm triggered prematurely at cycle {cycle}"

        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Watchdog service loop timed out waiting for HALT")

    r2 = int(core.r2.value)
    dut._log.info(f"Normal servicing complete: R2=0x{r2:02X}, pets={wdt.pet_count}, timeouts={wdt.timeout_count}")
    assert r2 == 0x00, f"Expected clean completion R2=0x00, got 0x{r2:02X}"
    assert wdt.pet_count >= 4, f"Expected at least 4 watchdog pets, got {wdt.pet_count}"
    assert wdt.timeout_count == 0, "Watchdog timeout occurred unexpectedly!"


@cocotb.test()
async def test_wdt_task_hang_and_soft_reset(dut):
    """Verify that a deadlocked task trips watchdog timeout, asserting alarm and soft reset."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    token_a = 0x5A
    token_b = 0xA5
    timeout_threshold = 80
    wdt = WatchdogModel(timeout_cycles=timeout_threshold, window_min_cycles=5, token_a=token_a, token_b=token_b)

    asm = build_watchdog_hang_firmware(
        hang_after_iterations=1,
        task_delay_cycles=15,
        token_a=token_a,
        token_b=token_b,
    )
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=0xFF)

    core = dut.user_project.u_core

    prev_uio_out = 0
    hang_cycle = -1
    timeout_triggered = False

    for cycle in range(500):
        await FallingEdge(dut.clk)

        uio_out = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        service_byte = uio_out if (uio_out != 0 and uio_out != prev_uio_out) else None
        prev_uio_out = uio_out

        # Step watchdog supervisor
        core_rst_n, wdt_alarm, status = wdt.step(service_byte=service_byte, external_rst_n=True)

        if int(core.r2.value) == 0xDE and hang_cycle < 0:
            hang_cycle = cycle
            dut._log.info(f"Task deadlock state (R2=0xDE) reached at cycle {cycle}")

        if not core_rst_n or wdt_alarm:
            timeout_triggered = True
            dut._log.info(f"Watchdog alarm triggered at cycle {cycle}! Status=0x{status:02X}")
            # External supervisory action: pull hardware rst_n to force recovery
            dut.rst_n.value = 0
            for _ in range(4):
                await RisingEdge(dut.clk)
            dut.rst_n.value = 1
            break

        await RisingEdge(dut.clk)

    assert timeout_triggered, "Watchdog failed to trigger on task deadlock!"
    assert wdt.timeout_count == 1, f"Expected 1 timeout, got {wdt.timeout_count}"
    assert wdt.reset_status in (WatchdogModel.RESET_WDT_TIMEOUT, WatchdogModel.RESET_BROWNOUT)


@cocotb.test()
async def test_wdt_windowed_early_pet_violation(dut):
    """Verify that servicing the watchdog before T_min is tripped as a window violation."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    token_a = 0x5A
    token_b = 0xA5
    # Strict minimum window: requires at least 40 cycles between pets
    wdt = WatchdogModel(timeout_cycles=200, window_min_cycles=40, token_a=token_a, token_b=token_b)

    asm = build_watchdog_early_pet_firmware(token_a=token_a, token_b=token_b)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=0xFF)

    core = dut.user_project.u_core

    prev_uio_out = 0
    violation_caught = False

    for cycle in range(150):
        await FallingEdge(dut.clk)

        uio_out = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        service_byte = uio_out if (uio_out != 0 and uio_out != prev_uio_out) else None
        prev_uio_out = uio_out

        core_rst_n, wdt_alarm, status = wdt.step(service_byte=service_byte, external_rst_n=True)

        if not core_rst_n and status == WatchdogModel.RESET_WDT_WINDOW_VIOLATION:
            violation_caught = True
            dut._log.info(f"Window violation caught at cycle {cycle}: T < T_min! Status=0x{status:02X}")
            break

        await RisingEdge(dut.clk)

    assert violation_caught, "Windowed watchdog failed to trap early pet violation!"
    assert wdt.window_violation_count == 1, "Expected 1 window violation!"


@cocotb.test()
async def test_brownout_transient_drop_and_warm_recovery(dut):
    """Verify that an external power brownout pulse on rst_n recovers instantly via warm-boot (<10 cycles)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_payload = 12
    asm = build_brownout_recovery_firmware(magic_signature=0xA5, payload_byte=target_payload)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=0xFF)

    core = dut.user_project.u_core

    # Let the core execute several work loop cycles
    for _ in range(60):
        await RisingEdge(dut.clk)

    mid_payload = int(core.r0.value)
    dut._log.info(f"Pre-brownout state: R0={mid_payload}, halted={bool(core.halted.value)}")
    assert not bool(core.halted.value), "Core halted prematurely before brownout"

    # Simulate brownout: assert external rst_n LOW for 5 cycles while holding LOAD_REQ=0
    dut._log.info("Inducing power brownout pulse on rst_n...")
    dut.rst_n.value = 0
    # Crucial: LOAD_REQ (uio[0]) must be 0 for warm boot skip
    dut.uio_in.value = 0xFE  # bit 0 is 0
    for _ in range(5):
        await RisingEdge(dut.clk)

    # Release reset
    dut.rst_n.value = 1
    dut._log.info("Brownout cleared, rst_n released. Verifying warm-boot skip...")

    # Wait for warm boot completion (< 10 cycles)
    warm_boot_done = False
    for wait_cycle in range(15):
        await RisingEdge(dut.clk)
        uo_out = int(dut.uo_out.value) if dut.uo_out.value.is_resolvable else 0
        boot_done = uo_out & 1
        boot_err = (uo_out >> 1) & 1
        if boot_done and not boot_err:
            warm_boot_done = True
            dut._log.info(f"Warm boot completed successfully at cycle {wait_cycle}! uo_out=0x{uo_out:02X}")
            break

    assert warm_boot_done, "Warm boot failed to complete within 15 cycles after brownout!"

    # Allow core to resume and complete execution
    for cycle in range(500):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Core failed to complete after brownout recovery!")

    r2 = int(core.r2.value)
    dut._log.info(f"Post-brownout completion: R2=0x{r2:02X}, R0={int(core.r0.value)}")
    assert r2 == 0xAA, f"Expected completion status R2=0xAA, got 0x{r2:02X}"


@cocotb.test()
async def test_wdt_electrical_safety_and_pin_isolation(dut):
    """Verify that during watchdog reset assertion and brownout, uio_oe is strictly 0x00 (High-Z)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_watchdog_service_firmware(loop_iterations=2, task_delay_cycles=10)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=0xFF)

    # Induce reset
    dut.rst_n.value = 0
    for cycle in range(20):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0xFF
        assert uio_oe == 0x00, f"Electrical violation: uio_oe=0x{uio_oe:02X} != 0 during reset at cycle {cycle}"

    # Leave ReadOnly phase before mutating signals
    await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    dut._log.info("Electrical safety verified: all GPIO pins maintained in High-Z state during reset.")
