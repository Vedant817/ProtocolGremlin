#!/usr/bin/env python3
"""Memory Protection Unit (MPU) & Multi-Tenant Partitioning Engine Model.

Provides cycle-accurate modeling of:
1. Multi-region spatial partitioning (Base, Limit, Permissions, Privilege).
2. Hardware IO pin mask protection (sanitizing external GPIO drive).
3. Temporal cycle budget enforcement (preventing task runaway/starvation).
4. Physical PPA trade-off scaling on IHP 130nm SG13G2 CMOS5L.
5. Assembly firmware generators for software sandboxing and hardware MPU trapping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional, Tuple


class AccessType(IntEnum):
    """Memory access classification."""
    READ = 0
    WRITE = 1
    EXECUTE = 2


class PrivilegeLevel(IntEnum):
    """Processor execution privilege."""
    SUPERVISOR = 0
    USER = 1


class MpuFaultType(IntEnum):
    """MPU fault trap codes."""
    NONE = 0x00
    EXEC_VIOLATION = 0xEF
    WRITE_VIOLATION = 0xEE
    READ_VIOLATION = 0xED
    IO_ACCESS_VIOLATION = 0xEA
    TIMEOUT_VIOLATION = 0xEB


@dataclass
class MemoryRegion:
    """Configuration descriptor for a memory partition."""
    region_id: int
    name: str
    base_addr: int
    limit_addr: int
    can_read: bool = True
    can_write: bool = False
    can_execute: bool = True
    privileged_only: bool = False
    io_mask: int = 0xFF   # Authorized GPIO pins (uio[7:0])


class MpuControllerModel:
    """Cycle-accurate model of an on-chip Memory Protection Unit (MPU)."""

    def __init__(self, num_regions: int = 4):
        self.num_regions = num_regions
        self.regions: List[MemoryRegion] = []
        self.privilege: PrivilegeLevel = PrivilegeLevel.USER
        self.enabled: bool = True
        self.cycle_budget: int = 100
        self.elapsed_cycles: int = 0
        self.fault_status: MpuFaultType = MpuFaultType.NONE
        self.fault_addr: int = 0
        self.active_region_id: int = 0

    def add_region(self, region: MemoryRegion) -> None:
        """Register a memory protection region."""
        if len(self.regions) < self.num_regions:
            self.regions.append(region)

    def set_privilege(self, level: PrivilegeLevel) -> None:
        """Set current execution privilege level."""
        self.privilege = level

    def check_access(self, addr: int, access_type: AccessType) -> Tuple[bool, MpuFaultType]:
        """Check if access to an address is permitted under current region descriptors."""
        if not self.enabled:
            return True, MpuFaultType.NONE

        matched_region: Optional[MemoryRegion] = None
        # Highest index has highest priority if regions overlap
        for region in reversed(self.regions):
            if region.base_addr <= addr <= region.limit_addr:
                matched_region = region
                break

        if matched_region is None:
            # Default background rule: Supervisor can access, User cannot
            if self.privilege == PrivilegeLevel.SUPERVISOR:
                return True, MpuFaultType.NONE
            fault = MpuFaultType.EXEC_VIOLATION if access_type == AccessType.EXECUTE else MpuFaultType.READ_VIOLATION
            self.fault_status = fault
            self.fault_addr = addr
            return False, fault

        self.active_region_id = matched_region.region_id

        # Privilege check
        if matched_region.privileged_only and self.privilege != PrivilegeLevel.SUPERVISOR:
            fault = MpuFaultType.EXEC_VIOLATION if access_type == AccessType.EXECUTE else MpuFaultType.READ_VIOLATION
            self.fault_status = fault
            self.fault_addr = addr
            return False, fault

        # Permission check
        if access_type == AccessType.EXECUTE and not matched_region.can_execute:
            self.fault_status = MpuFaultType.EXEC_VIOLATION
            self.fault_addr = addr
            return False, MpuFaultType.EXEC_VIOLATION

        if access_type == AccessType.WRITE and not matched_region.can_write:
            self.fault_status = MpuFaultType.WRITE_VIOLATION
            self.fault_addr = addr
            return False, MpuFaultType.WRITE_VIOLATION

        if access_type == AccessType.READ and not matched_region.can_read:
            self.fault_status = MpuFaultType.READ_VIOLATION
            self.fault_addr = addr
            return False, MpuFaultType.READ_VIOLATION

        return True, MpuFaultType.NONE

    def check_io_access(self, requested_pins: int, is_write: bool = True) -> Tuple[bool, int, MpuFaultType]:
        """Validate that requested pin drive conforms to the active region's IO mask."""
        if not self.enabled or self.privilege == PrivilegeLevel.SUPERVISOR:
            return True, requested_pins, MpuFaultType.NONE

        # Find active region or default to most restricted
        io_mask = 0x00
        for region in self.regions:
            if region.region_id == self.active_region_id:
                io_mask = region.io_mask
                break

        unauthorized = requested_pins & (~io_mask & 0xFF)
        if unauthorized != 0:
            self.fault_status = MpuFaultType.IO_ACCESS_VIOLATION
            # Return masked safe pins (unauthorized bits cleared)
            return False, requested_pins & io_mask, MpuFaultType.IO_ACCESS_VIOLATION

        return True, requested_pins, MpuFaultType.NONE

    def set_cycle_budget(self, budget: int) -> None:
        """Configure temporal cycle execution budget."""
        self.cycle_budget = budget
        self.elapsed_cycles = 0

    def step_cycle(self) -> bool:
        """Step execution by one clock cycle. Returns False if budget exceeded."""
        self.elapsed_cycles += 1
        if self.elapsed_cycles > self.cycle_budget:
            self.fault_status = MpuFaultType.TIMEOUT_VIOLATION
            return False
        return True

    def reset(self) -> None:
        """Reset MPU state."""
        self.fault_status = MpuFaultType.NONE
        self.fault_addr = 0
        self.elapsed_cycles = 0


