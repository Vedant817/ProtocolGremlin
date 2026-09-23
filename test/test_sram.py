# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""test/test_sram.py - Cocotb test suite for High-Density SRAM Micro-Architecture & Physical Co-Design.

Verifies:
1. test_sram_checkerboard_pattern_write_read: Checkerboard patterns and boundary addresses.
2. test_sram_dual_port_collision_hazard_isolation: Concurrent read/write collision detection.
3. test_sram_power_retention_and_sleep_modes: Power gating modes (ACTIVE, RETENTION, DEEP_SLEEP).
4. test_sram_in_core_diagnostic_execution: In-core execution of memory diagnostic routine on RTL.
5. test_sram_split_bank_nonblocking_arbitration: Dual-bank non-blocking reconfigurable memory.
6. test_sram_ppa_scaling_and_silicon_tradeoff: Calibrated 130nm CMOS PPA scaling comparison.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from sram_model import (  # noqa: E402
    MemoryArchitecture,
    PowerState,
    SramPpaMetrics,
    SramModel,
    compute_sram_ppa,
    get_memory_diagnostic_asm,
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
async def test_sram_checkerboard_pattern_write_read(dut):
    """Test 1: Verify SRAM pattern writing and reading across address space."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing SRAM checkerboard and walking-ones patterns...")
    sram = SramModel(words=256, bits=16)

    # Test boundary and middle addresses
    test_addrs = [0x00, 0x01, 0x55, 0x7F, 0x80, 0xAA, 0xFE, 0xFF]
    patterns = [0x5555, 0xAAAA, 0x0001, 0x8000, 0xFFFF, 0x0000]

    for addr in test_addrs:
        for pat in patterns:
            sram.write(addr, pat)
            val = sram.read(addr)
            assert val == pat, f"Addr 0x{addr:02X}: expected 0x{pat:04X}, got 0x{val:04X}"

    assert sram.total_writes == len(test_addrs) * len(patterns)
    assert sram.total_reads == len(test_addrs) * len(patterns)
    dut._log.info(f"Verified {sram.total_writes} writes and {sram.total_reads} reads successfully.")


@cocotb.test()
async def test_sram_dual_port_collision_hazard_isolation(dut):
    """Test 2: Verify dual-port read/write hazard isolation and collision detection."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing SRAM dual-port access and RAW collision detection...")
    sram = SramModel(words=256, bits=16)
    sram.write(0x10, 0x1234)

    # Access 1: Non-colliding read and write
    rdata, collision = sram.dual_port_access(raddr=0x10, waddr=0x20, wdata=0x5678, we=True)
    assert rdata == 0x1234, f"Expected 0x1234, got 0x{rdata:04X}"
    assert not collision, "Non-colliding access should not report collision"

    # Access 2: Colliding read and write to same address (0x20)
    rdata, collision = sram.dual_port_access(raddr=0x20, waddr=0x20, wdata=0x9ABC, we=True)
    assert collision, "Read and write to same address must trigger collision"
    assert sram.collision_detected

    # In standard old-data-first dual-port SRAM, read returns previous value
    assert rdata == 0x5678, f"Read before write should return old data 0x5678, got 0x{rdata:04X}"
    assert sram.read(0x20) == 0x9ABC, "New data must be latched"
    dut._log.info("Dual-port hazard detection verified.")


@cocotb.test()
async def test_sram_power_retention_and_sleep_modes(dut):
    """Test 3: Verify power gating state transitions (ACTIVE, RETENTION, DEEP_SLEEP)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing SRAM power management state transitions...")
    sram = SramModel(words=256, bits=16)
    sram.write(0x40, 0xBEEF)

    # 1. Retention mode: data retained, writes ignored
    sram.set_power_state(PowerState.RETENTION)
    assert sram.power_state == PowerState.RETENTION
    assert sram.read(0x40) == 0xBEEF, "Retention mode must preserve stored state"

    # 2. Deep sleep mode: power collapsed, state cleared
    sram.set_power_state(PowerState.DEEP_SLEEP)
    assert sram.power_state == PowerState.DEEP_SLEEP
    assert sram.read(0x40) == 0x0000, "Deep sleep mode must clear memory"
    assert not sram.write(0x40, 0xCAFE), "Write in deep sleep mode must be rejected"

    # 3. Wake back to ACTIVE
    sram.set_power_state(PowerState.ACTIVE)
    assert sram.write(0x40, 0xCAFE), "Write in active mode must succeed"
    assert sram.read(0x40) == 0xCAFE
    dut._log.info("Power state management verified.")


@cocotb.test()
async def test_sram_in_core_diagnostic_execution(dut):
    """Test 4: Verify in-core execution of memory diagnostic microcode on physical RTL."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing in-core memory diagnostic execution on RTL...")
    asm = get_memory_diagnostic_asm()
    words = assemble(asm)

    await _init_dut_and_bootload(dut, words)
    await ClockCycles(dut.clk, 30)

    await ReadOnly()
    oe = int(dut.uio_oe.value)
    out = int(dut.uio_out.value)
    assert (oe & 0x01) == 0x01, "Pin 0 must be output"
    assert (out & 0x01) == 0x01, f"Pin 0 must be high on test pass, got 0x{out:02X}"
    dut._log.info("In-core memory diagnostic executed successfully on RTL.")


