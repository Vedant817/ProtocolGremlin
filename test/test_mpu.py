# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb verification test suite for Memory Protection Unit (MPU) & Multi-Tenant Partitioning Engine.

Test cases:
1. test_mpu_authorized_tenant_execution: Verifies authorized tenant executes in partition and yields back.
2. test_mpu_out_of_bounds_write_detection: Verifies out-of-bounds pointer write is trapped (R2 = 0xEE).
3. test_mpu_io_pin_authorization_enforcement: Verifies unauthorized GPIO pin drive is trapped (R2 = 0xEA).
4. test_mpu_temporal_cycle_budget_trapping: Verifies runaway loop exceeding cycle budget is trapped (R2 = 0xEB).
5. test_mpu_hardware_macro_and_ppa_scaling: Validates cycle-accurate MPU model and IHP 130nm PPA scaling.
6. test_mpu_quarantine_pin_electrical_safety: Verifies GPIO pins remain strictly High-Z (uio_oe = 0x00) during quarantine.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from assembler import assemble
from bootload import bootload
from mpu_model import (
    AccessType,
    PrivilegeLevel,
    MpuFaultType,
    MemoryRegion,
    MpuControllerModel,
    MpuPpaModel,
    build_mpu_sandbox_supervisor_asm,
    build_mpu_illegal_write_trap_asm,
    build_mpu_io_permission_violation_asm,
    build_mpu_temporal_budget_enforcement_asm,
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
async def test_mpu_authorized_tenant_execution(dut):
    """Verify authorized tenant task executes cleanly in its partition and yields to supervisor."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_mpu_sandbox_supervisor_asm(base_addr=0x10, limit_addr=0x30, io_mask=0x0F)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(35):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt as expected"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)

    assert r0 == 50, f"Expected tenant computation R0=50 (0x32), got {r0}"
    assert r2 == 0x00, f"Expected clean supervisor return code R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"Authorized Tenant Execution PASS: R0={r0}, R2=0x{r2:02X}")


@cocotb.test()
async def test_mpu_out_of_bounds_write_detection(dut):
    """Verify out-of-bounds pointer write attempt is trapped with fault code R2 = 0xEE."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Guest attempts to access address 0x05 when allowed partition base is 0x20
    asm = build_mpu_illegal_write_trap_asm(illegal_addr=0x05, base_addr=0x20, limit_addr=0x40)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(35):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on MPU write trap"
    r2 = int(core.r2.value)
    uio_oe = int(dut.uio_oe.value)

    assert r2 == int(MpuFaultType.WRITE_VIOLATION), f"Expected R2=0xEE (WRITE_VIOLATION), got 0x{r2:02X}"
    assert uio_oe == 0x00, f"Expected pins tri-stated upon trap, got uio_oe=0x{uio_oe:02X}"
    dut._log.info(f"Out-of-Bounds Write Detection PASS: Trapped with R2=0x{r2:02X}, uio_oe=0x{uio_oe:02X}")


@cocotb.test()
async def test_mpu_io_pin_authorization_enforcement(dut):
    """Verify tenant attempting to drive restricted GPIO pins is intercepted and trapped (R2 = 0xEA)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Tenant attempts to drive pin 7 (0x80) when allowed mask is 0x0F (pins 0-3 only)
    asm = build_mpu_io_permission_violation_asm(attempted_pin_drive=0x80, allowed_mask=0x0F)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(35):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on MPU IO violation"
    r2 = int(core.r2.value)
    uio_oe = int(dut.uio_oe.value)

    assert r2 == int(MpuFaultType.IO_ACCESS_VIOLATION), f"Expected R2=0xEA (IO_ACCESS_VIOLATION), got 0x{r2:02X}"
    # Verify unauthorized pin 7 was NEVER driven
    assert (uio_oe & 0x80) == 0, f"Unauthorized pin 7 was driven! uio_oe=0x{uio_oe:02X}"
    assert uio_oe == 0x00, f"Expected all pins tri-stated upon trap, got uio_oe=0x{uio_oe:02X}"
    dut._log.info(f"IO Pin Authorization Enforcement PASS: Intercepted pin 7, R2=0x{r2:02X}, uio_oe=0x{uio_oe:02X}")


@cocotb.test()
async def test_mpu_temporal_cycle_budget_trapping(dut):
    """Verify runaway task loop exceeding cycle budget is preempted with fault code R2 = 0xEB."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Task needs 20 iterations, but budget is only 5
    asm = build_mpu_temporal_budget_enforcement_asm(loop_iterations=20, max_budget=5)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on temporal budget expiration"
    r2 = int(core.r2.value)
    assert r2 == int(MpuFaultType.TIMEOUT_VIOLATION), f"Expected R2=0xEB (TIMEOUT_VIOLATION), got 0x{r2:02X}"
    dut._log.info(f"Temporal Cycle Budget Trapping PASS: Preempted with R2=0x{r2:02X}")


@cocotb.test()
async def test_mpu_hardware_macro_and_ppa_scaling(dut):
    """Validate cycle-accurate MpuControllerModel reference simulator and IHP 130nm PPA scaling."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    mpu = MpuControllerModel(num_regions=4)

    # Region 0: Supervisor (0x00 - 0x1F, RWX, Privileged Only)
    mpu.add_region(MemoryRegion(
        region_id=0,
        name="Supervisor_Kernel",
        base_addr=0x00,
        limit_addr=0x1F,
        can_read=True,
        can_write=True,
        can_execute=True,
        privileged_only=True,
        io_mask=0xFF,
    ))

    # Region 1: Tenant A User Code (0x20 - 0x3F, RX, User, IO mask 0x0F)
    mpu.add_region(MemoryRegion(
        region_id=1,
        name="TenantA_Code",
        base_addr=0x20,
        limit_addr=0x3F,
        can_read=True,
        can_write=False,
        can_execute=True,
        privileged_only=False,
        io_mask=0x0F,
    ))

    # Region 2: Shared Data Buffer (0x40 - 0x7F, RW, User, Execute Never / XN)
    mpu.add_region(MemoryRegion(
        region_id=2,
        name="Shared_Buffer",
        base_addr=0x40,
        limit_addr=0x7F,
        can_read=True,
        can_write=True,
        can_execute=False,
        privileged_only=False,
        io_mask=0x00,
    ))

    # Test 1: User mode access to Privileged Supervisor space should fail
    mpu.set_privilege(PrivilegeLevel.USER)
    ok, fault = mpu.check_access(0x10, AccessType.EXECUTE)
    assert not ok and fault == MpuFaultType.EXEC_VIOLATION, "User should not execute supervisor code"

    # Test 2: User mode execute in XN Shared Buffer should fail
    ok, fault = mpu.check_access(0x50, AccessType.EXECUTE)
    assert not ok and fault == MpuFaultType.EXEC_VIOLATION, "Execute in XN region should fail"

    # Test 3: User mode write to Read-Only Tenant Code should fail
    ok, fault = mpu.check_access(0x28, AccessType.WRITE)
    assert not ok and fault == MpuFaultType.WRITE_VIOLATION, "Write to RO code region should fail"

    # Test 4: User mode authorized read/write in Shared Buffer should pass
    ok, fault = mpu.check_access(0x50, AccessType.WRITE)
    assert ok and fault == MpuFaultType.NONE, "Write to RW buffer should succeed"

    # Test 5: IO pin authorization in Region 1 (io_mask = 0x0F)
    mpu.check_access(0x20, AccessType.EXECUTE)  # Set active region to Region 1
    io_ok, masked_pins, io_fault = mpu.check_io_access(0x85)  # Attempts pin 7 (0x80) and pin 2,0 (0x05)
    assert not io_ok and io_fault == MpuFaultType.IO_ACCESS_VIOLATION, "Unauthorized pin drive should be flagged"
    assert masked_pins == 0x05, f"Expected unauthorized pin 7 masked off, got 0x{masked_pins:02X}"

    # Test 6: Temporal cycle budget countdown
    mpu.set_cycle_budget(3)
    assert mpu.step_cycle() is True
    assert mpu.step_cycle() is True
    assert mpu.step_cycle() is True
    assert mpu.step_cycle() is False, "Budget should expire after 3 cycles"
    assert mpu.fault_status == MpuFaultType.TIMEOUT_VIOLATION

    # Test 7: Validate analytical PPA scaling models
    ppa2 = MpuPpaModel.estimate_ppa(2)
    ppa4 = MpuPpaModel.estimate_ppa(4)
    ppa8 = MpuPpaModel.estimate_ppa(8)

    assert ppa4["standard_cells"] > ppa2["standard_cells"], "Cells should scale monotonically"
    assert ppa4["overhead_pct"] < 2.5, "4-region MPU overhead should be < 2.5%"
    assert ppa8["overhead_pct"] < 4.5, "8-region MPU overhead should be < 4.5%"
    assert ppa4["comparator_delay_ns"] < 2.0, "Comparator propagation delay should be < 2.0 ns"

    await RisingEdge(dut.clk)
    dut._log.info(f"Hardware MPU Macro & PPA Scaling PASS: 4-region ({ppa4['standard_cells']:.0f} cells, {ppa4['overhead_pct']:.2f}% overhead, {ppa4['comparator_delay_ns']:.2f}ns delay)")


@cocotb.test()
async def test_mpu_quarantine_pin_electrical_safety(dut):
    """Verify that during MPU fault trapping and quarantine, all GPIO pins remain strictly High-Z (uio_oe = 0x00)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_mpu_illegal_write_trap_asm(illegal_addr=0x02, base_addr=0x20, limit_addr=0x40)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core

    # Check that uio_oe remains 0x00 throughout all execution cycles
    for cycle in range(35):
        await RisingEdge(dut.clk)
        uio_oe = int(dut.uio_oe.value)
        assert uio_oe == 0x00, f"Cycle {cycle}: Expected strictly High-Z (uio_oe=0x00), got 0x{uio_oe:02X}"
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core failed to halt in quarantine"
    dut._log.info("Quarantine Pin Electrical Safety PASS: uio_oe remained strictly 0x00 (High-Z)")