class MpuPpaModel:
    """Analytical PPA estimation for Hardware MPU Macro on IHP 130nm SG13G2."""

    @staticmethod
    def estimate_ppa(num_regions: int = 4) -> Dict[str, float]:
        """Estimate standard cell count, GE, area, and timing delay for N regions."""
        # Baseline ASIC: 19,291 CMOS cells (~37,832 GE)
        # Per region:
        #   2 x 8-bit magnitude comparators: ~24 cells
        #   Registers (BASE: 8, LIMIT: 8, ATTR: 4, IO_MASK: 8) = 28 FFs (~56 cells)
        #   Permission & fault gating logic: ~16 cells
        # Total per region: 96 cells (~188 GE)
        # Global MPU control & priority arbiter: ~24 cells
        cells_per_region = 96
        global_cells = 24
        total_cells = global_cells + (num_regions * cells_per_region)
        total_ge = total_cells * 1.96
        area_um2 = total_cells * 3.10  # Standard cell average density
        overhead_pct = (total_ge / 37832.0) * 100.0
        comparator_delay_ns = 1.45 + (0.10 * num_regions)  # Carry tree + fanout

        return {
            "num_regions": float(num_regions),
            "standard_cells": float(total_cells),
            "gate_equivalents": float(total_ge),
            "area_um2": float(area_um2),
            "overhead_pct": float(overhead_pct),
            "comparator_delay_ns": float(comparator_delay_ns),
        }


def build_mpu_sandbox_supervisor_asm(base_addr: int = 0x10, limit_addr: int = 0x30, io_mask: int = 0x0F) -> str:
    """Generate assembly for supervisor sandboxing an authorized tenant task."""
    return f"""
    ; -------------------------------------------------------------
    ; MPU Sandboxing Supervisor: Authorized Tenant Execution
    ; -------------------------------------------------------------
    ; Supervisor establishes tenant boundary parameters in registers
    LDI R0, 0x10          ; Base address
    LDI R1, 0x30          ; Limit address
    LDI R2, 0x00          ; Status (0x00 = success)
    LDI R3, {io_mask}     ; Authorized IO pin mask
    ; Jump to tenant entry point
    JMP tenant_code

supervisor_return:
    ; Supervisor post-execution check
    HALT

tenant_code:
    ; Authorized tenant computes arithmetic payload
    LDI R0, 0x2A          ; 42
    ADDI R0, 0x08         ; 42 + 8 = 50 (0x32)
    ; Tenant yields back cleanly
    JMP supervisor_return
    """