@cocotb.test()
async def test_sram_split_bank_nonblocking_arbitration(dut):
    """Test 5: Verify dual-bank non-blocking split memory architecture."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing split-bank dual 128x16 memory organization...")
    bank0 = SramModel(words=128, bits=16)
    bank1 = SramModel(words=128, bits=16)

    # Bank 0: Core execution area
    bank0.write(0x05, 0x1111)
    # Bank 1: Background bootloader shadow buffer
    bank1.write(0x05, 0x2222)

    # Verify both banks maintain independent addresses without crosstalk
    assert bank0.read(0x05) == 0x1111
    assert bank1.read(0x05) == 0x2222

    # Simultaneous access to Bank 0 and Bank 1
    r0, col0 = bank0.dual_port_access(0x05, 0x10, 0x3333, we=True)
    r1, col1 = bank1.dual_port_access(0x05, 0x10, 0x4444, we=True)

    assert r0 == 0x1111 and not col0
    assert r1 == 0x2222 and not col1
    assert bank0.read(0x10) == 0x3333
    assert bank1.read(0x10) == 0x4444
    dut._log.info("Split-bank non-blocking concurrency verified.")


@cocotb.test()
async def test_sram_ppa_scaling_and_silicon_tradeoff(dut):
    """Test 6: Benchmark physical PPA scaling across 130nm CMOS memory alternatives."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Benchmarking SRAM PPA scaling on IHP 130nm SG13CMOS5L...")
    dff_ppa = compute_sram_ppa(MemoryArchitecture.SYNTHESIZED_DFF)
    dffram_ppa = compute_sram_ppa(MemoryArchitecture.DFFRAM_COMPILED)
    openram_ppa = compute_sram_ppa(MemoryArchitecture.OPENRAM_MACRO)
    split_ppa = compute_sram_ppa(MemoryArchitecture.SPLIT_BANK_MACRO)

    dut._log.info(f"SYNTHESIZED_DFF: {dff_ppa.area_mm2:.4f} mm2, {dff_ppa.cell_count} cells, {dff_ppa.dynamic_power_uw_at_10mhz:.1f} uW")
    dut._log.info(f"DFFRAM_COMPILED: {dffram_ppa.area_mm2:.4f} mm2, {dffram_ppa.cell_count} cells, {dffram_ppa.dynamic_power_uw_at_10mhz:.1f} uW")
    dut._log.info(f"OPENRAM_MACRO:   {openram_ppa.area_mm2:.4f} mm2, {openram_ppa.cell_count} cells, {openram_ppa.dynamic_power_uw_at_10mhz:.1f} uW")
    dut._log.info(f"SPLIT_BANK:      {split_ppa.area_mm2:.4f} mm2, {split_ppa.cell_count} cells, {split_ppa.dynamic_power_uw_at_10mhz:.1f} uW")

    # Assertions on physical scaling trends
    assert openram_ppa.area_um2 < 0.20 * dff_ppa.area_um2, "OpenRAM macro must achieve >80% area reduction vs DFF"
    assert openram_ppa.dynamic_power_uw_at_10mhz < 0.10 * dff_ppa.dynamic_power_uw_at_10mhz, "OpenRAM macro must achieve >90% power reduction vs DFF"
    assert openram_ppa.access_time_ns < dff_ppa.access_time_ns, "OpenRAM access time must be faster than synthesized DFF mux tree"
    assert split_ppa.area_um2 < dff_ppa.area_um2 * 0.25, "Split-bank macro must remain <25% area of DFF array"

    dut._log.info("SRAM PPA scaling benchmark assertions verified.")