def build_mpu_illegal_write_trap_asm(illegal_addr: int = 0x05, base_addr: int = 0x20, limit_addr: int = 0x40) -> str:
    """Generate assembly demonstrating out-of-bounds pointer write detection and trapping."""
    return f"""
    ; -------------------------------------------------------------
    ; MPU Out-of-Bounds Memory Write Trap
    ; -------------------------------------------------------------
    ; Supervisor sets up partition boundary [base_addr, limit_addr]
    LDI R1, {base_addr}   ; Base bound (0x20)
    LDI R2, 0x00          ; Clean status
    LDI R3, {illegal_addr}; Target pointer attempted by guest (0x05)

    ; Memory boundary check: test if attempted_addr < base_addr
    MOV R0, R3
    SUBI R0, {base_addr}  ; In 8-bit unsigned: 0x05 - 0x20 = 0xE5 (underflow, MSB set)
    ANDI R0, 0x80         ; Test sign/underflow bit
    JNZ mpu_write_trap

    ; If within lower bound, check upper bound
    MOV R0, R3
    SUBI R0, {limit_addr}
    ANDI R0, 0x80
    JZ mpu_write_trap     ; If target >= limit_addr without underflow

    ; Authorized write path
    LDI R2, 0x00
    HALT

mpu_write_trap:
    ; Quarantine trap: log fault and isolate pins
    LDI R2, 0xEE          ; MPU_FAULT_WRITE_VIOLATION
    GDIRI 0x00           ; Safe tri-state
    GWRI 0x00
    HALT
    """


def build_mpu_io_permission_violation_asm(attempted_pin_drive: int = 0x80, allowed_mask: int = 0x0F) -> str:
    """Generate assembly demonstrating IO pin authorization check and violation trapping."""
    return f"""
    ; -------------------------------------------------------------
    ; MPU IO Pin Authorization Violation Trap
    ; -------------------------------------------------------------
    ; Supervisor assigns allowed pin mask to R3
    LDI R3, {allowed_mask} ; Allowed mask (0x0F = pins 0-3 only)

    ; Tenant attempts to drive restricted pin (e.g. pin 7 = 0x80)
    LDI R1, {attempted_pin_drive}

    ; Sandbox checks unauthorized bits: attempted & ~allowed_mask
    MOV R0, R3
    XORI R0, 0xFF          ; R0 = inverted mask (0xF0)
    ANDI R0, {attempted_pin_drive} ; Check if attempted uses restricted pins
    JNZ io_violation_trap

    ; Authorized pin drive path
    GDIRI {allowed_mask}
    GWR R1
    LDI R2, 0x00
    HALT

io_violation_trap:
    ; Trap: isolate pins and record violation
    GDIRI 0x00            ; Force all pins to High-Z
    GWRI 0x00
    LDI R2, 0xEA           ; MPU_FAULT_IO_ACCESS_VIOLATION
    HALT
    """


def build_mpu_temporal_budget_enforcement_asm(loop_iterations: int = 20, max_budget: int = 5) -> str:
    """Generate assembly demonstrating temporal cycle budget enforcement and preemption."""
    return f"""
    ; -------------------------------------------------------------
    ; MPU Temporal Cycle Budget Enforcement
    ; -------------------------------------------------------------
    ; Supervisor establishes budget counter in R3
    LDI R3, {max_budget}     ; Budget ticks allowed (e.g. 5)
    LDI R1, {loop_iterations}; Work items to process (e.g. 20)
    LDI R0, 0x00             ; Work accumulator

work_loop:
    ; Tenant executes task step
    ADDI R0, 0x01
    SUBI R1, 0x01
    ; Check if tenant work complete
    MOV R2, R1
    JZ work_done

    ; Decrement supervisor budget counter
    DECJNZ R3, work_loop

    ; Budget exhausted before work completed: Timeout Fault Trap!
    LDI R2, 0xEB             ; MPU_FAULT_TIMEOUT_VIOLATION
    GDIRI 0x00
    HALT

work_done:
    LDI R2, 0x00             ; Finished within budget
    HALT
    """
